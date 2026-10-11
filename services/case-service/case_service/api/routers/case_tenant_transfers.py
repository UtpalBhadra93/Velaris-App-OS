"""Moving a case between tenants — the routes.

Design: `docs/Future/Done/CASE-TENANT-TRANSFER-DESIGN.md`. Implementation and the
classification of every case-linked table: `case_actions/tenant_transfer.py`.

    POST /cases/{case_id}/tenant-transfers        source admin requests
    GET  /case-tenant-transfers                   both sides list theirs
    POST /case-tenant-transfers/{id}/approve      a DIFFERENT source admin
    POST /case-tenant-transfers/{id}/cancel       source, before acceptance
    POST /case-tenant-transfers/{id}/accept       destination admin — the move
    POST /case-tenant-transfers/{id}/refuse       destination admin

**Each side acts only as itself.** The caller's tenant decides which side of a
transfer they are on; a destination admin cannot approve on the source's behalf,
and a source admin cannot accept for the destination. Neither side learns a
transfer exists unless it is one of the two tenants named on it — a stranger's
transfer id is a 404.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from velaris_shared import hxguard
from velaris_shared.auth.models import AuthenticatedUser
from velaris_shared.core.error_codes import coded
from velaris_shared.db.models import CaseTenantTransferModel, TenantModel
from velaris_shared.db.session import get_session

from case_service.case_actions import tenant_transfer as tt
from case_service.tenancy.identity import resolve_tenant

router = APIRouter(tags=["case-tenant-transfers"])

_guard = hxguard.guard(tt.PRIVILEGE)


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class RequestIn(_Strict):
    #: A tenant slug or id.
    destination_tenant: str = Field(min_length=1, max_length=255)
    reason: str = Field(min_length=10, max_length=4000)


class DecisionIn(_Strict):
    note: str | None = Field(default=None, max_length=4000)


def _raise(exc: tt.TransferError):
    raise coded(exc.status_code, exc.code, str(exc))


async def _caller_tenant(session: AsyncSession, user: AuthenticatedUser) -> uuid.UUID:
    resolved = await resolve_tenant(session, getattr(user, "tenant_id", None) or "default")
    if resolved is None:
        raise coded(403, "VEL-CASE-TENANT-TRANSFER-POLICY-403",
                    "The caller resolves to no tenant, so there is no side to act for.")
    return resolved[0]


async def _row(session: AsyncSession, transfer_id: uuid.UUID,
               tenant: uuid.UUID) -> CaseTenantTransferModel:
    row = await session.get(CaseTenantTransferModel, transfer_id)
    if row is None or tenant not in (row.source_tenant_id, row.destination_tenant_id):
        raise coded(404, "VEL-CASE-TENANT-TRANSFER-404", "No such transfer.")
    # A request the source has not approved does not exist for the destination —
    # the same rule as the list. Answering 403 or 409 here would confirm it.
    if tenant == row.destination_tenant_id and tenant != row.source_tenant_id \
            and row.state == "requested":
        raise coded(404, "VEL-CASE-TENANT-TRANSFER-404", "No such transfer.")
    return row


def _side(row: CaseTenantTransferModel, tenant: uuid.UUID) -> str:
    return "source" if tenant == row.source_tenant_id else "destination"


def _must_be(row: CaseTenantTransferModel, tenant: uuid.UUID, side: str) -> None:
    if _side(row, tenant) != side:
        raise coded(403, "VEL-CASE-TENANT-TRANSFER-POLICY-403",
                    f"Only the {side} tenant may do this.")


@router.post("/cases/{case_id}/tenant-transfers", status_code=201)
async def request_transfer(
    case_id: uuid.UUID,
    body: RequestIn,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(_guard),
) -> dict[str, Any]:
    from case_service.api.routers.cases import _get_case_or_404

    tenant = await _caller_tenant(session, user)
    case = await _get_case_or_404(session, case_id, user)
    # A NULL-tenant case is visible to every caller, which is not the same as
    # being theirs to give away.
    if case.tenant_id != tenant:
        raise coded(404, "VEL-CASE-TENANT-TRANSFER-404", "Case not found")
    await hxguard.require_case(session, user, "case.read", case_id)
    destination = await resolve_tenant(session, body.destination_tenant)
    if destination is None:
        raise coded(404, "VEL-CASE-TENANT-TRANSFER-404", "No such destination tenant.")
    try:
        row = await tt.request(session, case=case, destination_tenant_id=destination[0],
                               reason=body.reason, actor=str(user.user_id))
    except tt.TransferError as exc:
        _raise(exc)
    await session.commit()
    return tt.as_dict(row, side="source")


@router.get("/cases/{case_id}/tenant-transfers/preflight")
async def preflight_transfer(
    case_id: uuid.UUID,
    destination_tenant: str = Query(..., min_length=1, max_length=255),
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(_guard),
) -> dict[str, Any]:
    """What `request` would refuse, asked BEFORE the button (Oct item 6).

    Same gates as `request_transfer`, in the same order, and it writes nothing.
    HxArbiter is not asked here: the PEP logs a decision, and a screen that
    re-checks on every keystroke would fill the decision log with questions
    nobody acted on. `request` still asks it.
    """
    from case_service.api.routers.cases import _get_case_or_404

    tenant = await _caller_tenant(session, user)
    case = await _get_case_or_404(session, case_id, user)
    if case.tenant_id != tenant:
        raise coded(404, "VEL-CASE-TENANT-TRANSFER-404", "Case not found")
    await hxguard.require_case(session, user, "case.read", case_id)
    destination = await resolve_tenant(session, destination_tenant)
    if destination is None:
        raise coded(404, "VEL-CASE-TENANT-TRANSFER-404", "No such destination tenant.")
    dest = await session.get(TenantModel, destination[0])
    why = await tt.blockers(session, case, dest)
    open_one = (await session.execute(select(CaseTenantTransferModel).where(
        CaseTenantTransferModel.active_key == str(case.id)))).scalar_one_or_none()
    if open_one is not None:
        why.append("a transfer of this case is already open")
    return {"case_id": str(case_id), "destination_tenant_id": str(destination[0]),
            "can_move": not why, "blockers": why,
            "open_transfer_id": str(open_one.id) if open_one else None}


@router.get("/case-tenant-transfers")
async def list_transfers(
    direction: str = Query("all", pattern="^(all|outgoing|incoming)$"),
    state: str | None = Query(None, max_length=16),
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(_guard),
) -> dict[str, Any]:
    """Outgoing rows are the source's tombstones; incoming rows are requests
    awaiting (or past) this tenant's decision. An incoming request carries the
    case number, type and reason — never the case."""
    tenant = await _caller_tenant(session, user)
    q = select(CaseTenantTransferModel)
    if direction == "outgoing":
        q = q.where(CaseTenantTransferModel.source_tenant_id == tenant)
    elif direction == "incoming":
        # A request the source has not approved yet is not the destination's
        # business: it may still be cancelled by the second person.
        q = q.where(CaseTenantTransferModel.destination_tenant_id == tenant,
                    CaseTenantTransferModel.state != "requested")
    else:
        q = q.where(or_(
            CaseTenantTransferModel.source_tenant_id == tenant,
            (CaseTenantTransferModel.destination_tenant_id == tenant)
            & (CaseTenantTransferModel.state != "requested")))
    if state:
        q = q.where(CaseTenantTransferModel.state == state)
    rows = (await session.execute(
        q.order_by(CaseTenantTransferModel.requested_at.desc()).limit(200))).scalars().all()
    return {"items": [{**tt.as_dict(r, side=_side(r, tenant)), "side": _side(r, tenant)}
                      for r in rows]}


@router.post("/case-tenant-transfers/{transfer_id}/approve")
async def approve_transfer(
    transfer_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(_guard),
) -> dict[str, Any]:
    tenant = await _caller_tenant(session, user)
    row = await _row(session, transfer_id, tenant)
    _must_be(row, tenant, "source")
    try:
        await tt.approve(session, row=row, actor=str(user.user_id))
    except tt.TransferError as exc:
        _raise(exc)
    await session.commit()
    return tt.as_dict(row, side="source")


@router.post("/case-tenant-transfers/{transfer_id}/cancel")
async def cancel_transfer(
    transfer_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(_guard),
) -> dict[str, Any]:
    tenant = await _caller_tenant(session, user)
    row = await _row(session, transfer_id, tenant)
    _must_be(row, tenant, "source")
    try:
        await tt.cancel(session, row=row, actor=str(user.user_id))
    except tt.TransferError as exc:
        _raise(exc)
    await session.commit()
    return tt.as_dict(row, side="source")


@router.post("/case-tenant-transfers/{transfer_id}/refuse")
async def refuse_transfer(
    transfer_id: uuid.UUID,
    body: DecisionIn,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(_guard),
) -> dict[str, Any]:
    tenant = await _caller_tenant(session, user)
    row = await _row(session, transfer_id, tenant)
    _must_be(row, tenant, "destination")
    try:
        await tt.refuse(session, row=row, actor=str(user.user_id), note=body.note)
    except tt.TransferError as exc:
        _raise(exc)
    await session.commit()
    return tt.as_dict(row, side="destination")


@router.post("/case-tenant-transfers/{transfer_id}/accept")
async def accept_transfer(
    transfer_id: uuid.UUID,
    body: DecisionIn,
    request: Request,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(_guard),
) -> dict[str, Any]:
    """The move. One transaction: a failure part way rolls every write back."""
    from velaris_shared.db import repository as repo
    from case_service.core import case_lifecycle

    tenant = await _caller_tenant(session, user)
    row = await _row(session, transfer_id, tenant)
    _must_be(row, tenant, "destination")

    async def restart_sla(case) -> int:
        ct = await repo.get_case_type(session, case.case_type_id)
        ct_def = (ct.definition_json or {}) if ct else {}
        stage = next((s for s in ct_def.get("stages", []) or []
                      if isinstance(s, dict) and s.get("id") == case.current_stage_id), None)
        if stage is None:
            return 0
        return await case_lifecycle.start_stage_slas(
            session, case_id=case.id, case_type_id=case.case_type_id, stage=stage,
            case_type_def=ct_def, tenant_id=case.tenant_id)

    try:
        await tt.accept(session, row=row, actor=str(user.user_id), note=body.note,
                        restart_sla=restart_sla)
        await session.commit()
    except tt.TransferError as exc:
        await session.rollback()
        _raise(exc)
    except Exception:
        await session.rollback()
        raise
    return tt.as_dict(row, side="destination")
