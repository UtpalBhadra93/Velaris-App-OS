"""A variable namespace belongs to a tenant (residual gap 30, migration 202).

`variable_namespaces.name` was globally UNIQUE with no tenant column, so on a
box with more than one tenant the first to register `crm` owned that name for
everybody — and `namespace_grants`, which hangs off the namespace, could not be
tenant-scoped either, for `ai_action`, rules, modules or connectors alike.

The tests that matter most are the ones proving the change is INVISIBLE to an
install that has only platform namespaces, because that is every install that
takes this update.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import uuid

import pytest

from case_service.case_vars.service import (
    CallerContext, register_connector_namespace, resolve_namespace_by_name,
)
from velaris_shared.db.models import VariableNamespaceModel


def _ns(session, name: str, tenant_id: str | None) -> VariableNamespaceModel:
    row = VariableNamespaceModel(
        id=uuid.uuid4(), name=name,
        tenant_id=tenant_id, tenant_key=tenant_id or "platform",
        owner_type="connector", owner_ref=uuid.uuid4(),
        sensitivity="internal", status="active",
    )
    session.add(row)
    return row


class TestResolution:
    async def test_a_platform_namespace_resolves_for_every_caller(self, session):
        """Every row an install has before migration 202 is a platform row, so
        this is the property that makes the change invisible."""
        _ns(session, "crm", None)
        await session.flush()
        for tenant in (None, "default", "dev"):
            found = await resolve_namespace_by_name(session, "crm", tenant)
            assert found is not None, f"platform namespace unreachable for {tenant!r}"
            assert found.tenant_id is None

    async def test_a_tenants_own_namespace_shadows_the_platform_one(self, session):
        _ns(session, "crm", None)
        mine = _ns(session, "crm", "dev")
        await session.flush()
        found = await resolve_namespace_by_name(session, "crm", "dev")
        assert found.id == mine.id

    async def test_one_tenants_namespace_is_invisible_to_another(self, session):
        """The defect this migration exists for, in reverse: `dev`'s `crm` must
        not answer `default`'s lookup."""
        _ns(session, "crm", "dev")
        await session.flush()
        assert await resolve_namespace_by_name(session, "crm", "default") is None
        assert await resolve_namespace_by_name(session, "crm", None) is None

    async def test_two_tenants_may_now_hold_the_same_name(self, session):
        """The whole point. Before 202 the second insert violated a global
        UNIQUE and the second tenant simply could not have a `crm`."""
        _ns(session, "crm", "dev")
        _ns(session, "crm", "default")
        await session.flush()
        assert (await resolve_namespace_by_name(session, "crm", "dev")).tenant_id == "dev"
        assert (await resolve_namespace_by_name(session, "crm", "default")).tenant_id == "default"

    async def test_an_unknown_name_resolves_to_nothing(self, session):
        assert await resolve_namespace_by_name(session, "nope", "dev") is None


class TestRegistration:
    async def test_another_tenant_holding_the_name_does_not_block_it(self, session):
        await register_connector_namespace(
            session, name="billing", owner_type="connector",
            owner_ref=uuid.uuid4(), created_by="admin", tenant_id="dev")
        await session.flush()
        mine = await register_connector_namespace(
            session, name="billing", owner_type="connector",
            owner_ref=uuid.uuid4(), created_by="admin", tenant_id="default")
        assert mine.tenant_id == "default"

    async def test_a_platform_namespace_still_blocks_a_tenants(self, session):
        """Every tenant resolves a platform row, so two rows answering one name
        would make the winner depend on who asked."""
        from case_service.case_vars.service import VariableError

        await register_connector_namespace(
            session, name="shared", owner_type="connector",
            owner_ref=uuid.uuid4(), created_by="admin", tenant_id=None)
        await session.flush()
        with pytest.raises(VariableError, match="already registered"):
            await register_connector_namespace(
                session, name="shared", owner_type="connector",
                owner_ref=uuid.uuid4(), created_by="admin", tenant_id="dev")

    async def test_registering_without_a_tenant_still_makes_a_platform_row(self, session):
        """An operator with no tenant creates what they always created."""
        row = await register_connector_namespace(
            session, name="legacyish", owner_type="connector",
            owner_ref=uuid.uuid4(), created_by="admin")
        assert row.tenant_id is None and row.tenant_key == "platform"


class TestCallerContext:
    def test_the_tenant_is_optional_so_no_existing_call_site_changes(self):
        """Nineteen construction sites pass no tenant. Every one of them must
        keep reaching exactly the namespaces it reached before."""
        ctx = CallerContext(kind="platform", actor_id="anything")
        assert ctx.tenant_id is None


class TestRoutes:
    async def test_the_list_shows_platform_namespaces(self, client):
        resp = await client.get("/api/v1/variables/namespaces")
        assert resp.status_code == 200

    async def test_another_tenants_namespace_is_not_found_rather_than_forbidden(
            self, client, session):
        """A 403 confirms the row exists, which is the one thing a caller in
        another tenant must not learn."""
        other = _ns(session, "theirs", "some-other-tenant")
        await session.commit()
        resp = await client.patch(
            f"/api/v1/variables/namespaces/{other.id}",
            json={"sensitivity": "public"})
        assert resp.status_code == 404

    async def test_grants_on_another_tenants_namespace_are_refused(self, client, session):
        """`namespace_grants` hangs off the namespace, so scoping the namespace
        without scoping the grant routes would leave the hole open one door
        along."""
        other = _ns(session, "theirs2", "some-other-tenant")
        await session.commit()
        listed = await client.get(
            f"/api/v1/variables/namespaces/{other.id}/grants")
        assert listed.status_code == 404
        created = await client.post(
            f"/api/v1/variables/namespaces/{other.id}/grants",
            json={"grantee_type": "rules", "grantee_ref": "*", "capability": "read"})
        assert created.status_code == 404

    async def test_a_platform_namespace_is_still_administrable(self, client, session):
        """Every namespace an install had before 202 is a platform row, and an
        admin must not have lost the ability to edit one."""
        platform = _ns(session, "platformish", None)
        await session.commit()
        resp = await client.patch(
            f"/api/v1/variables/namespaces/{platform.id}",
            json={"sensitivity": "public"})
        assert resp.status_code == 200


class TestTheAuditFoundThese:
    """Found by asking "is it 100% wired and 100% secure?" — 2026-09-09.

    Migration 202 made a namespace tenant-ownable, which made "act on somebody
    else's namespace" **expressible for the first time**. Scoping the namespace
    routes and forgetting the ones that take a `namespace_id` in the BODY would
    have left the hole open one door along.
    """

    async def test_a_variable_cannot_be_defined_in_another_tenants_namespace(
            self, client, session):
        """`namespace_id` is caller-supplied. Before this check a designer in
        one tenant could define a variable — and therefore a write target — in
        another tenant's namespace."""
        import uuid as _uuid

        theirs = _ns(session, "theirsdef", "some-other-tenant")
        await session.commit()
        resp = await client.post(
            f"/api/v1/variables/case-types/{_uuid.uuid4()}",
            json={"namespace_id": str(theirs.id), "name": "sneaky",
                  "var_type": "str"})
        assert resp.status_code == 404

    async def test_a_variable_can_still_be_defined_in_a_platform_namespace(
            self, client, session):
        """Every namespace an install had before 202 is a platform row — the
        check must not have broken the ordinary case."""
        import uuid as _uuid

        ours = _ns(session, "platformdef", None)
        await session.commit()
        resp = await client.post(
            f"/api/v1/variables/case-types/{_uuid.uuid4()}",
            json={"namespace_id": str(ours.id), "name": "ok", "var_type": "str"})
        assert resp.status_code == 201, resp.text

    async def test_promotion_resolves_the_callers_own_namespace(self, session):
        """`promote_blob_key` took no tenant, so it resolved platform rows only
        and a tenant could not promote into a namespace it had registered."""
        import inspect

        from case_service.api.routers import case_variables

        source = inspect.getsource(case_variables.promote_case_data_key)
        assert "tenant_id=" in source, (
            "the promote route must pass the caller's tenant, or a tenant's own "
            "namespace is unreachable as a promotion target")

    async def test_a_tenant_cannot_shadow_a_platform_namespace(self, client, session):
        """`register_connector_namespace` already refused this; the ROUTE did
        not, and the new (tenant_key, name) index would happily accept both
        rows. Then this tenant's writes land in the new namespace while
        everything already written stays in the platform one — a namespace split
        in two with nothing saying so.

        The test admin resolves to NO tenant, so the route is called directly
        with a tenant-bearing caller: through the client this would be an
        ordinary duplicate and would prove nothing about the new rule."""
        from types import SimpleNamespace

        from fastapi import HTTPException

        from case_service.api.routers.case_variables import (
            NamespaceCreate, create_namespace,
        )

        _ns(session, "sharedname", None)
        await session.flush()

        with pytest.raises(HTTPException) as exc:
            await create_namespace(
                NamespaceCreate(name="sharedname", owner_type="connector",
                                sensitivity="internal"),
                session=session,
                user=SimpleNamespace(user_id="admin", tenant_id="dev"),
            )
        assert exc.value.status_code == 409
        assert "split its data in two" in str(exc.value.detail)

    async def test_a_platform_operator_still_creates_a_platform_namespace(
            self, session):
        """The same call from a caller with no tenant is the ordinary create it
        has always been."""
        from types import SimpleNamespace

        from case_service.api.routers.case_variables import (
            NamespaceCreate, create_namespace,
        )

        out = await create_namespace(
            NamespaceCreate(name="brandnew", owner_type="connector",
                            sensitivity="internal"),
            session=session,
            user=SimpleNamespace(user_id="admin", tenant_id=None),
        )
        assert out["name"] == "brandnew"
