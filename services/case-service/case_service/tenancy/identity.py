"""Canonical tenant identity — one answer to "which tenant is this caller?".

The platform grew two conventions for the same idea and they do not compare:

  * **UUID columns** — ``case_instances.tenant_id``, ``case_types.tenant_id`` and
    friends are ``GUID`` foreign keys into ``tenants.id``.
  * **Slug strings** — ``case_sessions.tenant_id`` holds a slug, and every
    settings lookup matches ``TenantModel.slug``.

``user_directory.tenant_id`` is a bare ``String(64)``, so a caller's tenant
token can legitimately be *either* form depending on how the directory was
populated. Code that assumed one form silently misbehaved on a deployment
using the other: a direct ``str(case.tenant_id) != user.tenant_id`` compare
fails closed (every tenant-scoped case 404s for its own users), while a
``TenantModel.slug == user.tenant_id`` lookup finds nothing (per-tenant
settings read back as "not enabled").

Everything here accepts BOTH forms and resolves to the tenant row, so the
convention a deployment happens to use stops being load-bearing.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import time
import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from case_service.db.models import TenantModel

#: The tenantless caller's token. Matches HxSentinel's DEFAULT_TENANT.
DEFAULT_TENANT = "default"

#: Tenants change about never; a short TTL keeps the resolver off the hot path
#: without holding a stale row long enough to matter. Negative results are
#: cached too, so an unknown token cannot be used to hammer the DB.
_CACHE_TTL_SECONDS = 60.0
_cache: dict[str, tuple[float, tuple[uuid.UUID, str] | None]] = {}


def _as_uuid(token: str) -> uuid.UUID | None:
    try:
        return uuid.UUID(token)
    except (ValueError, AttributeError, TypeError):
        return None


def clear_cache() -> None:
    """Drop the resolver cache — for tests and for tenant mutations."""
    _cache.clear()


async def resolve_tenant(
    session: AsyncSession, token: str | uuid.UUID | None,
) -> tuple[uuid.UUID, str] | None:
    """Resolve a tenant slug OR a tenant UUID to ``(id, slug)``.

    A UUID *object* is accepted as well as its string form: callers reach here
    from both worlds, and the GUID columns hand back real UUIDs. Coercing is
    the whole point of this module — refusing one of the two shapes would put
    the ``str(...)`` back at every call site.

    Returns None when the token names no tenant — callers must treat that as
    "deny", never as "any tenant".
    """
    key = str(token).strip() if token is not None else DEFAULT_TENANT
    if not key:
        return None

    hit = _cache.get(key)
    if hit is not None and hit[0] > time.monotonic():
        return hit[1]

    as_uuid = _as_uuid(key)
    stmt = select(TenantModel).where(
        TenantModel.id == as_uuid if as_uuid is not None else TenantModel.slug == key
    )
    row = (await session.execute(stmt.limit(1))).scalars().first()
    resolved = (row.id, row.slug) if row is not None else None
    _cache[key] = (time.monotonic() + _CACHE_TTL_SECONDS, resolved)
    return resolved


async def tenant_slug(session: AsyncSession, token: str | uuid.UUID | None) -> str:
    """The slug for a caller's token, for slug-keyed rows and settings lookups.

    Falls back to the token itself so an unregistered tenant behaves exactly as
    it did before — matching nothing rather than matching everything.
    """
    resolved = await resolve_tenant(session, token)
    return resolved[1] if resolved else (str(token) if token is not None else DEFAULT_TENANT)


async def same_tenant(
    session: AsyncSession, row_tenant_id: Any, token: str | uuid.UUID | None,
) -> bool:
    """Does a GUID tenant column belong to this caller's tenant?

    A NULL tenant column stays visible to everyone — platform-shared rows
    predate multi-tenancy and several features depend on that. Anything else
    must resolve, and an unresolvable token is a denial.
    """
    if row_tenant_id is None:
        return True

    key = str(token).strip() if token is not None else DEFAULT_TENANT
    # Fast path: the deployment stores UUID strings on the user, so the
    # comparison is direct and needs no query at all.
    if str(row_tenant_id) == key:
        return True

    resolved = await resolve_tenant(session, key)
    return resolved is not None and str(resolved[0]) == str(row_tenant_id)
