"""Calendar phase 1 — absences, working patterns, and the availability resolver.

The point of the feature is not the calendar screen. It is that until now
nothing on this platform knew when a *person* was available: `business_calendars`
describes the organisation, and `assignment_router` picked people who were on
holiday because "is she in today?" had no answer to give.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from velaris_shared.db.models import (
    BusinessCalendarModel, UserAbsenceModel, UserDirectoryModel, UserWorkPatternModel,
)
from case_service.calendar import service

from tests.conftest import client, session  # type: ignore[attr-defined]

# A Wednesday at 10:00 UTC — inside every default pattern in these tests — and
# the Saturday of the same week, which is outside all of them.
#
# Computed, not pinned. These were `datetime(2026, 9, 2)` and `(2026, 9, 5)`
# until 2026-09-08, when that date passed and took this file red with it: an
# absence that has already ended is correctly absent from the listing, so the
# tests were asserting the past. See `tests/phase_calendar/__init__.py`.
from tests.phase_calendar import next_wednesday, saturday_after

WED_10 = next_wednesday(10)
SAT_10 = saturday_after(WED_10, 10)


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


async def _directory(session: AsyncSession, user_id: str, *, manager: str | None = None,
                     tz: str = "UTC", tenant: str = "default") -> UserDirectoryModel:
    row = UserDirectoryModel(
        id=uuid.uuid4(), user_id=user_id, display_name=user_id,
        email=f"{user_id}@example.com", manager_user_id=manager,
        timezone=tz, tenant_id=tenant, is_active=True,
    )
    session.add(row)
    await session.commit()
    return row


async def _absence(session: AsyncSession, user_id: str, start: datetime, end: datetime,
                   *, status: str = "approved", delegate: str | None = None,
                   kind: str = "holiday", tenant: str = "default") -> UserAbsenceModel:
    row = UserAbsenceModel(
        id=uuid.uuid4(), tenant_id=tenant, user_id=user_id, kind=kind,
        starts_at=start, ends_at=end, all_day=True, status=status,
        delegate_user_id=delegate, source="manual", note="private detail",
        created_by="tester",
        approved_by="somebody-else" if status == "approved" else None,
    )
    session.add(row)
    await session.commit()
    return row


# ── the resolver ─────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_available_by_default(session: AsyncSession):
    """No pattern and no absence means available during ordinary hours — the
    state almost everybody is in."""
    assert await service.available_at(session, "alice", WED_10, tenant_id="default")


@pytest.mark.asyncio
async def test_outside_working_hours_is_not_available(session: AsyncSession):
    assert not await service.available_at(session, "alice", SAT_10, tenant_id="default")
    assert not await service.available_at(
        session, "alice", WED_10.replace(hour=3), tenant_id="default")


@pytest.mark.asyncio
async def test_an_approved_absence_makes_someone_unavailable(session: AsyncSession):
    await _absence(session, "alice", WED_10 - timedelta(days=1), WED_10 + timedelta(days=1))
    assert not await service.available_at(session, "alice", WED_10, tenant_id="default")


@pytest.mark.asyncio
async def test_a_requested_absence_changes_nothing(session: AsyncSession):
    """Otherwise anybody could quietly stop their own queue by asking for leave
    they never take."""
    await _absence(session, "alice", WED_10 - timedelta(days=1), WED_10 + timedelta(days=1),
                   status="requested")
    assert await service.available_at(session, "alice", WED_10, tenant_id="default")


@pytest.mark.asyncio
async def test_an_absence_that_started_last_week_still_counts(session: AsyncSession):
    """Overlap, not containment — a naive `starts_at BETWEEN` misses exactly the
    absence that matters today."""
    await _absence(session, "alice", WED_10 - timedelta(days=10), WED_10 + timedelta(hours=2))
    assert not await service.available_at(session, "alice", WED_10, tenant_id="default")


@pytest.mark.asyncio
async def test_a_cancelled_absence_does_not_count(session: AsyncSession):
    await _absence(session, "alice", WED_10 - timedelta(days=1), WED_10 + timedelta(days=1),
                   status="cancelled")
    assert await service.available_at(session, "alice", WED_10, tenant_id="default")


@pytest.mark.asyncio
async def test_working_pattern_overrides_the_default(session: AsyncSession):
    session.add(UserWorkPatternModel(
        id=uuid.uuid4(), tenant_id="default", user_id="nightshift",
        work_days=[1, 2, 3, 4, 5, 6], work_start_hour=22, work_end_hour=23,
        timezone="UTC", updated_by="admin",
    ))
    await session.commit()
    assert not await service.available_at(session, "nightshift", WED_10, tenant_id="default")
    assert await service.available_at(
        session, "nightshift", WED_10.replace(hour=22, minute=30), tenant_id="default")


@pytest.mark.asyncio
async def test_pattern_falls_back_through_its_business_calendar(session: AsyncSession):
    """A pattern row that sets only a timezone must not force the rest to be
    re-entered."""
    cal = BusinessCalendarModel(
        id=uuid.uuid4(), name="Late Shift", timezone="UTC",
        work_days=[1, 2, 3, 4, 5], work_start_hour=12, work_end_hour=20, holidays=[],
    )
    session.add(cal)
    await session.commit()
    session.add(UserWorkPatternModel(
        id=uuid.uuid4(), tenant_id="default", user_id="late", updated_by="admin",
        business_calendar_id=cal.id, timezone="UTC",
    ))
    await session.commit()

    pattern = await service.working_pattern(session, "late", tenant_id="default")
    assert (pattern.start_hour, pattern.end_hour) == (12, 20)
    assert not await service.available_at(session, "late", WED_10, tenant_id="default")


@pytest.mark.asyncio
async def test_the_persons_own_timezone_decides(session: AsyncSession):
    """09:00 in Sydney is not 09:00 in London, and a pattern is worthless if it
    is evaluated in the server's zone."""
    session.add(UserWorkPatternModel(
        id=uuid.uuid4(), tenant_id="default", user_id="sydney", updated_by="admin",
        work_days=[1, 2, 3, 4, 5], work_start_hour=9, work_end_hour=17,
        timezone="Australia/Sydney",
    ))
    await session.commit()
    # 23:00 UTC Wednesday is 09:00 Thursday in Sydney — a working hour there.
    assert await service.available_at(
        session, "sydney", WED_10.replace(hour=23), tenant_id="default")
    # 10:00 UTC is 20:00 in Sydney — outside the day.
    assert not await service.available_at(session, "sydney", WED_10, tenant_id="default")


@pytest.mark.asyncio
async def test_available_users_filters_the_list(session: AsyncSession):
    await _absence(session, "away", WED_10 - timedelta(days=1), WED_10 + timedelta(days=1))
    free = await service.available_users(
        session, ["here", "away", "also-here"], WED_10, tenant_id="default")
    assert free == ["here", "also-here"]


@pytest.mark.asyncio
async def test_a_delegate_covers_but_only_if_they_are_in(session: AsyncSession):
    await _absence(session, "alice", WED_10 - timedelta(days=1), WED_10 + timedelta(days=1),
                   delegate="bob")
    assert await service.delegate_for(session, "alice", WED_10, tenant_id="default") == "bob"

    # bob goes away too — cover that is also on holiday is not cover
    await _absence(session, "bob", WED_10 - timedelta(hours=1), WED_10 + timedelta(hours=1))
    assert await service.delegate_for(session, "alice", WED_10, tenant_id="default") is None


@pytest.mark.asyncio
async def test_delegation_is_followed_exactly_one_step(session: AsyncSession):
    """If A delegates to B and B to C, work does not silently land on C —
    nobody agreed to that."""
    await _absence(session, "a", WED_10 - timedelta(days=1), WED_10 + timedelta(days=1),
                   delegate="b")
    await _absence(session, "b", WED_10 - timedelta(days=1), WED_10 + timedelta(days=1),
                   delegate="c")
    assert await service.delegate_for(session, "a", WED_10, tenant_id="default") is None


@pytest.mark.asyncio
async def test_another_tenants_absence_is_invisible(session: AsyncSession):
    await _absence(session, "alice", WED_10 - timedelta(days=1), WED_10 + timedelta(days=1),
                   tenant="other-tenant")
    assert await service.available_at(session, "alice", WED_10, tenant_id="default")


@pytest.mark.asyncio
async def test_the_resolver_fails_open(session: AsyncSession, monkeypatch):
    """A calendar outage must not stop work being assigned. Guessing 'absent'
    fills every queue while everybody looks idle."""
    async def boom(*a, **k):
        raise RuntimeError("database on fire")

    monkeypatch.setattr(service, "absences_between", boom)
    assert await service.available_at(session, "alice", WED_10, tenant_id="default")
    assert await service.available_users(session, ["alice"], WED_10, tenant_id="default") == ["alice"]


# ── validation ───────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_validation_refuses_the_obvious_mistakes(session: AsyncSession):
    def probe(**over):
        row = UserAbsenceModel(
            tenant_id="default", user_id="alice", kind="holiday",
            starts_at=WED_10, ends_at=WED_10 + timedelta(days=1),
            source="manual", created_by="t", status="requested",
        )
        for k, v in over.items():
            setattr(row, k, v)
        return row

    for over, fragment in (
        ({"ends_at": WED_10 - timedelta(hours=1)}, "end after it starts"),
        ({"kind": "sabbatical"}, "kind must be one of"),
        ({"delegate_user_id": "alice"}, "cover for themselves"),
        ({"ends_at": WED_10 + timedelta(days=400)}, "leaver"),
    ):
        with pytest.raises(service.CalendarError) as exc:
            service.validate_absence(probe(**over))
        assert fragment in str(exc.value)


@pytest.mark.asyncio
async def test_leave_cannot_be_approved_by_the_person_taking_it(session: AsyncSession):
    row = await _absence(session, "alice", WED_10, WED_10 + timedelta(days=1),
                         status="requested")
    with pytest.raises(service.CalendarError) as exc:
        await service.approve(session, row, approver="alice")
    assert "somebody other than the person taking it" in str(exc.value)

    await service.approve(session, row, approver="bob")
    assert row.status == "approved" and row.approved_by == "bob"


# ── the API ──────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_absence_round_trip(client: AsyncClient, session: AsyncSession):
    await _directory(session, "alice")          # the directory is the vocabulary
    r = await client.post("/api/v1/calendar/absences", json={
        "user_id": "alice", "kind": "holiday",
        "starts_at": WED_10.isoformat(),
        "ends_at": (WED_10 + timedelta(days=2)).isoformat(),
    })
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["status"] == "requested"

    listed = await client.get("/api/v1/calendar/absences")
    assert any(a["id"] == body["id"] for a in listed.json())

    # The same person cannot record leave and then approve it — this admin
    # created the row, so a second one has to sign it off.
    mine = await client.post(f"/api/v1/calendar/absences/{body['id']}/approve")
    assert mine.status_code == 409

    ok = await client.post(f"/api/v1/calendar/absences/{body['id']}/approve",
                           headers=_headers_for(["admin", "superadmin"],
                                                username="second-admin"))
    assert ok.status_code == 200 and ok.json()["status"] == "approved"

    gone = await client.post(f"/api/v1/calendar/absences/{body['id']}/cancel")
    assert gone.status_code == 200 and gone.json()["status"] == "cancelled"


@pytest.mark.asyncio
async def test_overlapping_absences_are_refused(client: AsyncClient, session: AsyncSession):
    await _directory(session, "alice")
    payload = {
        "user_id": "alice",
        "starts_at": WED_10.isoformat(),
        "ends_at": (WED_10 + timedelta(days=2)).isoformat(),
    }
    assert (await client.post("/api/v1/calendar/absences", json=payload)).status_code == 201
    clash = await client.post("/api/v1/calendar/absences", json={
        **payload, "starts_at": (WED_10 + timedelta(days=1)).isoformat(),
        "ends_at": (WED_10 + timedelta(days=3)).isoformat(),
    })
    assert clash.status_code == 409 and "overlaps" in clash.json()["detail"]


@pytest.mark.asyncio
async def test_a_colleague_sees_busy_but_never_why(client: AsyncClient, session: AsyncSession):
    """"Sick" is health data. A team calendar that shows it is a compliance
    incident with a friendly icon."""
    await _directory(session, "alice")
    await _absence(session, "alice", WED_10, WED_10 + timedelta(days=1), kind="sick")

    colleague = _headers_for(["case_worker"])
    rows = (await client.get("/api/v1/calendar/absences", headers=colleague)).json()
    mine = [a for a in rows if a["user_id"] == "alice"]
    assert mine, "the absence should still be visible as busy"
    assert "kind" not in mine[0] and "note" not in mine[0]

    # an admin sees the whole record
    rows = (await client.get("/api/v1/calendar/absences")).json()
    mine = [a for a in rows if a["user_id"] == "alice"]
    assert mine[0]["kind"] == "sick" and mine[0]["note"] == "private detail"


@pytest.mark.asyncio
async def test_a_case_worker_cannot_book_a_colleagues_leave(client: AsyncClient, session: AsyncSession):
    """Booking somebody else's holiday is a quiet way to reroute their work."""
    await _directory(session, "alice")
    r = await client.post("/api/v1/calendar/absences",
                          headers=_headers_for(["case_worker"]), json={
                              "user_id": "alice",
                              "starts_at": WED_10.isoformat(),
                              "ends_at": (WED_10 + timedelta(days=1)).isoformat(),
                          })
    assert r.status_code == 403


@pytest.mark.asyncio
async def test_a_manager_may_approve_their_reports_leave(client: AsyncClient, session: AsyncSession):
    manager_id = str(uuid.uuid4())
    await _directory(session, "report", manager=manager_id)
    row = await _absence(session, "report", WED_10, WED_10 + timedelta(days=1),
                         status="requested")

    outsider = await client.post(f"/api/v1/calendar/absences/{row.id}/approve",
                                 headers=_headers_for(["manager"]))
    assert outsider.status_code == 403

    theirs = await client.post(f"/api/v1/calendar/absences/{row.id}/approve",
                               headers=_headers_for(["manager"], user_id=manager_id))
    assert theirs.status_code == 200


@pytest.mark.asyncio
async def test_availability_endpoint_answers_busy_with_a_delegate(client: AsyncClient, session: AsyncSession):
    await _absence(session, "alice", WED_10 - timedelta(days=1), WED_10 + timedelta(days=1),
                   delegate="bob")
    r = await client.get("/api/v1/calendar/availability",
                         params={"user_ids": ["alice", "bob"], "at": WED_10.isoformat()})
    assert r.status_code == 200
    by_user = {u["user_id"]: u for u in r.json()["users"]}
    assert by_user["alice"]["available"] is False
    assert by_user["alice"]["delegate_user_id"] == "bob"
    assert by_user["bob"]["available"] is True
    assert "kind" not in by_user["alice"]


@pytest.mark.asyncio
async def test_patterns_are_admin_only(client: AsyncClient):
    """Contracted hours are not a preference."""
    body = {"user_id": "alice", "work_days": [1, 2, 3], "work_start_hour": 8,
            "work_end_hour": 16, "timezone": "Europe/London"}
    denied = await client.put("/api/v1/calendar/patterns",
                              headers=_headers_for(["manager"]), json=body)
    assert denied.status_code == 403

    ok = await client.put("/api/v1/calendar/patterns", json=body)
    assert ok.status_code == 200
    read = await client.get("/api/v1/calendar/patterns/alice")
    assert read.json()["work_start_hour"] == 8 and read.json()["timezone"] == "Europe/London"


@pytest.mark.asyncio
async def test_a_bad_working_day_is_refused(client: AsyncClient):
    r = await client.put("/api/v1/calendar/patterns", json={
        "user_id": "alice", "work_days": [0, 8]})
    assert r.status_code == 400


@pytest.mark.asyncio
async def test_another_tenants_absence_is_absent_not_forbidden(client: AsyncClient, session: AsyncSession):
    row = await _absence(session, "alice", WED_10, WED_10 + timedelta(days=1),
                         status="requested", tenant="other-tenant")
    assert (await client.post(f"/api/v1/calendar/absences/{row.id}/approve")).status_code == 404
    assert (await client.post(f"/api/v1/calendar/absences/{row.id}/cancel")).status_code == 404
    listed = (await client.get("/api/v1/calendar/absences")).json()
    assert all(a["id"] != str(row.id) for a in listed)


@pytest.mark.asyncio
async def test_a_viewer_cannot_reach_the_calendar(client: AsyncClient):
    viewer = _headers_for(["viewer"])
    for method, url in (("GET", "/api/v1/calendar/absences"),
                        ("POST", "/api/v1/calendar/absences"),
                        ("GET", "/api/v1/calendar/availability?user_ids=alice"),
                        ("PUT", "/api/v1/calendar/patterns")):
        r = await client.request(method, url, headers=viewer,
                                 json={} if method in ("POST", "PUT") else None)
        assert r.status_code == 403, f"{method} {url} answered {r.status_code}"


# ── audit pass (2026-08-21) ──────────────────────────────────────────────

@pytest.mark.asyncio
async def test_leave_is_filed_under_the_directory_id_not_the_token_subject(
    client: AsyncClient, session: AsyncSession,
):
    """The one that would have made the whole feature inert.

    `auth_real` mints tokens whose `sub` is a UUID, while the SSO sync and the
    admin screens write a username into `user_directory.user_id` — and that is
    what assignment matches an assignee against. Filing leave under the UUID
    would look right on the screen and change **nothing** about who gets the
    work.
    """
    from velaris_shared.db.models import UserAbsenceModel
    from sqlalchemy import select as _select

    uid = str(uuid.uuid4())
    await _directory(session, "j.smith")          # directory keys on the username

    headers = _headers_for(["manager"], user_id=uid, username="j.smith")
    r = await client.post("/api/v1/calendar/absences", headers=headers, json={
        "starts_at": WED_10.isoformat(),
        "ends_at": (WED_10 + timedelta(days=1)).isoformat(),
    })
    assert r.status_code == 201, r.text
    assert r.json()["user_id"] == "j.smith", "filed under the token subject, not the directory"

    row = (await session.execute(_select(UserAbsenceModel))).scalars().one()
    assert row.user_id == "j.smith"


@pytest.mark.asyncio
async def test_an_unbounded_read_window_is_refused(client: AsyncClient):
    """Both list endpoints scan an index over (tenant, starts_at, ends_at).
    `start=1970` and `end=2999` is a full table scan any caller could ask for."""
    for path in ("/api/v1/calendar/absences", "/api/v1/calendar/events"):
        r = await client.get(path, params={
            "start": "1970-01-01T00:00:00Z", "end": "2999-01-01T00:00:00Z"})
        assert r.status_code == 400, f"{path} answered {r.status_code}"
        assert "days or fewer" in r.json()["detail"]


@pytest.mark.asyncio
async def test_values_the_column_cannot_hold_are_refused_not_500(client: AsyncClient):
    """The database would otherwise raise — a 500 for what is plainly a bad
    request — and on MySQL, depending on the mode, silently truncate instead."""
    long = "x" * 300
    r = await client.post("/api/v1/calendar/absences", json={
        "user_id": long,
        "starts_at": WED_10.isoformat(),
        "ends_at": (WED_10 + timedelta(days=1)).isoformat(),
    })
    assert r.status_code == 400

    note = await client.post("/api/v1/calendar/absences", json={
        "starts_at": WED_10.isoformat(),
        "ends_at": (WED_10 + timedelta(days=1)).isoformat(),
        "note": "y" * 5000,
    })
    assert note.status_code == 400


@pytest.mark.asyncio
async def test_an_unknown_timezone_is_refused_rather_than_silently_utc(client: AsyncClient):
    """A zone the server cannot resolve becomes UTC at read time — a wrong
    answer wearing a right one's clothes."""
    r = await client.put("/api/v1/calendar/patterns", json={
        "user_id": "alice", "timezone": "Mars/Olympus_Mons"})
    assert r.status_code == 400 and "not a timezone" in r.json()["detail"]

    ok = await client.put("/api/v1/calendar/patterns", json={
        "user_id": "alice", "timezone": "Asia/Kolkata"})
    assert ok.status_code == 200


@pytest.mark.asyncio
async def test_impossible_working_hours_are_refused(client: AsyncClient):
    for body in ({"user_id": "alice", "work_start_hour": 25},
                 {"user_id": "alice", "work_end_hour": 0},
                 {"user_id": "alice", "work_start_hour": 17, "work_end_hour": 9}):
        r = await client.put("/api/v1/calendar/patterns", json=body)
        assert r.status_code == 400, body


@pytest.mark.asyncio
async def test_leave_for_somebody_who_does_not_exist_is_refused(client: AsyncClient, session: AsyncSession):
    """A typo used to be stored happily: assignment matches
    `user_directory.user_id`, so leave for `alcie` changes nothing at all and
    looks perfectly fine on the screen."""
    r = await client.post("/api/v1/calendar/absences", json={
        "user_id": "alcie",
        "starts_at": WED_10.isoformat(),
        "ends_at": (WED_10 + timedelta(days=1)).isoformat(),
    })
    assert r.status_code == 400 and "not a known user_id" in r.json()["detail"]


@pytest.mark.asyncio
async def test_a_delegate_must_exist_too(client: AsyncClient, session: AsyncSession):
    await _directory(session, "alice")
    r = await client.post("/api/v1/calendar/absences", json={
        "user_id": "alice", "delegate_user_id": "ghost",
        "starts_at": WED_10.isoformat(),
        "ends_at": (WED_10 + timedelta(days=1)).isoformat(),
    })
    assert r.status_code == 400 and "delegate" in r.json()["detail"]


@pytest.mark.asyncio
async def test_another_tenants_person_is_not_a_known_user(client: AsyncClient, session: AsyncSession):
    """And the refusal is worded identically to the unknown case: whether
    somebody exists in another tenant is not this caller's business."""
    await _directory(session, "theirs", tenant="other-tenant")
    r = await client.post("/api/v1/calendar/absences", json={
        "user_id": "theirs",
        "starts_at": WED_10.isoformat(),
        "ends_at": (WED_10 + timedelta(days=1)).isoformat(),
    })
    assert r.status_code == 400 and "not a known user_id" in r.json()["detail"]
