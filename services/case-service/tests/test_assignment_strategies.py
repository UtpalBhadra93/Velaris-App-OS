"""Assignment router — `manager_of` and `skill_based`.

Both strategies were selectable in the Case Designer long before they worked:
each was a stub that logged its input and fell through to the fallback. These
tests exist to keep them honest, so the pair can never silently regress to
"looks configured, routes nowhere".

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest

from case_service.core.assignment_router import _RESOLVERS
from case_service.db.models import CaseAssignmentModel, UserDirectoryModel

pytestmark = pytest.mark.asyncio


async def _user(session, user_id: str, *, skills=None, manager=None, active=True):
    session.add(
        UserDirectoryModel(
            user_id=user_id,
            display_name=user_id,
            manager_user_id=manager,
            skills=skills or [],
            is_active=active,
        )
    )
    await session.flush()


async def _assign(session, user_id: str, n: int) -> None:
    """Give `user_id` n active assignments, so load ordering is testable."""
    for _ in range(n):
        session.add(
            CaseAssignmentModel(
                case_id=uuid.uuid4(),
                step_id="step-1",
                assignee_type="user",
                assignee_id=user_id,
                status="active",
            )
        )
    await session.flush()


class TestManagerOf:
    async def test_assigns_to_recorded_manager(self, session):
        await _user(session, "boss")
        await _user(session, "worker", manager="boss")

        kind, target = await _RESOLVERS["manager_of"](
            session, {}, {"current_assignee_id": "worker"}
        )
        assert (kind, target) == ("user", "boss")

    async def test_no_current_assignee_falls_back_to_role(self, session):
        kind, target = await _RESOLVERS["manager_of"](session, {}, {})
        assert (kind, target) == ("role", "manager")

    async def test_no_manager_recorded_falls_back_to_role(self, session):
        await _user(session, "orphan")
        kind, target = await _RESOLVERS["manager_of"](
            session, {}, {"current_assignee_id": "orphan"}
        )
        assert (kind, target) == ("role", "manager")

    async def test_inactive_manager_falls_back_to_role(self, session):
        """A stale pointer to a leaver must not park work on nobody."""
        await _user(session, "exboss", active=False)
        await _user(session, "worker2", manager="exboss")

        kind, target = await _RESOLVERS["manager_of"](
            session, {}, {"current_assignee_id": "worker2"}
        )
        assert (kind, target) == ("role", "manager")


class TestSkillBased:
    async def test_matches_user_holding_all_skills(self, session):
        await _user(session, "generalist", skills=["claims"])
        await _user(session, "specialist", skills=["claims", "fraud"])

        kind, target = await _RESOLVERS["skill_based"](
            session, {"skill_requirements": ["claims", "fraud"], "target": "q"}, {}
        )
        assert (kind, target) == ("user", "specialist")

    async def test_partial_match_is_not_a_match(self, session):
        await _user(session, "partial", skills=["claims"])

        kind, target = await _RESOLVERS["skill_based"](
            session, {"skill_requirements": ["claims", "fraud"], "target": "q"}, {}
        )
        assert (kind, target) == ("queue", "q")

    async def test_matching_is_case_insensitive(self, session):
        await _user(session, "shouty", skills=["CLAIMS"])

        kind, target = await _RESOLVERS["skill_based"](
            session, {"skill_requirements": ["Claims"], "target": "q"}, {}
        )
        assert (kind, target) == ("user", "shouty")

    async def test_no_requirements_falls_back_to_queue(self, session):
        """An unconfigured rule must not resolve to an arbitrary person."""
        await _user(session, "anyone", skills=["claims"])

        kind, target = await _RESOLVERS["skill_based"](
            session, {"skill_requirements": [], "target": "q"}, {}
        )
        assert (kind, target) == ("queue", "q")

    async def test_inactive_user_is_not_selected(self, session):
        await _user(session, "gone", skills=["claims"], active=False)

        kind, target = await _RESOLVERS["skill_based"](
            session, {"skill_requirements": ["claims"], "target": "q"}, {}
        )
        assert (kind, target) == ("queue", "q")

    async def test_picks_least_loaded_among_qualified(self, session):
        await _user(session, "busy", skills=["claims"])
        await _user(session, "free", skills=["claims"])
        await _assign(session, "busy", 3)
        await _assign(session, "free", 1)

        kind, target = await _RESOLVERS["skill_based"](
            session, {"skill_requirements": ["claims"], "target": "q"}, {}
        )
        assert (kind, target) == ("user", "free")

    async def test_unassigned_qualified_user_wins(self, session):
        """Someone with zero assignments has no load row at all."""
        await _user(session, "loaded", skills=["claims"])
        await _user(session, "idle", skills=["claims"])
        await _assign(session, "loaded", 2)

        kind, target = await _RESOLVERS["skill_based"](
            session, {"skill_requirements": ["claims"], "target": "q"}, {}
        )
        assert (kind, target) == ("user", "idle")


class TestDueAt:
    """`due_at` from the step's SLA policy (#41, the last slice)."""

    async def _case_with_policy(self, client, policies, step_sla_id):
        from tests.conftest import create_case, deploy_case_type

        ct = await deploy_case_type(
            client,
            name=f"SLA CT {uuid.uuid4().hex[:8]}",
            definition_json={"stages": [], "sla_policies": policies},
        )
        case = await create_case(client, ct["id"])
        return uuid.UUID(case["id"]), {"id": "s1", "sla_policy_id": step_sla_id}

    async def test_uses_goal_duration(self, session, client):
        from case_service.core.assignment_router import _due_at_for_step

        case_id, step = await self._case_with_policy(
            client,
            [{"id": "p1", "goal_duration": "PT4H", "deadline_duration": "PT8H"}],
            "p1",
        )
        due = await _due_at_for_step(session, case_id=case_id, step=step)
        assert due is not None
        delta = (due - datetime.now(timezone.utc)).total_seconds()
        assert 3.9 * 3600 < delta < 4.1 * 3600  # goal, not the 8h deadline

    async def test_falls_back_to_deadline_when_no_goal(self, session, client):
        from case_service.core.assignment_router import _due_at_for_step

        case_id, step = await self._case_with_policy(
            client, [{"id": "p2", "deadline_duration": "PT2H"}], "p2"
        )
        due = await _due_at_for_step(session, case_id=case_id, step=step)
        assert due is not None
        delta = (due - datetime.now(timezone.utc)).total_seconds()
        assert 1.9 * 3600 < delta < 2.1 * 3600

    async def test_no_sla_on_step_is_undated(self, session, client):
        from case_service.core.assignment_router import _due_at_for_step

        case_id, _ = await self._case_with_policy(client, [], None)
        assert await _due_at_for_step(session, case_id=case_id, step={"id": "s1"}) is None

    async def test_unknown_policy_id_is_undated(self, session, client):
        from case_service.core.assignment_router import _due_at_for_step

        case_id, step = await self._case_with_policy(
            client, [{"id": "p1", "goal_duration": "PT4H"}], "nope"
        )
        assert await _due_at_for_step(session, case_id=case_id, step=step) is None

    async def test_unparseable_duration_is_undated(self, session, client):
        """A malformed policy must not invent a due date."""
        from case_service.core.assignment_router import _due_at_for_step

        case_id, step = await self._case_with_policy(
            client, [{"id": "p3", "goal_duration": "banana"}], "p3"
        )
        assert await _due_at_for_step(session, case_id=case_id, step=step) is None
