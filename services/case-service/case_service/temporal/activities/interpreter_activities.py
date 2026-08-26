"""Activities for the `definition_json` interpreter (Tier-1 §12 / Tier-2 #21).

`docs/Future/temporal-decision-record.md` binds Velaris to **Option A**: one
generic workflow that *interprets* the existing case-type `definition_json` at
runtime. Option B — compiling each case type into generated workflow code —
is forbidden, permanently, because it would turn every case type into a code
artifact requiring a developer and break the no-code promise platform-wide.

These activities are where Option A is actually kept honest. They do not
contain a second copy of the process semantics: advancement asks
`_auto_advance_if_complete`, **the same function the synchronous API path
calls**, and stage entry goes through `case_lifecycle.on_stage_entered`, the
same chokepoint every other advance path uses. So there is exactly one
definition of "when does a stage complete" and one place that creates
assignments, starts SLAs and writes audit — the interpreter orchestrates, it
does not re-implement.

Constraint 3 of the decision record: **workflow state holds IDs, never case
data.** Every activity here takes and returns ids and small status values;
payloads are resolved inside the activity via direct repository calls and
never cross the Temporal boundary. Temporal's database must not become a copy
of case data that per-tenant encryption and residency routing do not cover.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import logging
import uuid
from typing import Any

from temporalio import activity

from velaris_shared.db import repository as repo
from velaris_shared.db.session import get_session_factory

logger = logging.getLogger(__name__)

#: Statuses at which the interpreter stops driving a case.
TERMINAL_STATUSES = ("resolved", "closed", "cancelled")


def _ordered_stage_ids(definition: dict[str, Any]) -> list[str]:
    """Stage ids in author-declared order.

    Sorted on `order` exactly as `_auto_advance_if_complete` does — if these
    two disagreed about sequence, the interpreter would advance to a different
    stage than the synchronous path, which is the divergence Option A exists
    to prevent.
    """
    stages = sorted(
        (definition or {}).get("stages", []) or [],
        key=lambda s: s.get("order", 0),
    )
    return [str(s["id"]) for s in stages if s.get("id")]


@activity.defn
async def load_case_plan(case_id: str) -> dict[str, Any]:
    """The case's stage plan and where it currently sits.

    Returns ids and status only — no case data (constraint 3).
    """
    factory = get_session_factory()
    async with factory() as session:
        case = await repo.get_case_instance(session, uuid.UUID(case_id))
        if case is None:
            return {"exists": False}
        ct = await repo.get_case_type(session, case.case_type_id)
        definition = (ct.definition_json if ct else {}) or {}
        return {
            "exists": True,
            "status": case.status,
            "terminal": case.status in TERMINAL_STATUSES,
            "current_stage_id": (
                str(case.current_stage_id) if case.current_stage_id else None
            ),
            "stage_ids": _ordered_stage_ids(definition),
            # Per-case-type opt-in: the interpreter never takes over a case
            # type that did not ask for it.
            "interpreted": str(definition.get("orchestration") or "").lower() == "temporal",
        }


@activity.defn
async def enter_stage(case_id: str, stage_id: str) -> dict[str, Any]:
    """Enter a stage through the normal lifecycle chokepoint.

    Deliberately calls `case_lifecycle.on_stage_entered` rather than writing
    `current_stage_id` directly: that chokepoint creates assignments, starts
    stage SLAs, refreshes urgency, writes the audit entry and emits the
    event-sourcing `StageEntered` event. Bypassing it would produce a case
    that looks advanced but has none of the machinery attached.
    """
    from case_service.core import case_lifecycle

    factory = get_session_factory()
    async with factory() as session:
        case = await repo.get_case_instance(session, uuid.UUID(case_id))
        if case is None:
            return {"entered": False, "reason": "case not found"}
        ct = await repo.get_case_type(session, case.case_type_id)
        definition = (ct.definition_json if ct else {}) or {}
        stage = next(
            (s for s in definition.get("stages", []) if str(s.get("id")) == stage_id),
            None,
        )
        if stage is None:
            return {"entered": False, "reason": f"stage {stage_id} not in definition"}

        assignments = await case_lifecycle.on_stage_entered(
            session,
            case_id=uuid.UUID(case_id),
            stage=stage,
            case_type_def=definition,
        )
        await session.commit()
        return {"entered": True, "stage_id": stage_id, "assignments": len(assignments)}


@activity.defn
async def try_advance(case_id: str) -> dict[str, Any]:
    """Ask whether the current stage is complete, and advance if so.

    Calls `_auto_advance_if_complete` — the SAME function the synchronous step
    -completion path calls — so the HxSentinel verify gate, the required-step
    rules and the "no required steps means no auto-advance" guard all apply
    identically whether a human or the interpreter asked.
    """
    from case_service.api.routers.cases import _auto_advance_if_complete

    factory = get_session_factory()
    async with factory() as session:
        case = await repo.get_case_instance(session, uuid.UUID(case_id))
        if case is None or not case.current_stage_id:
            return {"advanced": False}
        ct = await repo.get_case_type(session, case.case_type_id)
        definition = (ct.definition_json if ct else {}) or {}

        new_stage, held_by_risk = await _auto_advance_if_complete(
            session, uuid.UUID(case_id), str(case.current_stage_id), definition,
        )
        await session.commit()
        return {"advanced": new_stage is not None, "stage_id": new_stage,
                "held_by_risk": held_by_risk}


@activity.defn
async def case_status(case_id: str) -> dict[str, Any]:
    """Current status — the interpreter's stop condition."""
    factory = get_session_factory()
    async with factory() as session:
        case = await repo.get_case_instance(session, uuid.UUID(case_id))
        if case is None:
            return {"exists": False, "terminal": True}
        return {
            "exists": True,
            "status": case.status,
            "terminal": case.status in TERMINAL_STATUSES,
            "current_stage_id": (
                str(case.current_stage_id) if case.current_stage_id else None
            ),
        }
