"""F-00 — the agent registry.

Who exists, what they are allowed to be granted, and whether they are live.

Two rules shape everything here:

* **The capability vocabulary is closed.** `CAPABILITIES` is a literal set, not
  a free string. An agent's authority is the one thing an attacker most wants
  to widen, and the HxArbiter injection finding (design §29) is the reason: a
  value that reaches a decision must come from a fixed vocabulary the platform
  owns, never from text somebody supplied.
* **The ceiling is read live, never trusted from the token.** A token freezes a
  capability subset at mint; every check intersects it with the rows in
  `agent_capabilities` that are still un-revoked. Revoking a capability
  therefore narrows every outstanding token at once, which is the difference
  between a ceiling and a decoration.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import logging
import uuid
from typing import Any, Iterable

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from velaris_shared.db.models import (
    AgentCapabilityModel,
    AgentModel,
    AgentStatusHistoryModel,
    _utcnow,
)

log = logging.getLogger(__name__)


#: The closed capability vocabulary (design F-00: `agent.act_on_case`,
#: `agent.read_case`, `agent.propose`). These are the actions the `agent`
#: HxArbiter namespace decides about, so the two lists are the same list — a
#: capability with no namespace action would be authority nothing evaluates.
CAPABILITIES: frozenset[str] = frozenset({
    "agent.read_case",     # read a case's data
    "agent.act_on_case",   # change a case — advance it, write to it
    "agent.propose",       # produce a proposal a human confirms; changes nothing alone
})

#: Every kind the schema allows. `external` is in the CHECK constraint and NOT
#: here: this build issues internal and marketplace agents only. F-09 adds the
#: code path that admits the third, and it will be a code change with a review,
#: not a migration on a live table.
ISSUABLE_KINDS: frozenset[str] = frozenset({"internal", "marketplace"})
ALL_KINDS: frozenset[str] = frozenset({"internal", "marketplace", "external"})

#: Kinds a TOKEN may be minted for. `external` is here and NOT in
#: `ISSUABLE_KINDS`, and the gap is the F-09 review F-00 promised.
#:
#: `create_agent` — the route an operator reaches — still refuses `external`, so
#: nothing on the platform can mint an external principal by pressing a button.
#: The only path that creates one is `handoff.service.issue`, which does it as
#: part of a signed, case-scoped, expiring work order to a counterparty an
#: operator enrolled with a pinned key. Widening `ISSUABLE_KINDS` instead would
#: have made "external agent" a thing anybody could create and then wonder what
#: it was for.
TOKENABLE_KINDS: frozenset[str] = ISSUABLE_KINDS | {"external"}

STATUSES: frozenset[str] = frozenset({"active", "suspended", "retired"})


class AgentError(ValueError):
    """The caller asked for an agent that cannot exist, or a change that cannot
    be made. Routers translate these to 4xx; internal callers should not catch
    them, because reaching one means the code asked for something impossible."""


def _as_uuid(value: Any, what: str) -> uuid.UUID:
    if isinstance(value, uuid.UUID):
        return value
    try:
        return uuid.UUID(str(value))
    except (ValueError, TypeError, AttributeError) as exc:
        raise AgentError(f"{what} is not a valid id") from exc


def validate_capabilities(caps: Iterable[str]) -> list[str]:
    """Normalise and refuse anything outside the closed vocabulary.

    Refusing is the whole function. Silently dropping an unknown capability
    would let a typo read as a successful grant, and a caller that asked for
    `agent.act_on_cases` would believe its agent could act.
    """
    wanted = sorted({str(c).strip() for c in (caps or ()) if str(c).strip()})
    unknown = [c for c in wanted if c not in CAPABILITIES]
    if unknown:
        raise AgentError(
            "unknown capability " + ", ".join(repr(c) for c in unknown)
            + "; the vocabulary is " + ", ".join(sorted(CAPABILITIES)))
    return wanted


# ── reads ────────────────────────────────────────────────────────────────────

async def get_agent(
    session: AsyncSession, agent_id: Any, *, tenant_id: Any | None = None,
) -> AgentModel | None:
    """Fetch by id, optionally pinned to a tenant.

    `tenant_id` is not optional in practice — every route passes it. A caller
    that omits it is asking a platform-scoped question, and the only such
    caller is the token path, which then checks the tenant itself.
    """
    row = await session.get(AgentModel, _as_uuid(agent_id, "agent id"))
    if row is None:
        return None
    if tenant_id is not None and row.tenant_id != _as_uuid(tenant_id, "tenant id"):
        # Not 'forbidden' — a tenant asking about an agent it does not own gets
        # the same answer as asking about one that does not exist, which is the
        # posture the rest of the platform already takes (audit 2026-08-29:
        # tenant isolation answers 404, never 403).
        return None
    return row


async def resolve_agent(
    session: AsyncSession, *, tenant_id: Any, slug: str,
) -> AgentModel | None:
    """Fetch by the stable slug a call site knows itself by."""
    stmt = select(AgentModel).where(
        AgentModel.tenant_id == _as_uuid(tenant_id, "tenant id"),
        AgentModel.slug == str(slug),
    )
    return (await session.execute(stmt)).scalar_one_or_none()


async def list_agents(
    session: AsyncSession, *, tenant_id: Any, include_retired: bool = False,
) -> list[AgentModel]:
    stmt = select(AgentModel).where(
        AgentModel.tenant_id == _as_uuid(tenant_id, "tenant id"))
    if not include_retired:
        stmt = stmt.where(AgentModel.status != "retired")
    return list((await session.execute(stmt.order_by(AgentModel.slug))).scalars())


async def live_capabilities(session: AsyncSession, agent_id: Any) -> set[str]:
    """The ceiling as it stands right now.

    Read on every token check. An expired-but-unrevoked row does not exist —
    a capability is either granted or it has a `revoked_at`.
    """
    stmt = select(AgentCapabilityModel.capability).where(
        AgentCapabilityModel.agent_id == _as_uuid(agent_id, "agent id"),
        AgentCapabilityModel.revoked_at.is_(None),
    )
    return {c for (c,) in (await session.execute(stmt)).all()}


async def capability_history(
    session: AsyncSession, agent_id: Any,
) -> list[AgentCapabilityModel]:
    """Every grant ever made, revoked or not — the append-only record."""
    stmt = select(AgentCapabilityModel).where(
        AgentCapabilityModel.agent_id == _as_uuid(agent_id, "agent id"),
    ).order_by(AgentCapabilityModel.granted_at)
    return list((await session.execute(stmt)).scalars())


# ── writes ───────────────────────────────────────────────────────────────────

async def create_agent(
    session: AsyncSession,
    *,
    tenant_id: Any,
    slug: str,
    kind: str,
    display_name: str = "",
    model_binding: str | None = None,
    package_id: str | None = None,
    capabilities: Iterable[str] = (),
    created_by: Any | None = None,
    allow_external: bool = False,
) -> AgentModel:
    """Register an agent principal and its initial ceiling.

    `allow_external` is the F-09 door, and it is a named argument rather than a
    widened constant so that `grep -rn allow_external` finds every place an
    external principal can come into being. There is exactly one:
    `handoff.service._external_agent_token`, which does it as part of a signed,
    case-scoped, expiring work order to a counterparty an operator enrolled with
    a pinned key. Every other caller — the operator's own create route
    included — leaves it false and cannot make one.

    The caller is flushed but not committed: creation, the ceiling rows and the
    status-history row are one unit. An agent that exists with no record of
    having been created is exactly the gap this feature is meant to close.
    """
    kind = str(kind).strip()
    permitted = TOKENABLE_KINDS if allow_external else ISSUABLE_KINDS
    if kind not in permitted:
        raise AgentError(
            f"agent kind {kind!r} cannot be created here; this build issues "
            + ", ".join(sorted(permitted)))
    slug = str(slug).strip()
    if not slug:
        raise AgentError("an agent needs a slug")
    caps = validate_capabilities(capabilities)

    tid = _as_uuid(tenant_id, "tenant id")
    if await resolve_agent(session, tenant_id=tid, slug=slug) is not None:
        raise AgentError(f"an agent called {slug!r} already exists in this tenant")

    creator = _as_uuid(created_by, "creator id") if created_by is not None else None
    agent = AgentModel(
        id=uuid.uuid4(),
        tenant_id=tid,
        kind=kind,
        slug=slug,
        display_name=str(display_name or slug),
        model_binding=str(model_binding) if model_binding else None,
        package_id=str(package_id) if package_id else None,
        status="active",
        created_by=creator,
    )
    session.add(agent)
    await session.flush()

    for cap in caps:
        session.add(AgentCapabilityModel(
            id=uuid.uuid4(), agent_id=agent.id, capability=cap, granted_by=creator))
    session.add(AgentStatusHistoryModel(
        id=uuid.uuid4(), agent_id=agent.id, from_status=None, to_status="active",
        reason="created", changed_by=creator))
    await session.flush()
    log.info("agent created: %s/%s kind=%s caps=%s", tid, slug, kind, caps)
    return agent


async def ensure_agent(
    session: AsyncSession,
    *,
    tenant_id: Any,
    slug: str,
    kind: str,
    display_name: str = "",
    model_binding: str | None = None,
    package_id: str | None = None,
    capabilities: Iterable[str] = (),
    allow_external: bool = False,
) -> AgentModel:
    """Get the agent with this slug, creating it if it is absent.

    For the platform's own workers, which know their slug and should not need
    an operator to press a button before they can be attributed.

    **It never widens an existing agent.** If the row is already there, it is
    returned as it stands — capabilities, model binding and status untouched.
    An operator who narrowed a ceiling must not have it silently restored by
    the next restart, which is what a create-or-update would do.
    """
    existing = await resolve_agent(session, tenant_id=tenant_id, slug=slug)
    if existing is not None:
        return existing
    return await create_agent(
        session, tenant_id=tenant_id, slug=slug, kind=kind,
        display_name=display_name, model_binding=model_binding,
        package_id=package_id, capabilities=capabilities,
        allow_external=allow_external)


#: (tenant, slug) -> agent id, for the attribution path.
#:
#: Safe to cache and easy to get wrong. An agent's ID never changes, and this
#: cache holds nothing else — not its status, not its ceiling, neither of which
#: may ever be answered from memory (suspension is an incident-response control
#: and has to bite on the next call). Attribution asks "what is this actor
#: called", which is immutable; authorization asks "what may it do", which is
#: read live every time in `tokens.resolve_agent_token`.
_SLUG_CACHE: dict[tuple[str, str], str] = {}


def _cache_forget(tenant_id: Any, slug: str) -> None:
    _SLUG_CACHE.pop((str(tenant_id), str(slug)), None)


async def register_out_of_band(
    *,
    tenant_id: Any,
    slug: str,
    kind: str = "internal",
    display_name: str = "",
    model_binding: str | None = None,
    package_id: str | None = None,
    capabilities: Iterable[str] = (),
) -> str | None:
    """Resolve an agent's id for attribution, registering it if it is absent.

    Called from the AI hot path, so two properties matter more than elegance:

    * **It uses its own session and commits it.** The same reasoning as
      HxGuard's `_log_security_event`: the caller's transaction may be rolled
      back, and an identity created for a call that then failed should still
      exist — but far more importantly, a failure HERE must not poison the
      caller's transaction. A unique-violation race between two concurrent
      first calls would otherwise abort the request that lost it.
    * **It never raises.** Attribution failing must never fail the thing being
      attributed. `None` means "could not resolve", and the caller falls back
      to naming the slug, which is still better than a generic service id.

    Registration is create-if-absent and never widens an existing agent — an
    operator who narrowed a ceiling must not have it restored by a restart.
    """
    key = (str(tenant_id), str(slug))
    cached = _SLUG_CACHE.get(key)
    if cached is not None:
        return cached
    try:
        from velaris_shared.db.session import get_session_factory
        from case_service.tenancy.identity import resolve_tenant

        async with get_session_factory()() as own:
            # The callers hold a tenant SLUG, not a UUID — `tenant_slug` in
            # HxNexus, `grant.tenant_id` (a VARCHAR) in the marketplace. Passing
            # either straight to `ensure_agent` raises, which the guard below
            # would have swallowed into a permanent `unregistered:` fallback:
            # attribution that silently never works, on exactly the path this
            # feature exists to fix.
            resolved = await resolve_tenant(own, tenant_id)
            if resolved is None:
                log.debug("agent attribution: tenant %r does not resolve", tenant_id)
                return None
            agent = await ensure_agent(
                own, tenant_id=resolved[0], slug=slug, kind=kind,
                display_name=display_name, model_binding=model_binding,
                package_id=package_id, capabilities=capabilities)
            agent_id = str(agent.id)
            await own.commit()
    except Exception:                                          # noqa: BLE001
        # Includes the lost race, where another worker inserted first. Try one
        # plain read before giving up: the row now exists, and returning None
        # would attribute this call to a slug while its neighbour used the id.
        try:
            from velaris_shared.db.session import get_session_factory
            from case_service.tenancy.identity import resolve_tenant

            async with get_session_factory()() as own:
                resolved = await resolve_tenant(own, tenant_id)
                found = (None if resolved is None else
                         await resolve_agent(own, tenant_id=resolved[0], slug=slug))
            if found is None:
                log.debug("agent attribution: %s unresolved", slug, exc_info=True)
                return None
            agent_id = str(found.id)
        except Exception:                                      # noqa: BLE001
            log.debug("agent attribution: %s unresolved", slug, exc_info=True)
            return None

    _SLUG_CACHE[key] = agent_id
    return agent_id


async def grant_capability(
    session: AsyncSession, agent: AgentModel, capability: str, *,
    granted_by: Any | None = None,
) -> None:
    """Add one capability to the ceiling. Idempotent against live rows."""
    (cap,) = validate_capabilities([capability])
    if cap in await live_capabilities(session, agent.id):
        return
    session.add(AgentCapabilityModel(
        id=uuid.uuid4(), agent_id=agent.id, capability=cap,
        granted_by=_as_uuid(granted_by, "grantor id") if granted_by is not None else None))
    await session.flush()


async def revoke_capability(
    session: AsyncSession, agent: AgentModel, capability: str, *,
    revoked_by: Any | None = None,
) -> int:
    """Revoke one capability. Returns how many live rows were closed.

    Every live row for that capability is closed, not just one: duplicates are
    permitted by design (a re-grant after a revoke inserts a new row), so
    closing one would leave the capability standing.
    """
    cap = str(capability).strip()
    actor = _as_uuid(revoked_by, "revoker id") if revoked_by is not None else None
    stmt = select(AgentCapabilityModel).where(
        AgentCapabilityModel.agent_id == agent.id,
        AgentCapabilityModel.capability == cap,
        AgentCapabilityModel.revoked_at.is_(None),
    )
    rows = list((await session.execute(stmt)).scalars())
    for row in rows:
        row.revoked_at = _utcnow()
        row.revoked_by = actor
    await session.flush()
    return len(rows)


async def set_status(
    session: AsyncSession, agent: AgentModel, status: str, *,
    reason: str = "", changed_by: Any | None = None,
) -> AgentModel:
    """Change an agent's status, recording who and why.

    `retired` is terminal: a retired agent cannot be brought back, because the
    id has been attributed in a decision log that must keep meaning what it
    said. Bringing one back would let a new operator inherit an old agent's
    history. Create a new agent instead.
    """
    status = str(status).strip()
    if status not in STATUSES:
        raise AgentError(f"unknown agent status {status!r}")
    if agent.status == status:
        return agent
    if agent.status == "retired":
        raise AgentError(
            "a retired agent cannot be reactivated; its id is already "
            "attributed in the decision log. Create a new agent.")

    previous, agent.status = agent.status, status
    agent.updated_at = _utcnow()
    session.add(AgentStatusHistoryModel(
        id=uuid.uuid4(), agent_id=agent.id, from_status=previous, to_status=status,
        reason=str(reason or "")[:2000],
        changed_by=_as_uuid(changed_by, "actor id") if changed_by is not None else None))
    await session.flush()
    log.info("agent %s: %s -> %s (%s)", agent.slug, previous, status, reason or "no reason given")
    return agent


async def status_history(
    session: AsyncSession, agent_id: Any,
) -> list[AgentStatusHistoryModel]:
    stmt = select(AgentStatusHistoryModel).where(
        AgentStatusHistoryModel.agent_id == _as_uuid(agent_id, "agent id"),
    ).order_by(AgentStatusHistoryModel.changed_at)
    return list((await session.execute(stmt)).scalars())
