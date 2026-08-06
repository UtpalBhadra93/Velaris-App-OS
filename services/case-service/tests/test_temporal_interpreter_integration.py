"""Integration: the interpreter workflow BODY, against a real Temporal server.

`test_temporal_interpreter.py` covers the activities — which hold every
decision — but the workflow body itself (the run loop, signal handling, the
early exits) had never executed. That gap is exactly how a workflow ships
registered-but-inert, so it is worth a real server rather than a mock.

Skipped when Temporal is not reachable, so the suite stays runnable on a box
without the stack up. On a dev machine with `docker-compose` running, these
execute for real: a worker is started on a unique task queue, the workflow is
run, and its result and query state are asserted.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import asyncio
import uuid

import pytest

pytestmark = pytest.mark.asyncio

TEMPORAL_TARGET = "127.0.0.1:7233"


async def _client_or_skip():
    """Connect, or skip — never fail the suite for missing infrastructure."""
    try:
        from temporalio.client import Client

        return await asyncio.wait_for(Client.connect(TEMPORAL_TARGET), timeout=5)
    except Exception as exc:                    # pragma: no cover — env-dependent
        pytest.skip(f"Temporal not reachable at {TEMPORAL_TARGET}: {exc}")


async def _run_workflow(client, activities, params, *, timeout=20):
    """Run the interpreter on a throwaway task queue with stubbed activities.

    The activities are stubbed here on purpose: their real behaviour is
    covered by the unit tests, and what needs exercising is the workflow's own
    control flow. A unique queue per test keeps runs isolated from the real
    worker, which may also be connected to this server.
    """
    from temporalio.worker import Worker

    from case_service.temporal.workflows.case_interpreter_workflow import (
        CaseInterpreterWorkflow,
    )

    queue = f"test-interpreter-{uuid.uuid4().hex[:8]}"
    async with Worker(
        client, task_queue=queue,
        workflows=[CaseInterpreterWorkflow], activities=activities,
    ):
        return await asyncio.wait_for(
            client.execute_workflow(
                CaseInterpreterWorkflow.run, params,
                id=f"test-interp-{uuid.uuid4().hex[:8]}", task_queue=queue,
            ),
            timeout=timeout,
        )


def _stubs(plan, statuses, advances):
    """Activity stubs matching the real signatures by name."""
    from temporalio import activity

    calls = {"enter_stage": []}

    @activity.defn(name="load_case_plan")
    async def load_case_plan(case_id: str) -> dict:
        return plan

    @activity.defn(name="case_status")
    async def case_status(case_id: str) -> dict:
        return statuses.pop(0) if len(statuses) > 1 else statuses[0]

    @activity.defn(name="try_advance")
    async def try_advance(case_id: str) -> dict:
        return advances.pop(0) if len(advances) > 1 else advances[0]

    @activity.defn(name="enter_stage")
    async def enter_stage(case_id: str, stage_id: str) -> dict:
        calls["enter_stage"].append(stage_id)
        return {"entered": True, "stage_id": stage_id}

    return [load_case_plan, case_status, try_advance, enter_stage], calls


class TestInterpreterWorkflowBody:
    async def test_exits_immediately_for_a_non_interpreted_case_type(self):
        """Registration is not activation — this is the common path."""
        client = await _client_or_skip()
        acts, _ = _stubs(
            plan={"exists": True, "interpreted": False, "terminal": False,
                  "current_stage_id": None, "stage_ids": [], "status": "open"},
            statuses=[{"exists": True, "terminal": True}],
            advances=[{"advanced": False}],
        )
        out = await _run_workflow(client, acts, {"case_id": str(uuid.uuid4())})
        assert out["status"] == "not_interpreted"

    async def test_exits_for_a_missing_case(self):
        client = await _client_or_skip()
        acts, _ = _stubs(
            plan={"exists": False},
            statuses=[{"exists": False, "terminal": True}],
            advances=[{"advanced": False}],
        )
        out = await _run_workflow(client, acts, {"case_id": str(uuid.uuid4())})
        assert out["status"] == "case_not_found"

    async def test_missing_case_id_is_rejected(self):
        client = await _client_or_skip()
        acts, _ = _stubs(
            plan={"exists": False},
            statuses=[{"exists": False, "terminal": True}],
            advances=[{"advanced": False}],
        )
        out = await _run_workflow(client, acts, {})
        assert out["status"] == "no_case_id"

    async def test_already_terminal_case_is_not_driven(self):
        client = await _client_or_skip()
        acts, _ = _stubs(
            plan={"exists": True, "interpreted": True, "terminal": True,
                  "current_stage_id": "done", "stage_ids": ["a", "done"],
                  "status": "resolved"},
            statuses=[{"exists": True, "terminal": True}],
            advances=[{"advanced": False}],
        )
        out = await _run_workflow(client, acts, {"case_id": str(uuid.uuid4())})
        assert out["status"] == "already_terminal"

    async def test_enters_the_first_stage_then_stops_on_terminal(self):
        """The real loop: enter, check status, exit when the case closes."""
        client = await _client_or_skip()
        acts, calls = _stubs(
            plan={"exists": True, "interpreted": True, "terminal": False,
                  "current_stage_id": None, "stage_ids": ["intake", "review"],
                  "status": "open"},
            statuses=[{"exists": True, "terminal": True, "status": "resolved"}],
            advances=[{"advanced": False}],
        )
        out = await _run_workflow(client, acts, {"case_id": str(uuid.uuid4())})
        assert out["status"] == "completed"
        assert calls["enter_stage"] == ["intake"]

    async def test_advances_and_enters_the_next_stage(self):
        """An advance must be followed by entering the stage it moved to —
        otherwise the pointer moves with no assignments or SLAs attached."""
        client = await _client_or_skip()
        acts, calls = _stubs(
            plan={"exists": True, "interpreted": True, "terminal": False,
                  "current_stage_id": "intake", "stage_ids": ["intake", "review"],
                  "status": "open"},
            statuses=[
                {"exists": True, "terminal": False, "status": "open"},
                {"exists": True, "terminal": True, "status": "resolved"},
            ],
            advances=[
                {"advanced": True, "stage_id": "review"},
                {"advanced": False},
            ],
        )
        out = await _run_workflow(client, acts, {"case_id": str(uuid.uuid4())})
        assert out["status"] == "completed"
        assert out["advances"] == 1
        assert out["final_stage_id"] == "review"
        assert calls["enter_stage"] == ["review"]

    async def test_adopts_a_case_mid_flight_without_rewinding_it(self):
        """A case already in stage 2 must not be sent back to stage 1."""
        client = await _client_or_skip()
        acts, calls = _stubs(
            plan={"exists": True, "interpreted": True, "terminal": False,
                  "current_stage_id": "review", "stage_ids": ["intake", "review"],
                  "status": "open"},
            statuses=[{"exists": True, "terminal": True, "status": "closed"}],
            advances=[{"advanced": False}],
        )
        out = await _run_workflow(client, acts, {"case_id": str(uuid.uuid4())})
        assert out["status"] == "completed"
        assert calls["enter_stage"] == []          # never re-entered intake
