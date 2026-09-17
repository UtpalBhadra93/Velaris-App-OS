"""Privilege evaluation — Phase 1 of privilege-based authorization.

The decision moves from *matching a role name* to *evaluating privileges against
a named action*. A role becomes a bundle of privileges and nothing else.

**Nothing here is enforced yet.** This backend is registered alongside the
existing `require_role` gates, not in front of them. Phase 2 shadows it against
them; Phase 3 cuts over. Until then the only claim this module makes is the one
`tests/phase_authz` proves: for every action and every subject shape, it returns
exactly what `require_role` returns today.

Two design points that are easy to get wrong and expensive to get wrong:

**Seeding is derived, not written.** `SEEDED_ROLE_PRIVILEGES` is computed from
each action's `legacy_roles` rather than hand-authored, so the seed cannot drift
from the gates it reproduces. Hand-writing it would make the parity test a test
of my typing.

**`superadmin` is an identity bypass, permanently.** Not a temporary crutch —
it is the break-glass that makes a bad privilege configuration recoverable
instead of terminal. A privilege model that can lock every human out of the
screen that edits privileges has failed at the only moment it mattered.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

from typing import Any, Iterable

from velaris_shared.hxguard.actions import ACTIONS, GATE_ROLES, resource_of, verb_of

#: A privilege entry: {"resource": "compliance.*", "actions": ["read"],
#:                     "effect": "allow" | "deny"}
Privilege = dict[str, Any]

WILDCARD = "*"


# ── matching ──────────────────────────────────────────────────────

def resource_matches(pattern: str, resource: str) -> bool:
    """Segment-wise match, so a wildcard never over-reaches.

    `compliance.*` matches `compliance.ledger` but **not** `compliance_admin.x`
    — the comparison is on dot-separated segments, never on string prefixes.
    Getting this wrong is the classic way a privilege system quietly becomes
    permissive, so it is tested with generated names rather than examples.
    """
    if pattern == WILDCARD:
        return True
    pat = pattern.split(".")
    res = resource.split(".")
    if pat[-1] == WILDCARD:
        head = pat[:-1]
        return len(res) >= len(head) and res[:len(head)] == head
    return pat == res


def _verb_matches(allowed: Iterable[str], verb: str) -> bool:
    allowed = list(allowed)
    return WILDCARD in allowed or verb in allowed


def privilege_grants(priv: Privilege, action: str) -> bool:
    return (resource_matches(str(priv.get("resource", "")), resource_of(action))
            and _verb_matches(priv.get("actions") or [], verb_of(action)))


def evaluate_privileges(privs: Iterable[Privilege], action: str) -> bool:
    """Allow only if some entry grants it and none denies it.

    **Deny always wins**, whatever the order. Without an explicit deny,
    "administers everything except identity" cannot be expressed at all, and the
    migration stalls on exactly that case. No seeded role uses deny — the
    capability exists so Phase 3 has it when a real deployment needs it.
    """
    allowed = False
    for p in privs:
        if not privilege_grants(p, action):
            continue
        if str(p.get("effect", "allow")).lower() == "deny":
            return False
        allowed = True
    return allowed


# ── seeded role privileges (derived from the gates) ───────────────

ALL_ACCESS: list[Privilege] = [{"resource": WILDCARD, "actions": [WILDCARD]}]


def _privileges_for_role(role: str) -> list[Privilege]:
    """Exactly what `require_role` grants a subject holding only this role.

    `require_role` passes when the subject is superadmin, **or is admin**, or
    holds one of the named roles. So `admin` gets everything, and every other
    role gets precisely the actions naming it — including the ones where it was
    never written down, because admin's blanket pass made it unnecessary.
    """
    if role in ("admin", "superadmin"):
        return list(ALL_ACCESS)
    granted = sorted(a.name for a in ACTIONS.values() if role in a.legacy_roles)
    return [{"resource": resource_of(n), "actions": [verb_of(n)]} for n in granted]


#: Every role the platform knows, mapped to the privileges reproducing today's
#: behaviour. Roles absent from every gate (viewer, manager, case_worker,
#: developer, staff) seed to an EMPTY set — that is not an oversight, it is what
#: they can do at these gates today.
KNOWN_ROLES: tuple[str, ...] = (
    "superadmin", "admin", "manager", "designer", "case_worker",
    "devops", "integration", "security", "viewer", "developer", "staff",
    # Phase 4 step 2. `finance` was never named by a *dependency* gate, so it
    # seeded to nothing — but it was named by imperative checks in `payments.py`
    # and `crm.py` ("admin or finance sees unredacted financials"). Omitting it
    # here would silently revoke that at migration, which is exactly the kind of
    # tightening the parity discipline exists to prevent.
    "finance",
)

SEEDED_ROLE_PRIVILEGES: dict[str, list[Privilege]] = {
    role: _privileges_for_role(role) for role in KNOWN_ROLES
}


#: Resources named by access-group privileges as they exist today: a flat,
#: case-level vocabulary predating this namespace. Verified against the seeded
#: access roles on 2026-08-13 — `case`, `case_type`, `form`, `workflow`,
#: `document`. None of them collide with a platform action.
LEGACY_PRIVILEGE_RESOURCES = frozenset({
    "case", "case_type", "form", "workflow", "document", "queue", "report",
})

#: Platform resources that happen to sit UNDER a legacy head, and are reachable
#: from data anyway.
#:
#: The rule above is about **vocabulary**, not about spelling: a row saying
#: `{"resource": "case"}` was written years ago and means "every case-level
#: resource", so it must not reach a platform action. A row saying
#: `{"resource": "case.actions"}` could not have been written years ago — that
#: resource did not exist until migration 198 — so there is no legacy meaning to
#: protect and nothing an existing grant could accidentally satisfy.
#:
#: Without this, `case.actions.close` and its five siblings were reachable ONLY
#: through the seeded role table: an operator could not grant "may close a case"
#: to a role of their own from data, which is the corner of the model
#: `test_no_new_action_is_ungrantable` exists to keep empty. The same trap took
#: four `risk.*` actions on 2026-08-15, where the fix was a rename; a rename is
#: the wrong tool here, because `case.actions.*` is the honest name and the
#: eleven actions it gates are, in fact, case actions.
#:
#: **Exact matches only** — a prefix rule would let `case.anything.x` through.
PLATFORM_RESOURCES_UNDER_A_LEGACY_HEAD = frozenset({
    "case.actions",
})


def _crosses_into_platform_namespace(priv: Privilege) -> bool:
    """Does this access-group entry mean anything in the *platform* namespace?

    **The bare `*` deliberately does not.** The seeded `admin` access role carries
    `{"resource": "*", "actions": ["*"]}`, written years before platform actions
    existed and meaning "every *case-level* resource". Letting it satisfy
    `identity.users.write` would mean an access role silently conferring platform
    administration — invisible today, because the role-name merge already makes
    those users admin, and therefore invisible right up until Phase 4 removes
    that merge and the wildcard becomes the only thing granting it.

    So the boundary is explicit: an access-group entry reaches a platform action
    only by naming the new dotted namespace. Legacy vocabulary and legacy
    wildcards stay in the domain they were written for.
    """
    resource = str(priv.get("resource", ""))
    if resource in ("", WILDCARD):
        return False
    if resource in PLATFORM_RESOURCES_UNDER_A_LEGACY_HEAD:
        return True
    head = resource.split(".")[0]
    if head in LEGACY_PRIVILEGE_RESOURCES:
        return False
    return "." in resource


def effective_privileges(roles: Iterable[str], access_group_privileges: Iterable[Privilege] | None = None) -> list[Privilege]:
    """The privilege set a subject holds **for platform actions**.

    Seeded role privileges, plus only those access-group entries written in this
    namespace (see `_crosses_into_platform_namespace`). Case-level authorization
    is unaffected — `has_privilege()` and `core/access_control.py` keep reading
    the same rows with their own vocabulary.
    """
    from velaris_shared.hxguard import role_store

    out: list[Privilege] = []
    for role in roles or []:
        out.extend(SEEDED_ROLE_PRIVILEGES.get(role, []))
        # Phase 6: entries an admin granted this role, ADDED to the seed rather
        # than replacing it. The store is served from an in-process cache, so an
        # unloaded or failed cache contributes nothing and this call degrades to
        # exactly the pre-Phase-6 answer — see `role_store`. A "replace"
        # semantic would instead make a cold cache revoke everything.
        out.extend(p for p in role_store.privileges_for(role)
                   if _crosses_into_platform_namespace(p))
    out.extend(p for p in (access_group_privileges or [])
               if _crosses_into_platform_namespace(p))
    return out


# ── the backend ───────────────────────────────────────────────────

class PrivilegeBackend:
    """Answers `check(subject, action)` from privileges rather than role names.

    Shares `RbacBackend`'s contract: never raises, fail-closed on anything it
    does not understand, and returns a `Decision` the caller can audit.
    """

    name = "privilege"

    def evaluate(self, subject, action: str, resource: dict | None = None):
        from velaris_shared.hxguard.service import Decision

        if action not in ACTIONS:
            return Decision(False, f"unknown action '{action}' (fail-closed)", self.name)
        if getattr(subject, "kind", "user") != "user":
            return Decision(
                False, f"action '{action}' requires a user subject, got {subject.kind}",
                self.name)

        roles = list(getattr(subject, "roles", None) or [])
        if "superadmin" in roles:
            return Decision(True, "superadmin break-glass", self.name)

        privs = effective_privileges(roles, getattr(subject, "privileges", None))
        if evaluate_privileges(privs, action):
            return Decision(True, f"privilege grants '{action}'", self.name)
        return Decision(False, f"no privilege grants '{action}'", self.name)


# ── parity oracle ─────────────────────────────────────────────────

def legacy_role_gate_allows(subject_roles: Iterable[str], required: Iterable[str]) -> bool:
    """What `require_role(*required)` decides — the Phase-1 parity oracle.

    Mirrors `auth/dependencies.py::require_role._check` exactly, including the
    two bypasses that are easy to forget: superadmin passes everything, and
    `is_admin` passes everything, so `require_role("designer")` has always meant
    "designer **or admin**".
    """
    roles = set(subject_roles or [])
    if "superadmin" in roles or "admin" in roles:
        return True
    return bool(set(required) & roles)


__all__ = [
    "PrivilegeBackend", "SEEDED_ROLE_PRIVILEGES", "KNOWN_ROLES", "GATE_ROLES",
    "effective_privileges", "evaluate_privileges", "resource_matches",
    "privilege_grants", "legacy_role_gate_allows", "ALL_ACCESS",
]
