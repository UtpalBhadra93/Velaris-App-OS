"""Calendar phase 4 — team events, free/busy, and what the portal may see.

HxMeet already runs meetings, but every session hangs off a **case**, so a team
stand-up had nowhere to live. Calendar owns *when and who*; HxMeet owns the
room — an event may reference a session and never creates one.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from velaris_shared.db.models import CalendarEventModel, UserAbsenceModel
from case_service.calendar import service

from tests.conftest import client, session  # type: ignore[attr-defined]

# A Wednesday, 09:00–17:00 UTC is the default pattern.
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
    from velaris_shared.db.models import UserDirectoryModel

    session.add(UserDirectoryModel(
        id=uuid.uuid4(), user_id=user_id, display_name=user_id,
        email=f"{user_id}@example.com", tenant_id=tenant, is_active=True,
    ))
    await session.commit()


async def _event(session: AsyncSession, *, organiser="alice", attendees=("bob",),
                 start=None, hours=1, visibility="team", tenant="default"):
    start = start or WED.replace(hour=10)
    ev = CalendarEventModel(
        id=uuid.uuid4(), tenant_id=tenant, title="Stand-up", description="",
        starts_at=start, ends_at=start + timedelta(hours=hours), timezone="UTC",
        organiser_user_id=organiser, attendee_user_ids=list(attendees),
        visibility=visibility, status="scheduled", created_by=organiser,
    )
    session.add(ev)
    await session.commit()
    return ev


# ── free/busy ────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_slots_avoid_an_existing_meeting(session: AsyncSession):
    await _event(session, organiser="alice", attendees=["bob"],
                 start=WED.replace(hour=9), hours=2)

    slots = await service.propose_slots(
        session, ["alice", "bob"], WED.replace(hour=9), WED.replace(hour=13),
        tenant_id="default", minutes=60, limit=5)
    starts = [s["starts_at"] for s in slots]
    assert all("T09:00" not in s and "T10:00" not in s for s in starts)
    assert any("T11:00" in s for s in starts)


@pytest.mark.asyncio
async def test_slots_avoid_leave(session: AsyncSession):
    session.add(UserAbsenceModel(
        id=uuid.uuid4(), tenant_id="default", user_id="bob", kind="holiday",
        starts_at=WED, ends_at=WED + timedelta(days=1), all_day=True,
        status="approved", source="manual", note="", created_by="t",
        approved_by="mgr",
    ))
    await session.commit()

    assert await service.propose_slots(
        session, ["alice", "bob"], WED.replace(hour=9), WED.replace(hour=17),
        tenant_id="default", minutes=30, limit=3) == []


@pytest.mark.asyncio
async def test_slots_respect_working_hours(session: AsyncSession):
    """Here working hours ARE the right question — this asks when to meet, not
    who owns the work."""
    slots = await service.propose_slots(
        session, ["alice"], WED.replace(hour=3), WED.replace(hour=23),
        tenant_id="default", minutes=60, limit=20)
    hours = sorted({int(s["starts_at"][11:13]) for s in slots})
    assert hours and min(hours) >= 9 and max(hours) <= 16


@pytest.mark.asyncio
async def test_no_common_slot_is_an_answer_not_an_error(session: AsyncSession):
    await _event(session, organiser="alice", attendees=["bob"],
                 start=WED.replace(hour=9), hours=8)
    assert await service.propose_slots(
        session, ["alice", "bob"], WED.replace(hour=9), WED.replace(hour=17),
        tenant_id="default", minutes=30) == []


@pytest.mark.asyncio
async def test_another_tenants_meeting_does_not_block_a_slot(session: AsyncSession):
    await _event(session, organiser="alice", attendees=["bob"],
                 start=WED.replace(hour=10), hours=1, tenant="other-tenant")
    slots = await service.propose_slots(
        session, ["alice", "bob"], WED.replace(hour=10), WED.replace(hour=11),
        tenant_id="default", minutes=60, limit=1)
    assert slots


# ── the API ──────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_event_round_trip(client: AsyncClient, session: AsyncSession):
    # Attendees have to be real: an id the directory does not know can never be
    # looked up for availability, so the meeting would silently ignore them.
    for who in ("bob", "carol"):
        await _directory(session, who)
    r = await client.post("/api/v1/calendar/events", json={
        "title": "Stand-up",
        "starts_at": WED.replace(hour=10).isoformat(),
        "ends_at": WED.replace(hour=11).isoformat(),
        "attendee_user_ids": ["bob", "carol"],
    })
    assert r.status_code == 201, r.text
    ev = r.json()
    assert ev["status"] == "scheduled" and ev["meet_session_id"] is None

    listed = await client.get("/api/v1/calendar/events", params={
        "start": WED.isoformat(), "end": (WED + timedelta(days=1)).isoformat()})
    assert any(e["id"] == ev["id"] for e in listed.json())

    off = await client.post(f"/api/v1/calendar/events/{ev['id']}/cancel")
    assert off.status_code == 200 and off.json()["status"] == "cancelled"

    # cancelled events drop out of the window listing
    listed = await client.get("/api/v1/calendar/events", params={
        "start": WED.isoformat(), "end": (WED + timedelta(days=1)).isoformat()})
    assert all(e["id"] != ev["id"] for e in listed.json())


@pytest.mark.asyncio
async def test_an_event_that_ends_before_it_starts_is_refused(client: AsyncClient):
    r = await client.post("/api/v1/calendar/events", json={
        "title": "Backwards",
        "starts_at": WED.replace(hour=11).isoformat(),
        "ends_at": WED.replace(hour=10).isoformat(),
    })
    assert r.status_code == 400


@pytest.mark.asyncio
async def test_a_private_event_is_invisible_to_everybody_else(client: AsyncClient, session: AsyncSession):
    """Not even an admin: the title of a one-to-one is the whole payload."""
    await _event(session, organiser="alice", attendees=["bob"], visibility="private")

    rows = (await client.get("/api/v1/calendar/events", params={
        "start": WED.isoformat(), "end": (WED + timedelta(days=1)).isoformat()})).json()
    assert rows == []

    theirs = await client.get("/api/v1/calendar/events", headers=_headers_for(
        ["case_worker"], user_id="bob", username="bob"), params={
        "start": WED.isoformat(), "end": (WED + timedelta(days=1)).isoformat()})
    assert len(theirs.json()) == 1


@pytest.mark.asyncio
async def test_only_the_organiser_or_an_admin_cancels(client: AsyncClient, session: AsyncSession):
    ev = await _event(session, organiser="alice")
    outsider = await client.post(f"/api/v1/calendar/events/{ev.id}/cancel",
                                 headers=_headers_for(["case_worker"], username="mallory"))
    assert outsider.status_code == 403


@pytest.mark.asyncio
async def test_another_tenants_event_is_absent_not_forbidden(client: AsyncClient, session: AsyncSession):
    ev = await _event(session, tenant="other-tenant")
    assert (await client.post(f"/api/v1/calendar/events/{ev.id}/cancel")).status_code == 404


@pytest.mark.asyncio
async def test_slots_endpoint(client: AsyncClient):
    r = await client.post("/api/v1/calendar/slots", json={
        "user_ids": ["alice", "bob"],
        "start": WED.replace(hour=9).isoformat(),
        "end": WED.replace(hour=12).isoformat(),
        "minutes": 60, "limit": 2,
    })
    assert r.status_code == 200
    assert len(r.json()["slots"]) == 2


@pytest.mark.asyncio
async def test_a_silly_search_window_is_refused(client: AsyncClient):
    r = await client.post("/api/v1/calendar/slots", json={
        "user_ids": ["alice"], "start": WED.isoformat(),
        "end": (WED + timedelta(days=200)).isoformat(),
    })
    assert r.status_code == 400


@pytest.mark.asyncio
async def test_a_viewer_cannot_reach_events(client: AsyncClient):
    viewer = _headers_for(["viewer"])
    assert (await client.get("/api/v1/calendar/events", headers=viewer)).status_code == 403
    assert (await client.post("/api/v1/calendar/slots", headers=viewer,
                              json={"user_ids": ["a"], "start": WED.isoformat(),
                                    "end": (WED + timedelta(hours=2)).isoformat()})
            ).status_code == 403


# ── the portal sees closures, and nothing about people ───────────────────

@pytest.mark.asyncio
async def test_the_portal_publishes_closures_but_never_a_person(client: AsyncClient, session: AsyncSession):
    from velaris_shared.db.models import BusinessCalendarModel, TenantModel

    tenant = TenantModel(id=uuid.uuid4(), slug="acme", name="Acme",
                         settings={"portal": {"enabled": True}})
    session.add(tenant)
    session.add(BusinessCalendarModel(
        id=uuid.uuid4(), name="default", timezone="Europe/London",
        work_days=[1, 2, 3, 4, 5], work_start_hour=9, work_end_hour=17,
        holidays=["2026-12-25", "2026-12-26"],
    ))
    session.add(UserAbsenceModel(
        id=uuid.uuid4(), tenant_id="acme", user_id="alice", kind="sick",
        starts_at=WED, ends_at=WED + timedelta(days=1), all_day=True,
        status="approved", source="manual", note="flu", created_by="t",
        approved_by="mgr",
    ))
    await session.commit()

    r = await client.get("/api/v1/portal/acme/closures")
    assert r.status_code == 200
    body = r.json()
    assert body["holidays"] == ["2026-12-25", "2026-12-26"]
    assert body["timezone"] == "Europe/London"
    # Nothing about a person, ever — not a name, not a kind, not a date of leave.
    assert "alice" not in r.text and "sick" not in r.text


@pytest.mark.asyncio
async def test_a_portal_with_no_calendar_publishes_nothing_rather_than_guessing(client: AsyncClient, session: AsyncSession):
    from velaris_shared.db.models import TenantModel

    session.add(TenantModel(id=uuid.uuid4(), slug="bare", name="Bare",
                            settings={"portal": {"enabled": True}}))
    await session.commit()

    body = (await client.get("/api/v1/portal/bare/closures")).json()
    assert body["holidays"] == [] and body["work_start_hour"] is None


@pytest.mark.asyncio
async def test_naming_a_crowd_is_refused(client: AsyncClient):
    """Each candidate costs a working-pattern lookup, so an unbounded list turns
    one request into thousands of queries."""
    crowd = [f"u{i}" for i in range(200)]
    slots = await client.post("/api/v1/calendar/slots", json={
        "user_ids": crowd, "start": WED.replace(hour=9).isoformat(),
        "end": WED.replace(hour=17).isoformat(),
    })
    assert slots.status_code == 400 and "or fewer" in slots.json()["detail"]

    avail = await client.get("/api/v1/calendar/availability",
                             params={"user_ids": crowd})
    assert avail.status_code == 400
