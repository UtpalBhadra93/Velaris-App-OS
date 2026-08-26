"""Phase 4 step 1 — the access-role name merge is retired.

For the whole of P37 an access role's NAME was unioned into
`AuthenticatedUser.roles`, so `require_role("admin")` passed for anyone whose
access role happened to be called `admin`. The seeds were *chosen* to collide —
that is what kept routes working when access groups arrived — and it meant the
create-an-access-group screen could mint platform administrators.

`require_role` has had zero call sites since Phase 3, so the bridge carries
nothing. These tests pin the three things that must now be true together:

1. enrichment does not touch `user.roles`;
2. an access group reaches a platform action only through a dotted-namespace
   privilege entry — never through a name, and never through the legacy `*`;
3. the collision is *reported*, because losing a privilege is invisible at the
   request path: a denial for a missing privilege looks exactly like a correct
   denial.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import inspect
import uuid

import pytest

from velaris_shared.auth import dependencies as deps
from velaris_shared.auth.models import ActiveAccessGroup, AuthenticatedUser
from velaris_shared.db.models import (
    AccessGroupModel,
    AccessRoleModel,
    OperatorAccessGroupModel,
    PortalModel,
    UserDirectoryModel,
)
from velaris_shared.hxguard import role_names, service as hxguard_service


ADMIN_ONLY_ACTION = "identity.users.write"


async def _group_named(session, operator_id: str, role_name: str,
                       privileges: list[dict]) -> AccessGroupModel:
    portal = PortalModel(name="P", portal_type="staff", modules=[], homepage="/",
                         tenant_id="default")
    session.add(portal)
    await session.flush()
    role = AccessRoleModel(name=role_name, description="", privileges=privileges,
                           tenant_id="default")
    session.add(role)
    await session.flush()
    group = AccessGroupModel(name=f"G-{role_name}-{uuid.uuid4().hex[:4]}",
                             portal_id=portal.id, role_ids=[str(role.id)],
                             is_active=True, tenant_id="default")
    session.add(group)
    await session.flush()
    session.add(OperatorAccessGroupModel(operator_id=operator_id,
                                         access_group_id=group.id, is_primary=True))
    session.add(UserDirectoryModel(user_id=operator_id, email=f"{operator_id}@e.com",
                                   display_name=operator_id, tenant_id="default"))
    await session.commit()
    await session.refresh(group)
    return group


class TestEnrichmentNoLongerMerges:
    @pytest.mark.asyncio
    async def test_enrichment_leaves_user_roles_untouched(self, session):
        operator = f"op-{uuid.uuid4().hex[:8]}"
        await _group_named(session, operator, "admin", [{"resource": "case",
                                                         "actions": ["read"]}])
        user = AuthenticatedUser(user_id=operator, username=operator,
                                 roles=["viewer"], token="t")
        enriched = await deps._enrich_with_access_group(user, session)

        assert enriched.roles == ["viewer"], (
            "the access role is named 'admin'; before Phase 4 that alone made "
            "this user a platform admin")
        assert enriched.is_admin is False

    @pytest.mark.asyncio
    async def test_the_names_are_still_published_on_the_group(self, session):
        """Removing the merge must not hide the names: the UI shows them, and
        `core/access_control.py` still speaks that case-level vocabulary."""
        operator = f"op-{uuid.uuid4().hex[:8]}"
        await _group_named(session, operator, "staff", [{"resource": "case",
                                                         "actions": ["read"]}])
        user = AuthenticatedUser(user_id=operator, username=operator,
                                 roles=["viewer"], token="t")
        enriched = await deps._enrich_with_access_group(user, session)

        assert enriched.active_access_group is not None
        assert enriched.active_access_group.roles == ["staff"]

    def test_the_merge_is_gone_from_the_source(self):
        """A regression guard with teeth: the merge was one `set(...) | set(...)`
        line, and re-adding it would restore the escalation silently."""
        src = inspect.getsource(deps._enrich_with_access_group)
        assert "user.roles =" not in src, "enrichment must not assign user.roles"
        assert "merged_roles" not in src


class TestNoPlatformAccessByName:
    """Decided through `PrivilegeAdapter`, not `PrivilegeBackend` directly.

    `Subject.privileges` is a frozen tuple — it is the decision cache key — and
    the adapter is what thaws it. Calling the pure evaluator with a Subject
    would test a shape the running system never produces."""

    def _subject(self, roles, privileges):
        user = AuthenticatedUser(user_id="u1", username="u1", roles=list(roles), token="t")
        user.active_access_group = ActiveAccessGroup(
            id="g1", name="G", portal_id="p", portal_type="staff", portal_name="P",
            modules=[], homepage="/", roles=["admin"], privileges=list(privileges),
            allowed_case_type_ids=["*"], allowed_queue_ids=["*"])
        return hxguard_service.subject_from_user(user)

    async def _decide(self, roles, privileges):
        return await hxguard_service.PrivilegeAdapter().evaluate(
            None, self._subject(roles, privileges), ADMIN_ONLY_ACTION, None)

    @pytest.mark.asyncio
    async def test_the_legacy_wildcard_does_not_grant_a_platform_action(self):
        """The seeded `admin` access role's `{"resource": "*", "actions": ["*"]}`
        predates platform actions by years. It was harmless while the name merge
        made those users admin anyway; from this phase it is the only thing that
        would grant, so the namespace boundary is now load-bearing."""
        assert (await self._decide(["viewer"],
                                   [{"resource": "*", "actions": ["*"]}])).allow is False

    @pytest.mark.asyncio
    async def test_a_dotted_namespace_entry_does_grant(self):
        assert (await self._decide(
            ["viewer"],
            [{"resource": "identity.users", "actions": ["write"]}])).allow is True

    @pytest.mark.asyncio
    async def test_a_platform_role_on_the_account_still_grants(self):
        """The supported route survives — this is what an upgrade migrates to."""
        assert (await self._decide(["admin"], [])).allow is True

    def test_the_group_role_name_is_not_in_the_subject(self):
        subject = self._subject(["viewer"], [])
        assert "admin" not in subject.roles

    def test_the_privilege_round_trip_preserves_lists(self):
        """Found by the test above failing, not by review.

        `_freeze_privileges` wrote `str(v)`, so `["write"]` came back as the
        string `"['write']"` and the verb matcher iterated it one character at a
        time. Nothing depended on that path while the name merge existed, which
        is exactly why it survived three phases of parity testing."""
        entry = {"resource": "identity.users", "actions": ["read", "write"]}
        frozen = hxguard_service._freeze_privileges([entry])
        assert hash(frozen) is not None, "Subject is a frozen dataclass and a cache key"
        assert hxguard_service._thaw_privileges(frozen) == [entry]

    def test_two_subjects_with_different_privileges_do_not_share_a_cache_key(self):
        a = self._subject(["viewer"], [{"resource": "identity.users", "actions": ["read"]}])
        b = self._subject(["viewer"], [{"resource": "identity.users", "actions": ["write"]}])
        assert a.privileges != b.privileges


class TestCollisionReport:
    @pytest.mark.asyncio
    async def test_a_colliding_role_is_reported_with_its_blast_radius(self, session):
        operator = f"op-{uuid.uuid4().hex[:8]}"
        await _group_named(session, operator, "admin", [])
        found = await role_names.colliding_access_roles(session)

        hit = [r for r in found if r["access_role_name"] == "admin"]
        assert hit, "an access role named 'admin' must be reported"
        assert hit[0]["operators_affected"] >= 1
        assert hit[0]["groups"], "the report must name the groups carrying it"

    @pytest.mark.asyncio
    async def test_a_non_colliding_role_is_not_reported(self, session):
        operator = f"op-{uuid.uuid4().hex[:8]}"
        name = f"claims-team-{uuid.uuid4().hex[:6]}"
        await _group_named(session, operator, name, [])
        found = await role_names.colliding_access_roles(session)
        assert not [r for r in found if r["access_role_name"] == name]

    @pytest.mark.asyncio
    async def test_the_comparison_is_case_insensitive(self, session):
        """The merge unioned raw strings, so `Admin` never collided in practice.
        The *report* still names it: an operator reading a warning should not
        have to know which spelling was load-bearing."""
        operator = f"op-{uuid.uuid4().hex[:8]}"
        await _group_named(session, operator, "Admin", [])
        found = await role_names.colliding_access_roles(session)
        assert [r for r in found if r["access_role_name"] == "Admin"]

    @pytest.mark.asyncio
    async def test_the_startup_warning_never_raises(self, session):
        """It runs inside the lifespan. A report that can abort startup is worse
        than no report — see the `release_cron` outage, where one failed import
        behind a shared `try` turned every feature flag off platform-wide."""
        assert await role_names.warn_on_collisions(session) is not None

    def test_every_platform_role_is_reserved(self):
        from velaris_shared.hxguard.privileges import KNOWN_ROLES

        assert {r.lower() for r in KNOWN_ROLES} == set(role_names.RESERVED_PLATFORM_ROLES)
