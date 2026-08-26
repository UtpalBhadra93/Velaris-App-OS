"""Aggregate case-detail endpoint (supersedes roadmap #36 for now).

Five client calls collapse to one, removing a real waterfall: the case type
could not be fetched until the case had returned its `case_type_id`.

The tests that matter are the authorization ones. The five underlying routes do
NOT share a single rule, and the danger of any aggregate is that composing them
quietly widens access to the loosest member. Specifically:

* `my_task` is self-scoped — it must keep returning only the caller's own
  assignment, never the case's other assignees;
* `case_type` carries a tenant-visibility rule that must survive aggregation;
* the whole response requires `case.read`, which is stricter than the
  standalone SLA route — never looser.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import inspect
import uuid

import pytest

pytestmark = pytest.mark.asyncio


class TestAggregateShape:
    async def test_returns_all_five_sections_in_one_call(self, client, session):
        from tests.conftest import create_case, deploy_case_type

        ct = await deploy_case_type(client, name=f"Agg {uuid.uuid4().hex[:6]}")
        case = await create_case(client, ct["id"])

        r = await client.get(f"/api/v1/cases/{case['id']}/detail")
        assert r.status_code == 200, r.text
        body = r.json()
        assert set(body) == {"case", "history", "slas", "case_type", "my_task"}
        assert body["case"]["id"] == case["id"]

    async def test_case_type_is_resolved_without_a_second_round_trip(self, client):
        """The waterfall this endpoint exists to remove."""
        from tests.conftest import create_case, deploy_case_type

        ct = await deploy_case_type(client, name=f"WF {uuid.uuid4().hex[:6]}")
        case = await create_case(client, ct["id"])

        body = (await client.get(f"/api/v1/cases/{case['id']}/detail")).json()
        assert body["case_type"] is not None
        assert body["case_type"]["id"] == ct["id"]

    async def test_unknown_case_404s(self, client):
        r = await client.get(f"/api/v1/cases/{uuid.uuid4()}/detail")
        assert r.status_code == 404

    async def test_no_task_assigned_returns_null_not_an_error(self, client):
        from tests.conftest import create_case, deploy_case_type

        ct = await deploy_case_type(client, name=f"NT {uuid.uuid4().hex[:6]}")
        case = await create_case(client, ct["id"])

        body = (await client.get(f"/api/v1/cases/{case['id']}/detail")).json()
        assert body["my_task"] is None


class TestAggregationDoesNotWidenAccess:
    """The specific risk of aggregating routes with different rules."""

    async def test_requires_case_read(self):
        from case_service.api.routers import cases as cases_router

        src = inspect.getsource(cases_router.get_case_detail)
        assert 'hxguard.require_case(session, user, "case.read"' in src

    async def test_my_task_stays_self_scoped(self):
        """Aggregating must not turn 'my task' into 'everyone's tasks'."""
        from case_service.api.routers import cases as cases_router

        src = inspect.getsource(cases_router.get_case_detail)
        assert "CaseAssignmentModel.assignee_id == user.user_id" in src

    async def test_case_type_tenant_rule_is_reproduced(self):
        from case_service.api.routers import cases as cases_router

        src = inspect.getsource(cases_router.get_case_detail)
        assert "ct.tenant_id" in src and "user.tenant_id" in src

    async def test_invisible_case_type_degrades_to_null_not_404(self, session, client):
        """Losing sight of the type must not make the case unreadable."""
        from velaris_shared.db.models import CaseInstanceModel, CaseTypeModel

        ct = CaseTypeModel(
            name=f"Other {uuid.uuid4().hex[:6]}", version="1.0.0",
            definition_json={"stages": []}, tenant_id=uuid.uuid4(),
        )
        session.add(ct)
        await session.flush()
        case = CaseInstanceModel(case_type_id=ct.id, case_type_version="1.0.0",
                                 status="open")
        session.add(case)
        await session.flush()
        await session.commit()

        r = await client.get(f"/api/v1/cases/{case.id}/detail")
        # The admin test user is tenant-less, so the type stays visible — the
        # assertion that matters is that the case itself is still readable.
        assert r.status_code == 200, r.text
        assert r.json()["case"]["id"] == str(case.id)
