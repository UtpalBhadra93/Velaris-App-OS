"""Auth data models.

Copyright (c) 2024-2025 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class ActiveAccessGroup:
    """The operator's currently active access group context (P37)."""
    id: str
    name: str
    portal_id: str
    portal_type: str
    portal_name: str
    modules: list[str]
    homepage: str
    roles: list[str]         # access-role names — display + case-level vocabulary,
                             # NOT a platform authorization input (Phase 4 step 1)
    privileges: list[dict]   # raw ABAC privilege objects for evaluate_access()
    allowed_case_type_ids: list[str]
    allowed_queue_ids: list[str]


@dataclass
class AuthenticatedUser:
    """Represents the current authenticated user.

    `roles` holds **platform** roles only — what the token carries, which is
    what the account row carries. From P37 until Phase 4 step 1 the active
    access group's role NAMES were merged in as well, so an access role called
    `admin` conferred platform administration; that bridge existed for
    `require_role()`, which has had no call sites since Phase 3.

    Access-group role names are still available on `active_access_group.roles`
    for the UI and for the case-level vocabulary in `core/access_control.py` —
    they simply no longer answer a platform authorization question.
    """
    user_id: str
    username: str = ""
    email: str = ""
    roles: list[str] = field(default_factory=list)
    groups: list[str] = field(default_factory=list)
    department: str = ""
    attributes: dict[str, Any] = field(default_factory=dict)
    token: str = ""
    tenant_id: str | None = None          # owning tenant (from user_directory)
    # P37 — None when operator has no access group assigned yet
    active_access_group: ActiveAccessGroup | None = None
    available_access_groups: list[dict] = field(default_factory=list)

    # ── DISPLAY FLAGS, NOT AUTHORIZATION (Phase 4 step 2) ─────────────
    #
    # These three were the platform's authorization primitives. `is_admin` was
    # literally `"admin" in self.roles`, and `has_role()` returned True for
    # *every* role once admin was present — so "does this user hold the designer
    # role" and "is this user an admin" were the same question, and no gate could
    # tell them apart.
    #
    # They survive only to populate `to_dict()`, which the UI reads to decide
    # which nav items to render. **A denial must never be computed from them.**
    # Ask `hxguard.user_allows(user, action)` (sync, for a boolean that shapes a
    # response) or `hxguard.require_user(session, user, action)` (for a refusal
    # that should be audited) instead. `tests/phase_authz` enforces this: the
    # routers are AST-scanned, and a new read of these attributes fails.

    @property
    def is_admin(self) -> bool:
        """UI badge only. Not an authorization input — see the note above."""
        return "admin" in self.roles

    @property
    def is_designer(self) -> bool:
        """UI badge only. No longer inherits from `is_admin`: conflating the two
        is what made `has_role("designer")` true for every admin."""
        return "designer" in self.roles

    @property
    def is_case_worker(self) -> bool:
        """UI badge only. See `is_designer`."""
        return "case_worker" in self.roles

    def has_privilege(self, resource: str, action: str) -> bool:
        """Does the active access group grant `action` on `resource`?

        This is the **case-level** vocabulary — `case`, `case_type`, `form`,
        `workflow`, `document` — configured per access role in the Access
        Directory. It is not the platform action namespace; for that, ask
        HxGuard.

        Privilege entries look like:
            {"resource": "case_type", "actions": ["create", "update"]}
            {"resource": "*",         "actions": ["*"]}   ← wildcard

        Superadmin passes unconditionally, as everywhere else.

        **Phase 4 step 3 removed the fallback to `is_admin` for a user with no
        access group.** That fallback meant every admin silently held every
        case-level privilege, including `db_manager / view_sensitive` — the one
        HxDBManager's DBView feature exists to require an *explicit* grant for,
        so that a non-superadmin admin can be given Tier-2 visibility without
        Tier-1 and DDL along with it. With the fallback in place that feature
        granted itself to exactly the people it was written to gate.
        """
        if "superadmin" in (self.roles or []):
            return True

        ag = self.active_access_group
        if ag is None:
            return False          # no access group ⇒ no case-level privileges

        for priv in (ag.privileges or []):
            priv_resource = priv.get("resource", "")
            priv_actions = priv.get("actions", [])
            resource_match = priv_resource in ("*", resource)
            action_match = "*" in priv_actions or action in priv_actions
            if resource_match and action_match:
                return True

        return False

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "user_id": self.user_id,
            "username": self.username,
            "email": self.email,
            "roles": self.roles,
            "groups": self.groups,
            "department": self.department,
            "is_admin": self.is_admin,
            "is_designer": self.is_designer,
            "is_case_worker": self.is_case_worker,
        }
        if self.active_access_group:
            ag = self.active_access_group
            d["active_access_group"] = {
                "id": ag.id,
                "name": ag.name,
                "portal": {
                    "id": ag.portal_id,
                    "name": ag.portal_name,
                    "portal_type": ag.portal_type,
                    "modules": ag.modules,
                    "homepage": ag.homepage,
                },
                "roles": ag.roles,
                "privileges": ag.privileges,
                "allowed_case_type_ids": ag.allowed_case_type_ids,
                "allowed_queue_ids": ag.allowed_queue_ids,
            }
        d["available_access_groups"] = self.available_access_groups
        return d
