"""Phase 6 — the role → privilege store, and the cache that keeps it usable.

`privileges.effective_privileges()` is called from `PrivilegeBackend.evaluate`,
which is a **pure function of (subject, action)** with no session and no I/O.
That purity is not incidental:

  * it is what let the Phase-1 parity table prove the model exhaustively against
    `require_role` for every action × subject shape;
  * `hxguard.user_allows(user, action)` is deliberately sync (Phase 4) so
    redaction can ask per field without awaiting — one `await` per column per row
    is not a thing you retrofit later.

So the store cannot be read from the database at decision time. It is read into
this module-level cache and served synchronously, in the same shape as the
feature-flag cache in `api/routers/releases.py`.

**An empty cache means the seed, never a denial.** The flag cache can answer
False while cold because a dark feature is safe. An authorization cache cannot:
if the store REPLACED a role's privileges, a cold cache would mean every role
holds nothing, and the platform would lock every human out for the first seconds
of every boot and for the whole of any refresh failure. Hence the store is
**additive** — `effective_privileges` unions seed + store — and the failure mode
of this entire file is "behaves exactly as it did before Phase 6".

Revocation is still expressible, as an explicit ``{"effect": "deny"}`` entry:
deny wins over any allow, has existed since Phase 1, and no seeded role uses it.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import contextlib
import contextvars
import logging
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)

#: role name -> privilege entries. Empty until the first load, which is the
#: safe state (see the module docstring).
_STORE: dict[str, list[dict]] = {}

#: Bumped on every successful load. `Subject` carries it, so a privilege change
#: invalidates the 30s decision cache instead of being served stale from it —
#: the role→privilege mapping is resolved INSIDE the evaluator, so nothing else
#: about the subject changes when an admin edits a role.
_GENERATION: int = 0

#: A PROSPECTIVE store, visible only to the task that set it.
#:
#: The lockout pre-check has to ask "who would hold this action AFTERWARDS?", and
#: `_holders_of` answers from this module. The first version installed the
#: prospective entries into the module-level `_STORE` — which is shared, and the
#: check does database I/O, so it AWAITS. Every concurrent request landing in
#: that window evaluated against another admin's unsaved (and possibly about to
#: be rejected) edit. A `deny` under review briefly applied platform-wide.
#:
#: A ContextVar is per-task in asyncio, so the override reaches `_holders_of` —
#: same task — and nothing else.
_OVERRIDE: contextvars.ContextVar[dict[str, list[dict]] | None] = \
    contextvars.ContextVar("role_store_override", default=None)

#: Refresh cadence for the periodic reload. Writes refresh synchronously, so
#: this only has to catch changes made by another worker process.
REFRESH_INTERVAL_SECONDS = 60

#: Delay before the first load, so the database is up. Module-level rather than
#: inline so a test can exercise the loop instead of only its first sleep.
STARTUP_DELAY_SECONDS = 8


def generation() -> int:
    return _GENERATION


def privileges_for(role: str) -> list[dict]:
    """Stored entries for one role. Never raises; unknown role → nothing.

    A task-local override wins, so a lockout pre-check sees the prospective
    state and no other request does.
    """
    override = _OVERRIDE.get()
    if override is not None and role in override:
        return list(override[role])
    return list(_STORE.get(role, ()))


def all_stored() -> dict[str, list[dict]]:
    return {r: list(p) for r, p in _STORE.items()}


#: Bounds on a single entry. The row count is capped at the API; these cap what
#: one entry can weigh. Every entry is held in memory in every worker and walked
#: on every authorization decision, so an unbounded resource string is a slow
#: read on the hot path, not merely a large row.
MAX_RESOURCE_LEN = 200
MAX_ACTIONS_PER_ENTRY = 50
MAX_ACTION_LEN = 64


def _valid(entry: Any) -> bool:
    """Structural check. A malformed row must not take the whole cache down.

    Deliberately permissive about WHICH resource is named — the namespace
    boundary in `_crosses_into_platform_namespace` is what decides whether an
    entry reaches a platform action, and duplicating that judgement here would
    give two places to disagree.
    """
    if not isinstance(entry, dict):
        return False
    resource = entry.get("resource")
    if not isinstance(resource, str) or not resource or len(resource) > MAX_RESOURCE_LEN:
        return False
    actions = entry.get("actions")
    if not isinstance(actions, list) or not actions:
        return False
    if len(actions) > MAX_ACTIONS_PER_ENTRY:
        return False
    if not all(isinstance(a, str) and a and len(a) <= MAX_ACTION_LEN for a in actions):
        return False
    effect = str(entry.get("effect", "allow")).lower()
    return effect in ("allow", "deny")


async def refresh(session: AsyncSession) -> int:
    """Reload the cache from the database. Returns the number of roles loaded.

    On any failure the PREVIOUS contents are kept rather than cleared: a
    transient database problem must not quietly revoke whatever the store was
    granting. The cache is only ever replaced by a complete, successful read.
    """
    global _STORE, _GENERATION
    from case_service.db.models import RolePrivilegeModel

    rows = (await session.execute(select(RolePrivilegeModel))).scalars().all()
    fresh: dict[str, list[dict]] = {}
    for row in rows:
        entries = [e for e in (row.privileges or []) if _valid(e)]
        dropped = len(row.privileges or []) - len(entries)
        if dropped:
            # Say it. A silently ignored entry is an admin believing they
            # granted something they did not.
            logger.warning(
                "role_privileges: dropped %d malformed entr(ies) for role %r",
                dropped, row.role)
        if entries:
            fresh[row.role] = entries
    _STORE = fresh
    _GENERATION += 1
    return len(fresh)


async def set_role_privileges(
    session: AsyncSession, *, role: str, privileges: list[dict], updated_by: str,
) -> list[dict]:
    """Upsert one role's stored entries. The caller commits, then refreshes.

    Validation is strict here even though `refresh` is forgiving: rejecting a
    malformed entry at the door is the difference between an admin seeing an
    error and an admin seeing a grant that silently never applied.
    """
    from case_service.db.models import RolePrivilegeModel

    bad = [e for e in privileges if not _valid(e)]
    if bad:
        raise ValueError(
            f"malformed privilege entr{'y' if len(bad) == 1 else 'ies'}: {bad!r}. "
            'Each needs {"resource": str, "actions": [str, ...]} '
            'and an optional "effect" of allow|deny.')

    row = await session.get(RolePrivilegeModel, role)
    if row is None:
        row = RolePrivilegeModel(role=role, privileges=list(privileges),
                                 updated_by=updated_by)
        session.add(row)
    else:
        row.privileges = list(privileges)
        row.updated_by = updated_by
    await session.flush()
    return list(row.privileges)


async def role_privileges_cron() -> None:
    """Periodic reload, so a change made by one worker reaches the others.

    Own `try` per cycle: this loop must outlive a bad database moment, because
    stopping it would freeze every worker's view of the store at whatever it
    last read without anything saying so.
    """
    import asyncio
    from case_service.db.session import get_session_factory

    await asyncio.sleep(STARTUP_DELAY_SECONDS)      # let the DB come up
    while True:
        try:
            async with get_session_factory()() as session:
                await refresh(session)
        except Exception as exc:
            logger.warning("role_privileges refresh failed (keeping previous): %s", exc)
        await asyncio.sleep(REFRESH_INTERVAL_SECONDS)


def _valid_or_raise(privileges: list[dict]) -> None:
    """Strict door-check for writes. `refresh` is forgiving; this is not.

    Rejecting at the door is the difference between an admin seeing an error and
    an admin seeing a grant that silently never applied.
    """
    bad = [e for e in privileges if not _valid(e)]
    if bad:
        raise ValueError(
            f"malformed privilege entr{'y' if len(bad) == 1 else 'ies'}: {bad!r}. "
            'Each needs {"resource": str, "actions": [str, ...]} '
            'and an optional "effect" of allow|deny.')


@contextlib.contextmanager
def prospective(role: str, privileges: list[dict]):
    """Evaluate as if `role` had these entries — for THIS task only.

    Used by the lockout pre-check. Scoped to the task rather than the module
    because the check awaits on database I/O, and a shared mutation would let a
    concurrent request decide against an unsaved edit (see `_OVERRIDE`).
    """
    token = _OVERRIDE.set({**(_OVERRIDE.get() or {}), role: list(privileges)})
    try:
        yield
    finally:
        _OVERRIDE.reset(token)


async def assert_write_keeps_bootstrap_holders(
    session, *, role: str, privileges: list[dict], context: str = "",
) -> None:
    """Refuse a store write that would leave nobody able to grant access.

    **Why not `lockout.assert_not_last_holder`.** That guard takes the CURRENT
    state plus the accounts a change would strip, and a role-level `deny` does
    not name accounts — it revokes for every holder of the role at once. The
    first version of this staged the prospective entries and then called the
    guard, which quietly defeated it: with the deny already installed, the guard
    counted zero holders *before* the change too, hit its "nobody held it
    anyway, so this cannot make it worse" branch, and allowed the very edit it
    exists to refuse. Comparing before against after is the only form that
    distinguishes a fall to zero from a zero that was already there.

    The zero-holder escape hatch is preserved deliberately: a deployment that is
    already locked out, or an SSO-only estate whose identities are not rows in
    `velaris_users`, must still be able to edit roles.
    """
    from fastapi import HTTPException
    from case_service.hxguard import lockout

    before = await lockout._holders_of(session, lockout.BOOTSTRAP_ACTION)
    if not before:
        logger.warning(
            "role_privileges: nobody currently holds %s, so %s cannot make it "
            "worse — allowing.", lockout.BOOTSTRAP_ACTION, context or "this change")
        return

    # Task-local: the check awaits, and a shared mutation would let concurrent
    # requests decide against an edit that has not been saved and may be refused.
    with prospective(role, list(privileges)):
        after = await lockout._holders_of(session, lockout.BOOTSTRAP_ACTION)

    if not after:
        logger.error("role_privileges refused: %s would leave nobody holding %s",
                     context or "change", lockout.BOOTSTRAP_ACTION)
        raise HTTPException(
            409,
            f"Refused: this would leave nobody able to perform "
            f"'{lockout.BOOTSTRAP_ACTION}'. Grant it to another role or account "
            f"first. (If you are already locked out, use "
            f"scripts/grant_privilege.py.)",
        )
