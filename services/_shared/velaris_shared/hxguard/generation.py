"""HxGuard Phase C — the authorization generation, shared by every service.

**The problem this solves.** Each service runs its own in-process policy
decision point with a 30-second decision cache. `service.invalidate_cache()`
clears the cache of the process that calls it, and every mutation router lives
in case-service — so after the #13c-e split, revoking an access-group grant or
deleting a relationship tuple cleared case-service's cache and left
analytics-, hxgraph- and hxmigrate-service answering from stale entries.

**The shape of the fix.** One row in the database holds a counter. A mutation
bumps it *in the same transaction as the mutation itself*, so a rolled-back
change cannot leave a bumped generation behind, and a committed one cannot fail
to bump. Every service polls the counter on a short interval and clears its
local cache when the number moves.

**Why a counter and not a message bus.** It needs no ordering and no delivery
guarantee: a service that misses intermediate values still reads a different
number and still invalidates. It works with Redis switched off, which is the
default. And it is one indexed read of one row — cheaper than the decision it
protects.

**It fails open on purpose.** If the read fails the local cache is left alone
rather than cleared: a database blip must not turn into an authorization storm
where every service re-evaluates every decision at once. The staleness bound in
that window is the same 30-second TTL that existed before.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import logging

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from velaris_shared.db.models import HxGuardAuthzGenerationModel

from .service import invalidate_cache

log = logging.getLogger(__name__)

#: How often a service checks whether policy changed elsewhere. Well under the
#: 30s decision-cache TTL, so the shared signal is the thing that bounds
#: staleness rather than the TTL.
POLL_INTERVAL_SECONDS = 5

#: The singleton row's id, fixed by a CHECK constraint in migration 173.
ROW_ID = 1

#: Last generation this process saw. `None` until the first successful read —
#: which is not treated as a change, or every service would clear its cache once
#: at startup for no reason.
_seen: int | None = None


async def read_generation(session: AsyncSession) -> int | None:
    """Current platform generation, or None if it cannot be read."""
    try:
        return (await session.execute(
            select(HxGuardAuthzGenerationModel.generation)
            .where(HxGuardAuthzGenerationModel.id == ROW_ID)
        )).scalar_one_or_none()
    except Exception as exc:  # noqa: BLE001
        log.warning("authz generation read failed (keeping local cache): %s", exc)
        return None


async def bump(session: AsyncSession, *, reason: str, actor: str | None = None) -> None:
    """Record that a decision input changed, for every service to notice.

    Call this wherever `invalidate_cache()` is called, with the caller's own
    session so the bump commits with the mutation. It does NOT invalidate
    locally — the caller still does that, so the process making the change sees
    its own effect immediately rather than after a poll.
    """
    try:
        await session.execute(
            update(HxGuardAuthzGenerationModel)
            .where(HxGuardAuthzGenerationModel.id == ROW_ID)
            .values(
                generation=HxGuardAuthzGenerationModel.generation + 1,
                reason=reason[:100],
                updated_by=(actor or "")[:255] or None,
            )
        )
    except Exception as exc:  # noqa: BLE001
        # Never fail the mutation for this. The change is still correct where it
        # was made; the other services fall back to their 30s TTL.
        log.warning("authz generation bump failed for %s: %s", reason, exc)


async def poll_once(session: AsyncSession) -> bool:
    """Read the generation; clear the local cache if it moved. True if cleared."""
    global _seen
    current = await read_generation(session)
    if current is None:
        return False
    if _seen is None:
        _seen = current
        return False
    if current != _seen:
        _seen = current
        # Clearing the decision cache alone is not enough. The cache holds
        # ANSWERS; the role store holds the INPUTS, and it reloads on its own
        # 60-second cron. Clearing only the answers would make this service
        # re-evaluate immediately against a role store it has not reloaded —
        # re-deriving the same stale decision and caching it again, so a
        # revocation could still take a full minute to bite. Reload the inputs
        # first, then drop the answers derived from the old ones.
        try:
            from velaris_shared.hxguard import role_store
            await role_store.refresh(session)
        except Exception as exc:  # noqa: BLE001
            # Keep going: dropping the cache is still strictly safer than not.
            log.warning("role store refresh after generation change failed: %s", exc)
        invalidate_cache()
        log.info(
            "authz generation changed to %s — role store reloaded, decision cache cleared",
            current,
        )
        return True
    return False


async def generation_cron() -> None:
    """Poll forever. Started by `velaris_shared.app.platform_lifespan`.

    One `try` per cycle: this loop must outlive a bad database moment. Stopping
    it would freeze a service's view of policy with nothing saying so — the same
    reasoning as the role-store refresh it runs beside.
    """
    import asyncio

    from velaris_shared.db.session import get_session_factory

    while True:
        try:
            async with get_session_factory()() as session:
                await poll_once(session)
        except Exception as exc:  # noqa: BLE001
            log.warning("authz generation poll failed (keeping local cache): %s", exc)
        await asyncio.sleep(POLL_INTERVAL_SECONDS)
