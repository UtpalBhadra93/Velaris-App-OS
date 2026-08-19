"""Access-role names that used to mean something, and no longer do.

From P37 until Phase 4 step 1, `_enrich_with_access_group` merged an access
role's NAME into `AuthenticatedUser.roles`. The seeds were chosen to collide
deliberately — `admin`, `staff`, `viewer` matched the strings `require_role()`
compared against, so routes kept working without being touched. The cost was
that *naming* an access role `admin` conferred platform administration.

Step 1 removed the merge. On any deployment where an operator reached platform
admin only by that route, the privileges they exercised yesterday are gone
today — and nothing in the request path can say so, because a denial looks
identical to a correct denial.

So the collision is reported instead of guessed at:

- at startup, as a warning naming each colliding role and how many operators
  reach it;
- on `GET /status/authz-role-collisions`, for an operator who wants to check
  before upgrading rather than after.

The remedy is never to rename the role (that revokes case-level access for
every member at once). It is to give those operators the platform role on their
account, or to write the access role's platform intent as a privilege entry in
the dotted namespace — the form the model can actually evaluate.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from case_service.hxguard.privileges import KNOWN_ROLES

log = logging.getLogger(__name__)

#: Names that carried platform meaning while the merge existed. Compared
#: case-insensitively: the merge unioned raw strings, but an operator reading a
#: warning about `Admin` should not have to know that.
RESERVED_PLATFORM_ROLES: frozenset[str] = frozenset(r.lower() for r in KNOWN_ROLES)


async def colliding_access_roles(session: AsyncSession) -> list[dict[str, Any]]:
    """Access roles whose name used to grant platform privileges by itself.

    Reports the blast radius, not just the name: a colliding role nobody holds
    is a cosmetic finding, and one held by every operator is an upgrade task.
    """
    from case_service.db.models import (
        AccessGroupModel, AccessRoleModel, OperatorAccessGroupModel,
    )

    roles = (await session.execute(select(AccessRoleModel))).scalars().all()
    colliding = [r for r in roles if str(r.name or "").lower() in RESERVED_PLATFORM_ROLES]
    if not colliding:
        return []

    groups = (await session.execute(
        select(AccessGroupModel).where(AccessGroupModel.is_active == True)  # noqa: E712
    )).scalars().all()
    members: dict[str, set[str]] = {}
    for row in (await session.execute(select(OperatorAccessGroupModel))).scalars().all():
        members.setdefault(str(row.access_group_id), set()).add(str(row.operator_id))

    out: list[dict[str, Any]] = []
    for role in colliding:
        carrying = [g for g in groups
                    if str(role.id) in {str(r) for r in (g.role_ids or [])}]
        operators: set[str] = set()
        for g in carrying:
            operators |= members.get(str(g.id), set())
        out.append({
            "access_role_id": str(role.id),
            "access_role_name": role.name,
            "groups": [{"id": str(g.id), "name": g.name} for g in carrying],
            "operators_affected": len(operators),
            "granted_platform_role_until_phase4": role.name,
        })
    return sorted(out, key=lambda r: -r["operators_affected"])


async def warn_on_collisions(session: AsyncSession) -> list[dict[str, Any]]:
    """Log the collisions once at startup. Never raises — this is a report."""
    try:
        found = await colliding_access_roles(session)
    except Exception as exc:                      # pragma: no cover - reporting only
        log.debug("access-role collision check skipped: %s", exc)
        return []

    for row in found:
        (log.warning if row["operators_affected"] else log.info)(
            "access role %r shares a platform role name. Until Phase 4 step 1 that "
            "name alone conferred the %r platform role; it no longer does. "
            "%d operator(s) reach it. Grant the platform role on their account, or "
            "add a privilege entry in the dotted namespace (e.g. "
            "{\"resource\": \"identity.users\", \"actions\": [\"read\"]}). "
            "Do NOT rename the role — that revokes case-level access for every member.",
            row["access_role_name"], row["access_role_name"],
            row["operators_affected"],
        )
    return found
