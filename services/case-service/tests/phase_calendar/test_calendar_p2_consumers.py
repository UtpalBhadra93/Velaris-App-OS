"""Calendar phase 2 — the consumers. This is where the calendar pays.

Phase 1 recorded who is away. Nothing read it, so `assignment_router` still
picked people on holiday and escalation still landed on their desk. These tests
pin the integration, and above all pin the distinction that decides its shape:

    an **absence** is an explicit, approved fact somebody entered;
    a **working pattern** is an assumption nobody chose.

Only the fact belongs in a decision about who *owns* work. Filtering assignment
by working hours would mean a case arriving at 22:00 is assigned to nobody, and
every 24/7 deployment that never configured patterns would silently queue
everything overnight.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest

from case_service.core.assignment_router import _RESOLVERS
from velaris_shared.db.models import (
    CaseAssignmentModel, UserAbsenceModel, UserDirectoryModel,
)

pytestmark = pytest.mark.asyncio


async def _user(session, user_id: str, *, skills=None, manager=None, active=True):
    session.add(UserDirectoryModel(
        user_id=user_id, display_name=user_id, manager_user_id=manager,
        skills=skills or [], is_active=active,
    ))
    await session.flush()


async def _assign(session, user_id: str, n: int, *, when=None) -> None:
    for _ in range(n):
        session.add(CaseAssignmentModel(
            case_id=uuid.uuid4(), step_id="s1", assignee_type="user",
            assignee_id=user_id, status="active",
            assigned_at=when or datetime.now(timezone.utc),
        ))
    await session.flush()


async def _away(session, user_id: str, *, delegate=None, tenant="default",
                status="approved"):
    """On leave right now — the window straddles the moment the test runs."""
    now = datetime.now(timezone.utc)
    session.add(UserAbsenceModel(
        id=uuid.uuid4(), tenant_id=tenant, user_id=user_id, kind="holiday",
        starts_at=now - timedelta(days=1), ends_at=now + timedelta(days=1),
        all_day=True, status=status, delegate_user_id=delegate,
        source="manual", note="", created_by="tester",
        approved_by="someone" if status == "approved" else None,
    ))
    await session.flush()


# ── skill_based ──────────────────────────────────────────────────────────

async def test_skill_based_skips_the_specialist_on_holiday(session):
    await _user(session, "specialist", skills=["claims", "fraud"])
    await _user(session, "second", skills=["claims", "fraud"])
    await _away(session, "specialist")

    kind, target = await _RESOLVERS["skill_based"](
        session, {"skill_requirements": ["claims", "fraud"], "target": "q"}, {})
    assert (kind, target) == ("user", "second")


async def test_skill_based_falls_to_the_queue_when_everyone_qualified_is_away(session):
    await _user(session, "only-one", skills=["claims"])
    await _away(session, "only-one")

    kind, target = await _RESOLVERS["skill_based"](
        session, {"skill_requirements": ["claims"], "target": "q"}, {})
    assert (kind, target) == ("queue", "q")


async def test_a_requested_absence_does_not_move_work(session):
    """Otherwise asking for leave would quietly empty your own queue."""
    await _user(session, "asked", skills=["claims"])
    await _away(session, "asked", status="requested")

    kind, target = await _RESOLVERS["skill_based"](
        session, {"skill_requirements": ["claims"], "target": "q"}, {})
    assert (kind, target) == ("user", "asked")


async def test_another_tenants_absence_does_not_move_work(session):
    await _user(session, "theirs", skills=["claims"])
    await _away(session, "theirs", tenant="other-tenant")

    kind, target = await _RESOLVERS["skill_based"](
        session, {"skill_requirements": ["claims"], "target": "q"}, {})
    assert (kind, target) == ("user", "theirs")


# ── least_loaded / round_robin ───────────────────────────────────────────

async def test_least_loaded_skips_the_person_on_leave(session):
    """Somebody on holiday has the lightest load of all, which is exactly why
    this needs the filter."""
    await _user(session, "away")
    await _user(session, "busy")
    await _assign(session, "away", 1)
    await _assign(session, "busy", 5)
    await _away(session, "away")

    kind, target = await _RESOLVERS["least_loaded"](session, {"target": "r"}, {})
    assert (kind, target) == ("user", "busy")


async def test_least_loaded_falls_back_to_the_role_when_all_are_away(session):
    await _user(session, "a")
    await _assign(session, "a", 1)
    await _away(session, "a")

    kind, target = await _RESOLVERS["least_loaded"](session, {"target": "claims-team"}, {})
    assert (kind, target) == ("role", "claims-team")


async def test_round_robin_moves_on_to_the_next_person(session):
    """The point of round robin is the next person, not an empty desk."""
    now = datetime.now(timezone.utc)
    await _user(session, "oldest")
    await _user(session, "newer")
    await _assign(session, "oldest", 1, when=now - timedelta(days=5))
    await _assign(session, "newer", 1, when=now - timedelta(days=1))
    await _away(session, "oldest")

    kind, target = await _RESOLVERS["round_robin"](session, {"target": "r"}, {})
    assert (kind, target) == ("user", "newer")


# ── named targets and cover ──────────────────────────────────────────────

async def test_a_named_user_hands_over_to_their_cover(session):
    await _user(session, "alice")
    await _user(session, "bob")
    await _away(session, "alice", delegate="bob")

    kind, target = await _RESOLVERS["specific_user"](session, {"target": "alice"}, {})
    assert (kind, target) == ("user", "bob")


async def test_a_named_user_with_no_cover_still_gets_it(session):
    """A named target is somebody's decision. Rerouting it to a queue would lose
    the reason it was named."""
    await _user(session, "alice")
    await _away(session, "alice")

    kind, target = await _RESOLVERS["specific_user"](session, {"target": "alice"}, {})
    assert (kind, target) == ("user", "alice")


async def test_manager_of_hands_over_to_the_managers_cover(session):
    await _user(session, "worker", manager="boss")
    await _user(session, "boss")
    await _user(session, "deputy")
    await _away(session, "boss", delegate="deputy")

    kind, target = await _RESOLVERS["manager_of"](
        session, {}, {"current_assignee_id": "worker"})
    assert (kind, target) == ("user", "deputy")


async def test_working_hours_never_move_an_assignment(session):
    """The load-bearing distinction. A pattern that says "nights off" must not
    stop work being assigned at night — that is a guess about a desk, not a fact
    about ownership."""
    from velaris_shared.db.models import UserWorkPatternModel

    await _user(session, "nightless", skills=["claims"])
    session.add(UserWorkPatternModel(
        id=uuid.uuid4(), tenant_id="default", user_id="nightless",
        work_days=[1], work_start_hour=0, work_end_hour=1, timezone="UTC",
        updated_by="admin",
    ))
    await session.flush()

    kind, target = await _RESOLVERS["skill_based"](
        session, {"skill_requirements": ["claims"], "target": "q"}, {})
    assert (kind, target) == ("user", "nightless")


async def test_assignment_survives_a_broken_calendar(session, monkeypatch):
    """A calendar outage must never stop work being assigned."""
    from case_service.calendar import service as calendar

    async def boom(*a, **k):
        raise RuntimeError("calendar down")

    monkeypatch.setattr(calendar, "not_absent", boom)
    await _user(session, "someone", skills=["claims"])

    kind, target = await _RESOLVERS["skill_based"](
        session, {"skill_requirements": ["claims"], "target": "q"}, {})
    assert (kind, target) == ("user", "someone")


# ── escalation ───────────────────────────────────────────────────────────

async def test_escalation_reaches_the_cover_not_the_empty_desk(session):
    from case_service.core.sla_escalation import resolve_dynamic_target

    await _user(session, "alice")
    await _user(session, "bob")
    await _away(session, "alice", delegate="bob")

    kind, target = await resolve_dynamic_target(
        session, uuid.uuid4(), "user", "alice", "default")
    assert (kind, target) == ("user", "bob")


async def test_escalation_with_no_cover_still_reaches_the_person(session):
    """The next level of the tree is what handles silence — swapping in a whole
    queue would lose the reason they were named."""
    from case_service.core.sla_escalation import resolve_dynamic_target

    await _user(session, "alice")
    await _away(session, "alice")

    kind, target = await resolve_dynamic_target(
        session, uuid.uuid4(), "user", "alice", "default")
    assert (kind, target) == ("user", "alice")


async def test_escalation_to_the_current_assignees_manager_uses_cover(session):
    from case_service.core.sla_escalation import resolve_dynamic_target
    from velaris_shared.db.models import CaseInstanceModel

    await _user(session, "worker", manager="boss")
    await _user(session, "boss")
    await _user(session, "deputy")
    await _away(session, "boss", delegate="deputy")

    case_id = uuid.uuid4()
    session.add(CaseAssignmentModel(
        case_id=case_id, step_id="s1", assignee_type="user",
        assignee_id="worker", status="active",
        assigned_at=datetime.now(timezone.utc),
    ))
    await session.flush()

    kind, target = await resolve_dynamic_target(
        session, case_id, "manager_of_current_assignee", None, "default")
    assert (kind, target) == ("user", "deputy")
