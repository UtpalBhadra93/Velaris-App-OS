"""F-11 — intent-based process execution.

Somebody states an outcome; the platform composes a path to it from verified
fragments, shows it as a readable step list, and runs exactly the thing that was
agreed to.

Three things this surface is careful about:

* **The model never emits a process.** It emits a goal spec, and everything it
  says outside the fragment library's own vocabulary is dropped before the
  planner sees it. `dropped` is returned so an operator can see what was
  discarded rather than wondering.
* **The confirmation binds a hash.** The client passes back the hash it
  displayed; a plan that has moved since is refused. And the definition is
  re-hashed again at execution, because checking only at confirm time leaves a
  window in which the stored definition could be edited and run under somebody
  else's approval.
* **Publishing and verifying are separate.** A fragment arrives as a draft and
  only a deliberate act makes it composable, because a draft is something
  somebody is still writing.

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
from velaris_shared.db.models import IntentPlanModel, ProcessFragmentModel
from velaris_shared.db.session import get_session
from velaris_shared.middleware.endpoint_rate_limit import rate_limit

from case_service.core.timestamps import iso_utc
from case_service.intent import fragments as frag
from case_service.intent import planner, service
from case_service.tenancy.identity import resolve_tenant

log = logging.getLogger(__name__)

router = APIRouter(prefix="/intent", tags=["intent"])

#: Planning asks a model and searches the library. It is the expensive call.
_plan_rl = rate_limit(max_calls=20, window_seconds=60, name="intent-plan")


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class FragmentIn(_Strict):
    slug: str = Field(min_length=2, max_length=64, pattern=r"^[a-z0-9][a-z0-9._-]*$")
    display_name: str = Field(min_length=1, max_length=255)
    description: str = Field(default="", max_length=2000)
    requires: list[str] = Field(default_factory=list)
    provides: list[str] = Field(default_factory=list)
    data_classes: list[str] = Field(default_factory=list)
    capabilities: list[str] = Field(default_factory=list)
    stages: list[dict[str, Any]] = Field(default_factory=list)


class PlanIn(_Strict):
    intent: str = Field(min_length=3, max_length=4000)
    #: Supply this to skip the model entirely. The planner cannot tell the
    #: difference, which is the strongest statement about how little the model
    #: is trusted.
    goal_spec: dict[str, Any] | None = None
    case_id: uuid.UUID | None = None
    require_twin: bool = False


class ConfirmIn(_Strict):
    #: The hash the client displayed. A plan that has moved since is refused.
    plan_sha256: str = Field(min_length=64, max_length=64)


async def _tenant(session: AsyncSession, user: AuthenticatedUser) -> uuid.UUID:
    resolved = await resolve_tenant(session, getattr(user, "tenant_id", None) or "default")
    if resolved is None:
        raise HTTPException(403, "VEL-INTENT-TENANT-UNRESOLVED-403")
    return resolved[0]


def _frag_json(f: ProcessFragmentModel) -> dict[str, Any]:
    return {
        "slug": f.slug, "display_name": f.display_name, "description": f.description,
        "requires": f.requires, "provides": f.provides,
        "data_classes": f.data_classes, "capabilities": f.capabilities,
        "step_count": f.step_count, "status": f.status,
        "content_sha256": f.content_sha256,
        # Signed says the bytes match a key, not that the key belongs to
        # anybody. The key id is what an operator pins.
        "signed": bool(f.signing_key_id), "signing_key_id": f.signing_key_id,
        "conformance": f.conformance,
        "verified_by": f.verified_by, "verified_at": iso_utc(f.verified_at),
    }


def _plan_json(p: IntentPlanModel, steps: list[dict[str, Any]] | None = None
               ) -> dict[str, Any]:
    return {
        "id": str(p.id), "intent": p.intent, "goal_spec": p.goal_spec,
        "spec_source": p.spec_source, "status": p.status,
        "fragments": p.fragment_slugs, "steps": steps,
        "capabilities": p.capabilities, "data_classes": p.data_classes,
        "step_count": p.step_count, "plan_sha256": p.plan_sha256,
        "ladder": p.ladder, "stopped_at": p.stopped_at,
        "stopped_reason": p.stopped_reason,
        "confirmed_by": p.confirmed_by, "confirmed_at": iso_utc(p.confirmed_at),
        "confirmed_sha256": p.confirmed_sha256,
        "executed_at": iso_utc(p.executed_at),
        "created_at": iso_utc(p.created_at),
    }


# ── the library ─────────────────────────────────────────────────────────────


@router.get("/fragments")
async def list_fragments(
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(hxguard.guard("intent.fragments.read")),
) -> dict[str, Any]:
    """The library, and the vocabulary it can name."""
    tenant_uuid = await _tenant(session, user)
    rows = await service.library(session, tenant_uuid)
    return {
        "fragments": [_frag_json(f) for f in rows],
        "count": len(rows),
        # The closed set a goal spec may draw on. Published so an operator can
        # see why an intent could not be planned.
        "vocabulary": service.vocabulary([f for f in rows if f.status == "verified"]),
        "capabilities": sorted(frag.CAPABILITIES),
    }


@router.post("/fragments", status_code=201)
async def publish_fragment(
    body: FragmentIn,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(hxguard.guard("intent.fragments.write")),
) -> dict[str, Any]:
    """Add a fragment as a DRAFT. It cannot be composed until it is verified."""
    tenant_uuid = await _tenant(session, user)
    try:
        row = await service.publish_fragment(
            session, tenant_uuid=tenant_uuid, fragment=body.model_dump(),
            created_by=getattr(user, "user_id", None))
    except service.IntentError as exc:
        raise HTTPException(422, f"VEL-INTENT-FRAGMENT-422: {exc}") from exc
    await session.commit()
    return {"fragment": _frag_json(row)}


@router.post("/fragments/{slug}/verify")
async def verify_fragment(
    slug: str,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(hxguard.guard("intent.fragments.write")),
) -> dict[str, Any]:
    """Run the conformance gate.

    A fragment that declares LESS than its stages do is refused — that is the
    failure the whole gate exists for: it would be composed into plans approved
    on the strength of a smaller capability union than the one that runs.
    """
    tenant_uuid = await _tenant(session, user)
    row = (await session.execute(
        select(ProcessFragmentModel).where(
            ProcessFragmentModel.tenant_id == tenant_uuid,
            ProcessFragmentModel.slug == slug))).scalars().first()
    if row is None:
        raise HTTPException(404, "VEL-INTENT-404: no such fragment")
    try:
        await service.verify_fragment(session, row=row,
                                      actor=getattr(user, "user_id", None) or "unknown")
    except service.IntentError as exc:
        await session.commit()      # the conformance result is a record
        raise HTTPException(422, f"VEL-INTENT-CONFORMANCE-422: {exc}") from exc
    await session.commit()
    return {"fragment": _frag_json(row)}


# ── planning ────────────────────────────────────────────────────────────────


@router.post("/plans", status_code=201, dependencies=[Depends(_plan_rl)])
async def create_plan(
    body: PlanIn,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(hxguard.guard("intent.plans.create")),
) -> dict[str, Any]:
    """Turn an intent into a plan. A refusal is a 201 with a reason.

    "The platform was asked for this and could not compose it safely" is what a
    gap in the fragment library looks like from the outside, and it is worth
    keeping.
    """
    tenant_uuid = await _tenant(session, user)
    row = await service.plan(
        session, tenant_uuid=tenant_uuid, intent=body.intent,
        goal_spec=body.goal_spec, case_id=body.case_id,
        created_by=getattr(user, "user_id", None),
        require_twin=body.require_twin)
    await session.commit()
    steps = None
    if row.fragment_slugs:
        by_slug = {f.slug: f for f in await service.library(session, tenant_uuid)}
        steps = planner.readable({"fragment_slugs": row.fragment_slugs}, by_slug)
    return {"plan": _plan_json(row, steps)}


@router.get("/plans")
async def list_plans(
    status: str | None = Query(None, max_length=16),
    limit: int = Query(100, ge=1, le=500),
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(hxguard.guard("intent.plans.read")),
) -> dict[str, Any]:
    tenant_uuid = await _tenant(session, user)
    q = select(IntentPlanModel).where(IntentPlanModel.tenant_id == tenant_uuid)
    if status:
        q = q.where(IntentPlanModel.status == status)
    rows = list((await session.execute(
        q.order_by(IntentPlanModel.created_at.desc()).limit(limit))).scalars().all())
    return {"plans": [_plan_json(r) for r in rows], "count": len(rows)}


@router.get("/plans/{plan_id}")
async def get_plan(
    plan_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(hxguard.guard("intent.plans.read")),
) -> dict[str, Any]:
    tenant_uuid = await _tenant(session, user)
    row = await session.get(IntentPlanModel, plan_id)
    if row is None or row.tenant_id != tenant_uuid:
        raise HTTPException(404, "VEL-INTENT-404: no such plan")
    by_slug = {f.slug: f for f in await service.library(session, tenant_uuid)}
    steps = planner.readable({"fragment_slugs": row.fragment_slugs or []}, by_slug)
    return {"plan": _plan_json(row, steps), "definition": row.definition}


@router.post("/plans/{plan_id}/confirm")
async def confirm_plan(
    plan_id: uuid.UUID,
    body: ConfirmIn,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(hxguard.guard("intent.plans.confirm")),
) -> dict[str, Any]:
    """Agree to THIS plan. Pass back the hash you were shown."""
    tenant_uuid = await _tenant(session, user)
    row = await session.get(IntentPlanModel, plan_id)
    if row is None or row.tenant_id != tenant_uuid:
        raise HTTPException(404, "VEL-INTENT-404: no such plan")
    try:
        await service.confirm(session, row=row,
                              actor=getattr(user, "user_id", None) or "unknown",
                              plan_sha256=body.plan_sha256)
    except service.IntentError as exc:
        raise HTTPException(409, f"VEL-INTENT-CONFIRM-409: {exc}") from exc
    await session.commit()
    return {"plan": _plan_json(row)}


@router.post("/plans/{plan_id}/execute")
async def execute_plan(
    plan_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(hxguard.guard("intent.plans.confirm")),
) -> dict[str, Any]:
    """Run the confirmed plan, and refuse anything that is not it."""
    tenant_uuid = await _tenant(session, user)
    row = await session.get(IntentPlanModel, plan_id)
    if row is None or row.tenant_id != tenant_uuid:
        raise HTTPException(404, "VEL-INTENT-404: no such plan")
    try:
        out = await service.execute(session, row=row,
                                    actor=getattr(user, "user_id", None) or "unknown")
    except service.IntentError as exc:
        await session.commit()      # an abandoned plan is a record
        raise HTTPException(409, f"VEL-INTENT-EXECUTE-409: {exc}") from exc
    await session.commit()
    return out
