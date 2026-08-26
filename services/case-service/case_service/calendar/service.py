"""Calendar — when a person is available, and who covers when they are not.

`business_calendars` has always described the **organisation**: work days, work
hours, a timezone and holidays, which the SLA clock counts against. Nothing
described a **person**, so `assignment_router` picked people who were on holiday
and escalation trees escalated into empty inboxes: the question "is she in
today?" had no answer to give.

Everything here answers that question, and `available_at` is the whole contract —
inside the person's working pattern, and not inside an **approved** absence.

Two rules worth stating once:

* **Only `approved` counts.** A `requested` absence changes nothing. If a request
  rerouted work the moment it was typed, anybody could quietly stop their own
  queue by asking for leave they never take.
* **Fail open.** When the calendar cannot answer — no pattern, an unresolvable
  tenant, a query that raises — the person is available. A calendar outage must
  not stop work being assigned, and the failure mode of guessing "absent" is a
  queue that silently fills up while everybody looks idle.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from velaris_shared.db.models import (
    BusinessCalendarModel,
    CalendarEventModel,
    UserAbsenceModel,
    UserDirectoryModel,
    UserWorkPatternModel,
)

logger = logging.getLogger(__name__)

KINDS = ("holiday", "sick", "training", "other")
STATUSES = ("requested", "approved", "cancelled")
SOURCES = ("manual", "case", "import")

#: Personal detail. Stripped from anything a colleague sees — "sick" is health
#: data, and a team calendar that leaks it is a compliance incident with a
#: friendly icon.
PRIVATE_FIELDS = ("kind", "note")


class CalendarError(Exception):
    """A calendar record was refused. `status` is the HTTP code to answer with."""

    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.status = status


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _aware(value: datetime | None) -> datetime | None:
    """Half this schema's datetime columns come back naive on some backends.

    They all hold UTC, so stamping rather than converting is correct — the same
    fix `core/timestamps.iso_utc` applies at the API boundary.
    """
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


# ── tenant ────────────────────────────────────────────────────────────────

async def _tenant_candidates(session: AsyncSession, tenant_id: str) -> list[str]:
    """Every string form that names this tenant.

    Tenant columns are not one shape across this schema — `user_directory`
    stores a slug in a `String(64)` while `case_types` stores a GUID — and a
    caller arrives holding whichever their token carried. Resolving once and
    comparing against both forms is what stops a filter matching nothing.
    """
    names = {str(tenant_id)}
    try:
        from case_service.tenancy.identity import resolve_tenant

        resolved = await resolve_tenant(session, tenant_id)
        if resolved:
            names |= {resolved[1], str(resolved[0])}
    except Exception:                                    # pragma: no cover
        logger.debug("tenant resolution failed for %r", tenant_id, exc_info=True)
    return sorted(names)


# ── working pattern ───────────────────────────────────────────────────────

class WorkingPattern:
    """The hours a person is expected to be at work, in their own timezone."""

    def __init__(self, work_days: list[int], start_hour: int, end_hour: int,
                 tz_name: str) -> None:
        self.work_days = work_days or [1, 2, 3, 4, 5]
        self.start_hour = start_hour
        self.end_hour = end_hour
        self.tz_name = tz_name or "UTC"

    @property
    def tz(self):
        try:
            return ZoneInfo(self.tz_name)
        except (ZoneInfoNotFoundError, ValueError):
            logger.warning("unknown timezone %r on a working pattern; using UTC",
                           self.tz_name)
            return timezone.utc

    def covers(self, when: datetime) -> bool:
        local = _aware(when).astimezone(self.tz)
        if local.isoweekday() not in self.work_days:
            return False
        return self.start_hour <= local.hour < self.end_hour


DEFAULT_PATTERN = WorkingPattern([1, 2, 3, 4, 5], 9, 17, "UTC")


async def working_pattern(
    session: AsyncSession, user_id: str, *, tenant_id: str,
) -> WorkingPattern:
    """The person's pattern, falling back through their calendar to the default.

    Precedence, most specific first: the pattern row's own fields, then the
    business calendar it names, then `user_directory.timezone`, then the
    platform default. A pattern row that sets only a timezone is a normal thing
    to want and must not force the rest to be re-entered.
    """
    names = await _tenant_candidates(session, tenant_id)
    row = (await session.execute(
        select(UserWorkPatternModel).where(
            UserWorkPatternModel.user_id == user_id,
            UserWorkPatternModel.tenant_id.in_(names),
        )
    )).scalar_one_or_none()

    cal = None
    if row is not None and row.business_calendar_id:
        cal = await session.get(BusinessCalendarModel, row.business_calendar_id)

    directory = (await session.execute(
        select(UserDirectoryModel).where(UserDirectoryModel.user_id == user_id)
    )).scalar_one_or_none()

    def pick(*candidates):
        for c in candidates:
            if c is not None:
                return c
        return None

    return WorkingPattern(
        work_days=pick(getattr(row, "work_days", None),
                       getattr(cal, "work_days", None),
                       DEFAULT_PATTERN.work_days),
        start_hour=pick(getattr(row, "work_start_hour", None),
                        getattr(cal, "work_start_hour", None),
                        DEFAULT_PATTERN.start_hour),
        end_hour=pick(getattr(row, "work_end_hour", None),
                      getattr(cal, "work_end_hour", None),
                      DEFAULT_PATTERN.end_hour),
        tz_name=pick(getattr(row, "timezone", None),
                     getattr(cal, "timezone", None),
                     getattr(directory, "timezone", None),
                     DEFAULT_PATTERN.tz_name),
    )


# ── the questions everything else asks ────────────────────────────────────

async def absences_between(
    session: AsyncSession,
    user_ids: list[str],
    start: datetime,
    end: datetime,
    *,
    tenant_id: str,
    status: str = "approved",
) -> dict[str, list[UserAbsenceModel]]:
    """Approved absences overlapping [start, end), grouped by user.

    Overlap, not containment: an absence that started last week and ends
    tomorrow is the one that matters today, and a naive `starts_at BETWEEN`
    would miss exactly that case.
    """
    if not user_ids:
        return {}
    names = await _tenant_candidates(session, tenant_id)
    q = (select(UserAbsenceModel)
         .where(UserAbsenceModel.tenant_id.in_(names),
                UserAbsenceModel.user_id.in_(user_ids),
                UserAbsenceModel.starts_at < end,
                UserAbsenceModel.ends_at > start)
         .order_by(UserAbsenceModel.starts_at))
    if status:
        q = q.where(UserAbsenceModel.status == status)

    out: dict[str, list[UserAbsenceModel]] = {}
    for row in (await session.execute(q)).scalars().all():
        out.setdefault(row.user_id, []).append(row)
    return out


async def absent_at(
    session: AsyncSession, user_id: str, when: datetime | None = None, *,
    tenant_id: str,
) -> bool:
    """Is this person on **approved** leave at this moment?

    Deliberately narrower than `available_at`, and this is the distinction that
    matters most in the whole module:

    * an **absence** is an explicit, approved fact somebody entered;
    * a **working pattern** is an assumption — the platform default is 09:00 to
      17:00, Monday to Friday, which nobody chose.

    Anything that decides **who owns a piece of work** asks this one. Applying a
    guessed working window there would mean a case arriving at 22:00 could be
    assigned to nobody at all, and every 24/7 deployment that never configured
    patterns would silently start queueing everything overnight.

    `available_at` — absence *and* hours — is for "will they see it now?"
    questions: proposing a meeting slot, or colouring a screen.
    """
    when = _aware(when) or _utcnow()
    try:
        for row in (await absences_between(
                session, [user_id], when, when, tenant_id=tenant_id)).get(user_id, []):
            if _aware(row.starts_at) <= when < _aware(row.ends_at):
                return True
        return False
    except Exception:                                    # pragma: no cover
        logger.warning("absence lookup failed for %s; treating as present",
                       user_id, exc_info=True)
        return False


async def not_absent(
    session: AsyncSession, user_ids: list[str], when: datetime | None = None, *,
    tenant_id: str,
) -> list[str]:
    """The subset of `user_ids` not on approved leave, in the order given.

    One query rather than one per candidate: the assignment router calls this on
    every case creation.
    """
    if not user_ids:
        return []
    when = _aware(when) or _utcnow()
    try:
        away = await absences_between(session, user_ids, when, when, tenant_id=tenant_id)
        return [uid for uid in user_ids
                if not any(_aware(a.starts_at) <= when < _aware(a.ends_at)
                           for a in away.get(uid, []))]
    except Exception:                                    # pragma: no cover
        logger.warning("absence filter failed; treating everyone as present",
                       exc_info=True)
        return list(user_ids)


async def available_at(
    session: AsyncSession, user_id: str, when: datetime | None = None, *,
    tenant_id: str,
) -> bool:
    """Is this person at work at this moment — not on leave, and within hours?

    For "who should own this work?" use `absent_at` / `not_absent` instead: the
    working pattern is an assumption and an absence is a fact, and only the fact
    belongs in an assignment decision. See `absent_at`.

    Fails open: anything that cannot be answered answers True.
    """
    when = _aware(when) or _utcnow()
    try:
        absences = await absences_between(
            session, [user_id], when, when, tenant_id=tenant_id)
        # `starts_at < when AND ends_at > when` is what "at this moment" means;
        # the window query above is inclusive at both ends when start == end, so
        # re-check rather than trusting the range.
        for row in absences.get(user_id, []):
            if _aware(row.starts_at) <= when < _aware(row.ends_at):
                return False

        pattern = await working_pattern(session, user_id, tenant_id=tenant_id)
        return pattern.covers(when)
    except Exception:                                    # pragma: no cover
        logger.warning("availability lookup failed for %s; treating as available",
                       user_id, exc_info=True)
        return True


async def available_users(
    session: AsyncSession, user_ids: list[str], when: datetime | None = None, *,
    tenant_id: str,
) -> list[str]:
    """The subset of `user_ids` who are at work, in the order given.

    One query for the absences rather than one per candidate: the assignment
    router calls this on every case creation.
    """
    if not user_ids:
        return []
    when = _aware(when) or _utcnow()
    try:
        away = await absences_between(session, user_ids, when, when, tenant_id=tenant_id)
        out = []
        for uid in user_ids:
            if any(_aware(a.starts_at) <= when < _aware(a.ends_at)
                   for a in away.get(uid, [])):
                continue
            pattern = await working_pattern(session, uid, tenant_id=tenant_id)
            if pattern.covers(when):
                out.append(uid)
        return out
    except Exception:                                    # pragma: no cover
        logger.warning("availability filter failed; treating everyone as available",
                       exc_info=True)
        return list(user_ids)


async def delegate_for(
    session: AsyncSession, user_id: str, when: datetime | None = None, *,
    tenant_id: str,
) -> str | None:
    """Who covers for this person right now, if anybody.

    Only an approved absence names a delegate, and a delegate who is themselves
    away is not returned — cover that is also on holiday is not cover. The chain
    is followed exactly one step: if A delegates to B and B to C, work does not
    silently land on C, because nobody agreed to that.
    """
    when = _aware(when) or _utcnow()
    try:
        for row in (await absences_between(
                session, [user_id], when, when, tenant_id=tenant_id)).get(user_id, []):
            if not row.delegate_user_id:
                continue
            if not (_aware(row.starts_at) <= when < _aware(row.ends_at)):
                continue
            # The delegate's own *leave* disqualifies them; their working hours
            # do not. Cover at 22:00 is still cover.
            if not await absent_at(session, row.delegate_user_id, when, tenant_id=tenant_id):
                return row.delegate_user_id
        return None
    except Exception:                                    # pragma: no cover
        logger.warning("delegate lookup failed for %s", user_id, exc_info=True)
        return None


# ── writes ────────────────────────────────────────────────────────────────

def validate_absence(row: UserAbsenceModel) -> None:
    """Everything that must hold before an absence is stored."""
    if row.kind not in KINDS:
        raise CalendarError(f"kind must be one of: {', '.join(KINDS)}")
    if row.source not in SOURCES:
        raise CalendarError(f"source must be one of: {', '.join(SOURCES)}")
    start, end = _aware(row.starts_at), _aware(row.ends_at)
    if start is None or end is None:
        raise CalendarError("An absence needs a start and an end.")
    if end <= start:
        raise CalendarError("An absence must end after it starts.")
    if (end - start).days > 366:
        raise CalendarError(
            "An absence longer than a year is a leaver, not a holiday — "
            "deactivate the account instead.")
    if row.delegate_user_id and row.delegate_user_id == row.user_id:
        raise CalendarError("Somebody cannot cover for themselves.")


async def overlapping(
    session: AsyncSession, row: UserAbsenceModel, *, tenant_id: str,
) -> UserAbsenceModel | None:
    """An existing non-cancelled absence for the same person over the same time."""
    names = await _tenant_candidates(session, tenant_id)
    q = (select(UserAbsenceModel)
         .where(UserAbsenceModel.tenant_id.in_(names),
                UserAbsenceModel.user_id == row.user_id,
                UserAbsenceModel.status != "cancelled",
                UserAbsenceModel.starts_at < row.ends_at,
                UserAbsenceModel.ends_at > row.starts_at))
    if row.id:
        q = q.where(UserAbsenceModel.id != row.id)
    return (await session.execute(q.limit(1))).scalar_one_or_none()


async def approve(
    session: AsyncSession, row: UserAbsenceModel, *, approver: str,
) -> UserAbsenceModel:
    """Put an absence into effect. Never by the person taking it.

    The same two-person discipline bots and schedules carry, for a smaller but
    real reason: approved leave reroutes other people's work, and an approval
    anybody can grant themselves is a form rather than a gate.
    """
    if row.status == "approved":
        raise CalendarError("This absence is already approved.", status=409)
    if row.status == "cancelled":
        raise CalendarError("A cancelled absence cannot be approved.", status=409)

    me = (approver or "").strip().lower()
    for name in (row.user_id, row.created_by):
        if (name or "").strip().lower() == me:
            raise CalendarError(
                "Leave must be approved by somebody other than the person taking "
                "it. If you are the approver, ask a colleague to record it.",
                status=409)

    row.status = "approved"
    row.approved_by = approver
    row.approved_at = _utcnow()
    await session.flush()
    return row


async def cancel(session: AsyncSession, row: UserAbsenceModel) -> UserAbsenceModel:
    """Withdraw it. Kept rather than deleted — an approved absence that was
    cancelled is a fact somebody may need to explain later."""
    row.status = "cancelled"
    await session.flush()
    return row


def visible_absence(row: UserAbsenceModel, *, full: bool) -> dict:
    """One absence as JSON. `full=False` strips the personal half.

    A colleague sees *busy*; the person, their manager and an admin see why.
    """
    from case_service.core.timestamps import iso_utc

    out = {
        "id": str(row.id),
        "user_id": row.user_id,
        "starts_at": iso_utc(row.starts_at),
        "ends_at": iso_utc(row.ends_at),
        "all_day": row.all_day,
        "status": row.status,
        "delegate_user_id": row.delegate_user_id,
        "source": row.source,
        "approved_by": row.approved_by,
        "approved_at": iso_utc(row.approved_at),
        "created_by": row.created_by,
        "created_at": iso_utc(row.created_at),
    }
    if full:
        out["kind"] = row.kind
        out["note"] = row.note
        out["case_id"] = str(row.case_id) if row.case_id else None
    return out


# ── recurrence ────────────────────────────────────────────────────────────
#
# A rule, expanded on read. Storing occurrences instead would mean a daily
# stand-up writes 250 rows a year, an endless one has no bottom, and moving the
# meeting means rewriting all of them.

FREQUENCIES = ("daily", "weekly", "monthly")
#: The furthest a single read will expand. A window is already capped by the
#: API; this is the second bound, so a bad rule cannot turn one row into a
#: million dictionaries in memory.
MAX_OCCURRENCES = 500


def validate_recurrence(rule: dict | None) -> dict | None:
    """Refuse a rule that cannot fire, or that would never stop being asked about."""
    if not rule:
        return None
    if not isinstance(rule, dict):
        raise CalendarError("recurrence must be an object.")
    freq = str(rule.get("freq", "")).lower()
    if freq not in FREQUENCIES:
        raise CalendarError(f"recurrence.freq must be one of: {', '.join(FREQUENCIES)}")
    try:
        interval = int(rule.get("interval", 1))
    except (TypeError, ValueError):
        raise CalendarError("recurrence.interval must be a whole number.")
    if not 1 <= interval <= 52:
        raise CalendarError("recurrence.interval must be between 1 and 52.")

    out = {"freq": freq, "interval": interval}
    until = rule.get("until")
    if until:
        try:
            parsed = until if isinstance(until, datetime) else datetime.fromisoformat(str(until).replace("Z", "+00:00"))
        except ValueError:
            raise CalendarError("recurrence.until must be a date and time.")
        out["until"] = _aware(parsed).isoformat()
    count = rule.get("count")
    if count is not None:
        try:
            count = int(count)
        except (TypeError, ValueError):
            raise CalendarError("recurrence.count must be a whole number.")
        if not 1 <= count <= MAX_OCCURRENCES:
            raise CalendarError(f"recurrence.count must be between 1 and {MAX_OCCURRENCES}.")
        out["count"] = count
    return out


def _advance(when: datetime, freq: str, interval: int) -> datetime:
    """The next start after `when`.

    Monthly keeps the day of the month and **skips** a month that has no such
    day — the 31st does not become the 1st of the next month, because a meeting
    silently moving to a different month is worse than one that does not happen.
    """
    if freq == "daily":
        return when + timedelta(days=interval)
    if freq == "weekly":
        return when + timedelta(weeks=interval)

    month0 = when.month - 1 + interval
    year = when.year + month0 // 12
    month = month0 % 12 + 1
    day = when.day
    while True:
        try:
            return when.replace(year=year, month=month, day=day)
        except ValueError:
            month0 += interval
            year = when.year + month0 // 12
            month = month0 % 12 + 1


def occurrences(event, start: datetime, end: datetime) -> list[tuple[datetime, datetime]]:
    """Every (starts_at, ends_at) of this event that overlaps [start, end).

    A non-recurring event yields itself when it overlaps and nothing otherwise,
    so callers never branch on whether a rule is present.
    """
    # Both the row's own columns and the caller's window are coerced: half this
    # schema's datetimes come back naive on some backends, and a naive/aware
    # comparison raises rather than answering wrongly — which is better, but
    # only if it never reaches a caller.
    ev_start, ev_end = _aware(event.starts_at), _aware(event.ends_at)
    start, end = _aware(start), _aware(end)
    duration = ev_end - ev_start
    rule = event.recurrence or None
    excluded = {str(d)[:10] for d in (getattr(event, "excluded_dates", None) or [])}

    if not rule:
        return [(ev_start, ev_end)] if ev_start < end and ev_end > start else []

    freq = rule.get("freq", "weekly")
    interval = int(rule.get("interval", 1) or 1)
    until = _aware(datetime.fromisoformat(str(rule["until"]).replace("Z", "+00:00"))) if rule.get("until") else None
    limit = int(rule.get("count") or MAX_OCCURRENCES)

    out: list[tuple[datetime, datetime]] = []
    cursor = ev_start
    for _ in range(MAX_OCCURRENCES):
        if len(out) >= limit or cursor >= end:
            break
        if until and cursor > until:
            break
        finish = cursor + duration
        if finish > start and cursor.date().isoformat() not in excluded:
            out.append((cursor, finish))
        cursor = _advance(cursor, freq, interval)
    return out


# ── free/busy ─────────────────────────────────────────────────────────────

async def busy_between(
    session: AsyncSession,
    user_ids: list[str],
    start: datetime,
    end: datetime,
    *,
    tenant_id: str,
) -> dict[str, list[tuple[datetime, datetime]]]:
    """Every busy interval per user in the window — leave **and** meetings.

    Leave and a meeting are the same fact to a scheduler: the person cannot take
    a third thing at that time. Kept as intervals rather than a boolean so a
    caller can propose a slot rather than only test one.
    """
    if not user_ids:
        return {}
    start, end = _aware(start), _aware(end)
    out: dict[str, list[tuple[datetime, datetime]]] = {}

    for uid, rows in (await absences_between(
            session, user_ids, start, end, tenant_id=tenant_id)).items():
        out.setdefault(uid, []).extend(
            (_aware(r.starts_at), _aware(r.ends_at)) for r in rows)

    names = await _tenant_candidates(session, tenant_id)
    # Two shapes: a one-off that overlaps the window, and a series that started
    # before it. A recurring event's own `starts_at` is the first occurrence, so
    # filtering on it alone would hide every stand-up set up last year.
    events = (await session.execute(
        select(CalendarEventModel).where(
            CalendarEventModel.tenant_id.in_(names),
            CalendarEventModel.status == "scheduled",
            CalendarEventModel.starts_at < end,
            or_(CalendarEventModel.ends_at > start,
                CalendarEventModel.recurrence.is_not(None)),
        )
    )).scalars().all()
    for ev in events:
        # A repeating meeting blocks every one of its occurrences, not only the
        # first — the whole point of the rule.
        slots = occurrences(ev, start, end)
        if not slots:
            continue
        for uid in (ev.attendee_user_ids or []) + [ev.organiser_user_id]:
            if uid in user_ids:
                out.setdefault(uid, []).extend(slots)
    return out


async def propose_slots(
    session: AsyncSession,
    user_ids: list[str],
    start: datetime,
    end: datetime,
    *,
    tenant_id: str,
    minutes: int = 30,
    limit: int = 5,
) -> list[dict]:
    """Times in [start, end) when **everybody** named is free.

    "Free" here is the strict reading — inside each person's working pattern,
    not on leave, and not already in a meeting — because this answers *when
    shall we meet*, which is exactly the question working hours were meant for.
    Contrast `not_absent`, which decides who *owns* work and must ignore hours.

    Returns at most `limit` slots, earliest first, on a `minutes` grid. An empty
    list means no common slot, which is a real answer and not an error.
    """
    start, end = _aware(start), _aware(end)
    if not user_ids or end <= start:
        return []
    minutes = max(5, min(int(minutes), 8 * 60))

    busy = await busy_between(session, user_ids, start, end, tenant_id=tenant_id)
    patterns = {uid: await working_pattern(session, uid, tenant_id=tenant_id)
                for uid in user_ids}

    slots: list[dict] = []
    step = timedelta(minutes=minutes)
    cursor = start
    # A whole month at 30-minute steps is ~1,500 iterations; the cap keeps a
    # careless window from turning into a long loop.
    for _ in range(2000):
        if cursor >= end or len(slots) >= limit:
            break
        finish = cursor + step
        if finish > end:
            break
        ok = True
        for uid in user_ids:
            if not patterns[uid].covers(cursor) or not patterns[uid].covers(finish - timedelta(minutes=1)):
                ok = False
                break
            if any(b_start < finish and b_end > cursor for b_start, b_end in busy.get(uid, [])):
                ok = False
                break
        if ok:
            slots.append({"starts_at": cursor.isoformat(), "ends_at": finish.isoformat()})
        cursor = finish
    return slots
