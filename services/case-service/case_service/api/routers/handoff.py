"""F-09 — the agent-to-agent case handoff protocol.

Two audiences, two authentication schemes, and keeping them apart is the design.

**Our side** — enrol a counterparty, issue a work order, revoke one — is ordinary
staff work behind `handoff.*` HxGuard actions and a normal user session.

**Their side** — accept, read, propose, return — is authenticated by the WORK
ORDER TOKEN alone. No session, no login, no role. An external agent presents a
case-scoped, expiring, instantly revocable F-00 agent token, and that token is
the entire authority: `get_agent_caller` resolves it against the live grant, so
revoking the grant ends the handoff mid-request rather than at the next cache
expiry.

Three things this surface is careful about:

* **There is no verb that writes to a case.** An external agent proposes; a
  person on our side confirms, on the same Captured tab an email's proposal
  lands on, applied through their own grant.
* **A refusal is a row.** A counterparty reaching for something it may not have
  is a signal, and one that leaves no trace is a signal nobody receives — so
  every denial is recorded before the 403 is raised.
* **The token is shown once.** `issue` returns it in the response body and it is
  never stored; the row keeps the grant id, so an order is revoked without
  anybody holding the secret.

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
    HandoffActionModel,
    HandoffCounterpartyModel,
    HandoffWorkOrderModel,
)
from velaris_shared.db.session import get_session
from velaris_shared.middleware.endpoint_rate_limit import rate_limit

from case_service.agents.tokens import get_agent_caller
from case_service.core.timestamps import iso_utc
from case_service.handoff import projection as proj
from case_service.handoff import service
from case_service.tenancy.identity import resolve_tenant

log = logging.getLogger(__name__)

router = APIRouter(prefix="/handoff", tags=["handoff"])

#: The counterparty-facing verbs are the ones a stranger can reach. Bounded per
#: order by the token's own TTL as well, but a budget is not a rate limit.
_agent_rl = rate_limit(max_calls=120, window_seconds=60, name="handoff-agent")


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class EnrolIn(_Strict):
    slug: str = Field(min_length=2, max_length=64, pattern=r"^[a-z0-9][a-z0-9._-]*$")
    display_name: str = Field(min_length=1, max_length=255)
    #: Raw Ed25519 public key, base64. Pinned; never learned from a message.
    public_key: str = Field(min_length=40, max_length=200)
    #: What this organisation may EVER be granted. A dispatcher cannot exceed it.
    max_ceiling: str = Field(default=proj.CEILING_INTERNAL, max_length=20)
    notes: str | None = Field(default=None, max_length=2000)


class IssueIn(_Strict):
    case_id: uuid.UUID
    counterparty: str = Field(min_length=2, max_length=64)
    ceiling: str = Field(default=proj.CEILING_INTERNAL, max_length=20)
    allowed_actions: list[str] = Field(default_factory=lambda: ["accept", "read",
                                                                "propose", "return"])
    ttl_minutes: int | None = Field(default=None, ge=1, le=service.MAX_TTL_MINUTES)
    obligations: dict[str, Any] | None = None


class RevokeIn(_Strict):
    reason: str = Field(min_length=5, max_length=2000)


class AcceptIn(_Strict):
    #: Their signature over the order document's SHA-256, base64.
    signature: str = Field(min_length=40, max_length=200)


class ProposeIn(_Strict):
    target: str = Field(min_length=1, max_length=255)
    value: Any = None
    note: str | None = Field(default=None, max_length=2000)


class ReturnIn(_Strict):
    summary: str = Field(default="", max_length=4000)


async def _tenant(session: AsyncSession, user: AuthenticatedUser) -> uuid.UUID:
    resolved = await resolve_tenant(session, getattr(user, "tenant_id", None) or "default")
    if resolved is None:
        raise HTTPException(403, "VEL-HANDOFF-TENANT-UNRESOLVED-403")
    return resolved[0]


def _cp_json(c: HandoffCounterpartyModel) -> dict[str, Any]:
    return {
        "id": str(c.id), "slug": c.slug, "display_name": c.display_name,
        # The fingerprint, not the key: this is what an operator checks against
        # what the counterparty told them by another channel.
        "key_id": c.key_id, "status": c.status, "max_ceiling": c.max_ceiling,
        "notes": c.notes, "created_at": iso_utc(c.created_at),
    }


def _order_json(o: HandoffWorkOrderModel) -> dict[str, Any]:
    return {
        "id": str(o.id), "case_id": str(o.case_id),
        "counterparty_id": str(o.counterparty_id),
        "agent_id": str(o.agent_id) if o.agent_id else None,
        "ceiling": o.ceiling, "allowed_actions": o.allowed_actions,
        "obligations": o.obligations, "status": o.status,
        "expires_at": iso_utc(o.expires_at),
        "signed": bool(o.order_signature),
        "accepted_at": iso_utc(o.accepted_at),
        "returned_at": iso_utc(o.returned_at),
        "return_summary": o.return_summary,
        "revoked_at": iso_utc(o.revoked_at), "revoked_reason": o.revoked_reason,
        "created_by": o.created_by, "created_at": iso_utc(o.created_at),
    }


# ══ our side ════════════════════════════════════════════════════════════════


@router.post("/counterparties", status_code=201)
async def enrol_counterparty(
    body: EnrolIn,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(hxguard.guard("handoff.counterparties.write")),
) -> dict[str, Any]:
    """Pin an organisation's key. There is no self-registration path at all."""
    tenant_uuid = await _tenant(session, user)
    try:
        row = await service.enrol(
            session, tenant_uuid=tenant_uuid, slug=body.slug,
            display_name=body.display_name, public_key=body.public_key,
            max_ceiling=body.max_ceiling, notes=body.notes,
            created_by=getattr(user, "user_id", None))
    except (service.HandoffError, proj.CeilingError) as exc:
        raise HTTPException(422, f"VEL-HANDOFF-ENROL-422: {exc}") from exc
    await session.commit()
    return {"counterparty": _cp_json(row)}


@router.get("/counterparties")
async def list_counterparties(
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(hxguard.guard("handoff.counterparties.read")),
) -> dict[str, Any]:
    tenant_uuid = await _tenant(session, user)
    rows = list((await session.execute(
        select(HandoffCounterpartyModel)
        .where(HandoffCounterpartyModel.tenant_id == tenant_uuid)
        .order_by(HandoffCounterpartyModel.slug))).scalars().all())
    return {"counterparties": [_cp_json(r) for r in rows], "count": len(rows)}


@router.post("/counterparties/{slug}/suspend")
async def suspend_counterparty(
    slug: str,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(hxguard.guard("handoff.counterparties.write")),
) -> dict[str, Any]:
    """The fast path for an incident.

    It stops new orders AND refuses every action on the ones outstanding, so a
    suspicion about an organisation is acted on once rather than order by order.
    """
    tenant_uuid = await _tenant(session, user)
    row = (await session.execute(
        select(HandoffCounterpartyModel).where(
            HandoffCounterpartyModel.tenant_id == tenant_uuid,
            HandoffCounterpartyModel.slug == slug))).scalars().first()
    if row is None:
        raise HTTPException(404, "VEL-HANDOFF-404: no such counterparty")
    row.status = "suspended"
    await session.commit()
    return {"counterparty": _cp_json(row)}


@router.post("/orders", status_code=201)
async def issue_order(
    body: IssueIn,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(hxguard.guard("handoff.orders.issue")),
) -> dict[str, Any]:
    """Hand one case to one counterparty for one window.

    The token comes back **once** and is never stored. The row keeps the grant
    id, so the order is revocable without anybody holding the secret.
    """
    tenant_uuid = await _tenant(session, user)
    token_str = getattr(user, "tenant_id", None) or "default"
    counterparty = (await session.execute(
        select(HandoffCounterpartyModel).where(
            HandoffCounterpartyModel.tenant_id == tenant_uuid,
            HandoffCounterpartyModel.slug == body.counterparty))).scalars().first()
    if counterparty is None:
        raise HTTPException(
            404, "VEL-HANDOFF-404: no such counterparty. A case is never handed "
                 "to a party nobody enrolled.")
    try:
        order, raw = await service.issue(
            session, tenant_uuid=tenant_uuid, tenant_token=token_str,
            case_id=body.case_id, counterparty=counterparty,
            ceiling=body.ceiling, allowed_actions=body.allowed_actions,
            ttl_minutes=body.ttl_minutes, obligations=body.obligations,
            created_by=getattr(user, "user_id", None))
    except (service.HandoffError, proj.CeilingError) as exc:
        raise HTTPException(409, f"VEL-HANDOFF-ISSUE-409: {exc}") from exc
    except Exception as exc:                                     # noqa: BLE001
        raise HTTPException(409, f"VEL-HANDOFF-ISSUE-409: {exc}") from exc
    await session.commit()
    return {
        "order": _order_json(order),
        "token": raw,
        # What they must sign to accept. Handing over the digest rather than
        # asking them to recompute it removes the commonest integration bug
        # without weakening anything: they still sign with their own key.
        "accept_digest": service._sha(service.order_document(order)),
        "note": "The token is shown once and is not stored. Revoke the order to end it.",
    }


@router.get("/orders")
async def list_orders(
    case_id: uuid.UUID | None = Query(None),
    status: str | None = Query(None, max_length=16),
    limit: int = Query(100, ge=1, le=500),
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(hxguard.guard("handoff.orders.read")),
) -> dict[str, Any]:
    tenant_uuid = await _tenant(session, user)
    q = select(HandoffWorkOrderModel).where(HandoffWorkOrderModel.tenant_id == tenant_uuid)
    if case_id:
        q = q.where(HandoffWorkOrderModel.case_id == case_id)
    if status:
        q = q.where(HandoffWorkOrderModel.status == status)
    rows = list((await session.execute(
        q.order_by(HandoffWorkOrderModel.created_at.desc()).limit(limit))).scalars().all())
    return {"orders": [_order_json(r) for r in rows], "count": len(rows)}


@router.get("/orders/{order_id}/actions")
async def order_actions(
    order_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(hxguard.guard("handoff.orders.read")),
) -> dict[str, Any]:
    """Everything the counterparty did, and everything they were refused.

    `projection` on a read names the fields actually disclosed — the question a
    data-protection incident turns on, and one that cannot be reconstructed from
    a ceiling and a case that has since changed.
    """
    tenant_uuid = await _tenant(session, user)
    order = await session.get(HandoffWorkOrderModel, order_id)
    if order is None or order.tenant_id != tenant_uuid:
        raise HTTPException(404, "VEL-HANDOFF-404: no such work order")
    rows = list((await session.execute(
        select(HandoffActionModel)
        .where(HandoffActionModel.work_order_id == order_id)
        .order_by(HandoffActionModel.created_at))).scalars().all())
    return {
        "order": _order_json(order),
        "actions": [{"id": str(a.id), "kind": a.kind, "decision": a.decision,
                     "reason": a.reason, "projection": a.projection,
                     "payload_sha256": a.payload_sha256,
                     "proposal_id": str(a.proposal_id) if a.proposal_id else None,
                     "at": iso_utc(a.created_at)} for a in rows],
        "count": len(rows),
    }


@router.post("/orders/{order_id}/revoke")
async def revoke_order(
    order_id: uuid.UUID,
    body: RevokeIn,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(hxguard.guard("handoff.orders.issue")),
) -> dict[str, Any]:
    """Take the case back. Instant, because the token grant carries the authority."""
    tenant_uuid = await _tenant(session, user)
    order = await session.get(HandoffWorkOrderModel, order_id)
    if order is None or order.tenant_id != tenant_uuid:
        raise HTTPException(404, "VEL-HANDOFF-404: no such work order")
    try:
        await service.revoke(session, order=order, reason=body.reason)
    except service.HandoffError as exc:
        raise HTTPException(409, f"VEL-HANDOFF-409: {exc}") from exc
    await session.commit()
    return {"order": _order_json(order)}


# ══ their side — the protocol ═══════════════════════════════════════════════
#
# Authenticated by the work order token ALONE. No session, no role.


async def _order_for_caller(session: AsyncSession, order_id: uuid.UUID,
                            caller) -> HandoffWorkOrderModel:
    order = await session.get(HandoffWorkOrderModel, order_id)
    # 404 rather than 403: a 403 tells a stranger the order exists.
    if order is None:
        raise HTTPException(404, "VEL-HANDOFF-404: no such work order")
    if str(order.agent_id) != str(getattr(caller, "agent_id", None)):
        raise HTTPException(404, "VEL-HANDOFF-404: no such work order")
    return order


@router.post("/protocol/{order_id}/accept", dependencies=[Depends(_agent_rl)])
async def protocol_accept(
    order_id: uuid.UUID,
    body: AcceptIn,
    session: AsyncSession = Depends(get_session),
    caller=Depends(get_agent_caller),
) -> dict[str, Any]:
    """Sign the order with the key we pinned when we enrolled you."""
    order = await _order_for_caller(session, order_id, caller)
    try:
        await service.accept(session, order=order, signature=body.signature)
    except service.HandoffError as exc:
        await session.commit()          # the refusal is a record
        raise HTTPException(403, f"VEL-HANDOFF-ACCEPT-403: {exc}") from exc
    await session.commit()
    return {"order": _order_json(order)}


@router.get("/protocol/{order_id}/case", dependencies=[Depends(_agent_rl)])
async def protocol_read(
    order_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    caller=Depends(get_agent_caller),
) -> dict[str, Any]:
    """The case, as far as this order's ceiling admits — computed, not filtered."""
    order = await _order_for_caller(session, order_id, caller)
    try:
        view = await service.read(session, order=order,
                                  tenant_token=str(order.tenant_id))
    except service.HandoffError as exc:
        await session.commit()
        raise HTTPException(403, f"VEL-HANDOFF-READ-403: {exc}") from exc
    await session.commit()
    return view


@router.post("/protocol/{order_id}/propose", status_code=201,
             dependencies=[Depends(_agent_rl)])
async def protocol_propose(
    order_id: uuid.UUID,
    body: ProposeIn,
    session: AsyncSession = Depends(get_session),
    caller=Depends(get_agent_caller),
) -> dict[str, Any]:
    """Suggest a change. It changes nothing until a person here confirms it."""
    order = await _order_for_caller(session, order_id, caller)
    try:
        out = await service.propose(
            session, order=order, tenant_token=str(order.tenant_id),
            target=body.target, value=body.value, note=body.note)
    except service.HandoffError as exc:
        await session.commit()
        raise HTTPException(403, f"VEL-HANDOFF-PROPOSE-403: {exc}") from exc
    await session.commit()
    return out


@router.post("/protocol/{order_id}/return", dependencies=[Depends(_agent_rl)])
async def protocol_return(
    order_id: uuid.UUID,
    body: ReturnIn,
    session: AsyncSession = Depends(get_session),
    caller=Depends(get_agent_caller),
) -> dict[str, Any]:
    """Hand the case back. The token is revoked in the same breath."""
    order = await _order_for_caller(session, order_id, caller)
    try:
        await service.hand_back(session, order=order, summary=body.summary)
    except service.HandoffError as exc:
        await session.commit()
        raise HTTPException(403, f"VEL-HANDOFF-RETURN-403: {exc}") from exc
    await session.commit()
    return {"order": _order_json(order)}
