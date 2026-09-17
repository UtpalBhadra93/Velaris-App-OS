"""FastAPI auth dependencies.

Provides get_current_user and require_role dependencies for routes.

Copyright (c) 2024-2025 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import hashlib
import logging
import time
import uuid
from datetime import datetime, timezone
from typing import Callable

from fastapi import Depends, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from velaris_shared.auth.models import AuthenticatedUser, ActiveAccessGroup
from velaris_shared.auth.jwt_handler import decode_jwt_token, extract_user_from_claims
from velaris_shared.config import get_settings

logger = logging.getLogger(__name__)

# ── Revoked-session in-memory cache (30s TTL) ─────────────────────────────────
# Avoids a DB round-trip on every request in the common (non-revoked) case.
_REVOKED_HASHES: set[str] = set()
_REVOKED_CACHE_AT: float  = 0.0
_REVOKED_CACHE_TTL: float = 30.0


async def _refresh_revoked_cache(session: AsyncSession) -> None:
    global _REVOKED_CACHE_AT
    from velaris_shared.db.models import RevokedSessionModel
    now_utc = datetime.now(timezone.utc)
    rows = (await session.execute(
        select(RevokedSessionModel.token_hash).where(
            RevokedSessionModel.expires_at > now_utc
        )
    )).scalars().all()
    _REVOKED_HASHES.clear()
    _REVOKED_HASHES.update(rows)
    _REVOKED_CACHE_AT = time.monotonic()


def _is_token_revoked_cached(raw_token: str) -> bool:
    return hashlib.sha256(raw_token.encode()).hexdigest() in _REVOKED_HASHES

_bearer_scheme = HTTPBearer(auto_error=False)


async def _enrich_with_access_group(
    user: AuthenticatedUser,
    session: AsyncSession,
) -> AuthenticatedUser:
    """Load the operator's active access group from the DB and enrich the user.

    Falls back gracefully — if no access groups are assigned, the user object is
    returned unchanged. Since Phase 4 step 1 it is returned unchanged in one
    further respect: `user.roles` is never written here, so an access group can
    add context but cannot add platform authority.
    """
    from velaris_shared.db.models import (
        UserDirectoryModel, AccessGroupModel, AccessRoleModel,
        OperatorAccessGroupModel, PortalModel,
    )

    # ── Identity resolution (fixed 2026-08-15) ────────────────────
    #
    # These two tables key on a free-text operator id, and what lands in it
    # depends on who wrote the row: the SSO/LDAP sync and the admin CRUD screens
    # write a **username or email**, while `auth_real._make_tokens` mints tokens
    # with `sub = str(user.id)`, a **UUID**. So matching only on `user.user_id`
    # found nothing for a normally-logged-in operator: no access group ever
    # loaded, and `tenant_id` was never populated from the directory. P37 was
    # inert for every staff login, silently, since it shipped.
    #
    # Resolution is by PRECEDENCE, not by `IN (...)`. Merging the three
    # identifiers into one query would conflate two accounts the moment one
    # user's username equals another's email — rare, but the failure mode is
    # handing someone else's access group to the wrong person, so it is not a
    # risk worth taking for one fewer round trip.
    identifiers = [i for i in (user.user_id, user.username, user.email) if i]

    dir_row = None
    for ident in identifiers:
        dir_row = (await session.execute(
            select(UserDirectoryModel).where(UserDirectoryModel.user_id == ident)
        )).scalar_one_or_none()
        if dir_row is not None:
            break

    oag_rows = []
    for ident in identifiers:
        oag_rows = (await session.execute(
            select(OperatorAccessGroupModel).where(
                OperatorAccessGroupModel.operator_id == ident
            )
        )).scalars().all()
        if oag_rows:
            break

    # Always populate tenant_id from user_directory regardless of access groups
    if dir_row and dir_row.tenant_id:
        user.tenant_id = dir_row.tenant_id

    if not oag_rows:
        return user  # no P37 groups assigned yet — legacy flat roles still work

    # Determine active group: persisted current > primary > first
    active_group_id: uuid.UUID | None = None
    if dir_row and dir_row.current_access_group_id:
        # Verify it's still a valid membership
        ids = {str(r.access_group_id) for r in oag_rows}
        if str(dir_row.current_access_group_id) in ids:
            active_group_id = dir_row.current_access_group_id

    if active_group_id is None:
        primary = next((r for r in oag_rows if r.is_primary), None) or oag_rows[0]
        active_group_id = primary.access_group_id

    # Load the active group + its portal
    ag = (await session.execute(
        select(AccessGroupModel).where(AccessGroupModel.id == active_group_id)
    )).scalar_one_or_none()

    if ag is None:
        return user

    portal = (await session.execute(
        select(PortalModel).where(PortalModel.id == ag.portal_id)
    )).scalar_one_or_none()

    # Resolve role names from role_ids
    role_names: list[str] = []
    all_privileges: list[dict] = []
    if ag.role_ids:
        role_uuids = [uuid.UUID(r) if isinstance(r, str) else r for r in ag.role_ids]
        role_rows = (await session.execute(
            select(AccessRoleModel).where(AccessRoleModel.id.in_(role_uuids))
        )).scalars().all()
        for rr in role_rows:
            role_names.append(rr.name)
            all_privileges.extend(rr.privileges or [])

    # Build available_access_groups summary
    all_ag_ids = [r.access_group_id for r in oag_rows]
    all_ags = (await session.execute(
        select(AccessGroupModel).where(AccessGroupModel.id.in_(all_ag_ids))
    )).scalars().all()
    available = [
        {
            "id": str(a.id),
            "name": a.name,
            "is_primary": any(r.is_primary and r.access_group_id == a.id for r in oag_rows),
        }
        for a in all_ags
    ]

    active_ag = ActiveAccessGroup(
        id=str(ag.id),
        name=ag.name,
        portal_id=str(portal.id) if portal else "",
        portal_type=portal.portal_type if portal else "staff",
        portal_name=portal.name if portal else "",
        modules=portal.modules if portal else [],
        homepage=portal.homepage if portal else "/work-center",
        roles=role_names,
        privileges=all_privileges,
        allowed_case_type_ids=ag.allowed_case_type_ids or ["*"],
        allowed_queue_ids=ag.allowed_queue_ids or ["*"],
    )

    # Phase 4 step 1 (2026-08-15): access-role NAMES are no longer merged into
    # `user.roles`. They were, from P37 until now, so that `require_role("admin")`
    # kept working for operators whose access role happened to be named `admin` —
    # which also meant *naming* an access role `admin` conferred platform
    # administration. `require_role` has had zero call sites since Phase 3, so the
    # bridge has nothing left to carry.
    #
    # The names are still published on `active_access_group.roles` (UI, and the
    # case-level vocabulary in `core/access_control.py`); what they no longer do
    # is answer a platform authorization question. An access group reaches a
    # platform action only through a privilege entry naming the dotted namespace
    # — see `hxguard/privileges.py::_crosses_into_platform_namespace`, which is
    # load-bearing from this commit onward rather than merely defensive.
    user.active_access_group = active_ag
    user.available_access_groups = available
    return user


async def get_current_user(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer_scheme),
) -> AuthenticatedUser:
    """Extract and validate the current user from the request.

    In dev mode, returns a mock admin user.
    In jwt/oidc mode, validates the Bearer token.

    P37: enriches the user with active access group context when available.
    Falls back gracefully so all pre-P37 routes are unaffected.
    """
    settings = get_settings()

    if credentials is None:
        raise HTTPException(
            status_code=401,
            detail="Authentication required",
            headers={"WWW-Authenticate": "Bearer"},
        )
    try:
        claims = decode_jwt_token(
            credentials.credentials,
            secret=settings.auth_secret,
            issuer=settings.auth_issuer,
            audience=settings.auth_audience,
            public_key=settings.auth_rsa_public_key or "",
        )
        user = AuthenticatedUser(
            token=credentials.credentials,
            **extract_user_from_claims(claims),
        )
    except Exception as e:
        logger.warning("JWT validation failed: %s", e)
        raise HTTPException(
            status_code=401,
            detail=f"Invalid or expired token",
            headers={"WWW-Authenticate": "Bearer"},
        )

    # Purpose-built tokens are single-purpose: each is accepted only by the one
    # dependency written for it — MCP scoped tokens by the MCP transport
    # (`token_use="mcp"`, P4), agent tokens by the agent caller dependency
    # (`token_use="agent"`, F-00). Everywhere else — REST, proposals
    # confirm/reject, token minting — they fail closed, so a leaked non-human
    # token cannot touch any other surface.
    #
    # The check is "any token_use at all", not a list of known values. A session
    # token has never carried this claim, so nothing existing is affected, and
    # the next purpose-built token kind is refused here by default instead of
    # being admitted until somebody remembers to add it. Fail closed on the
    # unknown case is the whole point.
    token_use = claims.get("token_use")
    if token_use:
        raise HTTPException(
            status_code=401,
            detail=f"A {token_use} token is not valid on this endpoint",
            headers={"WWW-Authenticate": "Bearer"},
        )

    # P37 enrichment + security checks (is_active + revoked session)
    try:
        from velaris_shared.db.session import get_session_factory
        from velaris_shared.db.models import HelixUserModel

        session: AsyncSession | None = getattr(request.state, "db_session", None)
        if session is None:
            async with get_session_factory()() as session:
                user = await _enrich_with_access_group(user, session)
                await _security_check(user, credentials.credentials, session)
        else:
            user = await _enrich_with_access_group(user, session)
            await _security_check(user, credentials.credentials, session)
    except HTTPException:
        raise
    except Exception:
        logger.debug("P37 enrichment / security check skipped", exc_info=True)

    return user


async def _security_check(
    user: AuthenticatedUser,
    raw_token: str,
    session: AsyncSession,
) -> None:
    """Verify the token has not been revoked and the account is still active."""
    from velaris_shared.db.models import HelixUserModel

    # Refresh revoked-token cache if stale
    if time.monotonic() - _REVOKED_CACHE_AT > _REVOKED_CACHE_TTL:
        try:
            await _refresh_revoked_cache(session)
        except Exception:
            pass  # Cache miss is acceptable; we'll check DB on next refresh

    if _is_token_revoked_cached(raw_token):
        raise HTTPException(401, "Session has been revoked",
                            headers={"WWW-Authenticate": "Bearer"})

    # Check is_active on velaris_users (breach auto-disable lands here)
    try:
        hu = await session.get(HelixUserModel, uuid.UUID(user.user_id))
        if hu is not None and not hu.is_active:
            raise HTTPException(401, "Account is suspended. Contact your administrator.",
                                headers={"WWW-Authenticate": "Bearer"})
    except HTTPException:
        raise
    except Exception:
        pass  # If the lookup fails, don't block auth — log and continue


def require_role(*roles: str) -> Callable:
    """**RETIRED (Phase 4 step 2). Raises at import-of-use, deliberately.**

    This was the platform's authorization gate: it passed for `superadmin`, for
    anyone whose `is_admin` was true, or for any of the named roles — where
    `has_role()` itself returned True for every role once admin was present.
    Phase 3 replaced all 110 of its call sites with `hxguard.guard(action)`, and
    Phase 3.5 did the same for the 56 that hid behind the `require_admin()`
    alias.

    It is kept as a loud failure rather than deleted outright because the
    *symbol* is still imported (unused) by 25 routers, and a NameError at import
    time in an unrelated deploy is a worse first signal than this message. A
    reintroduced gate now fails when the route is built, not silently at request
    time with role-name semantics nobody expects any more.
    """
    raise RuntimeError(
        "require_role() was retired in Phase 4 of privilege-based authorization. "
        f"Use hxguard.guard('<action>') instead of require_role({roles!r}) — see "
        "case_service/hxguard/actions.py for the registry, and "
        "docs/Future/Done/privilege-based-authorization.md for why."
    )


async def _shadow_compare(request, user, roles, allowed: bool) -> None:
    """Phase-2 observation. Never raises, never alters the decision.

    Nothing calls this now that `require_role` is retired. It stays, with
    `hxguard/shadow.py`, because it is the harness a *future* migration reuses:
    run the new decider alongside the old one and count disagreements before
    changing any answer. Deleting it would mean rebuilding it.
    """
    try:
        from velaris_shared.config import get_settings
        if not getattr(get_settings(), "hxguard_privilege_shadow", True):
            return
        from velaris_shared.hxguard import shadow
        await shadow.compare(request, user, roles, allowed)
    except Exception:                       # observation must never break auth
        pass


def require_admin() -> Callable:
    """**RETIRED (Phase 4 step 2)** — the one-line alias that hid 56 gates from
    the Phase-0 inventory. See `require_role`."""
    return require_role("admin")
