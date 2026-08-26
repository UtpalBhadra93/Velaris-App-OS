"""Phase 3 — the lockout guard.

`access_groups.py` is the router that grants access, so it is the one router
whose cutover can end the project: revoke the wrong membership and the ability
to un-revoke it goes with it. §6.1 of the design requires three defences, and
these tests pin the first one — the refusal that fires *before* the write, while
there is still somebody able to authorise a repair.

The guard is deliberately narrow. It protects the ability to grant access, not
every privilege, because a model that refuses any reduction in permissions is
one nobody can use and everyone routes around.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import uuid

import pytest
from fastapi import HTTPException

from velaris_shared.db.models import (
    AccessGroupModel,
    AccessRoleModel,
    HelixUserModel,
    OperatorAccessGroupModel,
    PortalModel,
)
from velaris_shared.hxguard import lockout


async def _admin_user(session, username: str, *, roles=("admin",), active=True) -> HelixUserModel:
    u = HelixUserModel(
        username=username, email=f"{username}@example.com",
        roles=list(roles), is_active=active,
    )
    session.add(u)
    await session.commit()
    await session.refresh(u)
    return u


async def _group_with_member(session, user_id: str, *, role_name: str,
                             privileges: list[dict]) -> AccessGroupModel:
    portal = PortalModel(name="P", portal_type="staff", modules=[], homepage="/",
                         tenant_id="default")
    session.add(portal)
    await session.flush()
    role = AccessRoleModel(name=role_name, description="", privileges=privileges,
                           tenant_id="default")
    session.add(role)
    await session.flush()
    group = AccessGroupModel(name=f"G-{role_name}", portal_id=portal.id,
                             role_ids=[str(role.id)], is_active=True,
                             tenant_id="default")
    session.add(group)
    await session.flush()
    session.add(OperatorAccessGroupModel(operator_id=user_id,
                                         access_group_id=group.id, is_primary=True))
    await session.commit()
    await session.refresh(group)
    return group


class TestHolders:
    @pytest.mark.asyncio
    async def test_a_platform_admin_counts_as_a_holder(self, session):
        await _admin_user(session, f"admin-{uuid.uuid4().hex[:6]}")
        assert await lockout.holders_count(session) >= 1

    @pytest.mark.asyncio
    async def test_a_superadmin_counts_even_with_no_group(self, session):
        u = await _admin_user(session, f"sa-{uuid.uuid4().hex[:6]}", roles=("superadmin",))
        holders = await lockout._holders_of(session, lockout.BOOTSTRAP_ACTION)
        assert str(u.id) in holders

    @pytest.mark.asyncio
    async def test_an_inactive_account_is_not_a_holder(self, session):
        """A disabled account cannot repair anything, so counting it would let
        the guard pass while the platform is in fact locked out."""
        u = await _admin_user(session, f"off-{uuid.uuid4().hex[:6]}", active=False)
        holders = await lockout._holders_of(session, lockout.BOOTSTRAP_ACTION)
        assert str(u.id) not in holders

    @pytest.mark.asyncio
    async def test_a_viewer_is_not_a_holder(self, session):
        u = await _admin_user(session, f"v-{uuid.uuid4().hex[:6]}", roles=("viewer",))
        holders = await lockout._holders_of(session, lockout.BOOTSTRAP_ACTION)
        assert str(u.id) not in holders

    @pytest.mark.asyncio
    async def test_an_access_role_named_admin_no_longer_makes_a_holder(self, session):
        """Inverted by Phase 4 step 1, deliberately.

        Until step 1 this asserted the opposite: an access role *named* `admin`
        made its members holders, because the name merged into `user.roles`. That
        was the escalation the phase exists to remove, so counting it now would
        report holders who cannot in fact grant access — and this guard's whole
        job is to refuse the change that empties that set. Overcounting is the
        direction that lets a real lockout through."""
        u = await _admin_user(session, f"m-{uuid.uuid4().hex[:6]}", roles=("viewer",))
        await _group_with_member(session, str(u.id), role_name="admin", privileges=[])
        holders = await lockout._holders_of(session, lockout.BOOTSTRAP_ACTION)
        assert str(u.id) not in holders

    @pytest.mark.asyncio
    async def test_the_legacy_wildcard_still_does_not_reach_a_platform_action(self, session):
        """The seeded `admin` access role carries `{"resource": "*"}`, written
        for case-level resources years before platform actions existed. The
        namespace boundary is what stops it satisfying `identity.groups.write`,
        and from this phase on it is the *only* thing that does."""
        u = await _admin_user(session, f"w-{uuid.uuid4().hex[:6]}", roles=("viewer",))
        await _group_with_member(session, str(u.id), role_name="admin",
                                 privileges=[{"resource": "*", "actions": ["*"]}])
        holders = await lockout._holders_of(session, lockout.BOOTSTRAP_ACTION)
        assert str(u.id) not in holders

    @pytest.mark.asyncio
    async def test_a_modern_privilege_also_makes_a_holder(self, session):
        u = await _admin_user(session, f"p-{uuid.uuid4().hex[:6]}", roles=("viewer",))
        await _group_with_member(
            session, str(u.id), role_name=f"granter-{uuid.uuid4().hex[:4]}",
            privileges=[{"resource": "identity.groups", "actions": ["write"]}])
        holders = await lockout._holders_of(session, lockout.BOOTSTRAP_ACTION)
        assert str(u.id) in holders


class TestRefusal:
    @pytest.mark.asyncio
    async def test_removing_the_last_holder_is_refused(self, session):
        await session.execute(HelixUserModel.__table__.delete())
        await session.commit()
        only = await _admin_user(session, f"last-{uuid.uuid4().hex[:6]}")

        with pytest.raises(HTTPException) as exc:
            await lockout.assert_not_last_holder(
                session, removing_user_ids=[str(only.id)], context="test")
        assert exc.value.status_code == 409
        assert "grant_privilege.py" in exc.value.detail, (
            "the refusal must name the break-glass, or it is a dead end")

    @pytest.mark.asyncio
    async def test_removing_one_of_two_holders_is_allowed(self, session):
        """Narrow by design — it blocks the last one, not every change."""
        await session.execute(HelixUserModel.__table__.delete())
        await session.commit()
        a = await _admin_user(session, f"a-{uuid.uuid4().hex[:6]}")
        await _admin_user(session, f"b-{uuid.uuid4().hex[:6]}")
        await lockout.assert_not_last_holder(session, removing_user_ids=[str(a.id)])

    @pytest.mark.asyncio
    async def test_an_unrelated_removal_is_allowed(self, session):
        await session.execute(HelixUserModel.__table__.delete())
        await session.commit()
        await _admin_user(session, f"keep-{uuid.uuid4().hex[:6]}")
        viewer = await _admin_user(session, f"vv-{uuid.uuid4().hex[:6]}", roles=("viewer",))
        await lockout.assert_not_last_holder(session, removing_user_ids=[str(viewer.id)])


class TestAlreadyLockedOut:
    """The guard stops the platform going from "someone can grant access" to
    "nobody can". It must not police a zero it did not cause.

    This was found by two red tests in `phase37`: with no `velaris_users` rows
    at all, every member removal returned 409. That is not only a test-database
    shape — an SSO-only estate whose identities live in the directory rather
    than `velaris_users` computes the same zero, and would have had all access-
    group member management refused permanently, starting with the deployment
    that needed it most."""

    @pytest.mark.asyncio
    async def test_with_no_holders_at_all_the_change_is_allowed(self, session):
        await session.execute(HelixUserModel.__table__.delete())
        assert not await lockout._holders_of(session, lockout.BOOTSTRAP_ACTION)
        await lockout.assert_not_last_holder(
            session, removing_user_ids=["anyone"], context="already locked out")

    @pytest.mark.asyncio
    async def test_going_from_one_to_zero_is_still_refused(self, session):
        """The distinction that makes the exemption safe."""
        await session.execute(HelixUserModel.__table__.delete())
        u = await _admin_user(session, f"last-{uuid.uuid4().hex[:6]}")
        with pytest.raises(HTTPException) as exc:
            await lockout.assert_not_last_holder(
                session, removing_user_ids=[str(u.id)], context="the last one")
        assert exc.value.status_code == 409


class TestRevocationByEdit:
    """The first version of the guard covered removing a member and deleting a
    group. Three edits reached the same state without passing it:

        PATCH /access-groups/{id}   sets is_active, rewrites role_ids
        PATCH /access-roles/{id}    rewrites the privileges themselves
        DELETE /access-roles/{id}

    `PATCH` with `is_active: false` is the sharpest of the three — byte for byte
    the same revocation as the `DELETE` next to it, which *was* guarded."""

    @pytest.mark.asyncio
    async def test_deactivating_a_group_by_patch_is_refused(self, session):
        await session.execute(HelixUserModel.__table__.delete())
        u = await _admin_user(session, f"pg-{uuid.uuid4().hex[:6]}", roles=("viewer",))
        g = await _group_with_member(
            session, str(u.id), role_name=f"gr-{uuid.uuid4().hex[:4]}",
            privileges=[{"resource": "identity.groups", "actions": ["write"]}])
        with pytest.raises(HTTPException) as exc:
            await lockout.assert_group_edit_survivable(
                session, g, g.role_ids, False, context="patch deactivate")
        assert exc.value.status_code == 409

    @pytest.mark.asyncio
    async def test_dropping_the_granting_role_from_a_group_is_refused(self, session):
        await session.execute(HelixUserModel.__table__.delete())
        u = await _admin_user(session, f"dr-{uuid.uuid4().hex[:6]}", roles=("viewer",))
        g = await _group_with_member(
            session, str(u.id), role_name=f"gr-{uuid.uuid4().hex[:4]}",
            privileges=[{"resource": "identity.groups", "actions": ["write"]}])
        with pytest.raises(HTTPException):
            await lockout.assert_group_edit_survivable(
                session, g, [], True, context="patch drop roles")

    @pytest.mark.asyncio
    async def test_an_unrelated_group_edit_is_allowed(self, session):
        """A rename must not be refused. A guard that blocks harmless edits is
        one people route around, which is worse than not having it."""
        u = await _admin_user(session, f"ok-{uuid.uuid4().hex[:6]}", roles=("viewer",))
        g = await _group_with_member(
            session, str(u.id), role_name=f"gr-{uuid.uuid4().hex[:4]}",
            privileges=[{"resource": "identity.groups", "actions": ["write"]}])
        await lockout.assert_group_edit_survivable(
            session, g, g.role_ids, True, context="rename only")

    @pytest.mark.asyncio
    async def test_stripping_a_roles_privileges_is_refused(self, session):
        await session.execute(HelixUserModel.__table__.delete())
        u = await _admin_user(session, f"sr-{uuid.uuid4().hex[:6]}", roles=("viewer",))
        g = await _group_with_member(
            session, str(u.id), role_name=f"gr-{uuid.uuid4().hex[:4]}",
            privileges=[{"resource": "identity.groups", "actions": ["write"]}])
        role_obj = await session.get(AccessRoleModel, uuid.UUID(g.role_ids[0]))
        with pytest.raises(HTTPException):
            await lockout.assert_role_edit_survivable(
                session, role_obj, [], role_obj.name, context="strip privileges")

    @pytest.mark.asyncio
    async def test_renaming_a_role_without_touching_privileges_is_allowed(self, session):
        u = await _admin_user(session, f"rn-{uuid.uuid4().hex[:6]}", roles=("viewer",))
        g = await _group_with_member(
            session, str(u.id), role_name=f"gr-{uuid.uuid4().hex[:4]}",
            privileges=[{"resource": "identity.groups", "actions": ["write"]}])
        role_obj = await session.get(AccessRoleModel, uuid.UUID(g.role_ids[0]))
        await lockout.assert_role_edit_survivable(
            session, role_obj, role_obj.privileges, "renamed", context="rename")

    @pytest.mark.asyncio
    async def test_deleting_the_granting_role_is_refused(self, session):
        await session.execute(HelixUserModel.__table__.delete())
        u = await _admin_user(session, f"dl-{uuid.uuid4().hex[:6]}", roles=("viewer",))
        g = await _group_with_member(
            session, str(u.id), role_name=f"gr-{uuid.uuid4().hex[:4]}",
            privileges=[{"resource": "identity.groups", "actions": ["write"]}])
        role_obj = await session.get(AccessRoleModel, uuid.UUID(g.role_ids[0]))
        with pytest.raises(HTTPException):
            await lockout.assert_role_delete_survivable(session, role_obj)

    @pytest.mark.asyncio
    async def test_deleting_a_harmless_role_is_allowed(self, session):
        u = await _admin_user(session, f"hr-{uuid.uuid4().hex[:6]}", roles=("admin",))
        g = await _group_with_member(
            session, str(u.id), role_name=f"gr-{uuid.uuid4().hex[:4]}",
            privileges=[{"resource": "case", "actions": ["read"]}])
        role_obj = await session.get(AccessRoleModel, uuid.UUID(g.role_ids[0]))
        await lockout.assert_role_delete_survivable(session, role_obj)


class TestWiring:
    def test_the_destructive_endpoints_call_the_guard(self):
        """Five doors lead to the same room: removing a member, deleting a
        group, deactivating one by PATCH, and editing or deleting the role that
        grants the action."""
        from pathlib import Path
        src = (Path(__file__).resolve().parents[2] / "case_service" / "api"
               / "routers" / "access_groups.py").read_text()
        for call in ("assert_not_last_holder",
                     "assert_group_edit_survivable",
                     "assert_role_edit_survivable",
                     "assert_role_delete_survivable"):
            assert call in src, f"{call} is not wired into access_groups.py"
        # Count the CALL, not the name. A comment mentioning the guard is not a
        # guard, and a substring count cannot tell the difference — the same
        # lesson as the Phase-0 gate counts, one layer down.
        assert src.count("await lockout.assert_not_last_holder(") == 2, (
            "expected the direct guard on both remove_member and delete_access_group")
        # Phase 6 adds a third door with its own guard: a role-level `deny`
        # revokes for every holder of that role at once. It cannot reuse
        # assert_not_last_holder — see assert_write_keeps_bootstrap_holders.
        assert "await role_store.assert_write_keeps_bootstrap_holders(" in src

    def test_the_break_glass_script_exists_and_audits(self):
        """A break-glass that leaves no trace is indistinguishable from an
        attack — this is the one tool that can manufacture an administrator."""
        from pathlib import Path
        script = (Path(__file__).resolve().parents[2] / "scripts"
                  / "grant_privilege.py")
        assert script.exists()
        body = script.read_text()
        assert "breakglass_grant" in body
        assert "critical" in body

    def test_access_groups_is_fully_migrated(self):
        from pathlib import Path
        from velaris_shared.hxguard.actions import MIGRATED_ROUTERS
        assert "access_groups.py" in MIGRATED_ROUTERS
        src = (Path(__file__).resolve().parents[2] / "case_service" / "api"
               / "routers" / "access_groups.py").read_text()
        assert "require_role(" not in src
        # 19 before Phase 6; +2 for the role-privilege store endpoints
        # (GET /role-privileges, PUT /role-privileges/{role}).
        assert src.count("hxguard.guard(") == 21
