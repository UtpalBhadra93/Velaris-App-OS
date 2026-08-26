"""Schedules: writing them, approving them, and claiming a due one.

Three refusals carry this feature, and each is a refusal rather than a
convention:

* **approval** — a schedule is `draft` until somebody who is not its author
  approves it. An unattended action that runs at 2am while nobody is watching —
  emailing out, driving a supplier's site, touching hundreds of cases — is
  exactly what a second pair of eyes is for. It is also what stops the scheduler
  becoming a way around another gate: an approved schedule pointing at a *draft*
  bot still cannot run it;
* **any edit returns it to draft** — one rule, not a taxonomy of safe and unsafe
  fields. Changing a recipient list or a cron line is precisely the kind of edit
  a reviewer needs to see;
* **a missed window is skipped, never replayed** — the next occurrence is
  computed from *now*. A service that was down overnight must not send a night's
  worth of email at breakfast, and the skip is recorded so the silence is
  explained.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from velaris_shared.db.models import ScheduleModel, ScheduleRunModel
from case_service.scheduler.cron import CronError, CronExpression

log = logging.getLogger(__name__)

#: A firing this far behind is a missed window, not a late run: the platform was
#: down or the loop was stopped. It is recorded as `skipped` and the schedule
#: moves to its next occurrence.
MISSED_WINDOW = timedelta(minutes=30)

STATUSES = ("draft", "active", "disabled")


class ScheduleError(Exception):
    def __init__(self, message: str, *, status: int = 400):
        super().__init__(message)
        self.status = status


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


async def validate_schedule(session: AsyncSession, row: ScheduleModel) -> None:
    """Everything that must hold before a schedule may be stored.

    The cron line is proven to *fire* rather than merely to parse — `0 0 30 2 *`
    parses fine and never happens — and the action's own config is checked here
    too, so a broken action is refused at write time instead of failing silently
    at 2am.
    """
    from case_service.scheduler import actions

    try:
        CronExpression(row.cron).next_after(_utcnow(), row.timezone)
    except CronError as exc:
        raise ScheduleError(str(exc)) from exc

    handler = actions.get(row.action_kind)
    if handler is None:
        raise ScheduleError(
            f"Unknown action '{row.action_kind}'. Known: "
            f"{', '.join(actions.kinds())}.")
    await handler.validate(session, row, dict(row.action_config or {}))


async def approve(
    session: AsyncSession, row: ScheduleModel, *, approver: str,
) -> ScheduleModel:
    """Put a schedule into service. Never by its author — approval is the review."""
    if row.status == "active":
        raise ScheduleError("This schedule is already active.", status=409)

    # The author AND the last editor. `created_by` never changes, so comparing
    # against it alone let a reviewer approve a schedule, then rewrite what it
    # does — its recipients, its cron line, its whole action — and approve their
    # own change under the original author's name.
    me = (approver or "").strip().lower()
    for name, whose in ((row.created_by, "the person who wrote it"),
                        (row.last_edited_by, "the person who last changed it")):
        if (name or "").strip().lower() == me:
            raise ScheduleError(
                f"A schedule must be approved by somebody other than {whose}. "
                f"Approval is the review, and reviewing your own unattended 2am "
                f"job is not one. If you are the reviewer, ask the author to "
                f"make the change rather than making it yourself.", status=409)

    await validate_schedule(session, row)
    row.status = "active"
    row.approved_by = approver
    row.approved_at = _utcnow()
    row.next_run_at = CronExpression(row.cron).next_after(_utcnow(), row.timezone)
    await session.flush()
    return row


async def disable(session: AsyncSession, row: ScheduleModel) -> ScheduleModel:
    """Stop it firing. `next_run_at` is cleared so nothing can claim it."""
    row.status = "disabled"
    row.next_run_at = None
    await session.flush()
    return row


async def claim_due(
    session: AsyncSession, *, now: datetime | None = None, limit: int = 20,
) -> list[tuple[ScheduleModel, bool]]:
    """Take every schedule that is due, atomically.

    The claim is a conditional UPDATE whose WHERE still names the `next_run_at`
    that was read, so two workers racing for the same firing cannot both win —
    the loser's rowcount is zero. The row is advanced **before** the action runs,
    which is the difference between one late email and one every five seconds
    while a slow action is still going.

    Returns `(schedule, was_missed)`. A missed window is not run: its next
    occurrence is computed from now, so a platform that was down overnight does
    not deliver a night's backlog at breakfast.
    """
    now = now or _utcnow()
    claimed: list[tuple[ScheduleModel, bool]] = []

    rows = (await session.execute(
        select(ScheduleModel).where(
            ScheduleModel.status == "active",
            ScheduleModel.next_run_at.is_not(None),
            ScheduleModel.next_run_at <= now,
        ).order_by(ScheduleModel.next_run_at).limit(limit)
    )).scalars().all()

    for row in rows:
        due_at = _aware(row.next_run_at)
        try:
            following = CronExpression(row.cron).next_after(now, row.timezone)
        except CronError as exc:
            # A stored expression that no longer resolves must not spin: park it.
            log.error("schedule %s has an unusable cron: %s", row.id, exc)
            row.status = "disabled"
            row.next_run_at = None
            row.last_status = "error"
            row.last_error = str(exc)
            await session.flush()
            continue

        result = await session.execute(
            update(ScheduleModel)
            .where(ScheduleModel.id == row.id,
                   ScheduleModel.next_run_at == row.next_run_at,
                   ScheduleModel.status == "active")
            .values(next_run_at=following)
        )
        if (result.rowcount or 0) != 1:
            continue                    # another worker took this firing

        await session.refresh(row)
        claimed.append((row, bool(due_at and now - due_at > MISSED_WINDOW)))

    return claimed


async def record_run(
    session: AsyncSession, row: ScheduleModel, *, status: str, detail: dict,
    duration_ms: int | None = None,
) -> ScheduleRunModel:
    """Write the firing down — including the ones that did nothing."""
    run = ScheduleRunModel(
        schedule_id=row.id, status=status, detail=detail or {},
        duration_ms=duration_ms)
    session.add(run)
    row.last_run_at = _utcnow()
    row.last_status = status
    row.last_error = (detail or {}).get("error") if status == "error" else None
    await session.flush()
    return run


async def run_now(
    session: AsyncSession, row: ScheduleModel, *, actor: str,
) -> dict:
    """Fire a schedule by hand, off its timetable.

    Still gated on approval: "run now" on an unreviewed action would be the
    approval gate with an extra step. The timetable is untouched — a manual run
    must not move the next occurrence, or testing a schedule would silently skip
    tonight's.
    """
    from case_service.scheduler import actions

    if row.status != "active":
        raise ScheduleError(
            f"This schedule is {row.status}, so it cannot run. An unapproved "
            f"schedule has not been reviewed.", status=409)

    handler = actions.get(row.action_kind)
    if handler is None:
        raise ScheduleError(f"Unknown action '{row.action_kind}'.")

    started = _utcnow()
    # A manual run sits in an HTTP request, so its bound is tighter than the
    # loop's: a caller waiting on a browser tab is not a place to spend five
    # minutes, and the schedule's own firing is unaffected either way.
    budget = 60
    try:
        detail = await asyncio.wait_for(
            handler.run(session, row, dict(row.action_config or {})),
            timeout=budget)
        detail = {"manual": True, "actor": actor, **(detail or {})}
        status = "ok"
    except asyncio.TimeoutError:
        await session.rollback()
        row = await session.get(ScheduleModel, row.id)
        detail = {"manual": True, "actor": actor,
                  "error": f"The action did not finish within {budget}s. It was "
                           f"abandoned here; the schedule's own firing is "
                           f"unaffected."}
        status = "error"
    except Exception as exc:                       # noqa: BLE001 — recorded below
        detail = {"manual": True, "actor": actor, "error": str(exc)[:2000]}
        status = "error"

    ms = int((_utcnow() - started).total_seconds() * 1000)
    await record_run(session, row, status=status, detail=detail, duration_ms=ms)
    return {"status": status, "detail": detail}
