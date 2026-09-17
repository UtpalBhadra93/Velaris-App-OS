"""F-08 — self-healing processes.

HxEvolve mines deviations and proposes replay-proven changes, and then stops.
These routes close that loop, as far as a loop may honestly be closed: **one
approval, never none.**

Five things this surface is careful about:

* **Two switches, and it says which one is off.** An operator who has allowed
  healing on the box and a tenant who has not opted in are different situations,
  and "healing is off" without saying which would send somebody to the wrong
  screen.
* **Approving IS applying.** The roadmap asks for a one-approval deploy card, so
  there is one call. A separate "now really do it" step would either be a second
  approval or a rubber stamp, and neither is what was designed.
* **Rolling back is its own action.** The person undoing a healing change at
  3 a.m. is usually not the person who approved it at 2 p.m., and they need no
  new authority — the bytes were pre-signed at approval.
* **A refusal is a record.** An episode the ladder would not pass is still
  written. "The platform wanted to change this and was not allowed to" is
  exactly what somebody reviewing an autonomous system needs to read.
* **The boundary is published.** `/healing/status` returns the operator catalog
  and the forbidden surfaces it provably cannot reach, so an operator deciding
  whether to switch this on can see the argument rather than take it on trust.

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
from velaris_shared.db.models import HealingEpisodeModel, TenantModel
from velaris_shared.db.session import get_session
from velaris_shared.middleware.endpoint_rate_limit import rate_limit

from case_service.core.timestamps import iso_utc
from case_service.healing import catalog, service
from case_service.tenancy.identity import resolve_tenant

log = logging.getLogger(__name__)

router = APIRouter(prefix="/healing", tags=["healing"])

#: Opening an episode climbs the whole ladder — a replay lookup and, when a twin
#: is configured, a provisioned rehearsal. It is the expensive call here.
_open_rl = rate_limit(max_calls=10, window_seconds=60, name="healing-open")


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class OpenIn(_Strict):
    insight_id: uuid.UUID


class ApproveIn(_Strict):
    reason: str = Field(default="", max_length=2000)
    #: How long the change is watched before it may be called healed.
    watch_hours: int | None = Field(default=None, ge=1, le=720)


class RejectIn(_Strict):
    #: The only record of why the platform was told no, and a later scan will
    #: propose the same thing again.
    reason: str = Field(min_length=5, max_length=2000)


class RollbackIn(_Strict):
    reason: str = Field(min_length=5, max_length=2000)


async def _tenant(session: AsyncSession, user: AuthenticatedUser) -> uuid.UUID:
    resolved = await resolve_tenant(session, getattr(user, "tenant_id", None) or "default")
    if resolved is None:
        raise HTTPException(403, "VEL-HEALING-TENANT-UNRESOLVED-403")
    return resolved[0]


async def _load(session: AsyncSession, episode_id: uuid.UUID,
                tenant: uuid.UUID) -> HealingEpisodeModel:
    row = await session.get(HealingEpisodeModel, episode_id)
    # 404 rather than 403 for another tenant's episode: a 403 confirms the id.
    if row is None or row.tenant_id != tenant:
        raise HTTPException(404, "VEL-HEALING-404: no such episode")
    return row


def _episode_json(e: HealingEpisodeModel) -> dict[str, Any]:
    return {
        "id": str(e.id),
        "case_type_id": str(e.case_type_id),
        "insight_id": str(e.insight_id) if e.insight_id else None,
        "operator": e.operator,
        "diagnosis": e.diagnosis,
        "proposal": e.proposal,
        "ladder": e.ladder,
        "stopped_at": e.stopped_at,
        "stopped_reason": e.stopped_reason,
        "status": e.status,
        "approved_by": e.approved_by,
        "approved_at": iso_utc(e.approved_at),
        "decided_reason": e.decided_reason,
        "applied_at": iso_utc(e.applied_at),
        # The pre-signed rollback is REPORTED as present, never returned: it is
        # a config snapshot, and this endpoint is a status surface.
        "rollback_signed": bool(e.rollback_patch),
        "rollback_artifact": (f"{e.rollback_artifact_type}:{e.rollback_artifact_id}"
                              if e.rollback_artifact_id else None),
        "watch_until": iso_utc(e.watch_until),
        "watch_metric": e.watch_metric,
        "watch_bound_pct": (float(e.watch_bound_pct)
                            if e.watch_bound_pct is not None else None),
        "outcome": e.outcome,
        "outcome_at": iso_utc(e.outcome_at),
        "rolled_back_at": iso_utc(e.rolled_back_at),
        "rollback_reason": e.rollback_reason,
        "incident_case_id": str(e.incident_case_id) if e.incident_case_id else None,
        "replay_run_id": str(e.replay_run_id) if e.replay_run_id else None,
        "twin_run_id": str(e.twin_run_id) if e.twin_run_id else None,
        "sequence": int(e.sequence),
        "content_hash": e.content_hash,
        "created_at": iso_utc(e.created_at),
    }


@router.get("/status")
async def healing_status(
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(hxguard.guard("healing.episodes.read")),
) -> dict[str, Any]:
    """Both switches, the tenant's settings, and the boundary itself."""
    tenant_uuid = await _tenant(session, user)
    tenant = await session.get(TenantModel, tenant_uuid)
    allowed, why = await service.enabled_for(session, tenant_uuid)
    return {
        "enabled": allowed,
        "reason": why or None,
        "operator_switch": service.operator_enabled(),
        "tenant_settings": service.tenant_config(tenant),
        # Published so an operator deciding whether to switch this on can read
        # the argument rather than take it on trust.
        "boundary": catalog.describe(),
    }


@router.post("/episodes", status_code=201, dependencies=[Depends(_open_rl)])
async def open_episode(
    body: OpenIn,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(hxguard.guard("healing.episodes.approve")),
) -> dict[str, Any]:
    """Climb the ladder against one surfaced HxEvolve insight.

    A 201 either way: an episode the ladder refused is a well-formed answer to a
    well-formed question, and it is the record worth keeping.
    """
    tenant_uuid = await _tenant(session, user)
    try:
        episode = await service.open_episode(
            session, tenant_uuid=tenant_uuid, insight_id=body.insight_id,
            created_by=getattr(user, "user_id", None))
    except service.HealingError as exc:
        raise HTTPException(409, f"VEL-HEALING-409: {exc}") from exc
    await session.commit()
    return {"episode": _episode_json(episode)}


@router.get("/episodes")
async def list_episodes(
    status: str | None = Query(None, max_length=20),
    case_type_id: uuid.UUID | None = Query(None),
    limit: int = Query(100, ge=1, le=500),
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(hxguard.guard("healing.episodes.read")),
) -> dict[str, Any]:
    tenant_uuid = await _tenant(session, user)
    q = select(HealingEpisodeModel).where(HealingEpisodeModel.tenant_id == tenant_uuid)
    if status:
        q = q.where(HealingEpisodeModel.status == status)
    if case_type_id:
        q = q.where(HealingEpisodeModel.case_type_id == case_type_id)
    rows = list((await session.execute(
        q.order_by(HealingEpisodeModel.created_at.desc()).limit(limit))).scalars().all())
    return {"episodes": [_episode_json(r) for r in rows], "count": len(rows)}


@router.get("/episodes/{episode_id}")
async def get_episode(
    episode_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(hxguard.guard("healing.episodes.read")),
) -> dict[str, Any]:
    tenant_uuid = await _tenant(session, user)
    return {"episode": _episode_json(await _load(session, episode_id, tenant_uuid))}


@router.post("/episodes/{episode_id}/approve", status_code=201)
async def approve_episode(
    episode_id: uuid.UUID,
    body: ApproveIn,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(hxguard.guard("healing.episodes.approve")),
) -> dict[str, Any]:
    """The one approval — and it applies.

    Approving signs the rollback (from the artifact as it stands at this moment,
    so what is written back later is exactly what the approver was looking at)
    and then performs the change through HxBranch's own merge primitive. One
    human decision, one call.
    """
    tenant_uuid = await _tenant(session, user)
    episode = await _load(session, episode_id, tenant_uuid)
    actor = getattr(user, "user_id", None) or "unknown"
    try:
        await service.approve(session, episode=episode, actor=actor, reason=body.reason)
        await service.apply(session, episode=episode, watch_hours=body.watch_hours)
    except service.HealingError as exc:
        raise HTTPException(409, f"VEL-HEALING-APPLY-409: {exc}") from exc
    await session.commit()
    return {"episode": _episode_json(episode),
            "note": ("Applied and now being watched. It is not 'healed' until "
                     "the watch window closes without a regression.")}


@router.post("/episodes/{episode_id}/reject")
async def reject_episode(
    episode_id: uuid.UUID,
    body: RejectIn,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(hxguard.guard("healing.episodes.approve")),
) -> dict[str, Any]:
    tenant_uuid = await _tenant(session, user)
    episode = await _load(session, episode_id, tenant_uuid)
    try:
        await service.reject(session, episode=episode,
                             actor=getattr(user, "user_id", None) or "unknown",
                             reason=body.reason)
    except service.HealingError as exc:
        raise HTTPException(409, f"VEL-HEALING-409: {exc}") from exc
    await session.commit()
    return {"episode": _episode_json(episode)}


@router.post("/episodes/{episode_id}/rollback")
async def rollback_episode(
    episode_id: uuid.UUID,
    body: RollbackIn,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(hxguard.guard("healing.episodes.rollback")),
) -> dict[str, Any]:
    """Undo a healing change by hand, before its window closes.

    Its own action because the person doing this at 3 a.m. is usually not the
    person who approved it — and they need no new authority, because the bytes
    being written back were pre-signed at approval.
    """
    tenant_uuid = await _tenant(session, user)
    episode = await _load(session, episode_id, tenant_uuid)
    if episode.status not in ("watching", "healed", "applied"):
        raise HTTPException(
            409, f"VEL-HEALING-409: this episode is {episode.status!r}; there is "
                 f"nothing applied to undo")
    try:
        await service.rollback(session, episode=episode, reason=body.reason,
                               automatic=False)
    except service.HealingError as exc:
        raise HTTPException(409, f"VEL-HEALING-ROLLBACK-409: {exc}") from exc
    await session.commit()
    return {"episode": _episode_json(episode)}


@router.post("/watch/sweep")
async def sweep_watches(
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(hxguard.guard("healing.episodes.approve")),
) -> dict[str, Any]:
    """Close every watch window that is up. Also runs on a timer when enabled."""
    tenant_uuid = await _tenant(session, user)
    closed: list[dict[str, Any]] = []
    for episode in await service.due_watches(session):
        if episode.tenant_id != tenant_uuid:
            continue
        try:
            await service.close_watch(session, episode=episode)
        except service.HealingError as exc:
            log.warning("healing: could not close watch %s: %s", episode.id, exc)
            continue
        closed.append({"id": str(episode.id), "status": episode.status,
                       "detail": (episode.outcome or {}).get("detail")})
    await session.commit()
    return {"closed": closed, "count": len(closed)}


@router.get("/chain")
async def verify_chain(
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(hxguard.guard("healing.episodes.read")),
) -> dict[str, Any]:
    """Has the record of the platform changing itself been edited?"""
    return await service.verify_chain(session)
