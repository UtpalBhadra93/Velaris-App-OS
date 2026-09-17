"""Case lifecycle orchestrator.

High-level operations that compose the core modules (assignment
routing, SLA tracking, urgency calculation, relationship management)
into coherent lifecycle actions.

These functions are called by the API routers and Temporal activities.

Copyright (c) 2024-2025 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from case_service.core import (
    assignment_router,
    relationship_manager,
    sla_tracker,
    urgency_calculator,
)
from velaris_shared.db import repository as repo

logger = logging.getLogger(__name__)


def _sla_uses_v2(sla_policy: dict[str, Any]) -> bool:
    """P34b — a policy opts into v2 via use_v2=true OR escalation_tree_id."""
    return bool(sla_policy.get("use_v2") or sla_policy.get("escalation_tree_id"))


async def _start_policy_sla(
    session: AsyncSession,
    *,
    case_id: uuid.UUID,
    case_type_id: uuid.UUID | None,
    sla_policy: dict[str, Any],
    target_id: str,
    tenant_id: str | None = None,
):
    """Start one SLA instance, routing to v2 when the policy opts in."""
    if _sla_uses_v2(sla_policy):
        return await sla_tracker.start_sla_v2(
            session,
            case_id=case_id,
            case_type_id=case_type_id,
            sla_policy=sla_policy,
            target_id=target_id,
            tenant_id=tenant_id,
        )
    return await sla_tracker.start_sla(
        session,
        case_id=case_id,
        sla_policy=sla_policy,
        target_id=target_id,
    )


async def start_stage_slas(
    session: AsyncSession,
    *,
    case_id: uuid.UUID,
    case_type_id: uuid.UUID | None,
    stage: dict[str, Any],
    case_type_def: dict[str, Any],
    tenant_id: str | None = None,
) -> int:
    """Start the SLA configured on a stage, if any and not already running.

    Chokepoint for every stage-entry path (manual transition, auto-advance,
    module-driven advance). Idempotent: re-entering a stage while its SLA
    is still active does not start a duplicate.
    """
    sla_policy_id = stage.get("sla_policy_id")
    if not sla_policy_id:
        return 0

    stage_id = stage["id"]
    existing = await repo.get_sla_instances(session, case_id)
    for sla in existing:
        if (
            sla.sla_policy_id == sla_policy_id
            and sla.target_id == stage_id
            and sla.status in ("on_track", "at_risk", "paused")
        ):
            return 0

    for sp in case_type_def.get("sla_policies", []):
        if sp["id"] == sla_policy_id:
            await _start_policy_sla(
                session,
                case_id=case_id,
                case_type_id=case_type_id,
                sla_policy=sp,
                target_id=stage_id,
                tenant_id=tenant_id,
            )
            return 1
    logger.warning(
        "Stage %s references SLA policy %s not present in case type definition",
        stage_id, sla_policy_id,
    )
    return 0


async def on_case_created(
    session: AsyncSession,
    *,
    case_id: uuid.UUID,
    case_type_def: dict[str, Any],
    case_type_id: uuid.UUID | None = None,
    tenant_id: str | None = None,
) -> None:
    """Post-creation hook: start case-level SLAs and compute initial urgency.

    Called after the case instance row is inserted, before the
    Temporal workflow begins.
    """
    # Event sourcing cutover (#30a / §7.5): a case is marked event-sourced at
    # BIRTH or never. Flipping an existing case on later would leave a stream
    # that starts mid-history and looks complete — worse than no stream.
    from velaris_shared.config import get_settings
    from case_service.core import event_store

    if get_settings().event_sourcing_enabled:
        case = await repo.get_case_instance(session, case_id)
        if case is not None and not case.event_sourced:
            case.event_sourced = True
            await session.flush()
            await event_store.record(
                session, case_id=case_id, event_type=event_store.CASE_CREATED,
                data={
                    "case_type_id": str(case_type_id) if case_type_id else None,
                    "status": case.status,
                    "tenant_id": tenant_id,
                },
                actor_id=case.created_by, actor_type="user" if case.created_by else "system",
            )

    # Start case-level SLAs
    for sla_policy in case_type_def.get("sla_policies", []):
        # Only case-level SLAs (not stage-scoped)
        if sla_policy.get("scope", "case") == "case":
            await _start_policy_sla(
                session,
                case_id=case_id,
                case_type_id=case_type_id,
                sla_policy=sla_policy,
                target_id=str(case_id),
                tenant_id=tenant_id,
            )

    # Initial urgency
    await urgency_calculator.refresh_urgency_for_case(session, case_id)


async def on_stage_entered(
    session: AsyncSession,
    *,
    case_id: uuid.UUID,
    stage: dict[str, Any],
    case_type_def: dict[str, Any],
) -> list:
    """Called when a case enters a new stage.

    - Updates current_stage_id
    - Starts stage-level SLA
    - Creates assignments for stage steps
    - Refreshes urgency
    """
    stage_id = stage["id"]

    # Record the transition in the process event log BEFORE the stage pointer
    # moves, so the case's previous stage can be closed with a `stage_exit`.
    # Roadmap #40: these two events are the only source of stage-duration
    # history, and nothing wrote them before — `log_stage_transition` existed
    # but had no caller, so statistical SLA prediction had nothing to measure.
    case = await repo.get_case_instance(session, case_id)
    if case is not None:
        from velaris_shared.process_mining import event_logger
        try:
            if case.current_stage_id and case.current_stage_id != stage_id:
                await event_logger.log_stage_transition(
                    session, case_id=case_id, case_type_id=case.case_type_id,
                    stage_id=case.current_stage_id, activity_type="stage_exit",
                )
            await event_logger.log_stage_transition(
                session, case_id=case_id, case_type_id=case.case_type_id,
                stage_id=stage_id, activity_type="stage_enter",
            )
        except Exception as exc:      # observability must never block a case
            logger.warning("stage transition not logged for case %s: %s", case_id, exc)

    # Update case stage
    await repo.update_case_instance(
        session, case_id, values={"current_stage_id": stage_id}
    )

    # #30a: the projection above and the event below must agree — this is the
    # chokepoint `verify()` checks against.
    from case_service.core import event_store as _es
    await _es.record(
        session, case_id=case_id, event_type=_es.STAGE_ENTERED,
        data={"stage_id": stage_id, "stage_name": stage.get("name", "")},
    )

    # Start stage SLA if configured
    await start_stage_slas(
        session,
        case_id=case_id,
        case_type_id=None,
        stage=stage,
        case_type_def=case_type_def,
    )

    # The stage's `on_enter_status`, on the workflow path as well as the two
    # HTTP ones. Three places write `current_stage_id`, so a status effect wired
    # to one of them would hold when an operator clicked and quietly not hold
    # when the interpreter moved the case — which is the harder bug of the two
    # to notice. Never raises: a configuration error must not wedge a workflow.
    try:
        from case_service.case_actions import stages as _stages

        await _stages.apply_on_enter_status(
            session, case_id=case_id, stage=stage, actor=None)
    except Exception as exc:                                     # noqa: BLE001
        logger.warning("stage on_enter_status failed for case %s: %s", case_id, exc)

    # Create assignments
    steps = stage.get("steps", [])
    assignments = await assignment_router.create_assignments_for_stage(
        session,
        case_id=case_id,
        stage_id=stage_id,
        steps=steps,
    )

    # Refresh urgency
    await urgency_calculator.refresh_urgency_for_case(session, case_id)

    await repo.append_audit_entry(
        session,
        data={
            "case_id": case_id,
            "action": "stage_entered",
            "actor_type": "system",
            "details": {
                "stage_id": stage_id,
                "stage_name": stage.get("name", ""),
                "assignments_created": len(assignments),
            },
        },
    )

    return assignments


async def on_step_completed(
    session: AsyncSession,
    *,
    case_id: uuid.UUID,
    step_id: str,
    result: dict[str, Any] | None = None,
    completed_by: str | None = None,
) -> dict[str, Any]:
    """Called when a work item is completed.

    - Marks the assignment as completed
    - Refreshes urgency
    - Returns summary for the workflow signal
    """
    # Find and complete the active assignment for this step
    from sqlalchemy import select
    from velaris_shared.db.models import CaseAssignmentModel

    stmt = select(CaseAssignmentModel).where(
        CaseAssignmentModel.case_id == case_id,
        CaseAssignmentModel.step_id == step_id,
        CaseAssignmentModel.status == "active",
    )
    db_result = await session.execute(stmt)
    assignment = db_result.scalar_one_or_none()

    if assignment:
        now = datetime.now(timezone.utc)
        await repo.update_assignment(
            session,
            assignment.id,
            values={"status": "completed", "completed_at": now},
        )

    await urgency_calculator.refresh_urgency_for_case(session, case_id)

    from case_service.core import event_store as _es
    await _es.record(
        session, case_id=case_id, event_type=_es.STEP_COMPLETED,
        data={"step_id": step_id},
        actor_id=completed_by,
        actor_type="user" if completed_by else "system",
    )

    return {
        "case_id": str(case_id),
        "step_id": step_id,
        "completed_by": completed_by,
    }


async def on_status_changed(
    session: AsyncSession,
    *,
    case_id: uuid.UUID,
    old_status: str,
    new_status: str,
) -> None:
    """Called after a case status change.

    - Propagates status to parent cases
    - Cancels SLAs on terminal statuses
    - Pauses/resumes SLAs based on status
    - Refreshes urgency
    """
    from case_service.core import event_store as _es
    await _es.record(
        session, case_id=case_id, event_type=_es.STATUS_CHANGED,
        data={"from": old_status, "to": new_status},
    )

    # 198 — the THIRD copy of "which statuses end a case" lived here, and it
    # was the one that mattered most: a terminal status this tuple did not name
    # would leave the case's SLA clocks running forever on a closed case. Both
    # questions now come from the configured status set, and the two remaining
    # tuples below are only what an unreadable set falls back to.
    #
    # The pause is driven off the CATEGORY rather than off two hardcoded names,
    # so a tenant that adds `awaiting_survey` as `waiting` gets the pause with
    # no code change — which is the whole point of the category existing.
    from case_service.case_actions.statuses import resolve_set

    try:
        _set = await resolve_set(session)
        _new = _set.get(new_status)
        _old = _set.get(old_status)
        _is_terminal = bool(_new and _new.is_terminal)
        _is_waiting = bool(_new and _new.category == "waiting")
        _was_waiting = bool(_old and _old.category == "waiting")
    except Exception as _exc:  # noqa: BLE001
        logger.warning("status set unreadable in lifecycle: %s", _exc)
        _is_terminal = new_status in ("resolved", "closed", "cancelled")
        _is_waiting = new_status in ("pending_external", "pending_subcase")
        _was_waiting = old_status in ("pending_external", "pending_subcase")

    # Terminal statuses: cancel all SLAs
    if _is_terminal:
        await sla_tracker.cancel_slas_for_case(session, case_id)

    # Waiting statuses: pause SLAs
    elif _is_waiting:
        sla_instances = await repo.get_sla_instances(session, case_id)
        for sla in sla_instances:
            if sla.status in ("on_track", "at_risk"):
                await sla_tracker.pause_sla(
                    session,
                    case_id=case_id,
                    sla_policy_id=sla.sla_policy_id,
                )

    # Resuming from a wait: resume SLAs. Driven off the category on BOTH sides,
    # so leaving a wait for any non-waiting, non-terminal status resumes — not
    # only for the two statuses that used to be named here.
    elif _was_waiting and not _is_waiting and not _is_terminal:
        sla_instances = await repo.get_sla_instances(session, case_id)
        for sla in sla_instances:
            if sla.status == "paused":
                await sla_tracker.resume_sla(
                    session,
                    case_id=case_id,
                    sla_policy_id=sla.sla_policy_id,
                )

    # Propagate to parents
    await relationship_manager.propagate_status_change(
        session, case_id=case_id, new_status=new_status
    )

    # Refresh urgency
    await urgency_calculator.refresh_urgency_for_case(session, case_id)


async def on_priority_changed(
    session: AsyncSession,
    *,
    case_id: uuid.UUID,
    new_priority: str,
) -> None:
    """Called after a case priority change.

    - Propagates priority to parent cases
    - Refreshes urgency
    """
    await relationship_manager.propagate_priority_change(
        session, case_id=case_id, new_priority=new_priority
    )
    await urgency_calculator.refresh_urgency_for_case(session, case_id)
