"""Event sourcing for the Case aggregate (roadmap #30a, report §2.2).

Case state becomes a **projection** of an append-only stream rather than being
replaced by it. `case_instances` keeps working exactly as it does today; this
adds a verifiable history alongside it. That is the whole design: every
existing read path is untouched, and what you gain is the ability to prove the
current row is what the recorded history says it should be.

Three constraints from the report, all load-bearing:

**Selective, not wholesale (§2.2 caveat).** Case aggregate only. Forms,
marketplace and HxDocs are explicitly out of scope and stay relational.

**Clean cutover, never a backfill (§7.5).** A case that existed before the
store was switched on cannot have an honest stream reconstructed —
`case_step_completions` and `case_event_log` record what happened but were
never append-only, so a rebuilt "history" would be a guess wearing the costume
of a fact. Only cases flagged `event_sourced` have a stream. Everything else
keeps working and simply cannot be replayed. This is a forward-only feature
gain, not a migration.

**Append-only means append-only.** `UNIQUE (case_id, seq)` does three jobs at
once: it orders the stream, it detects gaps, and it is the optimistic
concurrency check — two writers computing the same next `seq` will see one
succeed and one fail rather than silently interleaving. Nothing here updates or
deletes an event, and no other module writes to the table.

The payoff is `verify()`: replay the stream, derive what the case *should* look
like, and compare it against the relational row. Divergence means a write
bypassed the lifecycle chokepoints — a class of bug that is otherwise invisible
until someone notices a case in an impossible state.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from case_service.db.models import CaseEventModel, CaseInstanceModel

logger = logging.getLogger(__name__)

# ── Event vocabulary (§2.2 names these explicitly) ────────────────
CASE_CREATED = "CaseCreated"
STAGE_ENTERED = "StageEntered"
STEP_COMPLETED = "StepCompleted"
ASSIGNED = "Assigned"
STATUS_CHANGED = "StatusChanged"
SLA_BREACHED = "SLABreached"

KNOWN_EVENTS = frozenset({
    CASE_CREATED, STAGE_ENTERED, STEP_COMPLETED,
    ASSIGNED, STATUS_CHANGED, SLA_BREACHED,
})

#: Payload cap. Events are thin by design — ids and context, never a copy of
#: case data, for the same reason webhook payloads are thin (Group I): every
#: consumer of a fat event becomes a PII copy that redaction never reaches.
MAX_EVENT_BYTES = 16 * 1024


class EventStoreError(Exception):
    """The append was refused — a contract violation, not a transient fault."""


class ConcurrencyError(EventStoreError):
    """Another writer took this sequence number. Re-read and retry."""


def _too_big(data: Any) -> bool:
    import json

    try:
        return len(json.dumps(data, default=str).encode()) > MAX_EVENT_BYTES
    except Exception:
        return True


async def is_event_sourced(session: AsyncSession, case_id: uuid.UUID) -> bool:
    """Whether this case has a stream at all (post-cutover)."""
    return bool((await session.execute(
        select(CaseInstanceModel.event_sourced).where(CaseInstanceModel.id == case_id)
    )).scalar_one_or_none())


async def next_seq(session: AsyncSession, case_id: uuid.UUID) -> int:
    current = (await session.execute(
        select(func.max(CaseEventModel.seq)).where(CaseEventModel.case_id == case_id)
    )).scalar_one()
    return int(current or 0) + 1


async def append(
    session: AsyncSession,
    *,
    case_id: uuid.UUID,
    event_type: str,
    data: dict[str, Any] | None = None,
    actor_id: str | None = None,
    actor_type: str = "system",
    expected_seq: int | None = None,
    occurred_at: datetime | None = None,
) -> CaseEventModel:
    """Append one event. Raises rather than writing something unverifiable.

    `expected_seq` lets a caller assert which position it believes it is
    writing — the optimistic-concurrency form. Omitted, the next sequence is
    computed and the UNIQUE constraint still catches a race.
    """
    if event_type not in KNOWN_EVENTS:
        raise EventStoreError(
            f"{event_type!r} is not a known case event — add it to KNOWN_EVENTS "
            f"deliberately rather than writing a stream nothing can replay"
        )
    data = data or {}
    if _too_big(data):
        raise EventStoreError(
            f"event payload exceeds {MAX_EVENT_BYTES} bytes — events are thin by "
            f"design; store an id and let the consumer fetch under its own authz"
        )

    seq = expected_seq if expected_seq is not None else await next_seq(session, case_id)

    event = CaseEventModel(
        case_id=case_id, seq=seq, event_type=event_type, data=data,
        actor_id=actor_id, actor_type=actor_type,
        occurred_at=occurred_at or datetime.now(timezone.utc),
    )
    session.add(event)
    try:
        await session.flush()
    except IntegrityError as exc:
        await session.rollback()
        raise ConcurrencyError(
            f"sequence {seq} already exists for case {case_id} — another writer "
            f"won the race; re-read and retry"
        ) from exc
    return event


async def record(
    session: AsyncSession,
    *,
    case_id: uuid.UUID,
    event_type: str,
    data: dict[str, Any] | None = None,
    actor_id: str | None = None,
    actor_type: str = "system",
) -> CaseEventModel | None:
    """Best-effort append from a lifecycle chokepoint.

    Returns None — without raising — when the store is off or the case predates
    the cutover, which is the normal condition for most cases and must not turn
    a working case operation into a failure. Genuine contract violations are
    still logged loudly, because a silently-skipped event is how a stream stops
    being trustworthy.
    """
    from case_service.config import get_settings

    if not get_settings().event_sourcing_enabled:
        return None
    if not await is_event_sourced(session, case_id):
        return None
    try:
        return await append(
            session, case_id=case_id, event_type=event_type, data=data,
            actor_id=actor_id, actor_type=actor_type,
        )
    except EventStoreError as exc:
        logger.error("case %s: event %s NOT recorded: %s", case_id, event_type, exc)
        return None


async def stream(
    session: AsyncSession, case_id: uuid.UUID, *, after_seq: int = 0, limit: int = 1000,
) -> list[CaseEventModel]:
    """Read a case's events in order."""
    return list((await session.execute(
        select(CaseEventModel)
        .where(CaseEventModel.case_id == case_id, CaseEventModel.seq > after_seq)
        .order_by(CaseEventModel.seq)
        .limit(limit)
    )).scalars().all())


def project(events: list[CaseEventModel]) -> dict[str, Any]:
    """Fold a stream into the case state it implies.

    This is the read model rebuilt from first principles — deliberately derived
    only from events, with no peeking at the relational row, because a
    projection that consults the thing it is meant to verify verifies nothing.
    """
    state: dict[str, Any] = {
        "exists": False,
        "status": None,
        "current_stage_id": None,
        "assignee_ids": [],
        "steps_completed": [],
        "sla_breaches": 0,
        "events": len(events),
        "last_seq": 0,
    }
    for e in events:
        data = e.data or {}
        state["last_seq"] = e.seq
        if e.event_type == CASE_CREATED:
            state["exists"] = True
            state["status"] = data.get("status", "open")
        elif e.event_type == STAGE_ENTERED:
            state["current_stage_id"] = data.get("stage_id")
        elif e.event_type == STEP_COMPLETED:
            step = data.get("step_id")
            if step and step not in state["steps_completed"]:
                state["steps_completed"].append(step)
        elif e.event_type == ASSIGNED:
            who = data.get("assignee_id")
            if who and who not in state["assignee_ids"]:
                state["assignee_ids"].append(who)
        elif e.event_type == STATUS_CHANGED:
            state["status"] = data.get("to", state["status"])
        elif e.event_type == SLA_BREACHED:
            state["sla_breaches"] += 1
    return state


async def verify(session: AsyncSession, case_id: uuid.UUID) -> dict[str, Any]:
    """Replay the stream and compare it against the relational row.

    Divergence means something wrote case state without going through the
    lifecycle chokepoints — a bug class that is otherwise invisible until
    someone notices a case in an impossible state.

    Also reports sequence gaps: `seq` is contiguous by construction, so a hole
    means an event was deleted, which nothing is supposed to be able to do.
    """
    case = await session.get(CaseInstanceModel, case_id)
    if case is None:
        raise EventStoreError(f"Case {case_id} not found")

    if not case.event_sourced:
        return {
            "case_id": str(case_id),
            "event_sourced": False,
            "verifiable": False,
            "reason": (
                "case predates the event-sourcing cutover — its history was never "
                "recorded as an append-only stream and is deliberately not "
                "reconstructed (§7.5)"
            ),
        }

    events = await stream(session, case_id, limit=100_000)
    projected = project(events)

    seqs = [e.seq for e in events]
    gaps = [n for n in range(1, (seqs[-1] if seqs else 0) + 1) if n not in set(seqs)]

    mismatches = []
    if projected["status"] is not None and projected["status"] != case.status:
        mismatches.append({
            "field": "status", "projected": projected["status"], "actual": case.status,
        })
    actual_stage = str(case.current_stage_id) if case.current_stage_id else None
    if projected["current_stage_id"] != actual_stage:
        mismatches.append({
            "field": "current_stage_id",
            "projected": projected["current_stage_id"], "actual": actual_stage,
        })

    return {
        "case_id": str(case_id),
        "event_sourced": True,
        "verifiable": True,
        "events": len(events),
        "sequence_gaps": gaps,
        "consistent": not mismatches and not gaps,
        "mismatches": mismatches,
        "projection": projected,
    }
