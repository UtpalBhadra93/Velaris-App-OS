"""Lockout protection — the guard that makes Phase 3 survivable.

A privilege model can lock every human out of the screen that edits privileges.
`access_groups.py` holds 19 of the platform gates and it is *the router that
grants access*, so the failure mode is not hypothetical: revoke the wrong role,
and the ability to un-revoke it goes with it.

Three defences, in the order they fire:

1. **This module** refuses any write that would leave nobody holding
   `identity.groups.write` — checked before the write, because afterwards there
   is no one left to authorise the repair.
2. **`superadmin`** remains an unconditional bypass, permanently. It is the
   account the refusal above assumes exists.
3. **`scripts/grant_privilege.py`** operates directly on the database, for the
   case where someone edited rows behind the API and defeated both.

The check is deliberately narrow. It protects the *ability to grant access*, not
every privilege: a model that refuses any reduction in permissions is one nobody
can use, and it would be routed around within a week.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import logging
from typing import Any, Iterable

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from case_service.hxguard.privileges import evaluate_privileges

log = logging.getLogger(__name__)

#: Losing this action means losing the ability to restore any other. It is the
#: single thing this module protects.
BOOTSTRAP_ACTION = "identity.groups.write"


async def _holders_of(session: AsyncSession, action: str) -> set[str]:
    """Every active operator who can currently perform `action`.

    Counts both routes to it — the platform roles on the account, and the
    privileges reachable through access-group membership — because a deployment
    part-way through the migration will have people arriving by each.
    """
    from case_service.db.models import (
        AccessGroupModel, AccessRoleModel, HelixUserModel,
        OperatorAccessGroupModel,
    )
    from case_service.hxguard.privileges import effective_privileges

    holders: set[str] = set()

    users = (await session.execute(
        select(HelixUserModel).where(HelixUserModel.is_active == True)  # noqa: E712
    )).scalars().all()

    # Keyed by every identifier a membership row might carry. `operator_id` is
    # free text: the SSO sync and the admin screens write a username or email,
    # while tokens carry the account UUID (see `_enrich_with_access_group`).
    # Keying only by UUID meant this guard never saw a membership-derived
    # holder — it UNDERCOUNTED, which for a lockout check is the safe direction
    # (it refuses more often than needed) but made the "counts both routes"
    # promise in the docstring untrue.
    memberships: dict[str, list[Any]] = {}
    for row in (await session.execute(select(OperatorAccessGroupModel))).scalars().all():
        memberships.setdefault(str(row.operator_id), []).append(row)

    groups = {str(g.id): g for g in
              (await session.execute(select(AccessGroupModel))).scalars().all()}
    roles = {str(r.id): r for r in
             (await session.execute(select(AccessRoleModel))).scalars().all()}

    for user in users:
        user_roles = list(user.roles or [])
        if user.is_superadmin or "superadmin" in user_roles:
            holders.add(str(user.id))
            continue

        ag_privs: list[dict] = []
        member_rows: list[Any] = []
        for ident in (str(user.id), user.username, user.email):
            if ident and memberships.get(ident):
                member_rows = memberships[ident]
                break
        for m in member_rows:
            group = groups.get(str(m.access_group_id))
            if group is None:
                continue
            for rid in (group.role_ids or []):
                role = roles.get(str(rid))
                if role is None:
                    continue
                ag_privs.extend(role.privileges or [])

        # Phase 4 step 1: access-role NAMES no longer merge into the platform
        # role list, so a holder can no longer arrive by one. Counting them here
        # after the merge is gone would OVERCOUNT holders — and this guard's
        # only job is to refuse the change that empties the set, so overcounting
        # is precisely the direction that lets a real lockout through.
        if evaluate_privileges(effective_privileges(user_roles, ag_privs), action):
            holders.add(str(user.id))

    return holders


async def assert_not_last_holder(
    session: AsyncSession,
    action: str = BOOTSTRAP_ACTION,
    *,
    removing_user_ids: Iterable[str] = (),
    context: str = "",
) -> None:
    """Refuse a change that would leave nobody able to grant access.

    `removing_user_ids` is who the pending change would strip. Called *before*
    the write: afterwards the refusal cannot be authorised by anyone.
    """
    holders = await _holders_of(session, action)
    remaining = holders - {str(u) for u in removing_user_ids}
    if remaining:
        return

    if not holders:
        # Nobody held it *before* the change either. This guard exists to stop
        # the platform going from "someone can grant access" to "nobody can";
        # it cannot prevent a state that already exists, and refusing here would
        # block all member management in exactly two situations where that is
        # the wrong answer: a deployment already locked out (the moment you most
        # need to edit groups), and one whose identities do not live in
        # `velaris_users` at all — SSO-only estates, and every test database.
        log.warning(
            "lockout: nobody currently holds %s, so %s cannot make it worse — "
            "allowing. Grant it to an account (scripts/grant_privilege.py).",
            action, context or "this change")
        return

    log.error("lockout refused: %s would leave nobody holding %s", context or "change", action)
    raise HTTPException(
        409,
        f"Refused: this would leave nobody able to perform '{action}'. "
        f"Grant it to another account first. "
        f"(If you are already locked out, use scripts/grant_privilege.py.)",
    )


async def holders_count(session: AsyncSession, action: str = BOOTSTRAP_ACTION) -> int:
    """How many accounts can currently perform `action` — for the UI warning."""
    return len(await _holders_of(session, action))


# ── revocation by edit, not by removal ────────────────────────────
#
# The first version of this module guarded the two obvious doors: removing a
# member, and deleting a group. Both leave the same room by a quieter route:
#
#   PATCH /access-groups/{id}  sets is_active and rewrites role_ids
#   PATCH /access-roles/{id}   rewrites the privileges themselves
#   DELETE /access-roles/{id}  removes them outright
#
# Editing the role is the widest of the four — it revokes for every member of
# every group carrying it, at once. The helpers below answer "who loses the
# action if this edit lands", so the same `assert_not_last_holder` can cover it.


def role_confers(role: Any, action: str = BOOTSTRAP_ACTION) -> bool:
    """Does this access role confer `action` on its holders **today**?

    Its privilege entries are the only route, matching `_holders_of`. Before
    Phase 4 step 1 the role's NAME was a second route (it merged into the
    platform role list), and it was counted here for that reason; with the merge
    gone, counting the name would report a role as bootstrap-conferring when it
    is not, and refuse edits that take nothing away.
    """
    from case_service.hxguard.privileges import effective_privileges

    return evaluate_privileges(
        effective_privileges([], list(role.privileges or [])), action)


async def members_of_group(session: AsyncSession, group_id: Any) -> set[str]:
    from case_service.db.models import OperatorAccessGroupModel

    rows = (await session.execute(
        select(OperatorAccessGroupModel.operator_id)
        .where(OperatorAccessGroupModel.access_group_id == group_id)
    )).scalars().all()
    return {str(r) for r in rows}


async def members_holding_role(session: AsyncSession, role_id: Any) -> set[str]:
    """Everyone who reaches this role through any active access group."""
    from case_service.db.models import AccessGroupModel, OperatorAccessGroupModel

    groups = (await session.execute(
        select(AccessGroupModel).where(AccessGroupModel.is_active == True)  # noqa: E712
    )).scalars().all()
    affected = [g.id for g in groups if str(role_id) in {str(r) for r in (g.role_ids or [])}]
    if not affected:
        return set()
    rows = (await session.execute(
        select(OperatorAccessGroupModel.operator_id)
        .where(OperatorAccessGroupModel.access_group_id.in_(affected))
    )).scalars().all()
    return {str(r) for r in rows}


async def assert_role_edit_survivable(
    session: AsyncSession, role: Any, new_privileges: Iterable[dict] | None,
    new_name: str | None = None, *, context: str = "",
    action: str = BOOTSTRAP_ACTION,
) -> None:
    """Refuse a role edit that revokes `action` from everyone holding it.

    Only fires when the edit actually takes the action away — a rename or a
    description change on the bootstrap role must not be refused, or the guard
    becomes something people route around.
    """
    if not role_confers(role, action):
        return                                   # it never granted this; nothing to lose

    class _After:
        name = new_name if new_name is not None else role.name
        privileges = list(new_privileges or [])

    if role_confers(_After, action):
        return                                   # still granted after the edit

    await assert_not_last_holder(
        session, action,
        removing_user_ids=await members_holding_role(session, role.id),
        context=context or f"editing access role {role.id}")


async def assert_role_delete_survivable(
    session: AsyncSession, role: Any, *, context: str = "",
    action: str = BOOTSTRAP_ACTION,
) -> None:
    if not role_confers(role, action):
        return
    await assert_not_last_holder(
        session, action,
        removing_user_ids=await members_holding_role(session, role.id),
        context=context or f"deleting access role {role.id}")


async def assert_group_edit_survivable(
    session: AsyncSession, group: Any, new_role_ids: Iterable[Any],
    new_is_active: bool, *, context: str = "", action: str = BOOTSTRAP_ACTION,
) -> None:
    """Refuse a group edit that revokes `action` from its members.

    Deactivating through PATCH is the same revocation as DELETE — that door was
    guarded and this one was not.
    """
    from case_service.db.models import AccessRoleModel

    kept = set() if not new_is_active else {str(r) for r in (new_role_ids or [])}
    current = {str(r) for r in (group.role_ids or [])}
    dropped = current - kept
    if not dropped:
        return

    roles = (await session.execute(
        select(AccessRoleModel).where(AccessRoleModel.id.in_(
            [r for r in (group.role_ids or [])]))
    )).scalars().all()
    losing = [r for r in roles if str(r.id) in dropped and role_confers(r, action)]
    if not losing:
        return

    still_granted = [r for r in roles if str(r.id) in kept and role_confers(r, action)]
    if still_granted:
        return                                   # another kept role still grants it

    await assert_not_last_holder(
        session, action,
        removing_user_ids=await members_of_group(session, group.id),
        context=context or f"editing access group {group.id}")
