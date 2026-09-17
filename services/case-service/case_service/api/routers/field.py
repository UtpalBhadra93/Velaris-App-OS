"""F-10 — offline-sovereign field operations, the hub's API.

The node is not an API client in the usual sense: it disappears for days and
comes back with a stream. So there are only three moments here — register it,
hand it a bundle, take the bundle's work back — plus the two that exist because
devices get lost: order a shred, and read what is queued for a person.

Three things this surface is careful about:

* **The bundle is the only thing that leaves.** `dispatch` returns it once; what
  the hub keeps is the record of which cases and which FIELD NAMES were on that
  device, so "what was on it" has an answer after the device does not.
* **A sync verifies before it merges, all or nothing.** A stream whose chain does
  not hold is not partially trustworthy: merging the part before the break would
  accept exactly the events an attacker chose to leave alone.
* **The conflict queue is a queue.** Nothing sensitive merges itself, and the
  only thing that clears a conflict is a person choosing.

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
    CaseInstanceModel,
    FieldDispatchModel,
    FieldNodeModel,
    FieldSyncEventModel,
)
from velaris_shared.db.session import get_session
from velaris_shared.middleware.endpoint_rate_limit import rate_limit

from case_service.core.timestamps import iso_utc
from case_service.field import service
from case_service.handoff import projection as proj
from case_service.tenancy.identity import resolve_tenant

log = logging.getLogger(__name__)

router = APIRouter(prefix="/field", tags=["field"])

#: Dispatch projects every charter case; sync verifies a whole chain. Both are
#: bounded work, but neither is cheap.
_heavy_rl = rate_limit(max_calls=20, window_seconds=60, name="field-heavy")


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class RegisterIn(_Strict):
    slug: str = Field(min_length=2, max_length=64, pattern=r"^[a-z0-9][a-z0-9._-]*$")
    display_name: str = Field(min_length=1, max_length=255)
    public_key: str = Field(min_length=40, max_length=200)
    #: What this node may EVER hold. An open charter is refused.
    charter: dict[str, Any]
    ceiling: str = Field(default=proj.CEILING_INTERNAL, max_length=20)
    snapshot_max_age_hours: int = Field(default=service.DEFAULT_STALE_HOURS,
                                        ge=1, le=24 * 30)


class DispatchIn(_Strict):
    offline_deny: list[str] | None = None


class SyncIn(_Strict):
    dispatch_id: uuid.UUID
    #: The node's chained stream, in order.
    events: list[dict[str, Any]] = Field(default_factory=list)
    #: Optional: the node signing its own chain head with the pinned key.
    signature: str | None = Field(default=None, max_length=200)


class ShredIn(_Strict):
    reason: str = Field(min_length=5, max_length=2000)


class ResolveIn(_Strict):
    resolution: str = Field(max_length=16)


async def _tenant(session: AsyncSession, user: AuthenticatedUser) -> uuid.UUID:
    resolved = await resolve_tenant(session, getattr(user, "tenant_id", None) or "default")
    if resolved is None:
        raise HTTPException(403, "VEL-FIELD-TENANT-UNRESOLVED-403")
    return resolved[0]


async def _node(session: AsyncSession, slug: str, tenant: uuid.UUID) -> FieldNodeModel:
    row = (await session.execute(
        select(FieldNodeModel).where(FieldNodeModel.tenant_id == tenant,
                                     FieldNodeModel.slug == slug))).scalars().first()
    if row is None:
        raise HTTPException(404, "VEL-FIELD-404: no such node")
    return row


def _node_json(n: FieldNodeModel, stale: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "id": str(n.id), "slug": n.slug, "display_name": n.display_name,
        # The fingerprint, never the key.
        "key_id": n.key_id, "status": n.status, "ceiling": n.ceiling,
        "charter": n.charter,
        "snapshot_max_age_hours": n.snapshot_max_age_hours,
        "subchain_head": n.subchain_head,
        "last_contact_at": iso_utc(n.last_contact_at),
        "shred_ordered_at": iso_utc(n.shred_ordered_at),
        "shredded_at": iso_utc(n.shredded_at),
        "staleness": stale,
        "created_at": iso_utc(n.created_at),
    }


@router.post("/nodes", status_code=201)
async def register_node(
    body: RegisterIn,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(hxguard.guard("field.nodes.write")),
) -> dict[str, Any]:
    """Enrol a node, with its key pinned and its charter fixed."""
    tenant_uuid = await _tenant(session, user)
    try:
        node = await service.register(
            session, tenant_uuid=tenant_uuid, slug=body.slug,
            display_name=body.display_name, public_key=body.public_key,
            charter=body.charter, ceiling=body.ceiling,
            snapshot_max_age_hours=body.snapshot_max_age_hours,
            created_by=getattr(user, "user_id", None))
    except (service.FieldError, proj.CeilingError) as exc:
        raise HTTPException(422, f"VEL-FIELD-REGISTER-422: {exc}") from exc
    except Exception as exc:                                     # noqa: BLE001
        raise HTTPException(422, f"VEL-FIELD-REGISTER-422: {exc}") from exc
    await session.commit()
    return {"node": _node_json(node)}


@router.get("/nodes")
async def list_nodes(
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(hxguard.guard("field.nodes.read")),
) -> dict[str, Any]:
    """Every node, and whether each is still entitled to be acting."""
    tenant_uuid = await _tenant(session, user)
    nodes = list((await session.execute(
        select(FieldNodeModel).where(FieldNodeModel.tenant_id == tenant_uuid)
        .order_by(FieldNodeModel.slug))).scalars().all())
    out = []
    for node in nodes:
        latest = (await session.execute(
            select(FieldDispatchModel)
            .where(FieldDispatchModel.node_id == node.id,
                   FieldDispatchModel.status == "issued")
            .order_by(FieldDispatchModel.issued_at.desc()).limit(1))).scalars().first()
        out.append(_node_json(node, service.staleness(node, latest)))
    return {"nodes": out, "count": len(out)}


@router.post("/nodes/{slug}/dispatch", status_code=201,
             dependencies=[Depends(_heavy_rl)])
async def dispatch_node(
    slug: str,
    body: DispatchIn,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(hxguard.guard("field.nodes.dispatch")),
) -> dict[str, Any]:
    """The bundle the node takes away. Returned once; the record is kept here."""
    tenant_uuid = await _tenant(session, user)
    node = await _node(session, slug, tenant_uuid)
    try:
        row, bundle = await service.dispatch(
            session, node=node, tenant_token=str(tenant_uuid),
            offline_deny=body.offline_deny,
            created_by=getattr(user, "user_id", None))
    except service.FieldError as exc:
        raise HTTPException(409, f"VEL-FIELD-DISPATCH-409: {exc}") from exc
    await session.commit()
    return {"dispatch_id": str(row.id), "cases": len(row.case_ids or []),
            "bundle": bundle}


@router.post("/nodes/{slug}/sync", dependencies=[Depends(_heavy_rl)])
async def sync_node(
    slug: str,
    body: SyncIn,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(hxguard.guard("field.nodes.dispatch")),
) -> dict[str, Any]:
    """Take the node's stream back. Verified in full before anything merges."""
    tenant_uuid = await _tenant(session, user)
    node = await _node(session, slug, tenant_uuid)
    row = await session.get(FieldDispatchModel, body.dispatch_id)
    if row is None or row.node_id != node.id:
        raise HTTPException(404, "VEL-FIELD-404: no such dispatch for this node")
    try:
        out = await service.sync(session, node=node, dispatch_row=row,
                                 events=body.events, signature=body.signature)
    except service.FieldError as exc:
        await session.commit()      # last contact is still a fact
        raise HTTPException(409, f"VEL-FIELD-SYNC-409: {exc}") from exc
    await session.commit()
    return out


@router.post("/nodes/{slug}/shred")
async def shred_node(
    slug: str,
    body: ShredIn,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(hxguard.guard("field.nodes.write")),
) -> dict[str, Any]:
    """Order the node wiped at next contact.

    It does not wait for the node. The staleness lockout is already running, and
    the two together are what makes a device that never comes back survivable.
    """
    tenant_uuid = await _tenant(session, user)
    node = await _node(session, slug, tenant_uuid)
    await service.order_shred(session, node=node, reason=body.reason)
    await session.commit()
    return {"node": _node_json(node),
            "note": ("The order executes at next contact. Until then the node is "
                     "locked out by its own snapshot age.")}


@router.get("/conflicts")
async def list_conflicts(
    node: str | None = Query(None, max_length=64),
    limit: int = Query(100, ge=1, le=500),
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(hxguard.guard("field.conflicts.resolve")),
) -> dict[str, Any]:
    """What a person has to decide. Nothing here decided itself."""
    tenant_uuid = await _tenant(session, user)
    q = select(FieldSyncEventModel).where(
        FieldSyncEventModel.tenant_id == tenant_uuid,
        FieldSyncEventModel.merge_state == "conflict")
    if node:
        q = q.where(FieldSyncEventModel.node_id == (await _node(session, node, tenant_uuid)).id)
    rows = list((await session.execute(
        q.order_by(FieldSyncEventModel.received_at).limit(limit))).scalars().all())

    # The hub's side of the disagreement, read now rather than described.
    # Without it the queue shows only the incoming value, which is exactly the
    # thing the screen exists not to do: asking somebody to approve a change
    # they cannot see the alternative to. One case is loaded per distinct
    # case_id, not per row, because a node in trouble queues many events
    # against the same case.
    case_ids = {r.case_id for r in rows if r.case_id}
    cases: dict[Any, CaseInstanceModel] = {}
    for cid in case_ids:
        case = await session.get(CaseInstanceModel, cid)
        if case is not None:
            cases[cid] = case

    def hub_value(row: FieldSyncEventModel) -> tuple[Any, bool]:
        """`(value, known)`. `known` is False when the hub has no such field —
        which is not the same as the hub holding an empty value, and a screen
        that showed them the same way would be lying about one of them."""
        case = cases.get(row.case_id)
        data = case.data if case is not None and isinstance(case.data, dict) else None
        if data is None or not row.target or row.target not in data:
            return None, False
        return data[row.target], True

    conflicts = []
    for r in rows:
        value, known = hub_value(r)
        conflicts.append({
            "id": str(r.id), "node_id": str(r.node_id),
            "case_id": str(r.case_id) if r.case_id else None,
            "seq": r.seq, "kind": r.kind, "target": r.target,
            "field_value": (r.payload or {}).get("value"),
            "hub_value": value,
            "hub_value_known": known,
            "reason": r.conflict_reason,
            "node_time": iso_utc(r.node_time),
            "received_at": iso_utc(r.received_at),
        })
    return {"conflicts": conflicts, "count": len(conflicts)}


@router.post("/conflicts/{event_id}/resolve")
async def resolve_conflict(
    event_id: uuid.UUID,
    body: ResolveIn,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(hxguard.guard("field.conflicts.resolve")),
) -> dict[str, Any]:
    """`keep_field` or `keep_hub`. The only thing that clears a conflict."""
    tenant_uuid = await _tenant(session, user)
    row = await session.get(FieldSyncEventModel, event_id)
    if row is None or row.tenant_id != tenant_uuid:
        raise HTTPException(404, "VEL-FIELD-404: no such conflict")
    try:
        await service.resolve(session, event=row, resolution=body.resolution,
                              actor=getattr(user, "user_id", None) or "unknown")
    except service.FieldError as exc:
        raise HTTPException(409, f"VEL-FIELD-RESOLVE-409: {exc}") from exc
    await session.commit()
    return {"id": str(row.id), "merge_state": row.merge_state,
            "resolution": row.resolution, "resolved_by": row.resolved_by}
