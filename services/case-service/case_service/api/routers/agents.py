"""F-00 — the agent principal API.

Managing agents, their capability ceiling, and their short-lived tokens.

The gates are not uniform, and the asymmetry is the design:

* **Reading is broad, minting is admin-only.** A token is authority handed to
  something that will act unattended; listing agents is not.
* **Suspension is separate from creation.** An incident responder must be able
  to stop an agent at 3am without also being able to create one or widen a
  ceiling. `agents.status.change` is held by security as well as admin;
  `agents.registry.write` is not.
* **Nothing here is platform-scoped.** Every route resolves the caller's tenant
  and pins every read and write to it. An agent belongs to exactly one tenant
  by construction (mig 182), so unlike HxArbiter's policies there is no
  platform escape hatch to guard — there is nothing to guard it against.

The raw token is returned **once**, at mint, and never again: the grant row
stores no copy of it, only the id it is signed with.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import logging
import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from velaris_shared import hxguard
from velaris_shared.auth.models import AuthenticatedUser
from velaris_shared.db.models import (
    AgentModel,
    AgentScorecardModel,
    AgentTokenGrantModel,
    AgentWorkforceModel,
)
from velaris_shared.db.session import get_session
from velaris_shared.middleware.endpoint_rate_limit import rate_limit

from case_service.agents import identity, tokens
from case_service.core.timestamps import iso_utc
from case_service.tenancy.identity import actor_uuid, resolve_tenant

log = logging.getLogger(__name__)

router = APIRouter(prefix="/agents", tags=["agents"])

#: Minting is the expensive, security-relevant call and the one worth flooding:
#: a caller who can mint can mint continuously. Capped well below what any
#: legitimate operator does by hand.
_mint_rl = rate_limit(max_calls=20, window_seconds=60, name="agents-mint")


class _Strict(BaseModel):
    """Unknown fields are refused, not ignored.

    A misspelled `capabilities` that silently became "no capabilities" would
    read as a successful create, and the agent would fail later somewhere with
    no trace of why.
    """
    model_config = ConfigDict(extra="forbid")


class AgentIn(_Strict):
    slug: str = Field(min_length=1, max_length=128)
    kind: str = Field(min_length=1, max_length=20)
    display_name: str = Field(default="", max_length=255)
    model_binding: str | None = Field(default=None, max_length=255)
    package_id: str | None = Field(default=None, max_length=255)
    capabilities: list[str] = Field(default_factory=list, max_length=16)


class CapabilityIn(_Strict):
    capability: str = Field(min_length=1, max_length=128)


class StatusIn(_Strict):
    status: str = Field(min_length=1, max_length=20)
    #: Required in substance, not by the schema: an empty reason is accepted
    #: for `active` but refused for the two that take an agent out of service,
    #: because "who suspended this and why" is the question asked afterwards.
    reason: str = Field(default="", max_length=2000)


class TokenIn(_Strict):
    #: Omitted means the agent's whole live ceiling. A subset is how a caller
    #: hands out less than the agent holds; anything beyond the ceiling is
    #: refused rather than trimmed.
    capabilities: list[str] | None = Field(default=None, max_length=16)
    ttl_minutes: int | None = Field(default=None, ge=1, le=1440)
    #: A case-scoped token is valid for that case and no other.
    case_id: uuid.UUID | None = None
    label: str | None = Field(default=None, max_length=255)


async def _tenant_uuid(session: AsyncSession, user: AuthenticatedUser) -> uuid.UUID:
    token = user.tenant_id or (user.attributes or {}).get("tenant_id")
    resolved = await resolve_tenant(session, token)
    if resolved is None:
        # An unresolvable tenant is a denial, never "any tenant".
        raise HTTPException(403, "VEL-AGENT-TENANT-UNRESOLVED-403")
    return resolved[0]


async def _actor(session: AsyncSession, user: AuthenticatedUser) -> uuid.UUID | None:
    """Who is doing this, for the `GUID` attribution columns on every agent table.

    `user.user_id` is a UUID for a password login and a **username** for an SSO
    or service principal, and these six columns are `GUID`. Passing the raw
    value through refused the second kind outright — `POST /agents` as
    `utpal.bhadra` answered *"creator id is not a valid id"*, a 422 on a
    perfectly good request. `actor_uuid` resolves either form; a caller this
    platform has no user row for is recorded as absent, which is what the
    nullable column already means.
    """
    return await actor_uuid(session, user)


async def _load(
    session: AsyncSession, agent_id: uuid.UUID, tenant: uuid.UUID,
) -> AgentModel:
    """The agent, or a 404 — including when it belongs to another tenant.

    404 and not 403, matching the isolation posture the rest of the platform
    settled on: a 403 confirms the id exists, which is itself a leak.
    """
    agent = await identity.get_agent(session, agent_id, tenant_id=tenant)
    if agent is None:
        raise HTTPException(404, "VEL-AGENT-404: no such agent")
    return agent


def _agent_json(agent: AgentModel, capabilities: set[str] | None = None) -> dict[str, Any]:
    out: dict[str, Any] = {
        "id": str(agent.id),
        "tenant_id": str(agent.tenant_id),
        "slug": agent.slug,
        "kind": agent.kind,
        "display_name": agent.display_name,
        "model_binding": agent.model_binding,
        "package_id": agent.package_id,
        "status": agent.status,
        "created_at": iso_utc(agent.created_at),
        "updated_at": iso_utc(agent.updated_at),
    }
    if capabilities is not None:
        out["capabilities"] = sorted(capabilities)
    return out


# ── registry ─────────────────────────────────────────────────────────────────

@router.get("")
async def list_agents(
    include_retired: bool = Query(False),
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(hxguard.guard("agents.registry.read")),
) -> dict[str, Any]:
    """Every agent principal in this tenant, with its live ceiling.

    Also reports whether tokens are switched on at all: an operator looking at
    a list of agents whose tokens nothing will accept should be told so here,
    rather than discovering it from a 401 on the agent's first call.
    """
    from velaris_shared.config import get_settings

    tenant = await _tenant_uuid(session, user)
    rows = await identity.list_agents(
        session, tenant_id=tenant, include_retired=include_retired)
    return {
        "tokens_enabled": bool(get_settings().agent_tokens_enabled),
        "capability_vocabulary": sorted(identity.CAPABILITIES),
        "issuable_kinds": sorted(identity.ISSUABLE_KINDS),
        "agents": [
            _agent_json(a, await identity.live_capabilities(session, a.id))
            for a in rows
        ],
    }


@router.post("", status_code=201)
async def create_agent(
    payload: AgentIn,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(hxguard.guard("agents.registry.write")),
) -> dict[str, Any]:
    tenant = await _tenant_uuid(session, user)
    try:
        agent = await identity.create_agent(
            session,
            tenant_id=tenant,
            slug=payload.slug,
            kind=payload.kind,
            display_name=payload.display_name,
            model_binding=payload.model_binding,
            package_id=payload.package_id,
            capabilities=payload.capabilities,
            created_by=await _actor(session, user),
        )
    except identity.AgentError as exc:
        raise HTTPException(422, f"VEL-AGENT-INVALID-422: {exc}") from exc
    await session.commit()
    return _agent_json(agent, await identity.live_capabilities(session, agent.id))


@router.get("/{agent_id}")
async def get_agent(
    agent_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(hxguard.guard("agents.registry.read")),
) -> dict[str, Any]:
    """One agent, with the full append-only record behind it.

    The history is here rather than behind its own route because it is the
    reason the tables are append-only: "what could this agent do in March, and
    who let it" has to be answerable in one call, or nobody asks it.
    """
    tenant = await _tenant_uuid(session, user)
    agent = await _load(session, agent_id, tenant)
    body = _agent_json(agent, await identity.live_capabilities(session, agent.id))
    body["capability_history"] = [
        {
            "capability": c.capability,
            "granted_at": iso_utc(c.granted_at),
            "granted_by": str(c.granted_by) if c.granted_by else None,
            "revoked_at": iso_utc(c.revoked_at) if c.revoked_at else None,
            "revoked_by": str(c.revoked_by) if c.revoked_by else None,
        }
        for c in await identity.capability_history(session, agent.id)
    ]
    body["status_history"] = [
        {
            "from": h.from_status,
            "to": h.to_status,
            "reason": h.reason,
            "changed_at": iso_utc(h.changed_at),
            "changed_by": str(h.changed_by) if h.changed_by else None,
        }
        for h in await identity.status_history(session, agent.id)
    ]
    return body


@router.post("/{agent_id}/capabilities")
async def grant_capability(
    agent_id: uuid.UUID,
    payload: CapabilityIn,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(hxguard.guard("agents.registry.write")),
) -> dict[str, Any]:
    tenant = await _tenant_uuid(session, user)
    agent = await _load(session, agent_id, tenant)
    if agent.status == "retired":
        raise HTTPException(422, "VEL-AGENT-RETIRED-422: a retired agent's "
                                 "ceiling cannot be changed")
    try:
        await identity.grant_capability(
            session, agent, payload.capability,
            granted_by=await _actor(session, user))
    except identity.AgentError as exc:
        raise HTTPException(422, f"VEL-AGENT-INVALID-422: {exc}") from exc
    await session.commit()
    return _agent_json(agent, await identity.live_capabilities(session, agent.id))


@router.delete("/{agent_id}/capabilities/{capability}")
async def revoke_capability(
    agent_id: uuid.UUID,
    capability: str,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(hxguard.guard("agents.registry.write")),
) -> dict[str, Any]:
    """Revoke a capability. Every outstanding token narrows on its next call.

    Not a 204: the caller needs to see the ceiling that is left, and a body
    saying "revoked 0 rows" is how you learn you spelled it wrong.
    """
    tenant = await _tenant_uuid(session, user)
    agent = await _load(session, agent_id, tenant)
    closed = await identity.revoke_capability(
        session, agent, capability, revoked_by=await _actor(session, user))
    await session.commit()
    body = _agent_json(agent, await identity.live_capabilities(session, agent.id))
    body["revoked_rows"] = closed
    return body


@router.post("/{agent_id}/status")
async def set_status(
    agent_id: uuid.UUID,
    payload: StatusIn,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(hxguard.guard("agents.status.change")),
) -> dict[str, Any]:
    """Suspend, resume or retire. Suspension bites on the agent's next call.

    A reason is required for anything that takes an agent out of service. The
    row is append-only precisely so that question has an answer, and an empty
    string is not one.
    """
    tenant = await _tenant_uuid(session, user)
    agent = await _load(session, agent_id, tenant)
    if payload.status in ("suspended", "retired") and not payload.reason.strip():
        raise HTTPException(
            422, "VEL-AGENT-REASON-422: taking an agent out of service needs "
                 "a reason; it is recorded permanently")
    try:
        await identity.set_status(
            session, agent, payload.status,
            reason=payload.reason, changed_by=await _actor(session, user))
    except identity.AgentError as exc:
        raise HTTPException(422, f"VEL-AGENT-INVALID-422: {exc}") from exc
    await session.commit()
    return _agent_json(agent, await identity.live_capabilities(session, agent.id))


# ── tokens ───────────────────────────────────────────────────────────────────

@router.post("/{agent_id}/tokens", status_code=201,
             dependencies=[Depends(_mint_rl)])
async def mint_token(
    agent_id: uuid.UUID,
    payload: TokenIn,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(hxguard.guard("agents.tokens.issue")),
) -> dict[str, Any]:
    """Mint a short-lived, capability-scoped token. Returned once, never again."""
    tenant = await _tenant_uuid(session, user)
    agent = await _load(session, agent_id, tenant)
    try:
        token, grant = await tokens.mint_agent_token(
            session, agent,
            capabilities=payload.capabilities,
            ttl_minutes=payload.ttl_minutes,
            case_id=payload.case_id,
            label=payload.label,
            issued_by=await _actor(session, user),
        )
    except identity.AgentError as exc:
        raise HTTPException(422, f"VEL-AGENT-TOKEN-422: {exc}") from exc
    await session.commit()
    log.info("agent token issued by %s for agent %s", user.user_id, agent.slug)
    return {
        "token": token,                       # the only time this is ever shown
        "grant_id": str(grant.id),
        "agent_id": str(agent.id),
        "capabilities": list(grant.capabilities or []),
        "case_id": str(grant.audience_case_id) if grant.audience_case_id else None,
        "expires_at": iso_utc(grant.expires_at),
    }


@router.get("/{agent_id}/tokens")
async def list_tokens(
    agent_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(hxguard.guard("agents.registry.read")),
) -> list[dict[str, Any]]:
    """Outstanding and past grants. No token value is stored, so none is shown."""
    tenant = await _tenant_uuid(session, user)
    agent = await _load(session, agent_id, tenant)
    rows = (await session.execute(
        select(AgentTokenGrantModel)
        .where(AgentTokenGrantModel.agent_id == agent.id)
        .order_by(AgentTokenGrantModel.created_at.desc())
        .limit(200)
    )).scalars().all()
    return [
        {
            "grant_id": str(g.id),
            "capabilities": list(g.capabilities or []),
            "case_id": str(g.audience_case_id) if g.audience_case_id else None,
            "label": g.label,
            "issued_by": str(g.issued_by) if g.issued_by else None,
            "created_at": iso_utc(g.created_at),
            "expires_at": iso_utc(g.expires_at),
            "revoked": bool(g.revoked),
            "revoked_at": iso_utc(g.revoked_at) if g.revoked_at else None,
        }
        for g in rows
    ]


@router.delete("/{agent_id}/tokens/{grant_id}", status_code=204)
async def revoke_token(
    agent_id: uuid.UUID,
    grant_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(hxguard.guard("agents.tokens.revoke")),
) -> None:
    tenant = await _tenant_uuid(session, user)
    agent = await _load(session, agent_id, tenant)
    grant = await session.get(AgentTokenGrantModel, grant_id)
    # The agent check is not redundant with the tenant check above: a grant id
    # from another tenant's agent must 404 here, and `session.get` alone would
    # happily return it.
    if grant is None or grant.agent_id != agent.id:
        raise HTTPException(404, "VEL-AGENT-GRANT-404: no such token grant")
    await tokens.revoke_grant(
        session, grant, revoked_by=await _actor(session, user))
    await session.commit()


# ══ F-05 — agents as workers ════════════════════════════════════════════════
#
# Appended to the agents router because a roster row is a fact about an agent.
# The gates differ from the ones above on purpose: enrolling an agent decides
# that a non-human may hold case work, which is a staffing decision rather than
# a registry edit.

#: A SEPARATE router for the collection-level workforce paths, and it is not
#: tidiness. `/agents/{agent_id}` is declared above with a `uuid.UUID` path
#: parameter, and FastAPI matches routes in declaration order: `GET
#: /agents/workforce` would be captured by it and answered 422 on the UUID
#: parse, never falling through. Mounting these on their own prefix — included
#: BEFORE the agents router in main.py — is what makes them reachable.
workforce_router = APIRouter(prefix="/agents/workforce", tags=["agents"])


class EnrolIn(_Strict):
    #: Required, and refused when blank. An agent that cannot hand work to a
    #: person is an agent whose failures have nowhere to go.
    escalation_queue: str = Field(min_length=1, max_length=255)
    skills: list[str] = Field(default_factory=list, max_length=32)
    #: NULL = uncapped. Written as an absent value rather than a large number so
    #: "no cap" reads as a decision somebody made.
    max_concurrent: int | None = Field(default=None, ge=1, le=1000)
    token_budget_per_hour: int | None = Field(default=None, ge=1)


class BenchIn(_Strict):
    benched: bool
    reason: str = Field(default="", max_length=2000)


class WeightProposalIn(_Strict):
    weight: float = Field(ge=0, le=10)
    reason: str = Field(min_length=1, max_length=2000)


class WeightDecisionIn(_Strict):
    approve: bool


def _workforce_json(
    row,
    availability: dict[str, Any] | None = None,
    agent: AgentModel | None = None,
) -> dict[str, Any]:
    body = {
        "agent_id": str(row.agent_id),
        "skills": list(row.skills or []),
        "max_concurrent": row.max_concurrent,
        "token_budget_per_hour": row.token_budget_per_hour,
        "escalation_queue": row.escalation_queue,
        "routing_weight": float(row.routing_weight or 1),
        "benched": bool(row.benched),
        "bench_reason": row.bench_reason,
        "benched_at": iso_utc(row.benched_at) if row.benched_at else None,
    }
    if availability is not None:
        body["availability"] = availability
    if agent is not None:
        # The roster is read by a person deciding who to bench. A page of UUIDs
        # cannot be read that way, and the name is the agent's, not the roster's
        # — so it is carried here rather than copied into the roster row, where
        # it would go stale the first time somebody renamed an agent.
        body["agent"] = {
            "slug": agent.slug,
            "display_name": agent.display_name,
            "kind": agent.kind,
            "status": agent.status,
            "model_binding": agent.model_binding,
        }
    return body


def _scorecard_json(card: AgentScorecardModel) -> dict[str, Any]:
    """One scorecard on the wire, including the proposal it carries.

    The proposal travels with the numbers deliberately: a supervisor approving
    a routing-weight change three days later must see the evidence the proposer
    saw, not what happens to be true when they open the screen.
    """
    return {
        "id": str(card.id),
        "agent_id": str(card.agent_id),
        "window": {"start": iso_utc(card.window_start),
                   "end": iso_utc(card.window_end)},
        "decisions": card.decisions,
        "reviewed": card.reviewed,
        "accepted": card.accepted,
        "overridden": card.overridden,
        "override_rate": (float(card.override_rate)
                          if card.override_rate is not None else None),
        "cost_total": float(card.cost_total),
        "cost_per_decision": (float(card.cost_per_decision)
                              if card.cost_per_decision is not None else None),
        "currency": card.currency,
        "proposed_weight": (float(card.proposed_weight)
                            if card.proposed_weight is not None else None),
        "proposal_status": card.proposal_status,
        "proposal_reason": card.proposal_reason,
        "decided_by": card.decided_by,
        "decided_at": iso_utc(card.decided_at),
        "created_at": iso_utc(card.created_at),
    }


@workforce_router.get("")
async def list_workforce(
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(hxguard.guard("agents.workforce.read")),
) -> dict[str, Any]:
    """The roster, with why each agent can or cannot take more work.

    The reasons are returned rather than a bare availability flag: "at capacity"
    and "over budget" are different problems with different fixes.
    """
    from velaris_shared.config import get_settings

    from case_service.agents import workforce

    tenant = await _tenant_uuid(session, user)
    rows = (await session.execute(
        select(AgentWorkforceModel)
        .where(AgentWorkforceModel.tenant_id == tenant)
        .order_by(AgentWorkforceModel.created_at)
    )).scalars().all()
    # One query for the names rather than one per row: a roster is small, but
    # the availability call below already costs two queries an agent and this
    # screen is the one an operator refreshes while an incident is running.
    agents: dict[uuid.UUID, AgentModel] = {}
    if rows:
        agents = {
            a.id: a for a in (await session.execute(
                select(AgentModel).where(
                    AgentModel.id.in_([r.agent_id for r in rows]))
            )).scalars()
        }
    settings = get_settings()
    return {
        # Reported for the same reason `list_agents` reports `tokens_enabled`:
        # an operator looking at scorecards that stopped updating should learn
        # here that nightly recomputation is off, not deduce it from dates.
        "scorecard_cron": {
            "enabled": bool(settings.agent_scorecard_cron_enabled),
            "interval_hours": settings.agent_scorecard_interval_hours,
            "window_days": settings.agent_scorecard_window_days,
        },
        "roster": [
            _workforce_json(r, await workforce.availability(session, r),
                            agents.get(r.agent_id))
            for r in rows
        ],
    }


@router.put("/{agent_id}/workforce")
async def enrol_agent(
    agent_id: uuid.UUID,
    payload: EnrolIn,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(hxguard.guard("agents.workforce.write")),
) -> dict[str, Any]:
    """Put an agent on the roster, or update its capacity.

    An agent in the registry is NOT automatically a worker — HxNexus's case-Q&A
    agent must never be handed a case step — so this is an explicit act.
    """
    from case_service.agents import workforce

    tenant = await _tenant_uuid(session, user)
    agent = await _load(session, agent_id, tenant)
    try:
        row = await workforce.enrol(
            session, agent=agent, escalation_queue=payload.escalation_queue,
            skills=payload.skills, max_concurrent=payload.max_concurrent,
            token_budget_per_hour=payload.token_budget_per_hour,
            created_by=await _actor(session, user))
    except workforce.WorkforceError as exc:
        raise HTTPException(422, f"VEL-AGENT-WORKFORCE-422: {exc}") from exc
    await session.commit()
    return _workforce_json(row, await workforce.availability(session, row), agent)


@router.post("/{agent_id}/workforce/bench")
async def bench_agent(
    agent_id: uuid.UUID,
    payload: BenchIn,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(hxguard.guard("agents.workforce.write")),
) -> dict[str, Any]:
    """Stop or resume giving an agent work.

    Benching does NOT reassign what the agent already holds — pulling work out
    from under a part-finished step is a bigger operation than stopping the
    flow, and doing it silently would be worse than not doing it.
    """
    from case_service.agents import workforce

    tenant = await _tenant_uuid(session, user)
    agent = await _load(session, agent_id, tenant)
    row = await workforce.profile(session, agent_id)
    if row is None:
        raise HTTPException(404, "VEL-AGENT-WORKFORCE-404: this agent is not on "
                                 "the roster")
    if payload.benched and not payload.reason.strip():
        raise HTTPException(
            422, "VEL-AGENT-REASON-422: benching an agent needs a reason; it is "
                 "the first question asked about one that stopped receiving work")
    if payload.benched:
        await workforce.bench(session, row, reason=payload.reason)
    else:
        await workforce.unbench(session, row)
    await session.commit()
    return _workforce_json(row, await workforce.availability(session, row), agent)


@router.post("/{agent_id}/workforce/scorecard", status_code=201)
async def build_scorecard(
    agent_id: uuid.UUID,
    days: int = Query(7, ge=1, le=365),
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(hxguard.guard("agents.workforce.read")),
) -> dict[str, Any]:
    """Compute this agent's performance from the F-01 ledger and F-02 costs.

    Nothing is recorded twice: every number counts the same rows those reports
    count, which is what makes the scorecard reconcile with them by
    construction.
    """
    from case_service.agents import workforce

    tenant = await _tenant_uuid(session, user)
    agent = await _load(session, agent_id, tenant)
    card = await workforce.scorecard(session, agent=agent, days=days)
    await session.commit()
    return _scorecard_json(card)


@workforce_router.get("/scorecards")
async def list_scorecards(
    agent_id: uuid.UUID | None = Query(None),
    status: str | None = Query(None, max_length=20),
    limit: int = Query(50, ge=1, le=500),
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(hxguard.guard("agents.workforce.read")),
) -> dict[str, Any]:
    """Scorecards for this tenant, newest first.

    `status=proposed` is the one a supervisor opens: the weight changes waiting
    on a person. Without it there is no way to find them — a proposal changes
    nothing until approved, so an unread one is simply invisible, which is the
    failure mode of every approval queue nobody built a queue for.

    Tenant-pinned like every other route here, and the filter is applied in the
    query rather than after it: filtering a page of rows returns fewer than
    `limit` and reads as "there are only three", which is a different claim.
    """
    q = (select(AgentScorecardModel)
         .where(AgentScorecardModel.tenant_id == await _tenant_uuid(session, user)))
    if agent_id is not None:
        q = q.where(AgentScorecardModel.agent_id == agent_id)
    if status:
        if status == "pending":       # what the screen calls it
            status = "proposed"
        if status not in {"proposed", "approved", "rejected"}:
            raise HTTPException(
                422, "VEL-AGENT-SCORECARD-STATUS-422: status must be one of "
                     "proposed, approved, rejected")
        q = q.where(AgentScorecardModel.proposal_status == status)
    rows = (await session.execute(
        q.order_by(AgentScorecardModel.created_at.desc()).limit(limit)
    )).scalars().all()
    return {"scorecards": [_scorecard_json(c) for c in rows]}


@workforce_router.post("/scorecards/{card_id}/propose")
async def propose_routing_weight(
    card_id: uuid.UUID,
    payload: WeightProposalIn,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(hxguard.guard("agents.workforce.read")),
) -> dict[str, Any]:
    """Propose a routing-weight change. It changes nothing until approved."""
    from case_service.agents import workforce

    tenant = await _tenant_uuid(session, user)
    card = await session.get(AgentScorecardModel, card_id)
    if card is None or card.tenant_id != tenant:
        raise HTTPException(404, "VEL-AGENT-SCORECARD-404: no such scorecard")
    try:
        await workforce.propose_weight(
            session, card, weight=payload.weight, reason=payload.reason)
    except workforce.WorkforceError as exc:
        raise HTTPException(422, f"VEL-AGENT-WORKFORCE-422: {exc}") from exc
    await session.commit()
    return {"id": str(card.id), "proposed_weight": float(card.proposed_weight),
            "status": card.proposal_status}


@workforce_router.post("/scorecards/{card_id}/decide")
async def decide_routing_weight(
    card_id: uuid.UUID,
    payload: WeightDecisionIn,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(hxguard.guard("agents.workforce.approve")),
) -> dict[str, Any]:
    """A supervisor's answer. Only an approval touches the roster.

    Its own action, separate from proposing: a system that adjusted its own
    routing from its own performance data is a feedback loop nobody is steering,
    and this approval is the person in it.
    """
    from case_service.agents import workforce

    tenant = await _tenant_uuid(session, user)
    card = await session.get(AgentScorecardModel, card_id)
    if card is None or card.tenant_id != tenant:
        raise HTTPException(404, "VEL-AGENT-SCORECARD-404: no such scorecard")
    try:
        row = await workforce.decide_weight(
            session, card, approve=payload.approve, decided_by=str(user.user_id))
    except workforce.WorkforceError as exc:
        raise HTTPException(422, f"VEL-AGENT-WORKFORCE-422: {exc}") from exc
    await session.commit()
    return {
        "id": str(card.id),
        "status": card.proposal_status,
        "routing_weight": (float(row.routing_weight) if row is not None else None),
    }
