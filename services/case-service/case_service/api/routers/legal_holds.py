"""Legal holds — the routes. Rules and the list of what a hold freezes:
`case_service/legal_hold/service.py`.

    GET  /legal-holds                      the caller's tenant's holds
    POST /legal-holds                      place one matter's hold on 1–500 cases
    GET  /legal-holds/cases/{case_id}      is this case held, and what that freezes
    POST /legal-holds/matters/release      release every active hold of one matter
    POST /legal-holds/{hold_id}/release    release one hold

**Three privileges, not one.** Reading names a dispute; placing protects a
record; releasing is what lets it be destroyed again and is the narrowest.

**Anyone who can read the case learns that it is held** — a case worker whose
Delete is refused is owed the reason. The matter, the stated reason and who
placed it are shown only to holders of `legal.holds.read`. The customer portal
never mentions a hold at all.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import uuid
from typing import Any, Literal

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from velaris_shared import hxguard
from velaris_shared.auth.dependencies import get_current_user
from velaris_shared.auth.models import AuthenticatedUser
from velaris_shared.core.error_codes import coded
from velaris_shared.db.models import CaseInstanceModel, LegalHoldModel
from velaris_shared.db.session import get_session

from case_service.legal_hold import service as lh
from velaris_shared.tenancy.identity import (
    caller_tenant_uuid, is_platform_operator, may_reach_tenant, scope_to_caller,
)

router = APIRouter(prefix="/legal-holds", tags=["legal-holds"])

#: One request, one matter, at most this many cases — the bulk ceiling the case
#: action dispatcher uses.
MAX_CASES = 500


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PlaceIn(_Strict):
    case_ids: list[uuid.UUID] = Field(min_length=1, max_length=MAX_CASES)
    matter_ref: str = Field(min_length=1, max_length=lh.MATTER_MAX)
    reason: str = Field(min_length=lh.REASON_MIN, max_length=lh.REASON_MAX)


class ReleaseIn(_Strict):
    reason: str = Field(min_length=lh.REASON_MIN, max_length=lh.REASON_MAX)


class ReleaseMatterIn(ReleaseIn):
    matter_ref: str = Field(min_length=1, max_length=lh.MATTER_MAX)


def _raise(exc: lh.LegalHoldError):
    raise coded(exc.status_code, exc.code, str(exc))


@router.get("")
async def list_holds(
    status: Literal["active", "released"] | None = None,
    matter_ref: str | None = Query(None, max_length=lh.MATTER_MAX),
    case_id: uuid.UUID | None = None,
    limit: int = Query(100, ge=1, le=500),
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(hxguard.guard(lh.PRIV_READ)),
) -> dict[str, Any]:
    q = select(LegalHoldModel).order_by(LegalHoldModel.placed_at.desc()).limit(limit)
    if status:
        q = q.where(LegalHoldModel.status == status)
    if matter_ref:
        q = q.where(LegalHoldModel.matter_ref == lh.normalise_matter(matter_ref))
    if case_id:
        q = q.where(LegalHoldModel.case_id == case_id)
    q = await scope_to_caller(q, LegalHoldModel, session, user)
    rows = (await session.execute(q)).scalars().all()
    return {"holds": [lh.as_dict(r) for r in rows], "count": len(rows),
            "frozen": list(lh.FROZEN),
            # So the screen offers Release only to whoever may — asked of the
            # server, never inferred from a role name.
            "can_release": hxguard.user_allows(user, lh.PRIV_RELEASE)}


@router.post("", status_code=201)
async def place_holds(
    body: PlaceIn,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(hxguard.guard(lh.PRIV_PLACE)),
) -> dict[str, Any]:
    """Place one matter's hold on each named case. Reported case by case.

    A case outside the caller's tenant is refused as not found — never a reason
    to learn which of the others exist. A case with no tenant belongs to the
    platform, so only the platform operator may freeze it.
    """
    tenant = await caller_tenant_uuid(session, user)
    operator = await is_platform_operator(session, user)
    results: list[dict[str, Any]] = []
    placed = 0
    for cid in dict.fromkeys(body.case_ids):          # de-duplicated, order kept
        item: dict[str, Any] = {"case_id": str(cid)}
        try:
            # The TENANT boundary, not a relationship to the case: a compliance
            # officer freezes cases they have never worked, so `case.read`
            # (assignee, editor, manager) is the wrong question here. What must
            # hold is that the case is in the caller's own tenant.
            case = await session.get(CaseInstanceModel, cid)
            if (case is None or (case.tenant_id is None and not operator)
                    or not await may_reach_tenant(session, user, case.tenant_id)):
                raise coded(404, lh.CODE_NOTFOUND, "Case not found")
            row = await lh.place(session, case=case, matter_ref=body.matter_ref,
                                 reason=body.reason, actor=str(user.user_id),
                                 tenant_uuid=tenant)
            item.update(ok=True, hold=lh.as_dict(row))
            placed += 1
        except lh.LegalHoldError as exc:
            if exc.status_code == 400:
                _raise(exc)                            # the request itself is wrong
            item.update(ok=False, code=exc.code, error=str(exc))
        except Exception as exc:                       # noqa: BLE001
            code = getattr(exc, "status_code", None)
            if code not in (403, 404):
                raise
            # 403 and 404 both read as "not found": a case in another tenant
            # must be indistinguishable from one that does not exist.
            item.update(ok=False, code=lh.CODE_NOTFOUND, error="Case not found")
        results.append(item)
    await session.commit()
    return {"placed": placed, "requested": len(results), "results": results}


@router.get("/cases/{case_id}")
async def case_hold_status(
    case_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(get_current_user),
) -> dict[str, Any]:
    """Whether the case is held. Details only for `legal.holds.read`."""
    await hxguard.require_case(session, user, "case.read", case_id)
    held = await lh.is_held(session, case_id)
    out: dict[str, Any] = {"case_id": str(case_id), "held": held,
                           "frozen": list(lh.FROZEN) if held else []}
    may_read = hxguard.user_allows(user, lh.PRIV_READ)
    out["can_read"] = may_read
    out["can_place"] = hxguard.user_allows(user, lh.PRIV_PLACE)
    out["can_release"] = hxguard.user_allows(user, lh.PRIV_RELEASE)
    if may_read and await lh.available(session):
        rows = (await session.execute(
            select(LegalHoldModel).where(LegalHoldModel.case_id == case_id)
            .order_by(LegalHoldModel.placed_at.desc()))).scalars().all()
        mine = [r for r in rows if await may_reach_tenant(session, user, r.tenant_id)]
        out["holds"] = [lh.as_dict(r) for r in mine]
        # A shared case may be held by a tenant this caller cannot see into.
        out["other_active_holds"] = sum(
            1 for r in rows if r.status == lh.ACTIVE and r not in mine)
    return out


@router.post("/matters/release")
async def release_matter(
    body: ReleaseMatterIn,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(hxguard.guard(lh.PRIV_RELEASE)),
) -> dict[str, Any]:
    """Release every active hold of one matter in the caller's tenant."""
    q = select(LegalHoldModel).where(
        LegalHoldModel.status == lh.ACTIVE,
        LegalHoldModel.matter_ref == lh.normalise_matter(body.matter_ref))
    q = await scope_to_caller(q, LegalHoldModel, session, user)
    rows = (await session.execute(q)).scalars().all()
    if not rows:
        raise coded(404, lh.CODE_NOTFOUND, "No active hold for that matter.")
    try:
        for row in rows:
            await lh.release(session, hold=row, reason=body.reason, actor=str(user.user_id))
    except lh.LegalHoldError as exc:
        _raise(exc)
    await session.commit()
    return {"released": len(rows), "matter_ref": rows[0].matter_ref,
            "case_ids": [str(r.case_id) for r in rows]}


@router.post("/{hold_id}/release")
async def release_hold(
    hold_id: uuid.UUID,
    body: ReleaseIn,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(hxguard.guard(lh.PRIV_RELEASE)),
) -> dict[str, Any]:
    row = await session.get(LegalHoldModel, hold_id)
    # The path guard refuses another tenant's hold already; checked here as
    # well, because MCP tools call handlers directly.
    if row is None or not await may_reach_tenant(session, user, row.tenant_id):
        raise coded(404, lh.CODE_NOTFOUND, "No such legal hold.")
    try:
        await lh.release(session, hold=row, reason=body.reason, actor=str(user.user_id))
    except lh.LegalHoldError as exc:
        _raise(exc)
    await session.commit()
    return {"hold": lh.as_dict(row),
            "case_still_held": await lh.is_held(session, row.case_id)}
