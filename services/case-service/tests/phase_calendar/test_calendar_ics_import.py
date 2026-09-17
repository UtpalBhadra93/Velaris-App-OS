"""Calendar — importing an .ics export (`source='import'` gets its producer).

`invites.build_ics` has written iCalendar since phase 5; nothing read it. The
round trip is the first test here, because a parser that cannot read what this
platform itself emits is not a parser.

The rest are the refusals, which is where the value is: an import must not
approve leave, must not adopt a row somebody typed, must not turn a `LOCATION`
into a clickable `javascript:` link, and must not guess at a timezone it does
not know.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from case_service.calendar.ics_import import (
    IcsError, ParseResult, apply_import, parse_ics,
)


def _wrap(*vevents: str) -> str:
    body = "\r\n".join(vevents)
    return ("BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//Test//EN\r\n"
            f"{body}\r\nEND:VCALENDAR\r\n")


def _event(**over) -> str:
    props = {
        "UID": "abc-123@example.test",
        "DTSTAMP": "20260901T090000Z",
        "DTSTART": "20260902T090000Z",
        "DTEND": "20260902T100000Z",
        "SUMMARY": "Stand-up",
    }
    props.update(over)
    lines = ["BEGIN:VEVENT"]
    lines += [f"{k}:{v}" for k, v in props.items() if v is not None]
    lines.append("END:VEVENT")
    return "\r\n".join(lines)


# ── the round trip ────────────────────────────────────────────────────────

class _Ev:
    """The subset of CalendarEventModel `build_ics` reads."""

    def __init__(self, **kw):
        self.__dict__.update(kw)


def test_reads_what_build_ics_writes():
    from case_service.calendar.invites import build_ics

    event = _Ev(
        id="11111111-2222-3333-4444-555555555555",
        title="Quarterly review; with a comma, and a \\ backslash",
        description="Line one\nLine two",
        starts_at=datetime(2026, 9, 2, 9, 0, tzinfo=timezone.utc),
        ends_at=datetime(2026, 9, 2, 10, 30, tzinfo=timezone.utc),
        status="scheduled",
        meeting_url="https://meet.example.test/room",
        external_emails=["guest@example.test"],
        recurrence={"freq": "weekly", "interval": 2, "count": 5},
        excluded_dates=[],
    )
    parsed = parse_ics(build_ics(event, organiser_email="me@example.test"))

    assert parsed.skipped == []
    assert len(parsed.events) == 1
    got = parsed.events[0]
    # The escaping survives the trip — a comma that ended the value early would
    # silently truncate the title.
    assert got.title == "Quarterly review; with a comma, and a \\ backslash"
    assert got.description == "Line one\nLine two"
    assert got.starts_at == event.starts_at
    assert got.ends_at == event.ends_at
    assert got.location == "https://meet.example.test/room"
    assert got.recurrence == {"freq": "weekly", "interval": 2, "count": 5}


# ── lexing ────────────────────────────────────────────────────────────────

def test_unfolds_continuation_lines():
    long_title = "A" * 200
    folded = "\r\n".join(["SUMMARY:" + long_title[:60],
                          " " + long_title[60:130],
                          " " + long_title[130:]])
    parsed = parse_ics(_wrap(_event(SUMMARY=None) .replace(
        "END:VEVENT", folded + "\r\nEND:VEVENT")))
    assert parsed.events[0].title == long_title


def test_colon_inside_a_quoted_param_is_not_the_separator():
    line = 'DTSTART;TZID="Europe/London":20260902T090000'
    parsed = parse_ics(_wrap(_event(DTSTART=None).replace(
        "END:VEVENT", line + "\r\nEND:VEVENT")))
    assert parsed.events[0].starts_at == datetime(2026, 9, 2, 8, 0, tzinfo=timezone.utc)


def test_valarm_duration_is_not_read_as_the_events():
    """A VALARM carries its own DURATION. Reading it as the event's would make
    every reminder-bearing meeting the length of its reminder."""
    with_alarm = _event(DTEND=None).replace(
        "END:VEVENT",
        "BEGIN:VALARM\r\nTRIGGER:-PT15M\r\nDURATION:PT5M\r\nACTION:DISPLAY\r\n"
        "END:VALARM\r\nEND:VEVENT")
    parsed = parse_ics(_wrap(with_alarm))
    got = parsed.events[0]
    assert (got.ends_at - got.starts_at).total_seconds() == 3600
    assert "no end time; assumed 1 hour" in got.notes


# ── dates and zones ───────────────────────────────────────────────────────

def test_all_day_event():
    parsed = parse_ics(_wrap(_event(
        DTSTART=None, DTEND=None).replace(
            "END:VEVENT",
            "DTSTART;VALUE=DATE:20260902\r\nDTEND;VALUE=DATE:20260903\r\nEND:VEVENT")))
    got = parsed.events[0]
    assert got.all_day is True
    assert got.starts_at == datetime(2026, 9, 2, tzinfo=timezone.utc)


def test_duration_instead_of_dtend():
    parsed = parse_ics(_wrap(_event(DTEND=None, DURATION="PT1H30M")))
    got = parsed.events[0]
    assert (got.ends_at - got.starts_at).total_seconds() == 5400


def test_unknown_timezone_is_skipped_not_assumed():
    """Defaulting to UTC would move the meeting and leave the row looking
    perfectly ordinary — the worst kind of wrong."""
    parsed = parse_ics(_wrap(_event(DTSTART=None).replace(
        "END:VEVENT", "DTSTART;TZID=Mars/Olympus:20260902T090000\r\nEND:VEVENT")))
    assert parsed.events == []
    assert "unknown timezone" in parsed.skipped[0][1]


def test_windows_zone_name_is_understood():
    parsed = parse_ics(_wrap(_event(DTSTART=None).replace(
        "END:VEVENT",
        "DTSTART;TZID=GMT Standard Time:20260902T090000\r\nEND:VEVENT")))
    assert parsed.events[0].starts_at == datetime(2026, 9, 2, 8, 0, tzinfo=timezone.utc)


def test_floating_time_is_reported_as_an_assumption():
    parsed = parse_ics(_wrap(_event(DTSTART="20260902T090000",
                                    DTEND="20260902T100000")))
    assert "floating time read as UTC" in parsed.events[0].notes


# ── recurrence ────────────────────────────────────────────────────────────

def test_unsupported_rrule_skips_the_event_rather_than_flattening_it():
    """`recurrence` cannot express BYDAY, so importing this without it would
    describe a different series from the one in the file."""
    parsed = parse_ics(_wrap(_event(RRULE="FREQ=WEEKLY;BYDAY=MO,WE;INTERVAL=1")))
    assert parsed.events == []
    assert "BYDAY" in parsed.skipped[0][1]
    # ...and the file still parses. One bad rule is not a bad calendar.
    both = parse_ics(_wrap(_event(RRULE="FREQ=YEARLY"),
                           _event(UID="ok@example.test")))
    assert len(both.events) == 1 and len(both.skipped) == 1


def test_supported_rrule_maps_to_our_vocabulary():
    parsed = parse_ics(_wrap(_event(RRULE="FREQ=DAILY;INTERVAL=3;UNTIL=20261001T000000Z")))
    rule = parsed.events[0].recurrence
    assert rule["freq"] == "daily" and rule["interval"] == 3
    assert rule["until"].startswith("2026-10-01")


# ── whole-file refusals ───────────────────────────────────────────────────

def test_not_a_calendar_is_refused():
    with pytest.raises(IcsError):
        parse_ics("just some text")


def test_oversized_file_is_refused():
    with pytest.raises(IcsError):
        parse_ics(_wrap(_event(DESCRIPTION="x" * 1_100_000)))


def test_unterminated_vevent_is_refused():
    with pytest.raises(IcsError):
        parse_ics("BEGIN:VCALENDAR\r\nBEGIN:VEVENT\r\nUID:a\r\nEND:VCALENDAR\r\n")


def test_free_time_is_not_busy_time():
    parsed = parse_ics(_wrap(_event(TRANSP="TRANSPARENT")))
    assert parsed.events == []
    assert "marked free" in parsed.skipped[0][1]


# ── writing ───────────────────────────────────────────────────────────────

import uuid  # noqa: E402

from sqlalchemy import select  # noqa: E402
from sqlalchemy.ext.asyncio import AsyncSession  # noqa: E402

from velaris_shared.db.models import CalendarEventModel, UserAbsenceModel  # noqa: E402

from tests.conftest import client, session  # type: ignore[attr-defined]  # noqa: E402,F401


async def _events(session: AsyncSession, user_id="alice") -> list[CalendarEventModel]:
    rows = (await session.execute(
        select(CalendarEventModel).where(
            CalendarEventModel.organiser_user_id == user_id))).scalars().all()
    return list(rows)


@pytest.mark.asyncio
async def test_dry_run_writes_nothing(session: AsyncSession):
    parsed = parse_ics(_wrap(_event()))
    outcome = await apply_import(session, parsed, tenant_id="default",
                                 user_id="alice", actor="alice", dry_run=True)
    assert outcome["created"] == 1 and outcome["dry_run"] is True
    assert await _events(session) == []


@pytest.mark.asyncio
async def test_second_import_updates_rather_than_duplicates(session: AsyncSession):
    first = parse_ics(_wrap(_event()))
    await apply_import(session, first, tenant_id="default", user_id="alice",
                       actor="alice", dry_run=False)
    await session.commit()

    again = parse_ics(_wrap(_event(SUMMARY="Stand-up (moved)")))
    outcome = await apply_import(session, again, tenant_id="default",
                                 user_id="alice", actor="alice", dry_run=False)
    await session.commit()

    rows = await _events(session)
    assert outcome["updated"] == 1 and outcome["created"] == 0
    assert len(rows) == 1 and rows[0].title == "Stand-up (moved)"


@pytest.mark.asyncio
async def test_an_import_never_adopts_a_row_somebody_typed(session: AsyncSession):
    """A manual row wearing the same UID must be left alone — otherwise an
    upload can rewrite a meeting by claiming its identifier."""
    typed = CalendarEventModel(
        id=uuid.uuid4(), tenant_id="default", title="Mine", description="",
        starts_at=datetime(2026, 9, 2, 9, tzinfo=timezone.utc),
        ends_at=datetime(2026, 9, 2, 10, tzinfo=timezone.utc), timezone="UTC",
        organiser_user_id="alice", attendee_user_ids=["alice"],
        visibility="team", status="scheduled", created_by="alice",
        source="manual", external_uid="abc-123@example.test",
    )
    session.add(typed)
    await session.commit()

    outcome = await apply_import(session, parse_ics(_wrap(_event())),
                                 tenant_id="default", user_id="alice",
                                 actor="alice", dry_run=False)
    await session.commit()

    rows = sorted(await _events(session), key=lambda r: r.source)
    assert outcome["created"] == 1
    assert [r.title for r in rows] == ["Stand-up", "Mine"]
    assert [r.source for r in rows] == ["import", "manual"]


@pytest.mark.asyncio
async def test_out_of_office_is_requested_never_approved(session: AsyncSession):
    """Only an approved absence reroutes work. A file must not be able to empty
    somebody's queue."""
    ooo = _event(**{"X-MICROSOFT-CDO-BUSYSTATUS": "OOF", "SUMMARY": "Annual leave"})
    outcome = await apply_import(session, parse_ics(_wrap(ooo)),
                                 tenant_id="default", user_id="alice",
                                 actor="alice", dry_run=False)
    await session.commit()

    rows = (await session.execute(select(UserAbsenceModel))).scalars().all()
    assert outcome["absences_requested"] == 1
    assert len(rows) == 1
    assert rows[0].status == "requested" and rows[0].source == "import"
    assert rows[0].approved_by is None


@pytest.mark.asyncio
async def test_reimporting_leave_does_not_stack_absences(session: AsyncSession):
    ooo = _wrap(_event(**{"X-MICROSOFT-CDO-BUSYSTATUS": "OOF"}))
    for _ in range(2):
        await apply_import(session, parse_ics(ooo), tenant_id="default",
                           user_id="alice", actor="alice", dry_run=False)
        await session.commit()
    rows = (await session.execute(select(UserAbsenceModel))).scalars().all()
    assert len(rows) == 1


@pytest.mark.asyncio
async def test_a_dangerous_location_never_becomes_a_link(session: AsyncSession):
    """Studio renders `meeting_url` as an anchor. A LOCATION is attacker-supplied
    text once a file can be uploaded, so the scheme is an allowlist — and the
    value is kept as description text rather than thrown away."""
    evil = _event(LOCATION="javascript:alert(document.cookie)")
    await apply_import(session, parse_ics(_wrap(evil)), tenant_id="default",
                       user_id="alice", actor="alice", dry_run=False)
    await session.commit()

    row = (await _events(session))[0]
    assert row.meeting_url is None
    assert "javascript:alert" in row.description


@pytest.mark.asyncio
async def test_imported_events_are_private_and_have_no_guest_list(session: AsyncSession):
    """A team-visible default would publish every meeting title the person holds,
    and a filled guest list is what `send_invitations` mails."""
    await apply_import(session, parse_ics(_wrap(_event(
        ORGANIZER="mailto:someone@example.test"))),
        tenant_id="default", user_id="alice", actor="alice", dry_run=False)
    await session.commit()

    row = (await _events(session))[0]
    assert row.visibility == "private"
    assert row.external_emails == []
    assert row.attendee_user_ids == ["alice"]


# ── the endpoint ──────────────────────────────────────────────────────────

from httpx import AsyncClient  # noqa: E402


async def _directory(session: AsyncSession, user_id: str, tenant: str = "default"):
    from velaris_shared.db.models import UserDirectoryModel

    session.add(UserDirectoryModel(
        id=uuid.uuid4(), user_id=user_id, display_name=user_id,
        email=f"{user_id}@example.com", tenant_id=tenant, is_active=True,
    ))
    await session.commit()


def _headers_for(roles: list[str], username: str) -> dict:
    from velaris_shared.auth.jwt_handler import create_dev_token
    from velaris_shared.config import get_settings

    st = get_settings()
    token = create_dev_token(
        user_id=str(uuid.uuid4()), username=username, roles=roles,
        secret=st.auth_secret, private_key=st.auth_rsa_private_key or "",
    )
    return {"Authorization": f"Bearer {token}"}


@pytest.mark.asyncio
async def test_endpoint_dry_runs_by_default(client: AsyncClient, session: AsyncSession):
    await _directory(session, "test-admin")
    r = await client.post("/api/v1/calendar/import", json={"ics": _wrap(_event())})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["dry_run"] is True and body["created"] == 1
    assert await _events(session, "test-admin") == []


@pytest.mark.asyncio
async def test_endpoint_writes_when_asked(client: AsyncClient, session: AsyncSession):
    await _directory(session, "test-admin")
    r = await client.post("/api/v1/calendar/import",
                          json={"ics": _wrap(_event()), "dry_run": False})
    assert r.status_code == 200, r.text
    assert r.json()["created"] == 1
    assert len(await _events(session, "test-admin")) == 1


@pytest.mark.asyncio
async def test_a_file_that_cannot_be_read_is_refused_whole(
        client: AsyncClient, session: AsyncSession):
    """Part-applying is worse than refusing: the operator cannot tell which half
    of their calendar is in."""
    await _directory(session, "test-admin")
    r = await client.post("/api/v1/calendar/import",
                          json={"ics": "not a calendar", "dry_run": False})
    assert r.status_code == 400
    assert "could not be read" in r.json()["detail"]


@pytest.mark.asyncio
async def test_importing_into_a_calendar_you_do_not_manage_is_refused(
        client: AsyncClient, session: AsyncSession):
    """A case worker imports their own calendar. Somebody else's is a quiet way
    to fill a colleague's diary — and to request leave in their name."""
    await _directory(session, "worker")
    await _directory(session, "someone-else")
    r = await client.post(
        "/api/v1/calendar/import",
        json={"ics": _wrap(_event()), "user_id": "someone-else", "dry_run": False},
        headers=_headers_for(["case_worker"], "worker"))
    assert r.status_code == 403, r.text
    assert await _events(session, "someone-else") == []
