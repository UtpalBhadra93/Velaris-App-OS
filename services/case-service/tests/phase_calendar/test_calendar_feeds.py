"""Calendar subscriptions — "add a calendar by URL".

Migration 175 read an uploaded file; a file is a snapshot and goes stale. This
is the same reader on a URL, re-read on a timetable.

**Most of this file is the fetch guard**, because that is the whole security
surface: a user supplies an address and this server requests it from inside the
network, which is the shape of every SSRF. Allowlisting the name is not enough —
the resolved address is checked too, and a redirect is refused rather than
followed, because a redirect is how an allowed host hands you an address nobody
allowed.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from velaris_shared.db.models import (
    CalendarEventModel, CalendarSubscriptionModel, UserAbsenceModel,
)
from case_service.calendar import feeds

from tests.conftest import client, session  # type: ignore[attr-defined]  # noqa: F401


@pytest.fixture
def allow_hosts(monkeypatch):
    """The operator's allowlist. Unset by default — see the first test."""

    def _set(value: str = "calendar.example.test,.google.com"):
        from velaris_shared.config import get_settings
        get_settings.cache_clear()
        monkeypatch.setenv("VELARIS_CASE_CALENDAR_FEED_HOSTS", value)
        get_settings.cache_clear()
        return value

    yield _set
    from velaris_shared.config import get_settings
    get_settings.cache_clear()


@pytest.fixture
def public_dns(monkeypatch):
    """Pretend every allowed name resolves somewhere public.

    Note `203.0.113.9` would NOT pass the real check: `ipaddress` counts the
    documentation ranges as reserved, and the guard refuses reserved as well as
    private. That is deliberate — a name resolving into TEST-NET is not a
    calendar server — so this fixture replaces the check rather than feeding it
    an address.
    """
    monkeypatch.setattr(feeds, "_refuse_internal", lambda host: ["203.0.113.9"])


# ── the allowlist ─────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_unset_allowlist_means_no_subscriptions_at_all(monkeypatch):
    """The feature arrives OFF on every existing install. Naming the hosts is an
    operator's decision, never an update's."""
    from velaris_shared.config import get_settings

    get_settings.cache_clear()
    monkeypatch.setenv("VELARIS_CASE_CALENDAR_FEED_HOSTS", "")
    get_settings.cache_clear()
    with pytest.raises(feeds.FeedError) as exc:
        await feeds.validate_url("https://calendar.example.test/a.ics")
    assert "administrator" in str(exc.value)
    get_settings.cache_clear()


@pytest.mark.asyncio
async def test_a_host_nobody_allowed_is_refused(allow_hosts, public_dns):
    allow_hosts()
    with pytest.raises(feeds.FeedError):
        await feeds.validate_url("https://evil.test/a.ics")


@pytest.mark.asyncio
async def test_a_domain_entry_does_not_match_a_lookalike(allow_hosts, public_dns):
    """`.google.com` must allow `calendar.google.com` and refuse
    `notgoogle.com` — a suffix match without the dot would take both."""
    allow_hosts()
    assert await feeds.validate_url("https://calendar.google.com/x.ics")
    with pytest.raises(feeds.FeedError):
        await feeds.validate_url("https://notgoogle.com/x.ics")


@pytest.mark.asyncio
async def test_only_http_https_and_webcal(allow_hosts, public_dns):
    allow_hosts()
    for bad in ("file:///etc/passwd", "gopher://calendar.example.test/",
                "javascript:alert(1)"):
        with pytest.raises(feeds.FeedError):
            await feeds.validate_url(bad)


@pytest.mark.asyncio
async def test_webcal_is_rewritten_rather_than_refused(allow_hosts, public_dns):
    """It is what Apple and Outlook put on the clipboard. Rewriting saves every
    user the same edit."""
    allow_hosts()
    out = await feeds.validate_url("webcal://calendar.example.test/a.ics")
    assert out.startswith("https://")


@pytest.mark.asyncio
async def test_credentials_in_the_url_are_stripped(allow_hosts, public_dns):
    """Otherwise a password is stored in a column that is read back to a screen."""
    allow_hosts()
    out = await feeds.validate_url("https://user:secret@calendar.example.test/a.ics")
    assert "secret" not in out and "user" not in out


# ── the resolved address ──────────────────────────────────────────────────

@pytest.mark.parametrize("address", [
    "127.0.0.1", "10.0.0.5", "192.168.1.10", "169.254.169.254", "::1",
])
@pytest.mark.asyncio
async def test_an_allowed_name_pointing_inside_is_still_refused(allow_hosts, monkeypatch,
                                                          address):
    """Allowlisting the NAME is not enough. A host an operator allowed can
    resolve to loopback, a private range, or the cloud metadata address — by
    mistake, or because whoever controls the DNS wants it to."""
    allow_hosts()
    monkeypatch.setattr(
        feeds.socket, "getaddrinfo",
        lambda *a, **k: [(None, None, None, None, (address, 0))])
    with pytest.raises(feeds.FeedError) as exc:
        await feeds.validate_url("https://calendar.example.test/a.ics")
    assert "not a public address" in str(exc.value)


@pytest.mark.asyncio
async def test_a_name_that_does_not_resolve_is_refused(allow_hosts, monkeypatch):
    import socket as _socket

    allow_hosts()
    monkeypatch.setattr(feeds.socket, "getaddrinfo",
                        lambda *a, **k: (_ for _ in ()).throw(_socket.gaierror()))
    with pytest.raises(feeds.FeedError):
        await feeds.validate_url("https://calendar.example.test/a.ics")


# ── fetching ──────────────────────────────────────────────────────────────

def _ics(uid: str = "feed-1@example.test", summary: str = "Feed meeting",
         extra: str = "") -> str:
    return ("BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//T//EN\r\n"
            "BEGIN:VEVENT\r\n"
            f"UID:{uid}\r\nDTSTAMP:20260901T090000Z\r\n"
            "DTSTART:20260902T090000Z\r\nDTEND:20260902T100000Z\r\n"
            f"SUMMARY:{summary}\r\n{extra}"
            "END:VEVENT\r\nEND:VCALENDAR\r\n")


class _Response:
    def __init__(self, status=200, body=b"", headers=None, redirect=False):
        self.status_code = status
        self._body = body
        self.headers = headers or {}
        self.is_redirect = redirect

    async def aiter_bytes(self):
        yield self._body

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _Client:
    """Stands in for httpx. Records the headers so the 304 path can be proven."""

    last_headers: dict = {}

    def __init__(self, response):
        self._response = response

    def __call__(self, *a, **kw):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def stream(self, method, url, headers=None):
        _Client.last_headers = dict(headers or {})
        return self._response


def _patch_http(monkeypatch, response):
    import httpx

    monkeypatch.setattr(httpx, "AsyncClient", _Client(response))


@pytest.mark.asyncio
async def test_a_redirect_is_refused_not_followed(allow_hosts, public_dns, monkeypatch):
    """A redirect is how an allowlisted host hands you an address nobody
    allowed — the standard way past every allowlist."""
    allow_hosts()
    _patch_http(monkeypatch, _Response(302, headers={"location": "http://169.254.169.254/"},
                                       redirect=True))
    with pytest.raises(feeds.FeedError) as exc:
        await feeds.fetch("https://calendar.example.test/a.ics")
    assert "redirects" in str(exc.value)


@pytest.mark.asyncio
async def test_an_oversized_feed_is_refused_while_streaming(
        allow_hosts, public_dns, monkeypatch):
    allow_hosts()
    from velaris_shared.config import get_settings

    monkeypatch.setenv("VELARIS_CASE_CALENDAR_FEED_MAX_BYTES", "1024")
    get_settings.cache_clear()
    _patch_http(monkeypatch, _Response(200, b"x" * 5000))
    with pytest.raises(feeds.FeedError) as exc:
        await feeds.fetch("https://calendar.example.test/a.ics")
    assert "larger than" in str(exc.value)
    get_settings.cache_clear()


@pytest.mark.asyncio
async def test_304_is_a_success_not_a_failure(allow_hosts, public_dns, monkeypatch):
    allow_hosts()
    _patch_http(monkeypatch, _Response(304))
    text, meta = await feeds.fetch("https://calendar.example.test/a.ics",
                                   etag='W/"abc"')
    assert text is None and meta["status"] == 304
    assert _Client.last_headers["If-None-Match"] == 'W/"abc"'


# ── refreshing ────────────────────────────────────────────────────────────

async def _subscription(session: AsyncSession, **over) -> CalendarSubscriptionModel:
    fields = {"tenant_id": "default", "user_id": "alice", "name": "Work",
              "url": "https://calendar.example.test/a.ics", "created_by": "alice"}
    fields.update(over)
    row = CalendarSubscriptionModel(id=uuid.uuid4(), **fields)
    session.add(row)
    await session.commit()
    return row


@pytest.mark.asyncio
async def test_refresh_writes_then_updates(allow_hosts, public_dns, monkeypatch,
                                           session: AsyncSession):
    allow_hosts()
    sub = await _subscription(session)

    _patch_http(monkeypatch, _Response(200, _ics().encode(),
                                       headers={"etag": 'W/"1"'}))
    first = await feeds.refresh(session, sub, actor="alice")
    await session.commit()
    assert first["status"] == "ok" and first["created"] == 1
    assert sub.etag == 'W/"1"' and sub.last_status == "ok"

    _patch_http(monkeypatch, _Response(200, _ics(summary="Renamed").encode()))
    second = await feeds.refresh(session, sub, actor="alice")
    await session.commit()
    assert second["updated"] == 1 and second["created"] == 0

    rows = (await session.execute(select(CalendarEventModel))).scalars().all()
    assert len(rows) == 1 and rows[0].title == "Renamed"
    assert rows[0].subscription_id == sub.id


@pytest.mark.asyncio
async def test_an_entry_that_leaves_the_feed_is_cancelled_not_deleted(
        allow_hosts, public_dns, monkeypatch, session: AsyncSession):
    """Unlike a file upload, a subscription IS authoritative for its own rows —
    otherwise a cancelled meeting sits in the calendar forever. But cancelling
    keeps the row visible and reversible, and keeps the history."""
    allow_hosts()
    sub = await _subscription(session)
    _patch_http(monkeypatch, _Response(200, _ics().encode()))
    await feeds.refresh(session, sub, actor="alice")
    await session.commit()

    empty = "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nEND:VCALENDAR\r\n"
    _patch_http(monkeypatch, _Response(200, empty.encode()))
    out = await feeds.refresh(session, sub, actor="alice")
    await session.commit()

    rows = (await session.execute(select(CalendarEventModel))).scalars().all()
    assert out["withdrawn"] == 1
    assert len(rows) == 1 and rows[0].status == "cancelled"


@pytest.mark.asyncio
async def test_approved_leave_is_never_withdrawn_by_a_feed(
        allow_hosts, public_dns, monkeypatch, session: AsyncSession):
    """A human approved it. A feed must not be able to undo a human decision, or
    to put somebody back on a rota they were taken off."""
    allow_hosts()
    sub = await _subscription(session)
    ooo = _ics(extra="X-MICROSOFT-CDO-BUSYSTATUS:OOF\r\n")
    _patch_http(monkeypatch, _Response(200, ooo.encode()))
    await feeds.refresh(session, sub, actor="alice")
    await session.commit()

    leave = (await session.execute(select(UserAbsenceModel))).scalars().one()
    leave.status = "approved"
    leave.approved_by = "manager"
    await session.commit()

    empty = "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nEND:VCALENDAR\r\n"
    _patch_http(monkeypatch, _Response(200, empty.encode()))
    await feeds.refresh(session, sub, actor="alice")
    await session.commit()

    await session.refresh(leave)
    assert leave.status == "approved"


@pytest.mark.asyncio
async def test_a_broken_feed_is_recorded_not_raised(
        allow_hosts, public_dns, monkeypatch, session: AsyncSession):
    """A calendar failing for a fortnight must SAY so. A sync that silently
    stops is worse than one that never started — the calendar still looks full."""
    allow_hosts()
    sub = await _subscription(session)
    _patch_http(monkeypatch, _Response(500))
    out = await feeds.refresh(session, sub, actor="alice")
    await session.commit()

    assert out["status"] == "error"
    assert sub.last_status == "error" and sub.consecutive_errors == 1
    assert "500" in sub.last_error


@pytest.mark.asyncio
async def test_a_feed_that_answers_with_html_is_recorded_not_raised(
        allow_hosts, public_dns, monkeypatch, session: AsyncSession):
    allow_hosts()
    sub = await _subscription(session)
    _patch_http(monkeypatch, _Response(200, b"<html>sign in</html>"))
    out = await feeds.refresh(session, sub, actor="alice")
    assert out["status"] == "error" and "not readable" in sub.last_error


@pytest.mark.asyncio
async def test_a_feed_that_keeps_failing_stops_being_polled(
        allow_hosts, session: AsyncSession):
    """Retrying a dead URL every hour forever is how a log becomes unreadable.
    It is not disabled — a manual refresh still runs, and the row says why."""
    allow_hosts()
    sub = await _subscription(session,
                              consecutive_errors=feeds.MAX_CONSECUTIVE_ERRORS)
    assert feeds.due(sub) is False


def test_due_respects_each_subscriptions_own_interval():
    """The schedule sets the granularity; the subscription sets the rate."""
    now = datetime.now(timezone.utc)
    sub = CalendarSubscriptionModel(
        id=uuid.uuid4(), tenant_id="default", user_id="alice", name="w",
        url="https://calendar.example.test/a.ics", created_by="a",
        enabled=True, interval_minutes=60, consecutive_errors=0,
        last_fetched_at=now - timedelta(minutes=30))
    assert feeds.due(sub, now=now) is False
    sub.last_fetched_at = now - timedelta(minutes=90)
    assert feeds.due(sub, now=now) is True
    sub.enabled = False
    assert feeds.due(sub, now=now) is False


# ── two feeds, one UID ────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_two_subscriptions_carrying_the_same_uid_do_not_fight(
        allow_hosts, public_dns, monkeypatch, session: AsyncSession):
    """A meeting a person and their team are both invited to appears in both
    feeds with the same UID. Matching on the UID alone would make each refresh
    steal the other's row, forever."""
    allow_hosts()
    one = await _subscription(session, name="Mine")
    two = await _subscription(session, name="Team")

    _patch_http(monkeypatch, _Response(200, _ics(summary="From mine").encode()))
    await feeds.refresh(session, one, actor="alice")
    await session.commit()
    _patch_http(monkeypatch, _Response(200, _ics(summary="From team").encode()))
    await feeds.refresh(session, two, actor="alice")
    await session.commit()

    rows = (await session.execute(select(CalendarEventModel))).scalars().all()
    assert len(rows) == 2
    assert {r.subscription_id for r in rows} == {one.id, two.id}


@pytest.mark.asyncio
async def test_a_feed_never_adopts_an_uploaded_row(
        allow_hosts, public_dns, monkeypatch, session: AsyncSession):
    """An upload owns rows no subscription created, and vice versa — otherwise
    the two would take each other's rows back and forth on every refresh."""
    from case_service.calendar.ics_import import apply_import, parse_ics

    allow_hosts()
    await apply_import(session, parse_ics(_ics()), tenant_id="default",
                       user_id="alice", actor="alice", dry_run=False)
    await session.commit()

    sub = await _subscription(session)
    _patch_http(monkeypatch, _Response(200, _ics().encode()))
    out = await feeds.refresh(session, sub, actor="alice")
    await session.commit()

    rows = (await session.execute(select(CalendarEventModel))).scalars().all()
    assert out["created"] == 1
    assert len(rows) == 2
    assert sorted(r.subscription_id is None for r in rows) == [False, True]


# ── the endpoints ─────────────────────────────────────────────────────────

def _headers_for(roles: list[str], username: str) -> dict:
    from velaris_shared.auth.jwt_handler import create_dev_token
    from velaris_shared.config import get_settings

    st = get_settings()
    token = create_dev_token(
        user_id=str(uuid.uuid4()), username=username, roles=roles,
        secret=st.auth_secret, private_key=st.auth_rsa_private_key or "")
    return {"Authorization": f"Bearer {token}"}


async def _directory(session: AsyncSession, user_id: str, tenant: str = "default"):
    from velaris_shared.db.models import UserDirectoryModel

    session.add(UserDirectoryModel(
        id=uuid.uuid4(), user_id=user_id, display_name=user_id,
        email=f"{user_id}@example.com", tenant_id=tenant, is_active=True))
    await session.commit()


@pytest.mark.asyncio
async def test_subscribe_and_list(allow_hosts, public_dns, client, session: AsyncSession):
    allow_hosts()
    await _directory(session, "test-admin")
    r = await client.post("/api/v1/calendar/subscriptions", json={
        "name": "Work", "url": "webcal://calendar.example.test/a.ics"})
    assert r.status_code == 201, r.text
    assert r.json()["url"].startswith("https://")

    listed = await client.get("/api/v1/calendar/subscriptions")
    assert listed.status_code == 200
    body = listed.json()
    assert len(body["subscriptions"]) == 1
    assert body["allowed_hosts"]


@pytest.mark.asyncio
async def test_a_host_outside_the_allowlist_is_refused_at_the_api(
        allow_hosts, public_dns, client, session: AsyncSession):
    allow_hosts()
    await _directory(session, "test-admin")
    r = await client.post("/api/v1/calendar/subscriptions", json={
        "name": "Bad", "url": "https://evil.test/a.ics"})
    assert r.status_code == 400
    assert "not an allowed calendar host" in r.json()["detail"]


@pytest.mark.asyncio
async def test_unsubscribing_keeps_the_entries_by_default(
        allow_hosts, public_dns, monkeypatch, client, session: AsyncSession):
    """Removing a subscription means "stop refreshing this", not "erase the last
    year of my diary"."""
    allow_hosts()
    await _directory(session, "test-admin")
    sub = await _subscription(session, user_id="test-admin")
    _patch_http(monkeypatch, _Response(200, _ics().encode()))
    await feeds.refresh(session, sub, actor="test-admin")
    await session.commit()

    r = await client.delete(f"/api/v1/calendar/subscriptions/{sub.id}")
    assert r.status_code == 204, r.text

    rows = (await session.execute(select(CalendarEventModel))).scalars().all()
    assert len(rows) == 1
    assert rows[0].status == "scheduled" and rows[0].subscription_id is None


@pytest.mark.asyncio
async def test_a_feed_failure_is_a_200_with_a_reason(
        allow_hosts, public_dns, monkeypatch, client, session: AsyncSession):
    """The request succeeded; what failed is somebody else's server."""
    allow_hosts()
    await _directory(session, "test-admin")
    sub = await _subscription(session, user_id="test-admin")
    _patch_http(monkeypatch, _Response(503))
    r = await client.post(f"/api/v1/calendar/subscriptions/{sub.id}/refresh")
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "error"


# ── export ────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_export_round_trips_through_our_own_reader(
        client, session: AsyncSession):
    """What this platform writes, it reads. The pair is the point."""
    from case_service.calendar.ics_import import parse_ics

    await _directory(session, "test-admin")
    session.add(CalendarEventModel(
        id=uuid.uuid4(), tenant_id="default", title="Design review",
        description="", starts_at=datetime(2026, 9, 2, 9, tzinfo=timezone.utc),
        ends_at=datetime(2026, 9, 2, 10, tzinfo=timezone.utc), timezone="UTC",
        organiser_user_id="test-admin", attendee_user_ids=["test-admin"],
        visibility="team", status="scheduled", created_by="test-admin"))
    await session.commit()

    r = await client.get("/api/v1/calendar/export", params={
        "start": "2026-09-01T00:00:00Z", "end": "2026-09-30T00:00:00Z"})
    assert r.status_code == 200, r.text
    assert r.headers["content-type"].startswith("text/calendar")
    assert ".ics" in r.headers["content-disposition"]

    parsed = parse_ics(r.text)
    assert [e.title for e in parsed.events] == ["Design review"]


@pytest.mark.asyncio
async def test_exported_leave_comes_back_as_leave(client, session: AsyncSession):
    from case_service.calendar.ics_import import parse_ics

    await _directory(session, "test-admin")
    session.add(UserAbsenceModel(
        id=uuid.uuid4(), tenant_id="default", user_id="test-admin",
        kind="holiday", starts_at=datetime(2026, 9, 7, tzinfo=timezone.utc),
        ends_at=datetime(2026, 9, 8, tzinfo=timezone.utc), all_day=True,
        status="approved", approved_by="mgr", source="manual", note="",
        created_by="test-admin"))
    await session.commit()

    r = await client.get("/api/v1/calendar/export", params={
        "start": "2026-09-01T00:00:00Z", "end": "2026-09-30T00:00:00Z"})
    parsed = parse_ics(r.text)
    assert len(parsed.events) == 1
    assert parsed.events[0].out_of_office is True


@pytest.mark.asyncio
async def test_export_does_not_carry_somebody_elses_private_meeting(
        client, session: AsyncSession):
    """Not listed as "Busy" — absent. An export leaves the platform, so the
    filter has to be at least as strict here as on a screen."""
    from case_service.calendar.ics_import import parse_ics

    await _directory(session, "test-admin")
    session.add(CalendarEventModel(
        id=uuid.uuid4(), tenant_id="default", title="Private one to one",
        description="", starts_at=datetime(2026, 9, 3, 9, tzinfo=timezone.utc),
        ends_at=datetime(2026, 9, 3, 10, tzinfo=timezone.utc), timezone="UTC",
        organiser_user_id="someone-else", attendee_user_ids=["someone-else"],
        visibility="private", status="scheduled", created_by="someone-else"))
    await session.commit()

    r = await client.get("/api/v1/calendar/export", params={
        "start": "2026-09-01T00:00:00Z", "end": "2026-09-30T00:00:00Z",
        "user_id": "someone-else"})
    assert "Private one to one" not in r.text
    assert parse_ics(r.text).events == []


# ── audit fixes (2026-08-27) ──────────────────────────────────────────────

@pytest.mark.asyncio
async def test_dns_is_resolved_off_the_event_loop(allow_hosts, monkeypatch):
    """`getaddrinfo` blocks, and this service runs ONE asyncio loop: a resolver
    taking five seconds would stall every other request in the process."""
    import asyncio
    import threading

    allow_hosts()
    calling_thread: list[str] = []
    monkeypatch.setattr(
        feeds.socket, "getaddrinfo",
        lambda *a, **k: (calling_thread.append(threading.current_thread().name)
                         or [(None, None, None, None, ("93.184.216.34", 0))]))
    await feeds.validate_url("https://calendar.example.test/a.ics")
    assert calling_thread and calling_thread[0] != threading.main_thread().name


@pytest.mark.asyncio
async def test_you_cannot_list_a_colleagues_subscriptions(
        allow_hosts, public_dns, client, session: AsyncSession):
    """A subscription NAME is typed by its owner and can be as personal as an
    absence note. Detail rights gate the whole row, not just the URL."""
    allow_hosts()
    await _directory(session, "worker")
    await _directory(session, "someone-else")
    await _subscription(session, user_id="someone-else", name="Therapy")

    r = await client.get("/api/v1/calendar/subscriptions",
                         params={"user_id": "someone-else"},
                         headers=_headers_for(["case_worker"], "worker"))
    assert r.status_code == 403, r.text
    assert "Therapy" not in r.text


@pytest.mark.asyncio
async def test_subscriptions_are_capped_per_person(
        allow_hosts, public_dns, client, session: AsyncSession):
    """Each one is a standing outbound request on a timetable. Unbounded, one
    account turns the platform into a traffic source."""
    from case_service.api.routers.calendar import MAX_SUBSCRIPTIONS

    allow_hosts()
    await _directory(session, "test-admin")
    for i in range(MAX_SUBSCRIPTIONS):
        await _subscription(session, user_id="test-admin", name=f"c{i}")

    r = await client.post("/api/v1/calendar/subscriptions", json={
        "name": "one too many", "url": "https://calendar.example.test/a.ics"})
    assert r.status_code == 400
    assert "limit" in r.json()["detail"]


@pytest.mark.asyncio
async def test_export_finds_events_when_the_token_names_the_tenant_differently(
        client, session: AsyncSession):
    """`tenant_id ==` is the filter that matches nothing. Tenant columns are not
    one shape across this schema, which is what `_tenant_candidates` is for —
    an exact compare hands back an empty file that reads as an empty diary."""
    from case_service.calendar import service as cal_service
    from case_service.calendar.ics_import import parse_ics

    await _directory(session, "test-admin")
    session.add(CalendarEventModel(
        id=uuid.uuid4(), tenant_id="00000000-0000-0000-0000-0000000000aa",
        title="Under the other name", description="",
        starts_at=datetime(2026, 9, 2, 9, tzinfo=timezone.utc),
        ends_at=datetime(2026, 9, 2, 10, tzinfo=timezone.utc), timezone="UTC",
        organiser_user_id="test-admin", attendee_user_ids=["test-admin"],
        visibility="team", status="scheduled", created_by="test-admin"))
    await session.commit()

    async def _both(_session, _tenant):
        return ["default", "00000000-0000-0000-0000-0000000000aa"]

    original = cal_service._tenant_candidates
    cal_service._tenant_candidates = _both
    try:
        r = await client.get("/api/v1/calendar/export", params={
            "start": "2026-09-01T00:00:00Z", "end": "2026-09-30T00:00:00Z"})
        assert [e.title for e in parse_ics(r.text).events] == ["Under the other name"]
    finally:
        cal_service._tenant_candidates = original


@pytest.mark.asyncio
async def test_export_refuses_absences_without_the_absence_privilege(
        client, session: AsyncSession, monkeypatch):
    """The two role sets are identical today, but a privilege granted from data
    can separate them — and then this would be the way to read absences without
    `calendar.absences.read`."""
    from velaris_shared import hxguard

    await _directory(session, "test-admin")
    real = hxguard.check

    async def _deny_absences(session_, subject, action, resource=None):
        if action == "calendar.absences.read":
            return hxguard.Decision(False, "denied for the test", "test")
        return await real(session_, subject, action, resource)

    monkeypatch.setattr(
        "case_service.api.routers.calendar.hxguard.check", _deny_absences)
    r = await client.get("/api/v1/calendar/export", params={
        "start": "2026-09-01T00:00:00Z", "end": "2026-09-30T00:00:00Z"})
    assert r.status_code == 403
    assert "calendar.absences.read" in r.json()["detail"]

    ok = await client.get("/api/v1/calendar/export", params={
        "start": "2026-09-01T00:00:00Z", "end": "2026-09-30T00:00:00Z",
        "include_absences": "false"})
    assert ok.status_code == 200


@pytest.mark.asyncio
async def test_imported_leave_with_an_impossible_range_is_skipped_not_committed(
        session: AsyncSession):
    """A feed is somebody else's data. A range the schema's CHECK would refuse
    must be skipped here, not become a 500 at commit that loses the refresh."""
    from case_service.calendar import service as cal_service
    from case_service.calendar.ics_import import apply_import, parse_ics

    def _always_bad(row):
        raise cal_service.CalendarError("leave may not span a year")

    original = cal_service.validate_absence
    cal_service.validate_absence = _always_bad
    try:
        ooo = _ics(extra="X-MICROSOFT-CDO-BUSYSTATUS:OOF\r\n")
        out = await apply_import(session, parse_ics(ooo), tenant_id="default",
                                 user_id="alice", actor="alice", dry_run=False)
        await session.commit()
    finally:
        cal_service.validate_absence = original

    rows = (await session.execute(select(UserAbsenceModel))).scalars().all()
    assert rows == []
    assert out["absences_requested"] == 0
    assert any("leave refused" in s["reason"] for s in out["skipped"])


@pytest.mark.asyncio
async def test_export_does_not_carry_a_past_meeting_that_is_not_recurring(
        client, session: AsyncSession):
    """`recurrence` holds a JSON **null** on rows saved without one, and
    `IS NOT NULL` is true for that — so the loose SQL filter that exists to
    catch a series beginning before the window also drags in ordinary past
    meetings. Found live: a September export came back holding August."""
    from case_service.calendar.ics_import import parse_ics

    await _directory(session, "test-admin")
    for title, start, rule in (
        ("August one-off", datetime(2026, 8, 19, 8, tzinfo=timezone.utc), None),
        ("August weekly",  datetime(2026, 8, 19, 8, tzinfo=timezone.utc),
         {"freq": "weekly", "interval": 1}),
        ("September one",  datetime(2026, 9, 9, 8, tzinfo=timezone.utc), None),
    ):
        session.add(CalendarEventModel(
            id=uuid.uuid4(), tenant_id="default", title=title, description="",
            starts_at=start, ends_at=start.replace(hour=9), timezone="UTC",
            organiser_user_id="test-admin", attendee_user_ids=["test-admin"],
            visibility="team", status="scheduled", created_by="test-admin",
            recurrence=rule))
    await session.commit()

    r = await client.get("/api/v1/calendar/export", params={
        "start": "2026-09-01T00:00:00Z", "end": "2026-09-30T00:00:00Z"})
    titles = sorted(e.title for e in parse_ics(r.text).events)
    # The weekly series belongs — it still happens in September, and ICS carries
    # a series with its ORIGINAL start plus the rule. The one-off does not.
    assert titles == ["August weekly", "September one"]
