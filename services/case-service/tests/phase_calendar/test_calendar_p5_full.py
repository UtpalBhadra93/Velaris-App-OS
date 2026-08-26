"""Calendar phase 5 — the parts that make it a calendar a team can run on.

Editing, repeating, joining and guests. Each of these was a gap somebody would
hit on the first day: a stand-up that had to be created fifty times, a meeting
that could not be moved, one with no link, and no way to invite anybody without
an account here.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from case_service.calendar import service
from case_service.calendar.invites import build_ics
from velaris_shared.db.models import (
    CalendarEventModel, UserAbsenceModel, UserDirectoryModel,
)

from tests.conftest import client, session  # type: ignore[attr-defined]

WED = datetime(2026, 9, 2, tzinfo=timezone.utc)


def _headers_for(roles: list[str], user_id: str | None = None,
                 username: str | None = None) -> dict:
    from velaris_shared.auth.jwt_handler import create_dev_token
    from velaris_shared.config import get_settings

    s = get_settings()
    token = create_dev_token(
        user_id=user_id or str(uuid.uuid4()),
        username=username or f"test-{roles[0] if roles else 'none'}",
        roles=roles, secret=s.auth_secret, private_key=s.auth_rsa_private_key or "",
    )
    return {"Authorization": f"Bearer {token}"}


async def _directory(session: AsyncSession, user_id: str, tenant: str = "default"):
    session.add(UserDirectoryModel(
        id=uuid.uuid4(), user_id=user_id, display_name=user_id,
        email=f"{user_id}@example.com", tenant_id=tenant, is_active=True,
    ))
    await session.commit()


async def _make_event(client: AsyncClient, **over):
    body = {
        "title": "Stand-up",
        "starts_at": WED.replace(hour=10).isoformat(),
        "ends_at": WED.replace(hour=10, minute=30).isoformat(),
    }
    body.update(over)
    r = await client.post("/api/v1/calendar/events", json=body)
    assert r.status_code == 201, r.text
    return r.json()


# ── recurrence ───────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_a_weekly_meeting_is_one_row_expanded_on_read(client: AsyncClient):
    """Storing occurrences instead would mean 250 rows a year, an endless series
    with no bottom, and a rewrite of all of them to move the meeting."""
    ev = await _make_event(client, recurrence={"freq": "weekly", "interval": 1})
    assert ev["recurrence"]["freq"] == "weekly"

    rows = (await client.get("/api/v1/calendar/events", params={
        "start": WED.isoformat(), "end": (WED + timedelta(days=28)).isoformat()})).json()
    mine = [r for r in rows if r["id"] == ev["id"]]
    assert len(mine) == 4, [r["starts_at"] for r in mine]
    assert all(r["is_recurring"] for r in mine)
    assert {r["occurrence_date"] for r in mine} == {
        "2026-09-02", "2026-09-09", "2026-09-16", "2026-09-23"}


@pytest.mark.asyncio
async def test_a_series_that_began_before_the_window_still_shows(client: AsyncClient):
    """Filtering on the row's own `starts_at` alone would hide every stand-up
    set up last year — the first occurrence is not the only one."""
    await _make_event(client, title="Old series",
                      starts_at=(WED - timedelta(days=90)).replace(hour=10).isoformat(),
                      ends_at=(WED - timedelta(days=90)).replace(hour=11).isoformat(),
                      recurrence={"freq": "weekly", "interval": 1},
                      allow_past=True)   # it started in the spring, deliberately
    rows = (await client.get("/api/v1/calendar/events", params={
        "start": WED.isoformat(), "end": (WED + timedelta(days=7)).isoformat()})).json()
    assert any(r["title"] == "Old series" for r in rows)


@pytest.mark.asyncio
async def test_one_occurrence_can_be_skipped_without_losing_the_series(client: AsyncClient):
    ev = await _make_event(client, recurrence={"freq": "weekly", "interval": 1})
    r = await client.post(f"/api/v1/calendar/events/{ev['id']}/skip",
                          params={"on": "2026-09-09"})
    assert r.status_code == 200 and r.json()["excluded_dates"] == ["2026-09-09"]

    rows = (await client.get("/api/v1/calendar/events", params={
        "start": WED.isoformat(), "end": (WED + timedelta(days=21)).isoformat()})).json()
    dates = {x["occurrence_date"] for x in rows if x["id"] == ev["id"]}
    assert "2026-09-09" not in dates and "2026-09-16" in dates


@pytest.mark.asyncio
async def test_skipping_a_one_off_is_refused(client: AsyncClient):
    ev = await _make_event(client)
    r = await client.post(f"/api/v1/calendar/events/{ev['id']}/skip",
                          params={"on": "2026-09-02"})
    assert r.status_code == 400 and "one-off" in r.json()["detail"]


@pytest.mark.asyncio
async def test_a_rule_that_cannot_fire_is_refused(client: AsyncClient):
    for rule in ({"freq": "fortnightly"}, {"freq": "weekly", "interval": 0},
                 {"freq": "weekly", "interval": 99}, {"freq": "daily", "count": 5000}):
        r = await client.post("/api/v1/calendar/events", json={
            "title": "Bad", "starts_at": WED.replace(hour=10).isoformat(),
            "ends_at": WED.replace(hour=11).isoformat(), "recurrence": rule})
        assert r.status_code == 400, rule


@pytest.mark.asyncio
async def test_monthly_from_the_31st_skips_short_months(session: AsyncSession):
    """It does not slide to the 1st: a meeting silently moving to a different
    month is worse than one that does not happen."""
    class Fake:
        starts_at = datetime(2026, 1, 31, 9, tzinfo=timezone.utc)
        ends_at = datetime(2026, 1, 31, 10, tzinfo=timezone.utc)
        recurrence = {"freq": "monthly", "interval": 1}
        excluded_dates: list = []

    dates = [o[0].date().isoformat() for o in service.occurrences(
        Fake(), datetime(2026, 1, 1, tzinfo=timezone.utc),
        datetime(2026, 7, 1, tzinfo=timezone.utc))]
    assert dates == ["2026-01-31", "2026-03-31", "2026-05-31"]


@pytest.mark.asyncio
async def test_a_repeating_meeting_blocks_every_occurrence(client: AsyncClient, session: AsyncSession):
    await _directory(session, "bob")
    await _make_event(client, attendee_user_ids=["bob"],
                      recurrence={"freq": "weekly", "interval": 1})

    # The SECOND occurrence, a week later — the one that only exists because the
    # rule is expanded on read.
    slots = await service.propose_slots(
        session, ["bob"], (WED + timedelta(days=7)).replace(hour=10),
        (WED + timedelta(days=7)).replace(hour=11), tenant_id="default",
        minutes=30, limit=3)
    starts = [s["starts_at"][11:16] for s in slots]
    assert "10:00" not in starts, "the second occurrence did not block the diary"
    assert "10:30" in starts, "it blocked more than its own half hour"


# ── editing ──────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_a_meeting_can_be_moved(client: AsyncClient):
    ev = await _make_event(client)
    r = await client.patch(f"/api/v1/calendar/events/{ev['id']}", json={
        "title": "Moved", "starts_at": WED.replace(hour=14).isoformat(),
        "ends_at": WED.replace(hour=15).isoformat()})
    assert r.status_code == 200
    assert r.json()["title"] == "Moved" and r.json()["starts_at"].startswith("2026-09-02T14")


@pytest.mark.asyncio
async def test_only_the_organiser_or_an_admin_edits(client: AsyncClient, session: AsyncSession):
    ev = await _make_event(client)
    r = await client.patch(f"/api/v1/calendar/events/{ev['id']}",
                           headers=_headers_for(["case_worker"], username="mallory"),
                           json={"title": "Hijacked"})
    assert r.status_code == 403


@pytest.mark.asyncio
async def test_an_edit_that_ends_before_it_starts_is_refused(client: AsyncClient):
    ev = await _make_event(client)
    r = await client.patch(f"/api/v1/calendar/events/{ev['id']}", json={
        "ends_at": WED.replace(hour=9).isoformat()})
    assert r.status_code == 400


@pytest.mark.asyncio
async def test_editing_an_absence_returns_it_to_requested(client: AsyncClient, session: AsyncSession):
    """The same rule the bot editor and the scheduler carry: approved leave
    reroutes other people's work, so moving its dates needs a second look."""
    await _directory(session, "alice")
    created = (await client.post("/api/v1/calendar/absences", json={
        "user_id": "alice", "starts_at": WED.isoformat(),
        "ends_at": (WED + timedelta(days=1)).isoformat()})).json()
    approved = await client.post(f"/api/v1/calendar/absences/{created['id']}/approve",
                                 headers=_headers_for(["admin", "superadmin"],
                                                      username="second-admin"))
    assert approved.status_code == 200 and approved.json()["status"] == "approved"

    r = await client.patch(f"/api/v1/calendar/absences/{created['id']}", json={
        "ends_at": (WED + timedelta(days=3)).isoformat()})
    assert r.status_code == 200
    assert r.json()["status"] == "requested" and r.json()["returned_to_requested"] is True


@pytest.mark.asyncio
async def test_a_no_op_edit_does_not_de_approve(client: AsyncClient, session: AsyncSession):
    """Or the editor becomes a denial of service on anybody's holiday."""
    await _directory(session, "alice")
    created = (await client.post("/api/v1/calendar/absences", json={
        "user_id": "alice", "starts_at": WED.isoformat(),
        "ends_at": (WED + timedelta(days=1)).isoformat()})).json()
    await client.post(f"/api/v1/calendar/absences/{created['id']}/approve",
                      headers=_headers_for(["admin", "superadmin"], username="second-admin"))

    r = await client.patch(f"/api/v1/calendar/absences/{created['id']}",
                           json={"kind": "holiday"})
    assert r.status_code == 200 and r.json()["status"] == "approved"


# ── guests ───────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_guests_are_kept_apart_from_attendees(client: AsyncClient, session: AsyncSession):
    """An availability lookup cannot find a working pattern for an email
    address, which is why the two lists are separate."""
    await _directory(session, "bob")
    ev = await _make_event(client, attendee_user_ids=["bob"],
                           external_emails=["counsel@example.com"])
    assert ev["attendee_user_ids"] == ["bob"]
    assert ev["external_emails"] == ["counsel@example.com"]


@pytest.mark.asyncio
async def test_a_bad_guest_address_is_refused(client: AsyncClient):
    r = await client.post("/api/v1/calendar/events", json={
        "title": "Guests", "starts_at": WED.replace(hour=10).isoformat(),
        "ends_at": WED.replace(hour=11).isoformat(),
        "external_emails": ["not-an-address"]})
    assert r.status_code == 400


@pytest.mark.asyncio
async def test_inviting_with_no_outbound_account_says_so(client: AsyncClient):
    ev = await _make_event(client, external_emails=["guest@example.com"])
    r = await client.post(f"/api/v1/calendar/events/{ev['id']}/invite")
    assert r.status_code == 409 and "outbound email account" in r.json()["detail"]


@pytest.mark.asyncio
async def test_inviting_nobody_is_refused(client: AsyncClient):
    ev = await _make_event(client)
    r = await client.post(f"/api/v1/calendar/events/{ev['id']}/invite")
    assert r.status_code == 400


def test_the_ics_is_the_thing_a_calendar_reads():
    """A comma that is not escaped truncates the summary in the guest's
    calendar rather than failing loudly."""
    class Fake:
        id = uuid.uuid4()
        title = "Weekly, sync"
        description = "Line one"
        status = "scheduled"
        starts_at = datetime(2026, 9, 1, 9, tzinfo=timezone.utc)
        ends_at = datetime(2026, 9, 1, 9, 30, tzinfo=timezone.utc)
        recurrence = {"freq": "weekly", "interval": 1, "until": "2026-12-01T00:00:00+00:00"}
        excluded_dates = ["2026-09-15"]
        external_emails = ["g@example.com"]
        meeting_url = "https://meet.example/x"

    ics = build_ics(Fake(), organiser_email="o@velaris.test")
    assert ics.startswith("BEGIN:VCALENDAR\r\n") and ics.endswith("END:VCALENDAR\r\n")
    assert "SUMMARY:Weekly\\, sync" in ics
    assert "RRULE:FREQ=WEEKLY;INTERVAL=1;UNTIL=20261201T000000Z" in ics
    assert "EXDATE:20260915T090000Z" in ics
    assert "ATTENDEE;ROLE=REQ-PARTICIPANT;RSVP=TRUE:mailto:g@example.com" in ics


@pytest.mark.asyncio
async def test_the_ics_endpoint_returns_a_file(client: AsyncClient):
    ev = await _make_event(client)
    r = await client.get(f"/api/v1/calendar/events/{ev['id']}/ics")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/calendar")
    assert "BEGIN:VEVENT" in r.text


# ── a room for a meeting that has no case ────────────────────────────────

@pytest.mark.asyncio
async def test_a_session_must_have_exactly_one_anchor(session: AsyncSession):
    from case_service.meet import service as meet

    with pytest.raises(ValueError):
        await meet.start_embedded_session(session, tenant_id="default", started_by="t")
    with pytest.raises(ValueError):
        await meet.start_embedded_session(
            session, tenant_id="default", started_by="t",
            case_id=uuid.uuid4(), calendar_event_id=uuid.uuid4())


@pytest.mark.asyncio
async def test_a_live_room_is_reused_and_a_finished_one_is_replaced(
    client: AsyncClient, session: AsyncSession, monkeypatch,
):
    """LiveKit ends a room when the last person leaves — right for a case
    session, which is a thing that happened, and wrong for a meeting, which is a
    thing that recurs. Rejoining must put you back in a room."""
    from velaris_shared.db.models import CaseSessionModel
    from case_service.meet import livekit, service as meet

    # The test deployment configures no meeting provider; the driver is what is
    # under test here, not the provider plumbing.
    async def embedded(*a, **k):
        return "embedded"

    monkeypatch.setattr(meet, "resolve_driver", embedded)
    monkeypatch.setattr(livekit, "configured", lambda: True)

    ev = await _make_event(client)
    live = CaseSessionModel(
        id=uuid.uuid4(), calendar_event_id=uuid.UUID(ev["id"]), tenant_id="default",
        driver="embedded", provider="livekit", status="active",
        started_by="t", started_at=datetime.now(timezone.utc),
    )
    session.add(live)
    row = await session.get(CalendarEventModel, uuid.UUID(ev["id"]))
    row.meet_session_id = live.id
    await session.commit()

    again = await client.post(f"/api/v1/calendar/events/{ev['id']}/meeting")
    assert again.status_code == 201 and again.json()["reused"] is True
    assert again.json()["meet_session_id"] == str(live.id)

    # …and once LiveKit has closed it, asking again starts a new one.
    live.status = "ended"
    await session.commit()
    restarted = await client.post(f"/api/v1/calendar/events/{ev['id']}/meeting")
    assert restarted.status_code == 201, restarted.text
    assert restarted.json()["reused"] is False
    assert restarted.json()["meet_session_id"] != str(live.id)


@pytest.mark.asyncio
async def test_the_listing_says_whether_the_room_is_live(client: AsyncClient, session: AsyncSession):
    """Or the screen promises a room LiveKit closed when the last person left."""
    from velaris_shared.db.models import CaseSessionModel

    ev = await _make_event(client, title="With a room")
    room = CaseSessionModel(
        id=uuid.uuid4(), calendar_event_id=uuid.UUID(ev["id"]), tenant_id="default",
        driver="embedded", provider="livekit", status="ended",
        started_by="t", started_at=datetime.now(timezone.utc),
    )
    session.add(room)
    row = await session.get(CalendarEventModel, uuid.UUID(ev["id"]))
    row.meet_session_id = room.id
    await session.commit()

    listed = (await client.get("/api/v1/calendar/events")).json()
    mine = [e for e in listed if e["id"] == ev["id"]][0]
    assert mine["room_status"] == "ended"


@pytest.mark.asyncio
async def test_a_pasted_link_and_a_created_room_do_not_both_apply(client: AsyncClient):
    ev = await _make_event(client, meeting_url="https://teams.example/abc")
    r = await client.post(f"/api/v1/calendar/events/{ev['id']}/meeting")
    assert r.status_code == 409 and "already carries a link" in r.json()["detail"]


# ── audit pass on phase 5 ────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_a_meeting_link_must_be_http(client: AsyncClient):
    """Studio renders the link as an anchor, so `javascript:` in an href is
    script execution one click away."""
    for bad in ("javascript:alert(1)", "data:text/html;base64,PHNjcmlwdD4=",
                "file:///etc/passwd"):
        r = await client.post("/api/v1/calendar/events", json={
            "title": "Link", "starts_at": WED.replace(hour=10).isoformat(),
            "ends_at": WED.replace(hour=11).isoformat(), "meeting_url": bad})
        assert r.status_code == 400, bad

    ok = await _make_event(client, meeting_url="https://meet.example/x")
    assert ok["meeting_url"] == "https://meet.example/x"

    # and the same on the way in through an edit
    bad = await client.patch(f"/api/v1/calendar/events/{ok['id']}",
                             json={"meeting_url": "javascript:alert(1)"})
    assert bad.status_code == 400


@pytest.mark.asyncio
async def test_an_event_cannot_be_anchored_to_another_tenants_case(client: AsyncClient, session: AsyncSession):
    """Not cosmetic: inviting sends email with `case_id=event.case_id`, so an
    unchecked id would file a message against another tenant's case."""
    from velaris_shared.db.models import CaseInstanceModel, CaseTypeModel

    ct = CaseTypeModel(id=uuid.uuid4(), name="Theirs", version="1.0.0",
                       description="", definition_json={}, tenant_id=uuid.uuid4())
    session.add(ct)
    await session.commit()
    case = CaseInstanceModel(id=uuid.uuid4(), case_type_id=ct.id,
                             case_type_version="1.0.0", tenant_id=str(uuid.uuid4()))
    session.add(case)
    await session.commit()

    r = await client.post("/api/v1/calendar/events", json={
        "title": "Cross", "starts_at": WED.replace(hour=10).isoformat(),
        "ends_at": WED.replace(hour=11).isoformat(), "case_id": str(case.id)})
    assert r.status_code == 400 and "does not exist here" in r.json()["detail"]


@pytest.mark.asyncio
async def test_a_room_cannot_be_claimed_from_the_request_body(client: AsyncClient, session: AsyncSession):
    """A session id in the body would let an event point at another tenant's
    room. Rooms are asked for through /events/{id}/meeting, which knows whose."""
    somebody_elses = uuid.uuid4()
    ev = await _make_event(client, meet_session_id=str(somebody_elses))
    assert ev["meet_session_id"] is None


@pytest.mark.asyncio
async def test_a_title_cannot_inject_a_line_into_the_invitation(session: AsyncSession):
    """A raw CR or LF ends the iCalendar *line*: everything after it is read as
    another property, so a title could add an ATTENDEE or a URL to the file
    somebody else receives."""
    class Fake:
        id = uuid.uuid4()
        title = "Sync\r\nATTENDEE;ROLE=CHAIR:mailto:evil@attacker.test"
        description = ""
        status = "scheduled"
        starts_at = datetime(2026, 9, 1, 9, tzinfo=timezone.utc)
        ends_at = datetime(2026, 9, 1, 10, tzinfo=timezone.utc)
        recurrence = None
        excluded_dates: list = []
        external_emails = ["g@example.com"]
        meeting_url = None

    ics = build_ics(Fake())
    assert "evil@attacker.test" in ics                      # it is still in the text…
    assert "\r\nATTENDEE;ROLE=CHAIR" not in ics             # …but not as a property
    attendees = [l for l in ics.split("\r\n") if l.startswith("ATTENDEE")]
    assert len(attendees) == 1 and "g@example.com" in attendees[0]


@pytest.mark.asyncio
async def test_a_series_cannot_grow_without_limit(client: AsyncClient, session: AsyncSession):
    from case_service.calendar.service import MAX_OCCURRENCES  # noqa: F401
    from case_service.api.routers.calendar import MAX_EXCLUSIONS

    ev = await _make_event(client, recurrence={"freq": "daily", "interval": 1})
    row = await session.get(CalendarEventModel, uuid.UUID(ev["id"]))
    row.excluded_dates = [f"2027-01-{d:02d}" for d in range(1, 32)] * 20   # > cap
    await session.commit()

    r = await client.post(f"/api/v1/calendar/events/{ev['id']}/skip",
                          params={"on": "2028-01-01"})
    assert r.status_code == 400 and str(MAX_EXCLUSIONS) in r.json()["detail"]


@pytest.mark.asyncio
async def test_a_meeting_in_the_past_needs_saying_so(client: AsyncClient):
    """Almost always a mistyped year or a wrong AM/PM — and it lands where
    nobody looks. Logging one that already happened is a real thing to want, so
    it is allowed, but said out loud."""
    past = datetime.now(timezone.utc) - timedelta(days=2)
    body = {"title": "Yesterday", "starts_at": past.isoformat(),
            "ends_at": (past + timedelta(hours=1)).isoformat()}

    r = await client.post("/api/v1/calendar/events", json=body)
    assert r.status_code == 400 and "already passed" in r.json()["detail"]

    ok = await client.post("/api/v1/calendar/events", json={**body, "allow_past": True})
    assert ok.status_code == 201


@pytest.mark.asyncio
async def test_a_meeting_that_just_finished_is_still_on_the_screen(client: AsyncClient):
    """Starting the window at "now" meant a meeting that ended an hour ago
    vanished — and with it the only way to edit, cancel or invite anybody to it.
    A calendar that hides yesterday is not a calendar."""
    past = datetime.now(timezone.utc) - timedelta(hours=3)
    ev = await _make_event(client, title="Just finished",
                           starts_at=past.isoformat(),
                           ends_at=(past + timedelta(hours=1)).isoformat(),
                           allow_past=True)

    rows = (await client.get("/api/v1/calendar/events")).json()   # no window given
    assert any(r["id"] == ev["id"] for r in rows), "it fell off the default window"


# ── invitations that actually reach somebody ─────────────────────────────

@pytest.mark.asyncio
async def test_an_invitation_smtp_refused_is_not_reported_as_sent(client: AsyncClient, session: AsyncSession, monkeypatch):
    """`EmailService.send` does not raise when SMTP does — it records the failure
    on the row and returns. Counting that as sent is how an invitation nobody
    received looks like a success."""
    from velaris_shared.db.models import EmailAccountModel
    from case_service.mail import service as mail

    session.add(EmailAccountModel(
        id=uuid.uuid4(), name="Outbound", address="ops@velaris.test",
        smtp_host="smtp.invalid", smtp_port=587, smtp_use_tls=True,
        is_active=True, is_default_outbound=True, tenant_id="default",
    ))
    await session.commit()

    async def refuse(**kwargs):
        raise RuntimeError("connection refused")

    monkeypatch.setattr(mail, "send_via_smtp", refuse)

    ev = await _make_event(client, external_emails=["guest@example.com"])
    r = await client.post(f"/api/v1/calendar/events/{ev['id']}/invite")
    assert r.status_code == 200
    body = r.json()
    assert body["sent"] == []
    assert body["failed"] and "connection refused" in body["failed"][0]["error"]
    assert body["from"] == "ops@velaris.test"


@pytest.mark.asyncio
async def test_each_guest_gets_their_own_join_link(client: AsyncClient, session: AsyncSession, monkeypatch):
    """An embedded room admits a guest only through a single-use invite token,
    so a shared link would let one guest in and lock the rest out."""
    from velaris_shared.db.models import CaseSessionModel, EmailAccountModel, EmailMessageModel
    from case_service.mail import service as mail
    from sqlalchemy import select as _select

    session.add(EmailAccountModel(
        id=uuid.uuid4(), name="Outbound", address="ops@velaris.test",
        smtp_host="smtp.invalid", smtp_port=587, smtp_use_tls=True,
        is_active=True, is_default_outbound=True, tenant_id="default",
    ))
    await session.commit()

    sent_bodies: list[str] = []

    async def capture(**kwargs):
        sent_bodies.append(str(kwargs.get("mime_message")))

    monkeypatch.setattr(mail, "send_via_smtp", capture)
    # `/meet/join` is a Studio page, so a guest link needs Studio's address.
    from velaris_shared.config import get_settings
    settings = get_settings()
    original_base = settings.public_base_url
    settings.public_base_url = "https://velaris.test"

    # `send_invites=False`: this test is about the link in the *explicit*
    # invitation, sent once the room exists. Scheduling with guests sends its
    # own — see `test_scheduling_with_guests_invites_them`.
    ev = await _make_event(client, external_emails=["a@example.com", "b@example.com"],
                           send_invites=False)
    row = await session.get(CalendarEventModel, uuid.UUID(ev["id"]))
    room = CaseSessionModel(
        id=uuid.uuid4(), calendar_event_id=row.id, tenant_id="default",
        driver="embedded", provider="livekit", status="active",
        started_by="t", started_at=datetime.now(timezone.utc),
    )
    session.add(room)
    row.meet_session_id = room.id
    await session.commit()

    r = await client.post(f"/api/v1/calendar/events/{ev['id']}/invite")
    assert r.status_code == 200 and len(r.json()["sent"]) == 2

    messages = (await session.execute(_select(EmailMessageModel))).scalars().all()
    links = []
    for m in messages:
        assert "/meet/join?token=" in (m.body_text or ""), m.body_text
        links.append((m.body_text or "").split("/meet/join?token=")[1].split()[0])
    assert len(set(links)) == 2, "the two guests share a single-use token"
    assert all(l for l in links)
    settings.public_base_url = original_base


@pytest.mark.asyncio
async def test_the_guest_token_outlives_the_wait_for_the_meeting(session: AsyncSession):
    """The platform default is fifteen minutes — right for a link read out during
    a call, useless in an invitation sent a week ahead."""
    from case_service.calendar.invites import _join_link_for
    from velaris_shared.db.models import CaseSessionModel, CaseSessionParticipantModel
    from sqlalchemy import select as _select

    ev = CalendarEventModel(
        id=uuid.uuid4(), tenant_id="default", title="Next week", description="",
        starts_at=datetime.now(timezone.utc) + timedelta(days=7),
        ends_at=datetime.now(timezone.utc) + timedelta(days=7, hours=1),
        timezone="UTC", organiser_user_id="admin", attendee_user_ids=[],
        external_emails=["g@example.com"], visibility="team", status="scheduled",
        created_by="admin",
    )
    room = CaseSessionModel(
        id=uuid.uuid4(), calendar_event_id=ev.id, tenant_id="default",
        driver="embedded", provider="livekit", status="active",
        started_by="t", started_at=datetime.now(timezone.utc),
    )
    ev.meet_session_id = room.id
    session.add_all([ev, room])
    await session.commit()

    link = await _join_link_for(session, ev, "g@example.com",
                                base_url="https://velaris.test", invited_by="admin")
    assert link and link.startswith("https://velaris.test/meet/join?token=")

    guest = (await session.execute(_select(CaseSessionParticipantModel))).scalars().first()
    expires = guest.invite_expires_at
    if expires.tzinfo is None:          # SQLite hands back naive UTC
        expires = expires.replace(tzinfo=timezone.utc)
    assert expires > datetime.now(timezone.utc) + timedelta(days=7)


@pytest.mark.asyncio
async def test_scheduling_with_guests_invites_them(client: AsyncClient, session: AsyncSession, monkeypatch):
    """Pressing Schedule with guests listed **is** the decision to invite them.
    Making somebody find the meeting again afterwards to press a second button
    is the kind of gap that leaves guests uninvited."""
    from velaris_shared.db.models import EmailAccountModel
    from case_service.mail import service as mail

    session.add(EmailAccountModel(
        id=uuid.uuid4(), name="Outbound", address="ops@velaris.test",
        smtp_host="smtp.invalid", smtp_port=587, smtp_use_tls=True,
        is_active=True, is_default_outbound=True, tenant_id="default",
    ))
    await session.commit()

    async def accept(**kwargs):
        return None

    monkeypatch.setattr(mail, "send_via_smtp", accept)

    r = await client.post("/api/v1/calendar/events", json={
        "title": "With guests",
        "starts_at": WED.replace(hour=10).isoformat(),
        "ends_at": WED.replace(hour=11).isoformat(),
        "external_emails": ["guest@example.com"],
    })
    assert r.status_code == 201
    assert r.json()["invitations"]["sent"] == ["guest@example.com"]


@pytest.mark.asyncio
async def test_scheduling_without_guests_sends_nothing(client: AsyncClient):
    ev = await _make_event(client)
    assert "invitations" not in ev


@pytest.mark.asyncio
async def test_an_edit_never_sends(client: AsyncClient, session: AsyncSession, monkeypatch):
    """A save is not a send: every typo fixed in a title would otherwise mail
    the whole guest list again."""
    from velaris_shared.db.models import EmailAccountModel, EmailMessageModel
    from case_service.mail import service as mail
    from sqlalchemy import select as _select

    session.add(EmailAccountModel(
        id=uuid.uuid4(), name="Outbound", address="ops@velaris.test",
        smtp_host="smtp.invalid", smtp_port=587, smtp_use_tls=True,
        is_active=True, is_default_outbound=True, tenant_id="default",
    ))
    await session.commit()

    async def accept(**kwargs):
        return None

    monkeypatch.setattr(mail, "send_via_smtp", accept)

    ev = await _make_event(client, external_emails=["guest@example.com"])
    before = len((await session.execute(_select(EmailMessageModel))).scalars().all())

    r = await client.patch(f"/api/v1/calendar/events/{ev['id']}", json={
        "title": "Renamed", "external_emails": ["guest@example.com", "second@example.com"]})
    assert r.status_code == 200

    after = len((await session.execute(_select(EmailMessageModel))).scalars().all())
    assert after == before, "an edit sent mail"


@pytest.mark.asyncio
async def test_no_public_address_refuses_rather_than_mailing_a_dead_link(client: AsyncClient, session: AsyncSession, monkeypatch):
    """`/meet/join` is a Studio page on its own origin. Guessing the API's
    address posts a link to a page the API does not serve — and for an external
    guest, one they could never reach."""
    from velaris_shared.config import get_settings
    from velaris_shared.db.models import CaseSessionModel, EmailAccountModel
    from case_service.mail import service as mail

    session.add(EmailAccountModel(
        id=uuid.uuid4(), name="Outbound", address="ops@velaris.test",
        smtp_host="smtp.invalid", smtp_port=587, smtp_use_tls=True,
        is_active=True, is_default_outbound=True, tenant_id="default",
    ))
    await session.commit()

    async def accept(**kwargs):
        return None

    monkeypatch.setattr(mail, "send_via_smtp", accept)
    settings = get_settings()
    original = settings.public_base_url
    settings.public_base_url = ""            # nothing configured

    try:
        ev = await _make_event(client, external_emails=["guest@example.com"],
                               send_invites=False)
        row = await session.get(CalendarEventModel, uuid.UUID(ev["id"]))
        room = CaseSessionModel(
            id=uuid.uuid4(), calendar_event_id=row.id, tenant_id="default",
            driver="embedded", provider="livekit", status="active",
            started_by="t", started_at=datetime.now(timezone.utc),
        )
        session.add(room)
        row.meet_session_id = room.id
        await session.commit()

        r = await client.post(f"/api/v1/calendar/events/{ev['id']}/invite")
        assert r.status_code == 200
        assert r.json()["sent"] == []
        assert "VELARIS_CASE_PUBLIC_BASE_URL" in r.json()["failed"][0]["error"]
    finally:
        settings.public_base_url = original


@pytest.mark.asyncio
async def test_a_pasted_link_needs_no_public_address(client: AsyncClient, session: AsyncSession, monkeypatch):
    """It is already absolute — the setting is only about links this platform
    has to build."""
    from velaris_shared.config import get_settings
    from velaris_shared.db.models import EmailAccountModel, EmailMessageModel
    from case_service.mail import service as mail
    from sqlalchemy import select as _select

    session.add(EmailAccountModel(
        id=uuid.uuid4(), name="Outbound", address="ops@velaris.test",
        smtp_host="smtp.invalid", smtp_port=587, smtp_use_tls=True,
        is_active=True, is_default_outbound=True, tenant_id="default",
    ))
    await session.commit()

    async def accept(**kwargs):
        return None

    monkeypatch.setattr(mail, "send_via_smtp", accept)
    settings = get_settings()
    original = settings.public_base_url
    settings.public_base_url = ""

    try:
        ev = await _make_event(client, external_emails=["guest@example.com"],
                               meeting_url="https://teams.example/abc",
                               send_invites=False)
        r = await client.post(f"/api/v1/calendar/events/{ev['id']}/invite")
        assert r.status_code == 200 and r.json()["sent"] == ["guest@example.com"]

        messages = (await session.execute(_select(EmailMessageModel))).scalars().all()
        assert any("https://teams.example/abc" in (m.body_text or "") for m in messages)
    finally:
        settings.public_base_url = original
