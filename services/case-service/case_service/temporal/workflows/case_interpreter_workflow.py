"""Temporal workflow: the `definition_json` INTERPRETER (Tier-1 §12 / #21).

This is Option A from `docs/Future/temporal-decision-record.md`, finally
realised: **one generic workflow that interprets the existing case-type
`definition_json` at runtime.** No case type is compiled into workflow code.
The definition format does not change. Case-type owners need zero migration —
their case types run inside a workflow they do not know about, and Temporal
supplies durability rather than a new authoring model.

Option B (generating workflow code per case type) remains **forbidden**,
including partially and including "just for complex cases". Any future
proposal to compile case types must supersede that record explicitly.

**It orchestrates; it does not re-implement.** Every decision it makes is
delegated to the code the synchronous API path already uses — advancement asks
`_auto_advance_if_complete`, stage entry goes through
`case_lifecycle.on_stage_entered`. That is what stops this from quietly
becoming a second, divergent definition of the process.

**Opt-in twice, deliberately.** The platform flag
`VELARIS_CASE_TEMPORAL_INTERPRETER_ENABLED` must be on *and* the case type must
declare `"orchestration": "temporal"` in its `definition_json`. A workflow that
drives case advancement should never switch itself on for case types that were
authored without it.

**Constraint 3:** workflow state holds only the case id. Case payloads are
resolved inside activities and never enter Temporal's database, so per-tenant
encryption and residency routing are not bypassed.

**Signals are nudges; the poll is the safety net.** The API signals
`step_completed` so the interpreter reacts promptly, but it also re-checks on a
timer — a missed signal delays an advance, it never strands a case. This is the
same posture `CaseLifecycleWorkflow` takes, on purpose: one failure model to
reason about, not two.

Start via::

    await client.start_workflow(
        CaseInterpreterWorkflow.run,
        {"case_id": "..."},
        id=f"helix-interpreter-{case_id}",
        task_queue="helix-case-service",
    )

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

from datetime import timedelta
from typing import Any

from temporalio import workflow
from temporalio.common import RetryPolicy

with workflow.unsafe.imports_passed_through():
    from case_service.temporal.activities.interpreter_activities import (
        case_status,
        enter_stage,
        load_case_plan,
        try_advance,
    )

_ACTIVITY_TIMEOUT = timedelta(seconds=30)
_RETRY = RetryPolicy(
    initial_interval=timedelta(seconds=1),
    backoff_coefficient=2.0,
    maximum_interval=timedelta(minutes=1),
)

#: Re-check even without a signal — catches step completions from paths that
#: cannot signal (rules, module-driven advance, direct repository callers).
_POLL_INTERVAL = timedelta(minutes=5)
#: Let the API request's transaction commit before the interpreter reads.
_SETTLE = timedelta(seconds=5)
#: Continue-as-new before history grows unbounded on a long-lived case.
_HISTORY_SOFT_LIMIT = 2000


@workflow.defn(name="helix.case.interpreter")
class CaseInterpreterWorkflow:
    """Drives a case through its declared stages by interpreting the definition."""

    def __init__(self) -> None:
        self._case_id: str = ""
        self._nudged = False
        self._terminal = False
        self._last_stage: str | None = None
        self._advances = 0

    # ── Queries ───────────────────────────────────────────────────

    @workflow.query(name="get_interpreter_state")
    def get_interpreter_state(self) -> dict[str, Any]:
        return {
            "case_id": self._case_id,
            "current_stage_id": self._last_stage,
            "advances": self._advances,
            "terminal": self._terminal,
        }

    # ── Signals (best-effort nudges) ──────────────────────────────

    @workflow.signal(name="step_completed")
    async def step_completed(self, data: dict[str, Any]) -> None:
        """A step finished — the current stage may now be complete."""
        self._nudged = True

    @workflow.signal(name="status_changed")
    async def status_changed(self, data: dict[str, Any]) -> None:
        self._nudged = True

    @workflow.signal(name="cancel_case")
    async def cancel_case(self, data: dict[str, Any]) -> None:
        self._terminal = True

    # ── Run ───────────────────────────────────────────────────────

    @workflow.run
    async def run(self, params: dict[str, Any]) -> dict[str, Any]:
        self._case_id = str(params.get("case_id") or "")
        if not self._case_id:
            return {"status": "no_case_id"}

        plan = await workflow.execute_activity(
            load_case_plan, self._case_id,
            start_to_close_timeout=_ACTIVITY_TIMEOUT, retry_policy=_RETRY,
        )

        if not plan.get("exists"):
            return {"status": "case_not_found"}
        if not plan.get("interpreted"):
            # The case type never asked to be interpreted. Exiting is the
            # correct outcome — not an error, and not something to retry.
            return {"status": "not_interpreted", "case_id": self._case_id}
        if plan.get("terminal"):
            return {"status": "already_terminal"}

        self._last_stage = plan.get("current_stage_id")

        # A case that has not entered a stage yet starts at the first declared
        # one. A case already mid-flight resumes where it is — the interpreter
        # can adopt a running case without rewinding it.
        if self._last_stage is None and plan.get("stage_ids"):
            first = plan["stage_ids"][0]
            result = await workflow.execute_activity(
                enter_stage, args=[self._case_id, first],
                start_to_close_timeout=_ACTIVITY_TIMEOUT, retry_policy=_RETRY,
            )
            if result.get("entered"):
                self._last_stage = first

        # Check first, wait second. The reverse — waiting before the first
        # check — makes the interpreter idle a full poll interval before doing
        # anything, including immediately after it has just entered a stage.
        # An integration test against a real server caught exactly that.
        while not self._terminal:
            status = await workflow.execute_activity(
                case_status, self._case_id,
                start_to_close_timeout=_ACTIVITY_TIMEOUT, retry_policy=_RETRY,
            )
            if not status.get("exists") or status.get("terminal"):
                self._terminal = True
                break

            advanced = await workflow.execute_activity(
                try_advance, self._case_id,
                start_to_close_timeout=_ACTIVITY_TIMEOUT, retry_policy=_RETRY,
            )
            if advanced.get("advanced"):
                self._advances += 1
                self._last_stage = advanced.get("stage_id")
                # Entering the new stage is what attaches its assignments and
                # SLAs. `_auto_advance_if_complete` moves the pointer; the
                # chokepoint does the rest.
                await workflow.execute_activity(
                    enter_stage, args=[self._case_id, self._last_stage],
                    start_to_close_timeout=_ACTIVITY_TIMEOUT, retry_policy=_RETRY,
                )
                # The next stage may already be complete — loop straight back
                # rather than sleeping on a case that is still moving.
                continue

            if workflow.info().get_current_history_length() > _HISTORY_SOFT_LIMIT:
                workflow.continue_as_new(args=[{"case_id": self._case_id}])

            # Nothing to do — idle until a signal or the poll interval.
            try:
                await workflow.wait_condition(
                    lambda: self._nudged or self._terminal,
                    timeout=_POLL_INTERVAL,
                )
            except TimeoutError:
                pass
            if self._nudged:
                self._nudged = False
                # Let the signalling request's transaction commit before the
                # next read, or the interpreter sees pre-commit state.
                await workflow.sleep(_SETTLE)

        return {
            "status": "completed",
            "case_id": self._case_id,
            "advances": self._advances,
            "final_stage_id": self._last_stage,
        }
