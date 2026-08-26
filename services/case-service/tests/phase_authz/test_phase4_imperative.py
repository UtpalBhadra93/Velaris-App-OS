"""Phase 4 steps 2 and 3 — the imperative checks, and the identity model.

Step 2 migrated the 205 role-name decisions living inside handler bodies. They
were never gates in the FastAPI sense, which is why three separate detectors
walked past them; the ledger in `legacy_sites.txt` is what finally counted them.

The interesting half is that they are not all refusals. A dependency either
passes or raises, but a statement in a body can also *shape a response* — scope
a query, redact a column, choose a 404 over a 403. A blanket rewrite to
"require admin" would have broken exactly those, so the migration kept the
shape: booleans stayed booleans (`hxguard.user_allows`), raises stayed raises
(`hxguard.require_user`), and ownership tests kept their ownership half with
only the override renamed.

Step 3 removed `has_privilege`'s fallback to `is_admin`. That fallback meant
every admin held every case-level privilege — including the one HxDBManager's
DBView feature exists to require an explicit grant for.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import pytest

from velaris_shared import hxguard
from velaris_shared.auth import dependencies as deps
from velaris_shared.auth.models import ActiveAccessGroup, AuthenticatedUser


def _user(roles, privileges=None):
    u = AuthenticatedUser(user_id="u1", username="u1", roles=list(roles), token="t")
    if privileges is not None:
        u.active_access_group = ActiveAccessGroup(
            id="g1", name="G", portal_id="p", portal_type="staff", portal_name="P",
            modules=[], homepage="/", roles=[], privileges=list(privileges),
            allowed_case_type_ids=["*"], allowed_queue_ids=["*"])
    return u


class TestIdentityModelIsNoLongerADecider:
    def test_has_role_is_deleted(self):
        assert not hasattr(_user(["admin"]), "has_role")

    def test_is_admin_survives_as_a_display_flag(self):
        assert _user(["admin"]).is_admin is True
        assert _user(["viewer"]).is_admin is False

    def test_admin_no_longer_implies_designer(self):
        """`is_designer` used to be `"designer" in roles or self.is_admin`. That
        inheritance is why `has_role("designer")` was true for every admin, and
        why no gate could distinguish the two."""
        assert _user(["admin"]).is_designer is False
        assert _user(["designer"]).is_designer is True

    def test_require_role_is_retired_loudly(self):
        """Kept as a raise rather than deleted: 25 routers still import the
        symbol, and a NameError in an unrelated deploy is a worse first signal
        than a message naming the replacement."""
        with pytest.raises(RuntimeError, match="retired in Phase 4"):
            deps.require_role("admin")
        with pytest.raises(RuntimeError, match="retired in Phase 4"):
            deps.require_admin()


class TestHasPrivilegeFallbackRemoved:
    def test_an_admin_with_no_access_group_holds_no_case_privileges(self):
        """Step 3. This is a real tightening, and the intended one: with the
        fallback, `has_privilege("db_manager", "view_sensitive")` was true for
        every admin, so DBView granted itself to precisely the people its
        docstring says must be granted it explicitly."""
        assert _user(["admin"]).has_privilege("db_manager", "view_sensitive") is False
        assert _user(["admin"]).has_privilege("case_type", "update") is False

    def test_an_explicit_access_group_privilege_still_grants(self):
        u = _user(["viewer"], [{"resource": "db_manager", "actions": ["view_sensitive"]}])
        assert u.has_privilege("db_manager", "view_sensitive") is True

    def test_superadmin_still_bypasses(self):
        assert _user(["superadmin"]).has_privilege("anything", "at_all") is True


class TestUserAllows:
    def test_it_answers_the_privilege_model(self):
        assert hxguard.user_allows(_user(["admin"]), "marketplace.admin.access") is True
        assert hxguard.user_allows(_user(["viewer"]), "marketplace.admin.access") is False

    def test_a_non_platform_action_is_denied_not_guessed(self):
        """Fail-closed: `user_allows` only speaks the platform namespace. A
        case-level action reaching it is a bug at the call site, and answering
        `True` would turn that bug into a grant."""
        assert hxguard.user_allows(_user(["admin"]), "case.read") is False

    def test_an_unknown_action_is_denied(self):
        assert hxguard.user_allows(_user(["admin"]), "does.not.exist") is False

    def test_it_never_raises(self):
        class Broken:
            @property
            def roles(self):
                raise RuntimeError("boom")
        assert hxguard.user_allows(Broken(), "marketplace.admin.access") is False

    def test_the_roles_that_only_imperative_checks_named(self):
        """`developer` and `finance` were invisible to every earlier phase: no
        dependency gate named them, so they seeded to an empty privilege set
        while their real authority lived in handler bodies."""
        assert hxguard.user_allows(_user(["finance"]), "finance.details.read") is True
        assert hxguard.user_allows(_user(["developer"]),
                                   "marketplace.packages.publish") is True
        assert hxguard.user_allows(_user(["developer"]),
                                   "marketplace.admin.access") is False


class TestOwnershipOverridesKeptTheirOwnershipHalf:
    """The `branches.py` and `marketplace.py` sites read
    `not user.is_admin and row.owner != caller`. Collapsing that into a single
    action would have granted edit rights to every action-holder AND revoked
    them from the owner — the migration has to preserve both halves."""

    def test_the_override_is_a_privilege_now(self):
        assert hxguard.user_allows(_user(["admin"]), "design.branches.override") is True
        assert hxguard.user_allows(_user(["designer"]), "design.branches.override") is False

    def test_the_ownership_half_still_exists_in_source(self):
        import inspect
        from case_service.api.routers import branches

        src = inspect.getsource(branches)
        assert 'user_allows(user, "design.branches.override") and b.owner_id' in src, (
            "the ownership clause must survive alongside the named override")


class TestNoRouterDecidesOnRoleNames:
    def test_no_router_reads_is_admin_or_has_role_for_a_decision(self):
        """The ledger permits `roles` reads (superadmin tests, display). It must
        permit no `is_admin` / `is_designer` / `has_role` read at all — those
        are the three that no longer mean what a caller would assume."""
        from case_service.hxguard_ledger.legacy_sites import scan

        offenders = [f"{s.file}::{s.function}" for s in scan()
                     if {"is_admin", "is_designer", "has_role"} & set(s.direct)]
        assert not offenders, offenders


class TestPhase4Parity:
    """The Phase-1 discipline, applied to the imperative sites.

    Every earlier phase proved its cutover against an oracle — what the code it
    replaced would have answered — rather than against what the author intended.
    These sites needed it just as much: they were transcribed from handler
    bodies, not generated, so a dropped clause is a silent tightening that no
    test would otherwise catch. It found two while this phase was being written:
    `design.rules.read` had lost `designer`, and `integration.connectors.rotate`
    had lost `integration`.
    """

    #: Transcribed from the replaced source, one entry per migrated action.
    LEGACY = {
        "platform.dbmigrate.access": {"admin", "superadmin"},
        "design.evolve.access": {"admin", "superadmin"},
        "platform.replay.access": {"admin", "superadmin"},
        "platform.stream.access": {"admin", "superadmin"},
        "platform.stream.read_all": {"admin", "superadmin"},
        "platform.dbmanager.access": {"admin", "superadmin"},
        "platform.dbmanager.read": {"admin", "superadmin", "designer", "developer"},
        "marketplace.admin.access": {"admin", "superadmin"},
        "marketplace.packages.publish": {"admin", "superadmin", "developer"},
        "marketplace.catalogue.read": {"admin", "superadmin", "developer", "manager"},
        "marketplace.workspaces.override": {"admin", "superadmin"},
        "design.branches.override": {"admin", "superadmin"},
        "design.case_types.write": {"admin", "superadmin", "designer"},
        "design.draft.apply": {"admin", "superadmin", "designer"},
        "design.rules.read": {"admin", "superadmin", "designer"},
        "finance.details.read": {"admin", "superadmin", "finance"},
        "casework.sensitive.read": {"admin", "superadmin"},
        "docs.files.delete": {"admin", "superadmin"},
        "integration.connectors.rotate": {"admin", "superadmin", "integration"},
        "compliance.crosstenant.read": {"admin", "superadmin"},
        "variables.operator.elevated": {"admin", "superadmin"},
    }

    SHAPES = ("superadmin", "admin", "designer", "developer", "finance", "manager",
              "devops", "integration", "security", "viewer", "case_worker", "staff")

    def test_every_migrated_action_matches_the_code_it_replaced(self):
        wrong = []
        for action, allowed in self.LEGACY.items():
            for role in self.SHAPES:
                got = hxguard.user_allows(_user([role]), action)
                if got != (role in allowed):
                    wrong.append(f"{action}/{role}: now={got} before={role in allowed}")
        assert not wrong, wrong

    def test_a_subject_with_no_roles_gets_nothing(self):
        assert not any(hxguard.user_allows(_user([]), a) for a in self.LEGACY)

    def test_the_table_is_not_vacuous(self):
        """Both answers must actually occur, or a table of all-False compared to
        all-False would 'prove' parity."""
        answers = {hxguard.user_allows(_user([r]), a)
                   for a in self.LEGACY for r in self.SHAPES}
        assert answers == {True, False}

    def test_every_migrated_action_is_registered(self):
        from velaris_shared.hxguard.actions import ACTIONS
        assert not (set(self.LEGACY) - set(ACTIONS))


class TestTheTwoNamespacesDoNotCollide:
    """Phase 4 put `casework.sensitive.read` and `docs.files.delete` in the PLATFORM
    registry, alongside the pre-existing CASE actions `case.read`, `case.update`
    and `docs.verify`.

    `check()` routes by set membership — ReBAC for case actions, the privilege
    evaluator for platform ones — so a name landing in both would be decided by
    whichever branch is tested first, silently, with the other model's answer
    never consulted. The prefixes now genuinely overlap, so this stops being
    theoretical."""

    def test_no_action_name_is_in_both_registries(self):
        from velaris_shared.hxguard.service import CASE_ACTIONS, PLATFORM_ACTIONS
        assert not (set(CASE_ACTIONS) & set(PLATFORM_ACTIONS))

    def test_user_allows_refuses_a_case_action_rather_than_answering_it(self):
        """`user_allows` cannot evaluate ReBAC — it has no session and no
        resource. Answering anyway would be the collision's damage in miniature."""
        from velaris_shared.hxguard.service import CASE_ACTIONS
        for action in CASE_ACTIONS:
            assert hxguard.user_allows(_user(["admin"]), action) is False, action
