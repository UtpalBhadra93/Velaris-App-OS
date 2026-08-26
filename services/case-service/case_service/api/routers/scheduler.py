"""Schedules — the timetable, its review gate, and its run history.

The literal paths (`/preview`, `/actions`) are declared **before**
`/{schedule_id}`: FastAPI matches in declaration order, and a parameterised UUID
path declared first swallows every literal that follows it, answering 422 on a
word that was never meant to be an id.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import logging
import uuid
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from velaris_shared import hxguard
from velaris_shared.auth.models import AuthenticatedUser
from velaris_shared.db.models import ScheduleModel, ScheduleRunModel
from velaris_shared.db.session import get_session
from velaris_shared.middleware.endpoint_rate_limit import rate_limit
from case_service.scheduler import actions, cron, service
from case_service.tenancy.identity import caller_tenant_or_refuse, may_reach_tenant
from case_service.core.timestamps import iso_utc


async def _mine_or_404(session, user, row: ScheduleModel) -> ScheduleModel:
    """A schedule belonging to another tenant is not yours to read or act on.

    404 rather than 403: "exists but is not yours" is an answer nobody outside
    that tenant needs. Without this, one tenant's admin could **approve, run,
    disable and delete** another tenant's timetable — proven against a running
    service on 2026-08-20, including a 204 on somebody else's schedule.
    """
    if not await may_reach_tenant(session, user, row.tenant_id):
        raise HTTPException(404, "Schedule not found.")
    return row


async def _load_or_404(session, user, schedule_id) -> ScheduleModel:
    row = await session.get(ScheduleModel, schedule_id)
    if row is None:
        raise HTTPException(404, "Schedule not found.")
    return await _mine_or_404(session, user, row)

log = logging.getLogger(__name__)

router = APIRouter(prefix="/scheduler", tags=["scheduler"])

#: "Run now" fires the action immediately, so on an approved email schedule it is
#: a send button that can be held down: a hundred recipients per press, by anyone
#: holding `scheduler.schedules.run`. The timetable itself is unaffected by this
#: limit — it is the manual path that gets a bound.
_run_rl = rate_limit(max_calls=10, window_seconds=60, name="schedule-run")

#: Parsing a cron line walks up to four years of candidate days, so the preview
#: is cheap per call and not free in a loop.
_preview_rl = rate_limit(max_calls=120, window_seconds=60, name="schedule-preview")


def _dict(row: ScheduleModel) -> dict:
    return {
        "id": str(row.id), "tenant_id": row.tenant_id, "name": row.name,
        "description": row.description, "cron": row.cron,
        "timezone": row.timezone, "action_kind": row.action_kind,
        "action_config": row.action_config or {},
        "status": row.status, "created_by": row.created_by,
        "last_edited_by": row.last_edited_by,
        "approved_by": row.approved_by,
        "approved_at": iso_utc(row.approved_at),
        "next_run_at": iso_utc(row.next_run_at),
        "last_run_at": iso_utc(row.last_run_at),
        "last_status": row.last_status, "last_error": row.last_error,
    }


class ScheduleIn(BaseModel):
    tenant_id: str = "default"
    name: str = Field(..., max_length=255)
    description: str = ""
    cron: str = Field(..., max_length=120)
    timezone: str = Field("UTC", max_length=64)
    action_kind: str = Field(..., max_length=30)
    action_config: dict = {}


#: What an edit may change. `status`, `created_by`, `approved_by`, `approved_at`
#: and `next_run_at` are absent on purpose: an editor that could set them would
#: be a way around the approval gate, which is the whole control here.
EDITABLE_FIELDS = ("name", "description", "cron", "timezone",
                   "action_kind", "action_config")


@router.get("/actions")
async def list_actions(
    _user: AuthenticatedUser = Depends(hxguard.guard("scheduler.schedules.read")),
):
    """What a schedule can fire — generated from the handlers, so the editor
    cannot offer an action that does not exist."""
    return {"actions": actions.catalogue()}


class PreviewIn(BaseModel):
    cron: str = Field(..., max_length=120)
    timezone: str = Field("UTC", max_length=64)
    count: int = Field(5, ge=1, le=20)


@router.post("/preview", dependencies=[Depends(_preview_rl)])
async def preview(
    body: PreviewIn,
    _user: AuthenticatedUser = Depends(hxguard.guard("scheduler.schedules.read")),
):
    """The next few firing times, so nobody saves a cron line they misread."""
    try:
        return {"cron": body.cron, "timezone": body.timezone,
                "next": cron.describe(body.cron, body.timezone, body.count)}
    except cron.CronError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.get("")
async def list_schedules(
    status: Optional[str] = Query(None),
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(hxguard.guard("scheduler.schedules.read")),
):
    stmt = select(ScheduleModel)
    if status:
        stmt = stmt.where(ScheduleModel.status == status)
    rows = (await session.execute(
        stmt.order_by(ScheduleModel.name))).scalars().all()
    # Filtered in Python, not SQL: a tenant token is legitimately a slug OR a
    # UUID, so `same_tenant` is the only comparison that holds on both
    # conventions — a raw WHERE fails closed on one of them.
    mine = [r for r in rows if await may_reach_tenant(session, user, r.tenant_id)]
    return {"schedules": [_dict(r) for r in mine]}


@router.post("", status_code=201)
async def create_schedule(
    body: ScheduleIn,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(hxguard.guard("scheduler.schedules.write")),
):
    """Write a schedule. It is created **draft** and does not fire until approved."""
    try:
        tenant_id = await caller_tenant_or_refuse(session, user, body.tenant_id)
    except PermissionError as exc:
        # A schedule's tenant decides which bots, connectors, cases and sync
        # destinations its action can resolve, so an unchecked `tenant_id` in the
        # body is a cross-tenant reach spelled as a field.
        raise HTTPException(403, str(exc)) from exc

    row = ScheduleModel(
        tenant_id=tenant_id, name=body.name, description=body.description,
        cron=body.cron, timezone=body.timezone, action_kind=body.action_kind,
        action_config=body.action_config or {}, status="draft",
        created_by=user.username)
    try:
        await service.validate_schedule(session, row)
    except (service.ScheduleError, Exception) as exc:
        raise HTTPException(getattr(exc, "status", 400), str(exc)) from exc

    session.add(row)
    await session.commit()
    await session.refresh(row)
    return _dict(row)


@router.get("/{schedule_id}")
async def get_schedule(
    schedule_id: uuid.UUID,
    limit: int = Query(20, ge=1, le=200),
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(hxguard.guard("scheduler.schedules.read")),
):
    row = await _load_or_404(session, user, schedule_id)
    runs = (await session.execute(
        select(ScheduleRunModel).where(ScheduleRunModel.schedule_id == row.id)
        .order_by(desc(ScheduleRunModel.fired_at)).limit(limit)
    )).scalars().all()
    next_times: list[str] = []
    try:
        next_times = cron.describe(row.cron, row.timezone, 3)
    except cron.CronError:
        pass
    return {
        "schedule": _dict(row),
        "next": next_times,
        "runs": [{"id": str(r.id), "status": r.status,
                  "fired_at": iso_utc(r.fired_at),
                  "duration_ms": r.duration_ms, "detail": r.detail or {}}
                 for r in runs],
    }


@router.post("/{schedule_id}/approve")
async def approve_schedule(
    schedule_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(hxguard.guard("scheduler.schedules.approve")),
):
    """Put a schedule into service — never by its author."""
    row = await _load_or_404(session, user, schedule_id)
    try:
        await service.approve(session, row, approver=user.username)
    except service.ScheduleError as exc:
        raise HTTPException(exc.status, str(exc)) from exc
    except Exception as exc:                                   # action refusals
        raise HTTPException(400, str(exc)) from exc
    await session.commit()

    from velaris_shared.db.security_events import log_security_event

    await log_security_event(
        session, event_type="schedule_approved", severity="high",
        user_id=user.user_id, resource_type="schedule", resource_id=str(row.id),
        action="approve", outcome="success",
        details={"name": row.name, "cron": row.cron, "timezone": row.timezone,
                 "action_kind": row.action_kind})
    await session.commit()
    await session.refresh(row)
    return _dict(row)


@router.post("/{schedule_id}/disable")
async def disable_schedule(
    schedule_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(hxguard.guard("scheduler.schedules.write")),
):
    """Stop it firing. `next_run_at` is cleared, so nothing can claim it."""
    row = await _load_or_404(session, user, schedule_id)
    await service.disable(session, row)
    await session.commit()
    return _dict(row)


@router.post("/{schedule_id}/run", dependencies=[Depends(_run_rl)])
async def run_schedule(
    schedule_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(hxguard.guard("scheduler.schedules.run")),
):
    """Fire it now, off its timetable. The timetable itself is untouched."""
    row = await _load_or_404(session, user, schedule_id)
    try:
        result = await service.run_now(session, row, actor=user.username)
    except service.ScheduleError as exc:
        raise HTTPException(exc.status, str(exc)) from exc
    await session.commit()
    return result


@router.delete("/{schedule_id}", status_code=204)
async def delete_schedule(
    schedule_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(hxguard.guard("scheduler.schedules.write")),
):
    row = await _load_or_404(session, user, schedule_id)
    name, kind, cron = row.name, row.action_kind, row.cron
    await session.delete(row)
    await session.commit()

    # Recorded, because the delete cascades: the schedule's whole run history
    # goes with it, and "what used to run at 2am?" must not become unanswerable
    # by pressing a button.
    from velaris_shared.db.security_events import log_security_event

    await log_security_event(
        session, event_type="schedule_deleted", severity="high",
        user_id=user.user_id, resource_type="schedule",
        resource_id=str(schedule_id), action="delete", outcome="success",
        details={"name": name, "action_kind": kind, "cron": cron})
    await session.commit()


@router.put("/{schedule_id}")
async def update_schedule(
    schedule_id: uuid.UUID,
    body: ScheduleIn,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(hxguard.guard("scheduler.schedules.write")),
):
    """Edit a schedule. **Any change returns it to `draft`.**

    One rule, not a taxonomy of safe and unsafe fields: changing a recipient
    list, a cron line or an action's target is exactly the kind of edit the
    reviewer needed to see. Editing nothing changes nothing — a no-op save must
    not de-approve a live schedule.
    """
    row = await _load_or_404(session, user, schedule_id)

    # `tenant_id` is not in EDITABLE_FIELDS, so an edit cannot move a schedule
    # into another tenant; `_load_or_404` already refused one that is not the
    # caller's.
    incoming = {f: getattr(body, f) for f in EDITABLE_FIELDS}
    changed = [f for f in EDITABLE_FIELDS if getattr(row, f) != incoming[f]]
    if not changed:
        return _dict(row)

    for field in changed:
        setattr(row, field, incoming[field])
    try:
        await service.validate_schedule(session, row)
    except (service.ScheduleError, Exception) as exc:
        await session.rollback()
        raise HTTPException(getattr(exc, "status", 400), str(exc)) from exc

    was_active = row.status == "active"
    previous_approver = row.approved_by
    # Who made this change, so approval can refuse them.
    row.last_edited_by = user.username
    row.status = "draft"
    row.approved_by = None
    row.approved_at = None
    # Cleared, not left standing: a draft schedule that still had a next firing
    # would be claimed by the loop, and the review would be decoration.
    row.next_run_at = None
    await session.commit()
    await session.refresh(row)

    if was_active:
        from velaris_shared.db.security_events import log_security_event

        await log_security_event(
            session, event_type="schedule_edited_to_draft", severity="high",
            user_id=user.user_id, resource_type="schedule",
            resource_id=str(row.id), action="update", outcome="success",
            details={"name": row.name, "fields": changed,
                     "previous_approver": previous_approver})
        await session.commit()
    return _dict(row)
