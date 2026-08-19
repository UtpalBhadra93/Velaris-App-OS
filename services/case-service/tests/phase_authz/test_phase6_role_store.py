"""Phase 6 — role → privilege in DATA.

The point of the phase is that "managers and developers may review risk" stops
being a code change. The point of these tests is the failure modes, because an
authorization source served from an in-process cache has exactly one way to be
catastrophic and it is not "the grant did not apply".

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import pytest

from case_service.hxguard import role_store
from case_service.hxguard.privileges import (
    SEEDED_ROLE_PRIVILEGES,
    effective_privileges,
    evaluate_privileges,
)


@pytest.fixture(autouse=True)
def _clean_store():
    """Every test starts from a cold cache and leaves one."""
    before = role_store.all_stored()
    role_store._STORE = {}
    yield
    role_store._STORE = {r: list(p) for r, p in before.items()}


def _holds(role: str, action: str) -> bool:
    return evaluate_privileges(effective_privileges([role]), action)


class TestColdCacheIsTheSeed:
    """The property everything else rests on.

    The cache is empty for the first seconds of every boot and for the whole of
    any refresh failure. If the store REPLACED a role's privileges, that window
    would be a silent platform-wide lockout. Additive makes it a no-op.
    """

    def test_empty_store_gives_exactly_the_seeded_privileges(self):
        for role in SEEDED_ROLE_PRIVILEGES:
            assert effective_privileges([role]) == SEEDED_ROLE_PRIVILEGES[role], role

    def test_a_seeded_holder_still_holds_with_a_cold_cache(self):
        assert _holds("manager", "risk.assessment.read")
        assert _holds("admin", "identity.groups.write")

    async def test_a_failed_refresh_keeps_the_previous_contents(self):
        """A transient DB problem must not quietly revoke what was granted."""
        role_store._STORE = {"developer": [{"resource": "risk.assessment",
                                            "actions": ["read"]}]}
        assert _holds("developer", "risk.assessment.read")

        class _Boom:
            async def execute(self, *_a, **_k):
                raise RuntimeError("database is down")

        with pytest.raises(RuntimeError):
            await role_store.refresh(_Boom())
        assert _holds("developer", "risk.assessment.read"), "revoked by a DB blip"

    async def test_the_cron_survives_a_failing_refresh(self):
        """The loop must outlive a bad database moment.

        If it exited, every worker would freeze at whatever it last read and
        nothing would say so.
        """
        import asyncio

        calls = {"n": 0}

        async def _always_fails(_session):
            calls["n"] += 1
            raise RuntimeError("still down")

        orig = (role_store.refresh, role_store.REFRESH_INTERVAL_SECONDS,
                role_store.STARTUP_DELAY_SECONDS)
        role_store.refresh = _always_fails
        role_store.REFRESH_INTERVAL_SECONDS = 0.001
        role_store.STARTUP_DELAY_SECONDS = 0
        try:
            task = asyncio.create_task(role_store.role_privileges_cron())
            # Long enough for several cycles. If the loop exited on the first
            # exception, the count would stop at 1.
            await asyncio.sleep(0.05)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        finally:
            (role_store.refresh, role_store.REFRESH_INTERVAL_SECONDS,
             role_store.STARTUP_DELAY_SECONDS) = orig

        assert calls["n"] > 1, (
            f"the loop stopped after {calls['n']} failure(s) — every worker "
            "would freeze at its last read with nothing saying so")


class TestGrantingFromData:
    def test_a_role_with_nothing_seeded_can_be_granted(self):
        """`developer` is named by no gate, so Phase 1 seeded it almost empty."""
        assert not _holds("developer", "risk.assessment.read")
        role_store._STORE = {"developer": [{"resource": "risk.assessment",
                                            "actions": ["read"]}]}
        assert _holds("developer", "risk.assessment.read")

    def test_the_seed_is_not_disturbed_by_a_grant(self):
        role_store._STORE = {"manager": [{"resource": "risk.taxonomy",
                                          "actions": ["write"]}]}
        assert _holds("manager", "risk.taxonomy.write")
        assert _holds("manager", "risk.assessment.read"), "seeded grant lost"

    def test_a_wildcard_grant_works_segment_wise(self):
        role_store._STORE = {"developer": [{"resource": "risk.*",
                                            "actions": ["*"]}]}
        for action in ("risk.assessment.read", "risk.outcomes.write",
                       "risk.rules.configure", "risk.taxonomy.write"):
            assert _holds("developer", action), action

    def test_a_grant_to_one_role_does_not_leak_to_another(self):
        role_store._STORE = {"developer": [{"resource": "risk.taxonomy",
                                            "actions": ["write"]}]}
        assert not _holds("viewer", "risk.taxonomy.write")
        assert not _holds("case_worker", "risk.taxonomy.write")


class TestRevocationIsExplicit:
    """Removal is a `deny` row, never an absence — see the migration comment."""

    def test_deny_revokes_a_seeded_grant(self):
        assert _holds("manager", "risk.assessment.read")
        role_store._STORE = {"manager": [{"resource": "risk.assessment",
                                          "actions": ["read"], "effect": "deny"}]}
        assert not _holds("manager", "risk.assessment.read")

    def test_deny_wins_over_an_allow_in_the_same_row(self):
        role_store._STORE = {"developer": [
            {"resource": "risk.assessment", "actions": ["read"]},
            {"resource": "risk.assessment", "actions": ["read"], "effect": "deny"},
        ]}
        assert not _holds("developer", "risk.assessment.read")

    def test_removing_the_row_restores_the_seed(self):
        role_store._STORE = {"manager": [{"resource": "risk.assessment",
                                          "actions": ["read"], "effect": "deny"}]}
        assert not _holds("manager", "risk.assessment.read")
        role_store._STORE = {}
        assert _holds("manager", "risk.assessment.read")


class TestTheNamespaceBoundaryStillHolds:
    """Phase 2's finding, which Phase 6 must not reopen.

    A legacy `{"resource": "*"}` entry means "every case-level resource" and was
    written years before platform actions existed. Honouring it here would let a
    store row confer platform administration.
    """

    def test_a_bare_wildcard_confers_no_platform_action(self):
        role_store._STORE = {"viewer": [{"resource": "*", "actions": ["*"]}]}
        assert not _holds("viewer", "identity.groups.write")
        assert not _holds("viewer", "risk.assessment.read")

    def test_legacy_case_vocabulary_confers_no_platform_action(self):
        role_store._STORE = {"viewer": [{"resource": "case", "actions": ["read"]}]}
        assert not _holds("viewer", "casework.records.any")


class TestMalformedEntries:
    def test_refresh_drops_a_bad_entry_without_losing_the_good_ones(self):
        assert role_store._valid({"resource": "risk.assessment", "actions": ["read"]})
        for bad in ({"resource": "", "actions": ["read"]},
                    {"resource": "risk.assessment", "actions": []},
                    {"resource": "risk.assessment", "actions": [""]},
                    {"resource": "risk.assessment", "actions": "read"},
                    {"resource": "risk.assessment", "actions": ["read"],
                     "effect": "maybe"},
                    "not a dict"):
            assert not role_store._valid(bad), bad

    def test_entry_size_is_bounded(self):
        """Every entry is held in memory in every worker and walked on every
        decision, so an unbounded string is a hot-path cost, not just a big row."""
        assert not role_store._valid(
            {"resource": "a" * (role_store.MAX_RESOURCE_LEN + 1), "actions": ["read"]})
        assert not role_store._valid(
            {"resource": "risk.assessment",
             "actions": ["read"] * (role_store.MAX_ACTIONS_PER_ENTRY + 1)})
        assert not role_store._valid(
            {"resource": "risk.assessment", "actions": ["r" * (role_store.MAX_ACTION_LEN + 1)]})
        assert role_store._valid(
            {"resource": "a" * role_store.MAX_RESOURCE_LEN, "actions": ["read"]})

    def test_a_write_refuses_rather_than_silently_dropping(self):
        with pytest.raises(ValueError):
            role_store._valid_or_raise([{"resource": "risk.assessment",
                                         "actions": ["read"], "effect": "maybe"}])


class TestCacheGenerationInvalidatesDecisions:
    """The subtle one.

    The role → privilege mapping is resolved INSIDE the evaluator, so editing a
    role changes nothing about the subject. Without the generation stamp the
    30-second decision cache would keep serving the old answer, and the grant
    would appear to work for some workers and not others.
    """

    async def test_the_generation_moves_on_a_real_refresh(self):
        before = role_store.generation()

        class _Rows:
            async def execute(self, *_a, **_k):
                class _R:
                    def scalars(self_):
                        class _S:
                            def all(self__): return []
                        return _S()
                return _R()

        await role_store.refresh(_Rows())
        assert role_store.generation() != before

    async def test_the_subject_carries_it(self):
        from case_service.auth.models import AuthenticatedUser
        from case_service.hxguard.service import subject_from_user

        user = AuthenticatedUser(user_id="u1", username="u1", email="u@x.com",
                                 roles=["developer"])
        first = subject_from_user(user)

        class _Rows:
            async def execute(self, *_a, **_k):
                class _R:
                    def scalars(self_):
                        class _S:
                            def all(self__): return []
                        return _S()
                return _R()

        await role_store.refresh(_Rows())
        second = subject_from_user(user)
        assert first != second, "an edit must not reuse the cached decision"


class TestProspectiveStateIsTaskLocal:
    """The flaw the audit found, and why it was not theoretical.

    The lockout pre-check asks "who would hold this AFTERWARDS?" and the answer
    comes from this module. Installing the prospective entries into the shared
    `_STORE` meant every concurrent request landing during the check's database
    round trip — the check AWAITS — decided against an unsaved edit that might
    then be refused. A `deny` under review briefly applied platform-wide.
    """

    async def test_a_concurrent_request_does_not_see_the_prospective_state(self):
        import asyncio

        seen = []

        async def checking_task():
            with role_store.prospective("manager", [
                {"resource": "risk.assessment", "actions": ["read"],
                 "effect": "deny"}]):
                assert not _holds("manager", "risk.assessment.read"), (
                    "the checking task must see its own prospective state")
                await asyncio.sleep(0.05)          # the DB round trip

        async def other_request():
            await asyncio.sleep(0.01)              # lands mid-window
            seen.append(_holds("manager", "risk.assessment.read"))

        await asyncio.gather(checking_task(), other_request())
        assert seen == [True], (
            "a concurrent request decided against another admin's unsaved edit")

    async def test_the_override_is_dropped_even_on_failure(self):
        with pytest.raises(RuntimeError):
            with role_store.prospective("manager", [
                    {"resource": "risk.assessment", "actions": ["read"],
                     "effect": "deny"}]):
                raise RuntimeError("lockout check blew up")
        assert _holds("manager", "risk.assessment.read"), "override leaked"


# ── The admin surface ────────────────────────────────────────────────────────


class TestRolePrivilegeEndpoints:
    async def test_list_splits_seeded_from_stored_from_effective(self, client):
        r = await client.get("/api/v1/role-privileges")
        assert r.status_code == 200, r.text
        by_role = {x["role"]: x for x in r.json()["roles"]}

        dev = by_role["developer"]
        # Three separate answers on purpose: an admin who cannot see the seed
        # will re-grant what the role already has, or try to remove it by
        # deleting a row that never existed.
        assert dev["stored"] == []
        assert dev["seeded"], "developer has seeded marketplace/devconn grants"
        assert dev["effective"] == dev["seeded"]

    async def test_put_grants_and_takes_effect_immediately(self, client):
        body = {"privileges": [{"resource": "risk.assessment", "actions": ["read"]}]}
        r = await client.put("/api/v1/role-privileges/developer", json=body)
        assert r.status_code == 200, r.text

        assert _holds("developer", "risk.assessment.read"), (
            "the writing worker must not wait for the cron")
        assert r.json()["stored"] == body["privileges"]
        assert {"resource": "risk.assessment", "actions": ["read"]} in r.json()["effective"]

    async def test_put_is_a_replace_of_the_stored_half_only(self, client):
        await client.put("/api/v1/role-privileges/developer", json={
            "privileges": [{"resource": "risk.assessment", "actions": ["read"]}]})
        await client.put("/api/v1/role-privileges/developer", json={
            "privileges": [{"resource": "risk.rules", "actions": ["configure"]}]})

        assert not _holds("developer", "risk.assessment.read"), "stored half replaced"
        assert _holds("developer", "risk.rules.configure")
        assert _holds("developer", "marketplace.packages.publish"), "seed disturbed"

    async def test_unknown_role_is_404(self, client):
        r = await client.put("/api/v1/role-privileges/wizard",
                             json={"privileges": []})
        assert r.status_code == 404

    async def test_malformed_entry_is_400_not_a_silent_drop(self, client):
        r = await client.put("/api/v1/role-privileges/developer", json={
            "privileges": [{"resource": "risk.assessment", "actions": ["read"],
                            "effect": "maybe"}]})
        assert r.status_code == 400
        assert not _holds("developer", "risk.assessment.read")

    async def test_the_change_is_audited(self, client, session):
        from sqlalchemy import select
        from case_service.db.models import SecurityEventModel

        await client.put("/api/v1/role-privileges/developer", json={
            "privileges": [{"resource": "risk.taxonomy", "actions": ["write"]}]})

        rows = (await session.execute(
            select(SecurityEventModel)
            .where(SecurityEventModel.event_type == "role_privileges_changed")
        )).scalars().all()
        assert rows, "a privilege change with no trace is not auditable"
        assert rows[-1].resource_id == "developer"
        assert rows[-1].details["after"] == [
            {"resource": "risk.taxonomy", "actions": ["write"]}]


class TestCatalogueExposesPlatformActions:
    """Item (c) — without this the store has no admin surface.

    The catalogue drove the privilege editor and listed only the 16 legacy
    case-level resources, so no screen could name a platform action.
    """

    async def test_platform_resources_are_offered(self, client):
        r = await client.get("/api/v1/access-roles/catalog")
        assert r.status_code == 200, r.text
        ids = {x["id"] for x in r.json()["resources"]}

        assert "risk.assessment" in ids
        assert "identity.groups" in ids
        assert "case" in ids, "the legacy half must survive"

    async def test_every_offered_platform_resource_can_actually_be_granted(self, client):
        """A checkbox that grants nothing is worse than no checkbox."""
        from case_service.hxguard.privileges import _crosses_into_platform_namespace

        r = await client.get("/api/v1/access-roles/catalog")
        for res in r.json()["resources"]:
            if not res.get("platform"):
                continue
            for act in res["actions"]:
                assert _crosses_into_platform_namespace(
                    {"resource": res["id"], "actions": [act["id"]]}), res["id"]

    async def test_the_two_namespaces_stay_distinguishable(self, client):
        """They have different evaluators; blurring them misleads the admin."""
        r = await client.get("/api/v1/access-roles/catalog")
        by_id = {x["id"]: x for x in r.json()["resources"]}
        assert by_id["risk.assessment"].get("platform") is True
        assert not by_id["case"].get("platform")

    async def test_the_catalogue_tracks_the_registry(self, client):
        """Generated, not hand-listed — so a new action is grantable at once."""
        from case_service.hxguard.actions import ACTIONS, resource_of, verb_of
        from case_service.hxguard.privileges import _crosses_into_platform_namespace

        r = await client.get("/api/v1/access-roles/catalog")
        offered = {(x["id"], a["id"]) for x in r.json()["resources"]
                   if x.get("platform") for a in x["actions"]}
        expected = {(resource_of(n), verb_of(n)) for n in ACTIONS
                    if _crosses_into_platform_namespace(
                        {"resource": resource_of(n), "actions": [verb_of(n)]})}
        assert offered == expected


class TestLockoutGuardCoversTheStore:
    """A `deny` here revokes for EVERY holder of the role at once.

    That makes this the fastest route to nobody holding `identity.groups.write`
    the platform has — faster than removing members one at a time, which is what
    the guard was originally built for.
    """

    async def _an_admin(self, session):
        import uuid as _uuid
        from case_service.db.models import HelixUserModel

        user = HelixUserModel(
            username=f"adm-{_uuid.uuid4().hex[:8]}",
            email=f"adm-{_uuid.uuid4().hex[:8]}@example.com",
            roles=["admin"], is_active=True, is_superadmin=False)
        session.add(user)
        await session.commit()
        return user

    async def test_denying_the_bootstrap_action_is_refused(self, client, session):
        await self._an_admin(session)

        r = await client.put("/api/v1/role-privileges/admin", json={
            "privileges": [{"resource": "identity.groups", "actions": ["write"],
                            "effect": "deny"}]})

        assert r.status_code == 409, r.text
        assert _holds("admin", "identity.groups.write"), "refused but applied anyway"

    async def test_the_refusal_leaves_the_cache_untouched(self, client, session):
        """The pre-check installs the prospective state to ask the question.

        If it were not restored in a `finally`, a refused edit would apply itself
        in memory — the change rejected on disk and live in the cache.
        """
        await self._an_admin(session)
        before = role_store.privileges_for("admin")

        await client.put("/api/v1/role-privileges/admin", json={
            "privileges": [{"resource": "identity.groups", "actions": ["write"],
                            "effect": "deny"}]})

        assert role_store.privileges_for("admin") == before

    async def test_an_unrelated_deny_is_allowed_through(self, client, session):
        """HAPPY PATH: the guard must only refuse the lockout, not every deny."""
        await self._an_admin(session)

        r = await client.put("/api/v1/role-privileges/manager", json={
            "privileges": [{"resource": "risk.assessment", "actions": ["read"],
                            "effect": "deny"}]})

        assert r.status_code == 200, r.text
        assert not _holds("manager", "risk.assessment.read")
        assert _holds("admin", "identity.groups.write")
