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

from velaris_shared.db.models import TenantModel

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


async def default_tenant_tokens(session: AsyncSession) -> list[str]:
    """Every spelling a string `tenant_id` column may use for the default tenant.

    For the callers with NO tenant claim. Such a caller is the platform-default
    tenant (see `is_superadmin`), and a string tenant column holds either the
    slug or the id — so both are returned. Scoped with `IS NULL` beside it, this
    is exactly what the single-case read lets the same caller see.
    """
    out = [DEFAULT_TENANT]
    resolved = await resolve_tenant(session, DEFAULT_TENANT)
    if resolved is not None:
        out.append(str(resolved[0]))
    return out


def is_superadmin(user: Any) -> bool:
    """Break-glass, and the only way across a tenant boundary.

    A token with **no** tenant is not a superuser: `same_tenant` already reads it
    as the platform-default tenant, and treating "unset" as "all tenants" is how
    a missing claim becomes an authorization bypass. Crossing has to be a role
    somebody was deliberately given.
    """
    roles = getattr(user, "roles", None) or []
    return "superadmin" in roles


async def may_reach_tenant(
    session: AsyncSession, user: Any, row_tenant_id: Any,
) -> bool:
    """May this caller see or act on a row belonging to that tenant?"""
    if is_superadmin(user):
        return True
    return await same_tenant(session, row_tenant_id, getattr(user, "tenant_id", None))


async def caller_tenant_or_refuse(
    session: AsyncSession, user: Any, requested: str | None,
) -> str:
    """The tenant a write may be attributed to.

    A caller-supplied ``tenant_id`` on a create or update body is a tenant-choice
    the caller does not have. Where the row it writes is later used to *resolve
    other rows* — as a schedule's tenant resolves the bots, connectors, cases and
    sync destinations its action may touch — accepting the field verbatim turns
    one string into a cross-tenant reach.

    So: a caller whose token names a tenant may only write into that tenant, and
    a token with no tenant at all (a platform-level operator) keeps the value it
    asked for. Raises ``PermissionError`` on a mismatch; callers map it to 403.
    """
    token = getattr(user, "tenant_id", None)
    asked = (requested or "").strip() or DEFAULT_TENANT
    if is_superadmin(user):
        # Break-glass writes into any tenant, deliberately: this is the role the
        # installer and platform operators hold, and it is auditable.
        return asked
    if not token:
        # No tenant claim is the platform-default tenant, not "any tenant".
        return DEFAULT_TENANT if asked == DEFAULT_TENANT else _refuse()

    mine = await resolve_tenant(session, token)
    theirs = await resolve_tenant(session, asked)
    if mine is not None and theirs is not None and mine[0] == theirs[0]:
        return asked
    if mine is not None and asked == DEFAULT_TENANT:
        # "default" is the shape a UI sends when it has no opinion; attribute it
        # to the caller's own tenant rather than to the platform's.
        return str(mine[1])
    return _refuse()


def _refuse() -> str:
    raise PermissionError(
        "This may only be written into your own tenant.")


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


# ── case visibility ──────────────────────────────────────────────────────────
#
# Split out from `same_tenant` deliberately. `same_tenant` is used by ~20 call
# sites across features that genuinely depend on platform-shared NULL rows
# (connectors, escalation trees, arbiter policies), and changing its meaning
# would change all of them at once. Case visibility is the one that had a
# proven cross-tenant read, so it gets its own function and its own switch.

#: Operator switch for the NULL-tenant branch of case reads.
#:
#: **Unset means "exactly as before"**: a case with no tenant is visible to
#: everybody, which is how every release up to now has behaved and what several
#: single-tenant installs rely on.
#:
#: It may only be turned on once `migrations/*/197_case_tenant_backfill.sql` has
#: run AND reported zero cases left without a tenant. Turning it on while rows
#: are still NULL hides those rows from everyone, their rightful owner included
#: — which is an outage, not a fix.
_STRICT_ENV = "VELARIS_CASE_TENANT_STRICT_READS"


def strict_case_reads() -> bool:
    """Is the NULL-tenant branch closed on this install? Default: no."""
    import os
    raw = os.environ.get(_STRICT_ENV, "")
    return str(raw).strip().lower() in ("1", "true", "yes", "on")


async def case_visible_to(
    session: AsyncSession, row_tenant_id: Any, token: str | uuid.UUID | None,
    *, is_superadmin_caller: bool = False,
) -> bool:
    """May this caller see a case carrying `row_tenant_id`?

    Two rules, and only the second one moves:

      * A case **owned by another tenant is never visible**, switch or no
        switch. This was not being enforced on the single-case read at all —
        `GET /cases/{id}` loaded by primary key with no tenant clause — which is
        how tenant `dev` read tenant `default`'s case on 2026-09-05.
      * A case with **no tenant** is visible to everyone by default, because
        that is what every released version does. Only `VELARIS_CASE_TENANT_
        STRICT_READS=true` closes it, and only a backfilled install may set it.

    Superadmin keeps its break-glass, same as `is_superadmin` elsewhere: it is
    the documented way across a tenant boundary and hiding rows from it would
    just push people to the database.
    """
    if is_superadmin_caller:
        return True
    if row_tenant_id is None:
        return not strict_case_reads()
    return await same_tenant(session, row_tenant_id, token)


# ── actor identity ───────────────────────────────────────────────────────────
#
# The same two-conventions problem as tenants, one column over.
#
# Fifteen columns record *who did this* as a `GUID`: `agents.created_by`,
# `agent_capabilities.granted_by`/`revoked_by`, `agent_token_grants.issued_by`/
# `revoked_by`, `agent_status_history.changed_by`, `agent_workforce.created_by`,
# `model_rate_cards.created_by`, `compliance_proofs.generated_by`,
# `simulation_gates.created_by`, `simulation_runs.created_by` and the four
# arbiter ones. But a caller's identifier is free text: `auth_real._make_tokens`
# mints `sub = str(user.id)`, a UUID, while an SSO login, a service account and
# every row in `user_directory` carry a **username or an email**.
#
# So the write worked for a password login and refused everybody else. Proven on
# 2026-09-06: `POST /agents` and `PUT /costing/model-rates` as `utpal.bhadra`
# both answered *"...is not a valid id"* — a 422 on a request that was correct in
# every respect, because a username had reached a UUID column. HxArbiter had
# already met this and degraded to NULL, which keeps the route working but
# throws the attribution away: the operator who authored a policy is recorded as
# nobody, on the one table an auditor reads to find out who.
#
# `actor_uuid` resolves both forms to `velaris_users.id`, exactly as
# `resolve_tenant` does for tenants, so neither the refusal nor the silent loss
# happens. It never invents an id: a caller that names nobody is recorded as
# absent, which is what those nullable columns already mean.

#: Actor lookups are cached on the same terms as tenants — a username maps to a
#: user row about as often as a tenant slug maps to a tenant. Negative results
#: are cached too, so an unknown principal cannot be used to hammer the DB.
_actor_cache: dict[str, tuple[float, uuid.UUID | None]] = {}


def clear_actor_cache() -> None:
    """Drop the actor resolver cache — for tests and for user mutations."""
    _actor_cache.clear()


async def actor_uuid(session: AsyncSession, user: Any) -> uuid.UUID | None:
    """The platform user id for a caller, for the `GUID` "who did this" columns.

    Accepts a UUID token, a username or an email, because all three legitimately
    arrive in `user.user_id` depending on how the caller signed in.

    Returns None when the caller names nobody this platform knows — a service
    principal, a token minted outside the user table. **None is a real answer**:
    every one of these columns is nullable and already means "not recorded".
    Coercing or inventing an id would put a fictional operator on an audit row,
    which is worse than an empty one.

    A UUID is returned as it stands, without a query and without checking that a
    user row exists. That is deliberate: it is exactly what every one of these
    call sites did before this function existed, so an install that is working
    today keeps storing precisely the value it stores today.
    """
    from velaris_shared.db.models import HelixUserModel

    raw = getattr(user, "user_id", None)
    direct = _as_uuid(str(raw).strip()) if raw is not None else None
    if direct is not None:
        return direct

    # Precedence, not `IN (...)` — the same reason `_enrich_with_access_group`
    # gives: merging the identifiers into one query hands one person's row to
    # another the moment a username equals somebody else's email.
    candidates = [
        str(v).strip() for v in (
            raw, getattr(user, "username", None), getattr(user, "email", None),
        ) if v is not None and str(v).strip()
    ]
    if not candidates:
        return None

    key = "\x00".join(candidates)
    hit = _actor_cache.get(key)
    if hit is not None and hit[0] > time.monotonic():
        return hit[1]

    found: uuid.UUID | None = None
    for ident in candidates:
        row = (await session.execute(
            select(HelixUserModel).where(
                HelixUserModel.username == ident
            ).limit(1)
        )).scalars().first()
        if row is None:
            row = (await session.execute(
                select(HelixUserModel).where(
                    HelixUserModel.email == ident
                ).limit(1)
            )).scalars().first()
        if row is not None:
            found = row.id
            break

    _actor_cache[key] = (time.monotonic() + _CACHE_TTL_SECONDS, found)
    return found
