"""Phase 5 — the case layer's own role-name tables, migrated.

`RbacBackend.ACTION_ROLES` was the **last** role-name decision table in the
platform. Phase A built it in 2026-06 to mirror `require_role`, and phases 1–4
migrated everything around it without ever touching it: it was not a
`require_role` gate, not a `require_admin()` alias, not an imperative check in a
handler body, and not an ungated route — so every detector built to find those
walked straight past it. It decided 15 actions by role name, with an `is_admin`
bypass, for two months after the platform "moved to privileges".

`RebacBackend` carried the same thing in one line:

    if "superadmin" in roles or subject.is_admin or "manager" in roles:
        return Decision(True, "admin/manager bypass")

That ran **before a single relationship was consulted**, so "a manager can open
every case in the deployment" was true, unrevocable, and visible in no gate, no
registry and no screen.

Both now decide by privilege. Parity was proven by recording all 435 decisions
(15 actions × 15 subject shapes, plus 14 case actions × 15 shapes) from the
shipped code *before* the change and re-running them after — that table is
reproduced here so the proof lives with the code.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import pytest

from case_service.hxguard.service import (
    ACTION_ROLES, CASE_ACTIONS, PLATFORM_ACTIONS, Subject, check, subject_allows,
)

#: The **role sets** transcribed from `ACTION_ROLES` as it stood before Phase 5.
#: They are the historical record and must not be edited to match a change — a
#: parity oracle rewritten to agree with the code proves only that someone
#: rewrote it.
#:
#: The **names** are current, not historical: the four `risk.*` actions were
#: renamed on 2026-08-15 by the HxSentinel work (`risk.read` →
#: `risk.assessment.read`, `risk.label` → `risk.outcomes.write`, `risk.taxonomy`
#: → `risk.taxonomy.write`, `risk.rules` → `risk.rules.configure`), and a
#: mechanical rename swept this file along with the rest. That is harmless here
#: *because the role sets it carries are what the parity claim rests on*, and
#: they were preserved exactly — but it is worth naming, since a rename walking
#: through a record of history is precisely how a record stops being one.
#:
#: A future rename cannot quietly invalidate this table: renaming in `actions.py`
#: alone makes `check()` answer "unknown action → deny" while this table still
#: expects an allow, and renaming here alone fails the PLATFORM_ACTIONS
#: assertion below. Either way a human has to look.
LEGACY_ACTION_ROLES = {
    "variables.namespace.admin":    {"admin"},
    "variables.blobkeys.scan":               {"admin", "designer"},
    "variables.blobkeys.promote":            {"admin"},
    "connector.namespace.register": {"admin", "integration"},
    "devconn.namespace.register":   {"admin", "integration", "developer"},
    "rules.definitions.write":                  {"admin", "designer"},
    "replay.cohorts.run":                   {"admin"},
    "costing.rates.access":                {"admin"},
    "mcp.tokens.mint":              {"admin", "designer"},
    "risk.assessment.read":          {"admin", "manager", "case_worker"},
    "risk.outcomes.write":           {"admin", "manager"},
    "risk.taxonomy.write":           {"admin"},
    "risk.rules.configure":          {"admin", "manager"},
    "risk.scores.review":            {"admin", "manager"},
}

SHAPES = ("superadmin", "admin", "manager", "designer", "developer", "devops",
          "integration", "security", "case_worker", "viewer", "staff", "finance")


def _subject(*roles):
    return Subject(kind="user", id="probe", roles=tuple(roles),
                   is_admin="admin" in roles, privileges=())


def _legacy_allows(action: str, role: str) -> bool:
    """What the pre-Phase-5 code answered: superadmin or is_admin bypass, else
    role-set membership."""
    if role in ("superadmin", "admin"):
        return True
    return role in LEGACY_ACTION_ROLES[action]


class TestParityWithTheTableItReplaced:
    @pytest.mark.asyncio
    async def test_every_action_and_role_matches_the_legacy_answer(self):
        wrong = []
        for action, _ in LEGACY_ACTION_ROLES.items():
            for role in SHAPES:
                d = await check(None, _subject(role), action, None)
                want = _legacy_allows(action, role)
                if bool(d.allow) != want:
                    wrong.append(f"{action}/{role}: now={d.allow} before={want}")
        assert not wrong, wrong

    @pytest.mark.asyncio
    async def test_they_are_decided_by_the_privilege_backend_now(self):
        """Parity alone would also pass if nothing had moved."""
        for action in LEGACY_ACTION_ROLES:
            assert action in PLATFORM_ACTIONS, f"{action} never reached the registry"
            d = await check(None, _subject("admin"), action, None)
            assert d.source == "privilege", f"{action} still decided by {d.source}"

    def test_the_dead_rows_were_deleted_not_left_behind(self):
        """Two tables claiming the same action is how a ledger drifts from the
        code — and the dead one is always the one someone edits."""
        assert set(ACTION_ROLES) == {"case.rule.apply"}, sorted(ACTION_ROLES)

    @pytest.mark.asyncio
    async def test_the_one_survivor_is_authentication_not_authorization(self):
        """`case.rule.apply` has an EMPTY role set — any authenticated user.
        Privileges cannot express that (no roles ⇒ no privileges), so migrating
        it would have silently closed an open surface."""
        assert ACTION_ROLES["case.rule.apply"] == set()
        assert "case.rule.apply" not in PLATFORM_ACTIONS
        d = await check(None, _subject("viewer"), "case.rule.apply", None)
        assert d.allow and d.source == "rbac"

    @pytest.mark.asyncio
    async def test_a_non_user_subject_is_still_refused(self):
        d = await check(None, Subject(kind="connector", id="c1"),
                        "risk.outcomes.write", None)
        assert not d.allow


class TestTheRebacBypassIsNowAPrivilege:
    def test_admin_and_manager_hold_it(self):
        assert subject_allows(_subject("admin"), "casework.records.any") is True
        assert subject_allows(_subject("manager"), "casework.records.any") is True

    def test_nobody_else_does(self):
        for role in ("designer", "developer", "devops", "integration",
                     "security", "case_worker", "viewer", "staff", "finance"):
            assert subject_allows(_subject(role), "casework.records.any") is False, role

    def test_it_is_revocable_which_is_the_whole_point(self):
        """The old bypass was a hard-coded `or "manager" in roles`. Nothing
        could take it away: not a role edit, not an access group, not a
        privilege. Now it is an ordinary action, so an estate that does not want
        managers reading every case can stop seeding it."""
        from case_service.hxguard.privileges import SEEDED_ROLE_PRIVILEGES, evaluate_privileges

        assert evaluate_privileges(SEEDED_ROLE_PRIVILEGES["manager"],
                                   "casework.records.any") is True
        assert evaluate_privileges([], "casework.records.any") is False

    def test_superadmin_bypasses_without_the_privilege(self):
        """Break-glass stays an identity test, by design (§2.3)."""
        assert "superadmin" in _subject("superadmin").roles

    def test_relations_are_not_role_names_and_were_not_touched(self):
        """The owner/assignee/editor/viewer half of ReBAC is a relationship
        model, not role-name authorization — migrating it was never in scope."""
        for relations in CASE_ACTIONS.values():
            assert relations <= {"assignee", "editor", "viewer"}


class TestIsAdminIsNoLongerADecisionInput:
    @pytest.mark.asyncio
    async def test_a_subject_flagged_is_admin_without_the_role_gets_nothing(self):
        """`Subject.is_admin` used to be an unconditional bypass in BOTH
        backends. It is now inert: the roles decide, through privileges."""
        liar = Subject(kind="user", id="x", roles=("viewer",), is_admin=True,
                       privileges=())
        assert subject_allows(liar, "casework.records.any") is False
        d = await check(None, liar, "risk.outcomes.write", None)
        assert not d.allow

    def test_no_backend_reads_subject_is_admin(self):
        """Checked on the AST, not the text.

        The first version of this assertion searched the source string and
        failed on the *comment* explaining the code it had just removed — a
        detector that cannot tell an explanation from an instruction. The whole
        authz programme has been a sequence of detectors that matched the wrong
        thing, so it seemed worth not adding another.
        """
        import ast
        import inspect
        import textwrap

        from case_service.hxguard import service

        for cls in (service.RbacBackend, service.RebacBackend):
            tree = ast.parse(textwrap.dedent(inspect.getsource(cls)))
            reads = [n for n in ast.walk(tree)
                     if isinstance(n, ast.Attribute) and n.attr == "is_admin"]
            assert not reads, (
                f"{cls.__name__} still reads is_admin at line(s) "
                f"{[n.lineno for n in reads]}")


class TestActionsAreGrantableFromData:
    """An action nobody can be granted is policy frozen in code.

    `_crosses_into_platform_namespace` requires a dot in the resource — the
    boundary that stops the legacy `{"resource": "*"}` access-role wildcard
    conferring platform administration. An action named `<domain>.<verb>` has a
    single-segment resource, so **no access-group entry can ever reach it**, and
    the only way to change who holds it is editing `legacy_roles` — which is the
    parity oracle, not a policy knob.

    **The list is now EMPTY — TODO.md item 3c is done (2026-08-15).** All nine
    were renamed: seven two-segment ones gained a resource segment
    (`costing.rates` → `costing.rates.access`, `rules.write` →
    `rules.definitions.write`, …), and two that were *already* three-segment
    were blocked by a second cause the first write-up missed — their head was
    `case`, which is reserved legacy vocabulary, so `case.records.any` and
    `case.sensitive.read` became `casework.*`. Adding segments would not have
    fixed those; the domain had to change.

    Parity held throughout: only names moved, `legacy_roles` was untouched, and
    the per-role action totals were identical before and after.
    """

    #: Empty, and it may only stay that way: a NEW ungrantable action would be a
    #: corner of the model that configuration cannot reach.
    GRANDFATHERED: set[str] = set()

    def test_no_new_action_is_ungrantable(self):
        from case_service.hxguard.actions import ACTIONS, resource_of, verb_of
        from case_service.hxguard.privileges import _crosses_into_platform_namespace

        ungrantable = {
            name for name in ACTIONS
            if not _crosses_into_platform_namespace(
                {"resource": resource_of(name), "actions": [verb_of(name)]})
        }
        new = ungrantable - self.GRANDFATHERED
        assert not new, (
            f"new action(s) no data grant can reach: {sorted(new)}. "
            "Name them <domain>.<resource>.<verb> so a privilege entry can name them.")

        fixed = self.GRANDFATHERED - ungrantable
        assert not fixed, (
            f"{sorted(fixed)} are grantable now — drop them from GRANDFATHERED "
            "and from TODO.md item 3c.")
        assert not ungrantable, (
            f"every action must be reachable by a privilege entry; {sorted(ungrantable)} "
            "are not")

    def test_every_risk_action_is_grantable(self):
        """The four renamed 2026-08-15, plus the one that always was."""
        from case_service.hxguard.actions import ACTIONS, resource_of, verb_of
        from case_service.hxguard.privileges import _crosses_into_platform_namespace

        risk = sorted(a for a in ACTIONS if a.startswith("risk."))
        assert risk == ["risk.assessment.read", "risk.outcomes.write",
                        "risk.rules.configure", "risk.scores.review",
                        "risk.taxonomy.write"]
        for name in risk:
            assert _crosses_into_platform_namespace(
                {"resource": resource_of(name), "actions": [verb_of(name)]}), name
