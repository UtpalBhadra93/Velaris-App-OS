"""F-12 — process-as-negotiation. The capstone.

Two organisations agree a shared workflow. Each side publishes what it offers and
what it requires; a deterministic matcher finds the joint composition; both sides
sign; a person here approves; and it goes to shadow before it enforces.

Three things this surface is careful about:

* **`/verify` re-runs the matcher.** That endpoint is what "no trusted third
  party" means in practice — either side can recompute the other's answer, and a
  mismatch is a disagreement about the contract itself rather than about it.
* **Suspension needs nothing from the other side.** No negotiation, no notice,
  no cooperation. It fail-closes the seam to a queue rather than an error, so a
  counterparty mid-case is not left holding work it cannot hand back.
* **The lifecycle is one step at a time.** `approved → shadow → enforced`,
  because skipping shadow would make the first real case the experiment.

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
from velaris_shared.db.models import HandoffCounterpartyModel, JointContractModel
from velaris_shared.db.session import get_session

from case_service.core.timestamps import iso_utc
from case_service.negotiate import matcher, service
from case_service.tenancy.identity import resolve_tenant

log = logging.getLogger(__name__)

router = APIRouter(prefix="/negotiate", tags=["negotiate"])


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ProposeIn(_Strict):
    counterparty: str = Field(min_length=2, max_length=64)
    name: str = Field(min_length=1, max_length=255)
    our_offer: dict[str, Any] = Field(default_factory=dict)
    their_offer: dict[str, Any] = Field(default_factory=dict)
    our_requirements: dict[str, Any] = Field(default_factory=dict)
    their_requirements: dict[str, Any] = Field(default_factory=dict)
    #: Renegotiation is a NEW contract, never an edit: what both sides signed
    #: must stay exactly as it was signed.
    supersedes: uuid.UUID | None = None


class SignIn(_Strict):
    signature: str = Field(min_length=40, max_length=200)


class PromoteIn(_Strict):
    to: str = Field(max_length=16)


class SuspendIn(_Strict):
    reason: str = Field(min_length=5, max_length=2000)


class AnchorIn(_Strict):
    direction: str = Field(max_length=8)
    chain_head: str = Field(min_length=16, max_length=64)
    signature: str | None = Field(default=None, max_length=200)


async def _tenant(session: AsyncSession, user: AuthenticatedUser) -> uuid.UUID:
    resolved = await resolve_tenant(session, getattr(user, "tenant_id", None) or "default")
    if resolved is None:
        raise HTTPException(403, "VEL-NEGOTIATE-TENANT-UNRESOLVED-403")
    return resolved[0]


async def _contract(session: AsyncSession, contract_id: uuid.UUID,
                    tenant: uuid.UUID) -> JointContractModel:
    row = await session.get(JointContractModel, contract_id)
    if row is None or row.tenant_id != tenant:
        raise HTTPException(404, "VEL-NEGOTIATE-404: no such contract")
    return row


def _json(c: JointContractModel) -> dict[str, Any]:
    return {
        "id": str(c.id), "name": c.name, "version": c.version,
        "counterparty_id": str(c.counterparty_id),
        "supersedes": str(c.supersedes) if c.supersedes else None,
        "status": c.status, "unmatched_reason": c.unmatched_reason,
        "composition": c.composition, "resolution": c.resolution,
        "effective_ceiling": c.effective_ceiling,
        # What the seam will ACTUALLY carry right now. A term agreed is not a
        # term in force, and reading the negotiated ceiling directly is the
        # mistake this field exists to prevent.
        "seam_ceiling": service.seam_ceiling(c),
        "contract_sha256": c.contract_sha256,
        "signed_by_us": bool(c.our_signature),
        "signed_by_them": bool(c.their_signature),
        "approved_by": c.approved_by, "approved_at": iso_utc(c.approved_at),
        "enforced_at": iso_utc(c.enforced_at),
        "suspended_at": iso_utc(c.suspended_at),
        "suspended_by": c.suspended_by, "suspended_reason": c.suspended_reason,
        "created_at": iso_utc(c.created_at),
    }


@router.post("/contracts", status_code=201)
async def propose_contract(
    body: ProposeIn,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(hxguard.guard("negotiate.contracts.propose")),
) -> dict[str, Any]:
    """Run the matcher. A 201 either way — `unmatchable` is a real outcome.

    "These two sets of requirements have no joint process" is the result of a
    genuine negotiation, and the clause that failed is what the next round has
    to move.
    """
    tenant_uuid = await _tenant(session, user)
    counterparty = (await session.execute(
        select(HandoffCounterpartyModel).where(
            HandoffCounterpartyModel.tenant_id == tenant_uuid,
            HandoffCounterpartyModel.slug == body.counterparty))).scalars().first()
    if counterparty is None:
        raise HTTPException(
            404, "VEL-NEGOTIATE-404: no such counterparty. A contract is never "
                 "agreed with a party nobody enrolled — the pinned key is what "
                 "both signatures are checked against.")
    try:
        row = await service.propose(
            session, tenant_uuid=tenant_uuid, counterparty=counterparty,
            name=body.name, our_offer=body.our_offer, their_offer=body.their_offer,
            our_requirements=body.our_requirements,
            their_requirements=body.their_requirements,
            supersedes=body.supersedes,
            created_by=getattr(user, "user_id", None))
    except service.NegotiationError as exc:
        raise HTTPException(422, f"VEL-NEGOTIATE-422: {exc}") from exc
    await session.commit()
    return {"contract": _json(row)}


@router.get("/contracts")
async def list_contracts(
    status: str | None = Query(None, max_length=16),
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(hxguard.guard("negotiate.contracts.read")),
) -> dict[str, Any]:
    tenant_uuid = await _tenant(session, user)
    q = select(JointContractModel).where(JointContractModel.tenant_id == tenant_uuid)
    if status:
        q = q.where(JointContractModel.status == status)
    rows = list((await session.execute(
        q.order_by(JointContractModel.created_at.desc()))).scalars().all())
    return {"contracts": [_json(r) for r in rows], "count": len(rows)}


@router.get("/contracts/{contract_id}/verify")
async def verify_contract(
    contract_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(hxguard.guard("negotiate.contracts.read")),
) -> dict[str, Any]:
    """Re-run the matcher on the stored inputs. Do we still get this contract?

    This endpoint is what "no trusted third party" means in practice: either
    side can recompute the other's answer, and a mismatch is a disagreement
    about the contract itself rather than a difference of opinion about it.
    """
    tenant_uuid = await _tenant(session, user)
    row = await _contract(session, contract_id, tenant_uuid)
    counterparty = await session.get(HandoffCounterpartyModel, row.counterparty_id)
    if counterparty is None:
        raise HTTPException(409, "VEL-NEGOTIATE-409: the counterparty is gone")
    try:
        return service.verify_their_match(
            row, platform_ceiling=counterparty.max_ceiling)
    except matcher.MatchError as exc:
        raise HTTPException(422, f"VEL-NEGOTIATE-422: {exc}") from exc


@router.post("/contracts/{contract_id}/counter-sign")
async def counter_sign(
    contract_id: uuid.UUID,
    body: SignIn,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(hxguard.guard("negotiate.contracts.propose")),
) -> dict[str, Any]:
    """Record their signature, checked against the key we pinned."""
    tenant_uuid = await _tenant(session, user)
    row = await _contract(session, contract_id, tenant_uuid)
    try:
        await service.counter_sign(session, row=row, signature=body.signature)
    except service.NegotiationError as exc:
        raise HTTPException(403, f"VEL-NEGOTIATE-SIGN-403: {exc}") from exc
    await session.commit()
    return {"contract": _json(row)}


@router.post("/contracts/{contract_id}/approve")
async def approve_contract(
    contract_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(hxguard.guard("negotiate.contracts.approve")),
) -> dict[str, Any]:
    """A person here agrees. Both signatures must already be on it."""
    tenant_uuid = await _tenant(session, user)
    row = await _contract(session, contract_id, tenant_uuid)
    try:
        await service.approve(session, row=row,
                              actor=getattr(user, "user_id", None) or "unknown")
    except service.NegotiationError as exc:
        raise HTTPException(409, f"VEL-NEGOTIATE-409: {exc}") from exc
    await session.commit()
    return {"contract": _json(row)}


@router.post("/contracts/{contract_id}/promote")
async def promote_contract(
    contract_id: uuid.UUID,
    body: PromoteIn,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(hxguard.guard("negotiate.contracts.approve")),
) -> dict[str, Any]:
    """One stage at a time: `approved → shadow → enforced`."""
    tenant_uuid = await _tenant(session, user)
    row = await _contract(session, contract_id, tenant_uuid)
    try:
        await service.promote(session, row=row, to=body.to,
                              actor=getattr(user, "user_id", None) or "unknown")
    except service.NegotiationError as exc:
        raise HTTPException(409, f"VEL-NEGOTIATE-409: {exc}") from exc
    await session.commit()
    return {"contract": _json(row)}


@router.post("/contracts/{contract_id}/suspend")
async def suspend_contract(
    contract_id: uuid.UUID,
    body: SuspendIn,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(hxguard.guard("negotiate.contracts.suspend")),
) -> dict[str, Any]:
    """Close the seam, now, without asking anybody.

    Its own action because the person doing this in an incident is not
    necessarily the person who approved the contract — and they must not need to
    be.
    """
    tenant_uuid = await _tenant(session, user)
    row = await _contract(session, contract_id, tenant_uuid)
    out = await service.suspend(session, row=row,
                                actor=getattr(user, "user_id", None) or "unknown",
                                reason=body.reason)
    await session.commit()
    return {"contract": _json(row), "seam": out.get("seam"),
            "note": out.get("note")}


@router.post("/contracts/{contract_id}/anchors", status_code=201)
async def add_anchor(
    contract_id: uuid.UUID,
    body: AnchorIn,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(hxguard.guard("negotiate.contracts.propose")),
) -> dict[str, Any]:
    """Record a chain head — ours going out, or theirs coming in."""
    tenant_uuid = await _tenant(session, user)
    row = await _contract(session, contract_id, tenant_uuid)
    try:
        anchor = await service.anchor(
            session, row=row, direction=body.direction,
            chain_head=body.chain_head, signature=body.signature)
    except service.NegotiationError as exc:
        raise HTTPException(422, f"VEL-NEGOTIATE-422: {exc}") from exc
    await session.commit()
    return {"anchor": {"id": str(anchor.id), "direction": anchor.direction,
                       "seq": anchor.seq, "chain_head": anchor.chain_head,
                       "divergence": anchor.divergence,
                       "anchored_at": iso_utc(anchor.anchored_at)}}


@router.get("/contracts/{contract_id}/anchors")
async def audit_anchors(
    contract_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(hxguard.guard("negotiate.contracts.read")),
) -> dict[str, Any]:
    """Has either side's history been rewritten under the other's nose?"""
    tenant_uuid = await _tenant(session, user)
    row = await _contract(session, contract_id, tenant_uuid)
    return await service.audit_anchors(session, row)
