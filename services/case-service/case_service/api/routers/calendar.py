"""Calendar — absences, working patterns, and who is available.

Absence is personal data before it is a scheduling input, and this router is
where that distinction is enforced: a colleague sees **busy**, while the person,
their manager and an admin see the kind and the note. `visible_absence(full=…)`
is the single place that decides, so no handler can forget.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import re
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from velaris_shared import hxguard
from velaris_shared.auth.models import AuthenticatedUser
from velaris_shared.db.models import (
    CalendarEventModel, UserAbsenceModel, UserDirectoryModel, UserWorkPatternModel,
)
from velaris_shared.db.session import get_session
from velaris_shared.middleware.endpoint_rate_limit import rate_limit
from case_service.calendar import service
from case_service.calendar.invites import send_invitations
from velaris_shared.config import get_settings

router = APIRouter(prefix="/calendar", tags=["calendar"])

#: Inviting sends mail in somebody's name to addresses outside this platform.
#: Bounded so a held-down button cannot turn into twenty messages to a guest —
#: resending is legitimate, spamming from a mistake is not.
_invite_rl = rate_limit(max_calls=10, window_seconds=60, name="calendar-invite")


def _tenant(user: AuthenticatedUser) -> str:
    return getattr(user, "tenant_id", None) or "default"


def _actor(user: AuthenticatedUser) -> str:
    return (getattr(user, "username", None)
            or getattr(user, "email", None)
            or getattr(user, "user_id", None)
            or "system")


def _identifiers(user: AuthenticatedUser) -> list[str]:
    """Every identifier this caller might be filed under, most specific first."""
    return [str(i) for i in (getattr(user, "user_id", None),
                             getattr(user, "username", None),
                             getattr(user, "email", None)) if i]


async def _me(session: AsyncSession, user: AuthenticatedUser) -> str:
    """The **directory** key for the caller, not the token's subject.

    This matters more than it looks. `auth_real` mints tokens with `sub` set to a
    UUID, while the SSO sync and the admin screens write a username or an email
    into `user_directory.user_id` — and `user_directory.user_id` is what
    assignment matches an assignee against. Filing somebody's leave under the
    UUID would produce an absence that looks right on this screen and changes
    **nothing** about who gets the work, which is the whole point of the record.
    The same mismatch made P37's access groups inert for every staff login for
    months.

    Resolution is by **precedence**, never `IN (...)`: merging the three
    identifiers into one query conflates two accounts the moment one person's
    username equals another's email.
    """
    for ident in _identifiers(user):
        row = (await session.execute(
            select(UserDirectoryModel).where(UserDirectoryModel.user_id == ident)
        )).scalar_one_or_none()
        if row is not None:
            return row.user_id
    # Nobody in the directory: fall back to the token's own identity rather than
    # refusing — a brand-new operator may not have been synced yet, and their
    # leave is still worth recording.
    return _identifiers(user)[0] if _identifiers(user) else "unknown"


def _is_admin(user: AuthenticatedUser) -> bool:
    roles = {str(r).lower() for r in (getattr(user, "roles", None) or [])}
    return bool(roles & {"admin", "superadmin"})


async def _manages(session: AsyncSession, user: AuthenticatedUser, subject_user_id: str) -> bool:
    """Does the caller manage this person, per `user_directory.manager_user_id`?"""
    row = (await session.execute(
        select(UserDirectoryModel).where(UserDirectoryModel.user_id == subject_user_id)
    )).scalar_one_or_none()
    if row is None or not row.manager_user_id:
        return False
    return str(row.manager_user_id) in set(_identifiers(user))


async def _may_see_detail(session: AsyncSession, user: AuthenticatedUser,
                          subject_user_id: str) -> bool:
    """Who sees *why* somebody is away: they do, their manager does, an admin does.

    Nobody else, ever. "Sick" is health data, and a team calendar that shows it
    is a compliance incident with a friendly icon.
    """
    if _is_admin(user):
        return True
    if subject_user_id in set(_identifiers(user)):
        return True
    return await _manages(session, user, subject_user_id)


async def _may_write_for(session: AsyncSession, user: AuthenticatedUser,
                         subject_user_id: str) -> bool:
    """Holding `calendar.absences.write` lets you record **your own** absence.

    Recording somebody else's needs a reason to: you manage them, or you are an
    admin. Otherwise a case worker could book a colleague's holiday, which is a
    quiet way to reroute that colleague's work.
    """
    return await _may_see_detail(session, user, subject_user_id)


#: A read window nobody legitimately needs to exceed. Both list endpoints scan
#: an index over (tenant, starts_at, ends_at); without a bound, `start=1970` and
#: `end=2999` is a full table scan any authenticated caller can ask for.
MAX_WINDOW_DAYS = 400

#: A note is a line about why somebody is away, not a document.
MAX_NOTE_CHARS = 2000

#: How many people one availability or slot question may name. Each candidate
#: costs a working-pattern lookup, and an unbounded list turns one request into
#: thousands of queries — the cheapest denial of service there is.
MAX_SUBJECTS = 100


def _check_window(start: datetime, end: datetime) -> None:
    if end <= start:
        raise HTTPException(400, "`end` must be after `start`.")
    if (end - start).days > MAX_WINDOW_DAYS:
        raise HTTPException(
            400, f"Ask for {MAX_WINDOW_DAYS} days or fewer at a time.")


def _check_len(value: str | None, limit: int, what: str) -> None:
    """Refuse what the column cannot hold.

    Without this the database raises instead, which answers 500 to what is
    plainly a bad request — and on MySQL, depending on the mode, it would
    silently truncate rather than raise at all.
    """
    if value is not None and len(value) > limit:
        raise HTTPException(400, f"{what} must be {limit} characters or fewer.")


#: Guests are invited by email, so the address is the whole identity. Bounded
#: because an invitation is a message this platform sends on the caller's behalf.
MAX_GUESTS = 50

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _check_guests(emails: list[str] | None) -> None:
    if not emails:
        return
    if len(emails) > MAX_GUESTS:
        raise HTTPException(400, f"An event is capped at {MAX_GUESTS} external guests.")
    for address in emails:
        address = str(address).strip()
        _check_len(address, 320, "An email address")
        if not _EMAIL_RE.match(address):
            raise HTTPException(400, f"'{address}' is not an email address.")


#: A calendar entry's link is rendered as an anchor in Studio. `javascript:` and
#: `data:` in an href are script execution one click away, so the scheme is an
#: allowlist rather than a blocklist.
MEETING_URL_SCHEMES = ("http", "https")

#: How many single dates a series may have skipped. Unbounded, a caller could
#: grow one row without limit, one call at a time.
MAX_EXCLUSIONS = 500


def _check_meeting_url(url: str | None) -> None:
    if not url:
        return
    _check_len(url, 2000, "The meeting link")
    from urllib.parse import urlsplit

    if urlsplit(url).scheme.lower() not in MEETING_URL_SCHEMES:
        raise HTTPException(
            400, "A meeting link must be an http or https URL.")


async def _check_case_anchor(session: AsyncSession, case_id, user: AuthenticatedUser) -> None:
    """A case-linked event must link to a case in **this** tenant.

    Not cosmetic: an invitation to that event sends email with
    `case_id=event.case_id`, so an unchecked id would file a message against
    another tenant's case.
    """
    if case_id is None:
        return
    from velaris_shared.db.models import CaseInstanceModel
    from case_service.tenancy.identity import same_tenant

    row = await session.get(CaseInstanceModel, case_id)
    if row is None or not await same_tenant(session, getattr(row, "tenant_id", None), _tenant(user)):
        raise HTTPException(400, "That case does not exist here.")


def _check_timezone(name: str | None) -> None:
    """A zone the server cannot resolve is a silent fallback to UTC later, which
    is a wrong answer wearing a right one's clothes."""
    if not name:
        return
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

    try:
        ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, ModuleNotFoundError):
        raise HTTPException(400, f"'{name}' is not a timezone this server knows.")


async def _known_user(session: AsyncSession, user_id: str, user: AuthenticatedUser,
                      what: str = "user_id") -> None:
    """Refuse an id that names nobody, or somebody in another tenant.

    A typo used to be stored happily: assignment matches `user_directory.user_id`,
    so leave recorded for `alcie` changes nothing at all and looks perfectly
    fine on the screen. The directory is the vocabulary, so it decides.

    A NULL `tenant_id` on a directory row stays visible, as everywhere else —
    those rows predate multi-tenancy.
    """
    from case_service.tenancy.identity import same_tenant

    row = (await session.execute(
        select(UserDirectoryModel).where(UserDirectoryModel.user_id == user_id)
    )).scalar_one_or_none()
    if row is None:
        raise HTTPException(400, f"'{user_id}' is not a known {what}. Use the id "
                                 f"from the user directory.")
    if not await same_tenant(session, row.tenant_id, _tenant(user)):
        # Same wording as the unknown case: whether somebody exists in another
        # tenant is not this caller's business.
        raise HTTPException(400, f"'{user_id}' is not a known {what}. Use the id "
                                 f"from the user directory.")


async def _absence_or_404(session: AsyncSession, absence_id: uuid.UUID,
                          user: AuthenticatedUser) -> UserAbsenceModel:
    from case_service.tenancy.identity import same_tenant

    row = await session.get(UserAbsenceModel, absence_id)
    if row is None or not await same_tenant(session, row.tenant_id, _tenant(user)):
        raise HTTPException(404, "Absence not found")
    return row


# ── schemas ───────────────────────────────────────────────────────────────

class AbsenceIn(BaseModel):
    user_id:          str | None = None      # None = the caller
    kind:             str = "holiday"
    starts_at:        datetime
    ends_at:          datetime
    all_day:          bool = True
    delegate_user_id: str | None = None
    note:             str = ""


class PatternIn(BaseModel):
    user_id:              str
    business_calendar_id: uuid.UUID | None = None
    work_days:            list[int] | None = None
    work_start_hour:      int | None = None
    work_end_hour:        int | None = None
    timezone:             str | None = None


# ── absences ──────────────────────────────────────────────────────────────

@router.get("/absences")
async def list_absences(
    user_id: str | None = None,
    status: str | None = None,
    start: datetime | None = None,
    end: datetime | None = None,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(hxguard.guard("calendar.absences.read")),
):
    """Absences in a window. Defaults to the next 90 days.

    The kind and the note appear only where the caller is entitled to them.
    """
    start = start or datetime.now(timezone.utc)
    end = end or (start + timedelta(days=90))
    _check_window(start, end)

    names = await service._tenant_candidates(session, _tenant(user))
    q = (select(UserAbsenceModel)
         .where(UserAbsenceModel.tenant_id.in_(names),
                UserAbsenceModel.starts_at < end,
                UserAbsenceModel.ends_at > start)
         .order_by(UserAbsenceModel.starts_at))
    if user_id:
        q = q.where(UserAbsenceModel.user_id == user_id)
    if status:
        q = q.where(UserAbsenceModel.status == status)

    rows = (await session.execute(q)).scalars().all()
    out = []
    for row in rows:
        full = await _may_see_detail(session, user, row.user_id)
        out.append(service.visible_absence(row, full=full))
    return out


@router.post("/absences", status_code=201)
async def create_absence(
    body: AbsenceIn,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(hxguard.guard("calendar.absences.write")),
):
    """Record an absence. It is `requested` until somebody else approves it.

    Requested changes nothing about assignment — see `calendar.service`.
    """
    subject = body.user_id or await _me(session, user)
    _check_len(subject, 255, "user_id")
    _check_len(body.delegate_user_id, 255, "delegate_user_id")
    _check_len(body.note, MAX_NOTE_CHARS, "The note")
    await _known_user(session, subject, user)
    if body.delegate_user_id:
        await _known_user(session, body.delegate_user_id, user, "delegate")
    if not await _may_write_for(session, user, subject):
        raise HTTPException(403, "You can only record your own absence, or one for "
                                 "somebody you manage.")

    row = UserAbsenceModel(
        tenant_id=_tenant(user), user_id=subject, kind=body.kind,
        starts_at=body.starts_at, ends_at=body.ends_at, all_day=body.all_day,
        delegate_user_id=body.delegate_user_id, note=body.note,
        status="requested", source="manual", created_by=_actor(user),
    )
    try:
        service.validate_absence(row)
    except service.CalendarError as exc:
        raise HTTPException(exc.status, str(exc)) from exc

    clash = await service.overlapping(session, row, tenant_id=_tenant(user))
    if clash is not None:
        raise HTTPException(
            409, f"This overlaps an existing absence ({clash.status}) for the same "
                 f"person. Cancel that one first, or change the dates.")

    session.add(row)
    await session.commit()
    await session.refresh(row)
    return service.visible_absence(row, full=True)


class AbsencePatchIn(BaseModel):
    kind:             str | None = None
    starts_at:        datetime | None = None
    ends_at:          datetime | None = None
    all_day:          bool | None = None
    delegate_user_id: str | None = None
    note:             str | None = None


@router.patch("/absences/{absence_id}")
async def update_absence(
    absence_id: uuid.UUID,
    body: AbsencePatchIn,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(hxguard.guard("calendar.absences.write")),
):
    """Change an absence. **Any change returns it to `requested`.**

    One rule, not a taxonomy of safe and unsafe fields — the same discipline the
    bot editor and the scheduler carry. Approved leave reroutes other people's
    work, and moving the dates of an already-approved absence without a second
    look is exactly the review the approval exists to force.

    Changing nothing changes nothing, including the approval: a no-op save must
    not de-approve somebody's holiday.
    """
    row = await _absence_or_404(session, absence_id, user)
    if not await _may_write_for(session, user, row.user_id):
        raise HTTPException(403, "You can only change your own absence, or one for "
                                 "somebody you manage.")
    if row.status == "cancelled":
        raise HTTPException(409, "A cancelled absence cannot be edited. Record a new one.")

    _check_len(body.note, MAX_NOTE_CHARS, "The note")
    _check_len(body.delegate_user_id, 255, "delegate_user_id")
    if body.delegate_user_id:
        await _known_user(session, body.delegate_user_id, user, "delegate")

    fields = ("kind", "starts_at", "ends_at", "all_day", "delegate_user_id", "note")
    incoming = {f: getattr(body, f) for f in fields if getattr(body, f) is not None}
    changed = [f for f, v in incoming.items() if getattr(row, f) != v]
    if not changed:
        return service.visible_absence(row, full=True)

    for f in changed:
        setattr(row, f, incoming[f])
    try:
        service.validate_absence(row)
    except service.CalendarError as exc:
        await session.rollback()
        raise HTTPException(exc.status, str(exc)) from exc

    clash = await service.overlapping(session, row, tenant_id=_tenant(user))
    if clash is not None:
        await session.rollback()
        raise HTTPException(409, "That would overlap another absence for the same person.")

    was_approved = row.status == "approved"
    row.status = "requested"
    row.approved_by = None
    row.approved_at = None
    await session.commit()
    await session.refresh(row)
    out = service.visible_absence(row, full=True)
    out["returned_to_requested"] = was_approved
    return out


@router.post("/absences/{absence_id}/approve")
async def approve_absence(
    absence_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(hxguard.guard("calendar.absences.approve")),
):
    """Put leave into effect — never for the person taking it."""
    row = await _absence_or_404(session, absence_id, user)
    if not (_is_admin(user) or await _manages(session, user, row.user_id)):
        raise HTTPException(403, "Only this person's manager, or an admin, may "
                                 "approve their leave.")
    try:
        await service.approve(session, row, approver=_actor(user))
    except service.CalendarError as exc:
        raise HTTPException(exc.status, str(exc)) from exc
    await session.commit()
    await session.refresh(row)
    return service.visible_absence(row, full=True)


@router.post("/absences/{absence_id}/cancel")
async def cancel_absence(
    absence_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(hxguard.guard("calendar.absences.write")),
):
    """Withdraw it. The row is kept: a cancelled approval is a fact somebody may
    have to explain later."""
    row = await _absence_or_404(session, absence_id, user)
    if not await _may_write_for(session, user, row.user_id):
        raise HTTPException(403, "You can only cancel your own absence, or one for "
                                 "somebody you manage.")
    await service.cancel(session, row)
    await session.commit()
    await session.refresh(row)
    return service.visible_absence(row, full=True)


# ── availability ──────────────────────────────────────────────────────────

@router.get("/availability")
async def availability(
    user_ids: list[str] = Query(default=[]),
    at: datetime | None = None,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(hxguard.guard("calendar.absences.read")),
):
    """Who, of these people, is at work — and who covers the ones who are not.

    This is the read every consumer makes, exposed so a screen can make it too.
    It answers **busy or not**, with no reason attached, whoever asks.
    """
    if not user_ids:
        raise HTTPException(400, "Name at least one user_id.")
    if len(user_ids) > MAX_SUBJECTS:
        raise HTTPException(400, f"Ask about {MAX_SUBJECTS} people or fewer at a time.")
    when = at or datetime.now(timezone.utc)
    tenant = _tenant(user)
    free = set(await service.available_users(session, user_ids, when, tenant_id=tenant))
    out = []
    for uid in user_ids:
        entry: dict[str, Any] = {"user_id": uid, "available": uid in free}
        if uid not in free:
            entry["delegate_user_id"] = await service.delegate_for(
                session, uid, when, tenant_id=tenant)
        out.append(entry)
    return {"at": when.isoformat(), "users": out}


# ── working patterns ──────────────────────────────────────────────────────

@router.get("/patterns/{user_id}")
async def get_pattern(
    user_id: str,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(hxguard.guard("calendar.absences.read")),
):
    """The hours this person is expected to work, after every fallback."""
    pattern = await service.working_pattern(session, user_id, tenant_id=_tenant(user))
    return {
        "user_id": user_id, "work_days": pattern.work_days,
        "work_start_hour": pattern.start_hour, "work_end_hour": pattern.end_hour,
        "timezone": pattern.tz_name,
    }


@router.put("/patterns")
async def upsert_pattern(
    body: PatternIn,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(hxguard.guard("calendar.patterns.write")),
):
    """Set somebody's contracted hours. Admin only — this is not a preference."""
    if body.work_days is not None and not all(1 <= d <= 7 for d in body.work_days):
        raise HTTPException(400, "work_days are ISO weekday numbers, 1 (Monday) to 7.")
    if body.work_start_hour is not None and not 0 <= body.work_start_hour <= 23:
        raise HTTPException(400, "work_start_hour is 0 to 23.")
    if body.work_end_hour is not None and not 1 <= body.work_end_hour <= 24:
        raise HTTPException(400, "work_end_hour is 1 to 24.")
    _check_len(body.user_id, 255, "user_id")
    _check_timezone(body.timezone)
    if (body.work_start_hour is not None and body.work_end_hour is not None
            and body.work_end_hour <= body.work_start_hour):
        raise HTTPException(400, "The working day must end after it starts.")

    names = await service._tenant_candidates(session, _tenant(user))
    row = (await session.execute(
        select(UserWorkPatternModel).where(
            UserWorkPatternModel.user_id == body.user_id,
            UserWorkPatternModel.tenant_id.in_(names),
        )
    )).scalar_one_or_none()

    if row is None:
        row = UserWorkPatternModel(tenant_id=_tenant(user), user_id=body.user_id,
                                   updated_by=_actor(user))
        session.add(row)
    for field in ("business_calendar_id", "work_days", "work_start_hour",
                  "work_end_hour", "timezone"):
        setattr(row, field, getattr(body, field))
    row.updated_by = _actor(user)
    await session.commit()
    await session.refresh(row)
    return {
        "user_id": row.user_id,
        "business_calendar_id": str(row.business_calendar_id) if row.business_calendar_id else None,
        "work_days": row.work_days, "work_start_hour": row.work_start_hour,
        "work_end_hour": row.work_end_hour, "timezone": row.timezone,
    }


# ── team events ───────────────────────────────────────────────────────────

class EventIn(BaseModel):
    title:             str
    description:       str = ""
    starts_at:         datetime
    ends_at:           datetime
    timezone:          str = "UTC"
    attendee_user_ids: list[str] = []
    #: Guests with no account here. Kept apart from `attendee_user_ids` on
    #: purpose: an availability lookup cannot find a working pattern for an
    #: email address.
    external_emails:   list[str] = []
    case_id:           uuid.UUID | None = None
    #: A link somebody pasted. To have this platform create the room instead,
    #: POST to /events/{id}/meeting.
    meeting_url:       str | None = None
    #: {"freq": "daily|weekly|monthly", "interval": 1, "until": "<iso>"}
    recurrence:        dict | None = None
    visibility:        str = "team"
    #: A meeting in the past is almost always a mistyped year or a wrong AM/PM,
    #: and it lands where nobody looks. Logging one that already happened is a
    #: real thing to want, so it is allowed — but said out loud.
    allow_past:        bool = False
    #: Ask HxMeet for a room as part of scheduling, so the invitation that goes
    #: out a moment later has a link in it.
    create_room:       bool = False
    #: Email the external guests. On by default **at creation**: pressing
    #: Schedule with guests listed is the decision to invite them, and making
    #: somebody find the meeting again afterwards to press a second button is
    #: the kind of gap that leaves guests uninvited. An edit never sends —
    #: see `update_event`.
    send_invites:      bool = True


class EventPatchIn(BaseModel):
    title:             str | None = None
    description:       str | None = None
    starts_at:         datetime | None = None
    ends_at:           datetime | None = None
    timezone:          str | None = None
    attendee_user_ids: list[str] | None = None
    external_emails:   list[str] | None = None
    meeting_url:       str | None = None
    recurrence:        dict | None = None
    visibility:        str | None = None


class SlotsIn(BaseModel):
    user_ids: list[str]
    start:    datetime
    end:      datetime
    minutes:  int = 30
    limit:    int = 5


def _event_json(ev: CalendarEventModel, *, room_status: str | None = None) -> dict:
    from case_service.core.timestamps import iso_utc

    return {
        # `active`, `ended`, `cancelled`, or None when there is no room. The
        # screen needs it to say "Join" rather than promising a room that
        # LiveKit closed when the last person left.
        "room_status": room_status,
        "id": str(ev.id), "title": ev.title, "description": ev.description,
        "starts_at": iso_utc(ev.starts_at), "ends_at": iso_utc(ev.ends_at),
        "timezone": ev.timezone, "organiser_user_id": ev.organiser_user_id,
        "attendee_user_ids": ev.attendee_user_ids or [],
        "external_emails": ev.external_emails or [],
        "case_id": str(ev.case_id) if ev.case_id else None,
        "meet_session_id": str(ev.meet_session_id) if ev.meet_session_id else None,
        "meeting_url": ev.meeting_url,
        "recurrence": ev.recurrence,
        "excluded_dates": ev.excluded_dates or [],
        "visibility": ev.visibility, "status": ev.status,
        "created_by": ev.created_by, "created_at": iso_utc(ev.created_at),
        "updated_by": ev.updated_by,
    }


def _may_see_event(ev: CalendarEventModel, user: AuthenticatedUser) -> bool:
    """`private` means the organiser and the attendees, and nobody else.

    An admin is not exempt: the title of a one-to-one is the whole payload, and
    "the admin can read every private meeting" is not a property worth having.
    """
    if ev.visibility != "private":
        return True
    mine = set(_identifiers(user))
    return bool(mine & ({ev.organiser_user_id} | set(ev.attendee_user_ids or [])))


@router.get("/events")
async def list_events(
    start: datetime | None = None,
    end: datetime | None = None,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(hxguard.guard("calendar.events.read")),
):
    """Team events in a window. Defaults to the next 30 days."""
    # Default window looks **backwards** as well as forwards. Starting at "now"
    # meant a meeting that finished an hour ago vanished from the screen — and
    # with it the only way to edit, cancel or invite anybody to it. A calendar
    # that hides yesterday is not a calendar.
    now = datetime.now(timezone.utc)
    start = start or (now - timedelta(days=7))
    end = end or (now + timedelta(days=30))
    _check_window(start, end)

    names = await service._tenant_candidates(session, _tenant(user))
    # A series that began before the window still has occurrences inside it, so
    # the `ends_at` filter cannot be the only one — see `service.occurrences`.
    from sqlalchemy import or_ as _or

    rows = (await session.execute(
        select(CalendarEventModel)
        .where(CalendarEventModel.tenant_id.in_(names),
               CalendarEventModel.status == "scheduled",
               CalendarEventModel.starts_at < end,
               _or(CalendarEventModel.ends_at > start,
                   CalendarEventModel.recurrence.is_not(None)))
        .order_by(CalendarEventModel.starts_at)
    )).scalars().all()

    from velaris_shared.db.models import CaseSessionModel

    # One query for every room in the window rather than one per row: the month
    # view asks for a hundred meetings at a time.
    session_ids = [ev.meet_session_id for ev in rows if ev.meet_session_id]
    room_status: dict = {}
    if session_ids:
        room_status = {
            r.id: r.status for r in (await session.execute(
                select(CaseSessionModel).where(CaseSessionModel.id.in_(session_ids))
            )).scalars().all()
        }

    out = []
    for ev in rows:
        if not _may_see_event(ev, user):
            continue
        base = _event_json(ev, room_status=room_status.get(ev.meet_session_id))
        for occ_start, occ_end in service.occurrences(ev, start, end):
            entry = dict(base)
            entry["starts_at"] = occ_start.isoformat()
            entry["ends_at"] = occ_end.isoformat()
            # The series id, plus which occurrence this is. A caller cancelling
            # one date needs both, and a caller cancelling the series needs the
            # id alone.
            entry["occurrence_date"] = occ_start.date().isoformat()
            entry["is_recurring"] = bool(ev.recurrence)
            out.append(entry)
    out.sort(key=lambda e: e["starts_at"])
    return out


async def _start_room(session: AsyncSession, ev: CalendarEventModel,
                      user: AuthenticatedUser):
    """Ask HxMeet for a room for this event, whatever driver the tenant runs.

    Shared by scheduling (which asks for one as part of the same act) and the
    explicit *Add room* — one copy, because a second would be the one that
    forgets the driver check.
    """
    from case_service.meet import livekit, service as meet

    tenant = _tenant(user)
    driver = await meet.resolve_driver(session, tenant)
    try:
        if driver == "embedded":
            if not livekit.configured():
                raise HTTPException(501, "The embedded meeting driver is not configured.")
            return await meet.start_embedded_session(
                session, calendar_event_id=ev.id, tenant_id=tenant,
                started_by=_actor(user), title=ev.title)
        if driver == "off_platform":
            return await meet.start_session(
                session, calendar_event_id=ev.id, tenant_id=tenant,
                started_by=_actor(user), title=ev.title)
        raise HTTPException(501, f"Meeting driver '{driver}' is not available yet.")
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(502, f"Meeting provider error: {exc}") from exc


@router.post("/events", status_code=201)
async def create_event(
    body: EventIn,
    request: Request,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(hxguard.guard("calendar.events.write")),
):
    """Schedule a team event.

    Nothing here starts video: `meet_session_id` **references** an HxMeet room
    rather than creating one. Calendar owns when and who; HxMeet owns the room.
    """
    if body.ends_at <= body.starts_at:
        raise HTTPException(400, "An event must end after it starts.")
    grace = datetime.now(timezone.utc) - timedelta(minutes=5)
    if service._aware(body.starts_at) < grace and not body.allow_past:
        raise HTTPException(
            400, "That start time has already passed. Check the date, or set "
                 "`allow_past` to record a meeting that already happened.")
    if body.visibility not in ("team", "private"):
        raise HTTPException(400, "visibility must be 'team' or 'private'.")
    if len(body.attendee_user_ids) > 200:
        raise HTTPException(400, "An event is capped at 200 attendees. A broadcast "
                                 "is an announcement, not a meeting.")
    _check_len(body.title.strip(), 255, "The title")
    _check_len(body.description, MAX_NOTE_CHARS, "The description")
    _check_timezone(body.timezone)
    for attendee in body.attendee_user_ids:
        _check_len(str(attendee), 255, "An attendee id")
        # An attendee who is not in the directory can never be looked up for
        # availability, so the meeting would silently ignore them.
        await _known_user(session, str(attendee), user, "attendee")
    _check_guests(body.external_emails)
    _check_meeting_url(body.meeting_url)
    await _check_case_anchor(session, body.case_id, user)
    try:
        recurrence = service.validate_recurrence(body.recurrence)
    except service.CalendarError as exc:
        raise HTTPException(exc.status, str(exc)) from exc

    ev = CalendarEventModel(
        tenant_id=_tenant(user), title=body.title.strip(),
        description=body.description, starts_at=body.starts_at, ends_at=body.ends_at,
        timezone=body.timezone, organiser_user_id=await _me(session, user),
        attendee_user_ids=[str(a) for a in body.attendee_user_ids],
        external_emails=[e.strip() for e in body.external_emails],
        case_id=body.case_id,
        # A room is asked for through /events/{id}/meeting, which knows how to
        # create one and whose it is. Taking a session id from the request body
        # would let an event point at another tenant's room.
        meet_session_id=None,
        meeting_url=body.meeting_url, recurrence=recurrence, excluded_dates=[],
        visibility=body.visibility, status="scheduled", created_by=_actor(user),
    )
    if not ev.title:
        raise HTTPException(400, "An event needs a title.")
    session.add(ev)
    await session.commit()
    await session.refresh(ev)

    out = _event_json(ev)

    # Scheduling is one act. A room, then the invitations that carry its link —
    # in that order, because an invitation sent first would name no way to join.
    # Neither can fail the meeting: it exists, and both are repeatable from the
    # calendar.
    if body.create_room and not ev.meeting_url:
        try:
            room = await _start_room(session, ev, user)
            ev.meet_session_id = room.id
            await session.commit()
            await session.refresh(ev)
            out = _event_json(ev, room_status=room.status)
            out["join_url"] = room.join_url
        except HTTPException as exc:
            out["room_error"] = exc.detail

    if body.send_invites and ev.external_emails:
        base_url = get_settings().public_base_url
        try:
            out["invitations"] = await send_invitations(
                session, ev, tenant_id=_tenant(user), sender=_actor(user),
                base_url=base_url)
            await session.commit()
        except service.CalendarError as exc:
            out["invitations"] = {"sent": [], "failed": [{"email": "*", "error": str(exc)}]}

    return out


async def _event_or_404(session: AsyncSession, event_id: uuid.UUID,
                        user: AuthenticatedUser) -> CalendarEventModel:
    from case_service.tenancy.identity import same_tenant

    ev = await session.get(CalendarEventModel, event_id)
    if ev is None or not await same_tenant(session, ev.tenant_id, _tenant(user)):
        raise HTTPException(404, "Event not found")
    if not _may_see_event(ev, user):
        # A private meeting somebody else holds is not "forbidden" to this
        # caller — it is none of their business that it exists.
        raise HTTPException(404, "Event not found")
    return ev


def _organiser_or_admin(ev: CalendarEventModel, user: AuthenticatedUser) -> None:
    if not (_is_admin(user) or ev.organiser_user_id in set(_identifiers(user))):
        raise HTTPException(403, "Only the organiser, or an admin, may change this.")


@router.patch("/events/{event_id}")
async def update_event(
    event_id: uuid.UUID,
    body: EventPatchIn,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(hxguard.guard("calendar.events.write")),
):
    """Change a meeting — move it, rename it, add somebody, add the link.

    Editing the **series** moves every future occurrence; there is no per-date
    edit, deliberately. Splitting a series into edited and unedited halves needs
    exception rows, and the honest small version is: move the series, or cancel
    one date and book a new meeting.
    """
    ev = await _event_or_404(session, event_id, user)
    _organiser_or_admin(ev, user)
    if ev.status == "cancelled":
        raise HTTPException(409, "A cancelled event cannot be edited. Schedule a new one.")

    if body.title is not None:
        _check_len(body.title.strip(), 255, "The title")
        if not body.title.strip():
            raise HTTPException(400, "An event needs a title.")
    _check_len(body.description, MAX_NOTE_CHARS, "The description")
    if body.timezone is not None:
        _check_timezone(body.timezone)
    if body.visibility is not None and body.visibility not in ("team", "private"):
        raise HTTPException(400, "visibility must be 'team' or 'private'.")
    if body.attendee_user_ids is not None:
        if len(body.attendee_user_ids) > 200:
            raise HTTPException(400, "An event is capped at 200 attendees.")
        for attendee in body.attendee_user_ids:
            _check_len(str(attendee), 255, "An attendee id")
            await _known_user(session, str(attendee), user, "attendee")
    if body.external_emails is not None:
        _check_guests(body.external_emails)
    if body.meeting_url is not None:
        _check_meeting_url(body.meeting_url)

    starts = body.starts_at or ev.starts_at
    ends = body.ends_at or ev.ends_at
    if service._aware(ends) <= service._aware(starts):
        raise HTTPException(400, "An event must end after it starts.")

    if body.recurrence is not None:
        try:
            ev.recurrence = service.validate_recurrence(body.recurrence or None)
        except service.CalendarError as exc:
            raise HTTPException(exc.status, str(exc)) from exc

    for field in ("title", "description", "starts_at", "ends_at", "timezone",
                  "attendee_user_ids", "external_emails", "meeting_url", "visibility"):
        value = getattr(body, field)
        if value is not None:
            setattr(ev, field, value.strip() if field == "title" else value)
    ev.updated_by = _actor(user)
    await session.commit()
    await session.refresh(ev)
    return _event_json(ev)


@router.post("/events/{event_id}/skip")
async def skip_occurrence(
    event_id: uuid.UUID,
    on: str,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(hxguard.guard("calendar.events.write")),
):
    """Cancel **one** occurrence of a series — "we skip next Monday".

    Without this, skipping a week means deleting the series and rebuilding it,
    which loses what was agreed. `on` is the occurrence's date, `YYYY-MM-DD`.
    """
    ev = await _event_or_404(session, event_id, user)
    _organiser_or_admin(ev, user)
    if not ev.recurrence:
        raise HTTPException(400, "This is a one-off. Cancel it rather than skipping a date.")
    try:
        datetime.strptime(on, "%Y-%m-%d")
    except ValueError:
        raise HTTPException(400, "`on` must be a date, YYYY-MM-DD.")

    excluded = list(ev.excluded_dates or [])
    if len(excluded) >= MAX_EXCLUSIONS:
        raise HTTPException(
            400, f"This series already skips {MAX_EXCLUSIONS} dates. End it and "
                 f"start a new one.")
    if on not in excluded:
        excluded.append(on)
        ev.excluded_dates = sorted(excluded)
        ev.updated_by = _actor(user)
        await session.commit()
        await session.refresh(ev)
    return _event_json(ev)


@router.post("/events/{event_id}/cancel")
async def cancel_event(
    event_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(hxguard.guard("calendar.events.write")),
):
    """Cancel it. Kept rather than deleted: attendees may need to see it was
    called off rather than find it silently gone."""
    ev = await _event_or_404(session, event_id, user)
    _organiser_or_admin(ev, user)
    ev.status = "cancelled"
    await session.commit()
    await session.refresh(ev)
    return _event_json(ev)


@router.post("/events/{event_id}/meeting", status_code=201)
async def create_meeting_room(
    event_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(hxguard.guard("calendar.events.write")),
):
    """Give this event a room, through **HxMeet**.

    HxMeet owns rooms; this asks it for one and remembers the answer. Until
    migration 172 it could only hold a meeting attached to a case, so a team
    stand-up could never have a link — `case_sessions` now takes a calendar
    event as its anchor instead, exactly one of the two.

    Nothing here talks to a video provider directly: the driver, the connector,
    the recording policy and the token minting all stay in HxMeet, and this
    endpoint would be the wrong place to have a second copy of any of them.
    """
    from case_service.meet import livekit, service as meet

    from velaris_shared.db.models import CaseSessionModel

    ev = await _event_or_404(session, event_id, user)
    _organiser_or_admin(ev, user)
    if ev.status == "cancelled":
        raise HTTPException(409, "This event is cancelled.")
    if ev.meeting_url:
        raise HTTPException(
            409, "This event already carries a link. Clear `meeting_url` first if "
                 "you want a room created here instead.")

    # An existing room is reused **while it is live**, and replaced once it is
    # not. LiveKit ends a room when the last person leaves — right for a case
    # session, which is a thing that happened, and wrong for a meeting, which is
    # a thing that recurs. Rejoining a stand-up after everybody stepped out must
    # put you back in a room, exactly as it does everywhere else; the alternative
    # was the 409 "this event already has a room" for a room nobody could enter.
    if ev.meet_session_id:
        existing = await session.get(CaseSessionModel, ev.meet_session_id)
        if existing is not None and existing.status == "active":
            return {**_event_json(ev), "join_url": existing.join_url,
                    "driver": existing.driver, "reused": True}

    row = await _start_room(session, ev, user)

    ev.meet_session_id = row.id
    ev.updated_by = _actor(user)
    await session.commit()
    await session.refresh(ev)
    return {**_event_json(ev), "join_url": row.join_url, "driver": row.driver,
            "reused": False}


@router.post("/events/{event_id}/invite", dependencies=[Depends(_invite_rl)])
async def invite_guests(
    event_id: uuid.UUID,
    request: Request,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(hxguard.guard("calendar.events.write")),
):
    """Email the invitation to this event's external guests.

    Sent explicitly rather than automatically on save: an invitation is a
    message this platform sends in somebody's name, and a half-finished meeting
    that mails twenty people because the organiser was still typing is a worse
    failure than one that needs a second click.

    The message carries an **`.ics` attachment**, which is what makes it an
    invitation rather than a paragraph — a guest's own calendar reads it, and no
    account here is needed.
    """
    ev = await _event_or_404(session, event_id, user)
    _organiser_or_admin(ev, user)
    if ev.status == "cancelled":
        raise HTTPException(409, "This event is cancelled.")
    if not ev.external_emails:
        raise HTTPException(400, "This event has no external guests to invite.")

    # `/meet/join` is a **Studio** page, not an API route, so the link has to
    # name where Studio is — which is not this request's origin, and on this
    # deployment is not even the same port. Guessing produces a link that
    # resolves to nothing, or to a host an external guest cannot reach; saying so
    # is the better failure.
    base_url = get_settings().public_base_url
    try:
        result = await send_invitations(session, ev, tenant_id=_tenant(user),
                                        sender=_actor(user), base_url=base_url)
    except service.CalendarError as exc:
        raise HTTPException(exc.status, str(exc)) from exc
    return result


@router.get("/events/{event_id}/ics")
async def event_ics(
    event_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(hxguard.guard("calendar.events.read")),
):
    """The event as an `.ics` file — for adding it to a calendar by hand."""
    from fastapi.responses import Response

    from case_service.calendar.invites import build_ics

    ev = await _event_or_404(session, event_id, user)
    return Response(
        content=build_ics(ev, method="PUBLISH"),
        media_type="text/calendar; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{ev.id}.ics"'},
    )


@router.post("/slots")
async def propose_slots(
    body: SlotsIn,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(hxguard.guard("calendar.events.read")),
):
    """When is everybody named free?

    The strict reading of free — inside each person's working pattern, not on
    leave, not already in a meeting — because *when shall we meet* is exactly the
    question working hours were meant to answer. (Assignment uses the opposite
    rule: see `calendar.service.absent_at`.)

    An empty list means no common slot. That is an answer, not an error.
    """
    if not body.user_ids:
        raise HTTPException(400, "Name at least one user_id.")
    if len(body.user_ids) > MAX_SUBJECTS:
        raise HTTPException(400, f"Ask about {MAX_SUBJECTS} people or fewer at a time.")
    if body.end <= body.start:
        raise HTTPException(400, "`end` must be after `start`.")
    if (body.end - body.start).days > 62:
        raise HTTPException(400, "Search a window of two months or less.")

    slots = await service.propose_slots(
        session, [str(u) for u in body.user_ids], body.start, body.end,
        tenant_id=_tenant(user), minutes=body.minutes, limit=body.limit,
    )
    return {"slots": slots, "searched": {"from": body.start.isoformat(),
                                         "to": body.end.isoformat(),
                                         "minutes": body.minutes}}
