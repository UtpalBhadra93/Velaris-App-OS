"""Staged rollouts of content versions — the operator's API (Oct item 18a).

Request, approve (a different person), change the percentage or flip the
switch, then promote or roll back. Every change is an event on the rollout, and
the lock — one open rollout per case type — answers with the open rollout and
the three ways forward rather than a bare refusal.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from velaris_shared import hxguard
from velaris_shared.auth.models import AuthenticatedUser
from velaris_shared.db.models import CaseTypeModel, ContentRolloutModel
from velaris_shared.db.session import get_session

from case_service.rollouts import service
from case_service.tenancy.identity import resolve_tenant

router = APIRouter(prefix="/rollouts", tags=["rollouts"])


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class StartIn(_Strict):
    artifact_kind: str = Field(default="case_type", max_length=20)
    baseline_id: uuid.UUID
    candidate_id: uuid.UUID
    strategy: str = Field(max_length=20)
    percent: int = Field(default=0, ge=0, le=100)
    note: str | None = Field(default=None, max_length=2000)


class ApproveIn(_Strict):
    strategy: str | None = Field(default=None, max_length=20)
    percent: int | None = Field(default=None, ge=0, le=100)
    reason: str | None = Field(default=None, max_length=2000)


class PercentIn(_Strict):
    percent: int = Field(ge=0, le=100)
    reason: str | None = Field(default=None, max_length=2000)


class CloseIn(_Strict):
    reason: str | None = Field(default=None, max_length=2000)


class PolicyIn(_Strict):
    shadow: bool | None = None
    targeting: dict[str, Any] | None = None
    ramp: dict[str, Any] | None = None
    guard: dict[str, Any] | None = None


class RebaseIn(_Strict):
    reason: str = Field(min_length=5, max_length=2000)
    dry_run: bool = True
    limit: int = Field(default=500, ge=1, le=5000)


class SupersedeIn(_Strict):
    candidate_id: uuid.UUID
    strategy: str = Field(max_length=20)
    percent: int = Field(default=0, ge=0, le=100)
    reason: str | None = Field(default=None, max_length=2000)


def _actor(user: AuthenticatedUser) -> str:
    return (getattr(user, "username", None) or getattr(user, "email", None)
            or getattr(user, "user_id", None) or "unknown")


async def _tenant(session: AsyncSession, user: AuthenticatedUser) -> uuid.UUID:
    resolved = await resolve_tenant(session, getattr(user, "tenant_id", None) or "default")
    if resolved is None:
        raise HTTPException(403, "VEL-ROLLOUT-TENANT-403: the caller has no tenant here")
    return resolved[0]


async def _row(session: AsyncSession, rollout_id: uuid.UUID,
               tenant_uuid: uuid.UUID) -> ContentRolloutModel:
    try:
        return await service.get(session, rollout_id, tenant_uuid)
    except LookupError:
        raise HTTPException(404, "VEL-ROLLOUT-404: no such rollout") from None


def _raise(exc: Exception) -> None:
    if isinstance(exc, service.RolloutLocked):
        raise HTTPException(409, exc.detail()) from exc
    if isinstance(exc, PermissionError):
        raise HTTPException(403, f"VEL-ROLLOUT-403: {exc}") from exc
    if isinstance(exc, LookupError):
        raise HTTPException(404, "VEL-ROLLOUT-404: no such case type") from exc
    if isinstance(exc, service.RolloutError):
        raise HTTPException(409, f"VEL-ROLLOUT-409: {exc}") from exc
    raise exc


async def _full(session: AsyncSession, row: ContentRolloutModel) -> dict[str, Any]:
    out = service.to_json(row)
    names = {}
    for cid in (row.baseline_id, row.candidate_id):
        ct = await session.get(CaseTypeModel, cid)
        names[str(cid)] = ct.version if ct else None
    out["baseline_version"] = names.get(str(row.baseline_id))
    out["candidate_version"] = names.get(str(row.candidate_id))
    out["arms"] = await service.arm_counts(session, row.id)
    out["policy"] = service.policy_json(await service.get_policy(session, row.id))
    return out


@router.get("")
async def list_rollouts(
    state: str | None = Query(None, pattern="^(open|closed|all)$"),
    limit: int = Query(100, ge=1, le=500),
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(hxguard.guard("content.rollouts.read")),
) -> dict[str, Any]:
    tenant_uuid = await _tenant(session, user)
    q = select(ContentRolloutModel).where(ContentRolloutModel.tenant_id == tenant_uuid)
    if state == "open":
        q = q.where(ContentRolloutModel.state.in_(service.OPEN_STATES))
    elif state == "closed":
        q = q.where(ContentRolloutModel.state.in_(service.CLOSED_STATES))
    rows = (await session.execute(
        q.order_by(ContentRolloutModel.created_at.desc()).limit(limit))).scalars().all()
    return {"rollouts": [await _full(session, r) for r in rows], "count": len(rows),
            "strategies": list(service.STRATEGIES)}


@router.get("/versions")
async def version_status(
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(hxguard.guard("content.rollouts.read")),
) -> dict[str, Any]:
    """Every case type version a rollout touched, and where its requests now go."""
    return {"versions": await service.version_status(session, await _tenant(session, user))}


@router.get("/{rollout_id}")
async def get_rollout(
    rollout_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(hxguard.guard("content.rollouts.read")),
) -> dict[str, Any]:
    row = await _row(session, rollout_id, await _tenant(session, user))
    return {"rollout": await _full(session, row), "events": await service.events(session, row.id)}


@router.post("", status_code=201)
async def request_rollout(
    body: StartIn,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(hxguard.guard("content.rollouts.request")),
) -> dict[str, Any]:
    """Request a rollout. It changes nothing until somebody else approves it."""
    tenant_uuid = await _tenant(session, user)
    try:
        row = await service.start(
            session, tenant_uuid=tenant_uuid, kind=body.artifact_kind,
            baseline_id=body.baseline_id, candidate_id=body.candidate_id,
            strategy=body.strategy, percent=body.percent, actor=_actor(user), note=body.note)
    except Exception as exc:                                     # noqa: BLE001
        _raise(exc)
    await session.commit()
    return {"rollout": await _full(session, row)}


@router.post("/{rollout_id}/approve")
async def approve_rollout(
    rollout_id: uuid.UUID,
    body: ApproveIn,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(hxguard.guard("content.rollouts.approve")),
) -> dict[str, Any]:
    """The operator's decision; the requested strategy and percent may be overridden."""
    row = await _row(session, rollout_id, await _tenant(session, user))
    try:
        row = await service.approve(session, row, actor=_actor(user), strategy=body.strategy,
                                    percent=body.percent, reason=body.reason)
    except Exception as exc:                                     # noqa: BLE001
        _raise(exc)
    await session.commit()
    return {"rollout": await _full(session, row)}


@router.patch("/{rollout_id}/percent")
async def change_percent(
    rollout_id: uuid.UUID,
    body: PercentIn,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(hxguard.guard("content.rollouts.change")),
) -> dict[str, Any]:
    """Raise or lower a percentage, switch a flag, or flip blue-green. New cases only."""
    row = await _row(session, rollout_id, await _tenant(session, user))
    try:
        row = await service.set_percent(session, row, percent=body.percent,
                                        actor=_actor(user), reason=body.reason)
    except Exception as exc:                                     # noqa: BLE001
        _raise(exc)
    await session.commit()
    return {"rollout": await _full(session, row)}


@router.post("/{rollout_id}/promote")
async def promote_rollout(
    rollout_id: uuid.UUID,
    body: CloseIn,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(hxguard.guard("content.rollouts.close")),
) -> dict[str, Any]:
    row = await _row(session, rollout_id, await _tenant(session, user))
    try:
        row = await service.promote(session, row, actor=_actor(user), reason=body.reason)
    except Exception as exc:                                     # noqa: BLE001
        _raise(exc)
    await session.commit()
    return {"rollout": await _full(session, row)}


@router.post("/{rollout_id}/rollback")
async def rollback_rollout(
    rollout_id: uuid.UUID,
    body: CloseIn,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(hxguard.guard("content.rollouts.close")),
) -> dict[str, Any]:
    """Every NEW case goes back to the baseline. Cases already created stay pinned."""
    row = await _row(session, rollout_id, await _tenant(session, user))
    try:
        row = await service.rollback(session, row, actor=_actor(user), reason=body.reason)
    except Exception as exc:                                     # noqa: BLE001
        _raise(exc)
    await session.commit()
    return {"rollout": await _full(session, row)}


@router.post("/{rollout_id}/supersede", status_code=201)
async def supersede_rollout(
    rollout_id: uuid.UUID,
    body: SupersedeIn,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(hxguard.guard("content.rollouts.close")),
) -> dict[str, Any]:
    """Roll this one back and request a new candidate against the same baseline.

    The new rollout needs approval like any other — superseding is not a way
    round the second person.
    """
    tenant_uuid = await _tenant(session, user)
    row = await _row(session, rollout_id, tenant_uuid)
    try:
        new = await service.supersede(session, row, candidate_id=body.candidate_id,
                                      strategy=body.strategy, percent=body.percent,
                                      actor=_actor(user), reason=body.reason)
    except Exception as exc:                                     # noqa: BLE001
        await session.rollback()
        _raise(exc)
    await session.commit()
    return {"superseded": str(rollout_id), "rollout": await _full(session, new)}


@router.put("/{rollout_id}/policy")
async def set_policy(
    rollout_id: uuid.UUID,
    body: PolicyIn,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(hxguard.guard("content.rollouts.change")),
) -> dict[str, Any]:
    """Shadow, targeting, a scheduled ramp, a guard. Replaces the whole policy."""
    row = await _row(session, rollout_id, await _tenant(session, user))
    try:
        await service.set_policy(session, row, shadow=body.shadow, targeting=body.targeting,
                                 ramp=body.ramp, guard=body.guard, actor=_actor(user))
    except Exception as exc:                                     # noqa: BLE001
        _raise(exc)
    await session.commit()
    return {"rollout": await _full(session, row)}


@router.get("/{rollout_id}/outcomes")
async def rollout_outcomes(
    rollout_id: uuid.UUID,
    min_cases: int = Query(30, ge=5, le=100_000),
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(hxguard.guard("content.rollouts.read")),
) -> dict[str, Any]:
    """The A/B comparison: how each arm's cases are doing."""
    from case_service.rollouts import outcomes
    row = await _row(session, rollout_id, await _tenant(session, user))
    return {"rollout_id": str(row.id), **await outcomes.compare(session, row.id,
                                                                min_cases=min_cases)}


@router.get("/{rollout_id}/shadow")
async def rollout_shadow(
    rollout_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(hxguard.guard("content.rollouts.read")),
) -> dict[str, Any]:
    """What the candidate would have done on baseline cases. It changed nothing."""
    from case_service.rollouts import shadow
    row = await _row(session, rollout_id, await _tenant(session, user))
    return {"rollout_id": str(row.id), **await shadow.summary(session, row.id)}


@router.post("/{rollout_id}/rebase")
async def rebase_rollout(
    rollout_id: uuid.UUID,
    body: RebaseIn,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(hxguard.guard("content.rollouts.close")),
) -> dict[str, Any]:
    """After a rollback: move open cases off the withdrawn version where nothing
    they have done would be lost. A dry run (the default) changes nothing."""
    row = await _row(session, rollout_id, await _tenant(session, user))
    try:
        out = await service.rebase(session, row, actor=_actor(user), reason=body.reason,
                                   dry_run=body.dry_run, limit=body.limit)
    except Exception as exc:                                     # noqa: BLE001
        _raise(exc)
    if not body.dry_run:
        await session.commit()
    return out
