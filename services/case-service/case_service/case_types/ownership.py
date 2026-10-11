"""Who owns a case type, and what a tenant sees (Oct item 8i).

Agreed with the user 2026-09-29 / 09-30:

* A **default** is a case type with no tenant (`tenant_id` NULL). It stays ONE
  platform row that every tenant reads — it is never copied into each tenant.
* A **tenant-owned** case type belongs to one tenant and nobody else sees it.
* **Copy-on-write.** A tenant that edits a default gets its own fork, and the
  fork wins for that tenant from then on: the default it forked is no longer
  listed for it (only that row — a newer default version is still offered). The default itself only changes
  when the platform operator changes it, and a forked tenant is shown that an
  update is available — it is never overwritten.
* New case types get the caller's tenant. Only the platform operator may write
  a default, and keeps exactly the behaviour every earlier release had
  (`tenant_for_write`'s rule): a single-tenant install changes nothing.

Not copied into a fork, recorded rather than guessed: escalation trees,
notification overrides, email templates, webhook subscriptions and intake
events keep pointing at the default. A fork is the case type's DESIGN — its
row, stages, steps, variables, statuses and action configuration.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import inspect, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from velaris_shared.db.repository import case_types_visible_to
from velaris_shared.db.models import (
    CaseActionConfigModel,
    CaseStatusModel,
    CaseTypeModel,
    CaseTypeStageModel,
    CaseTypeStepModel,
    CaseTypeVariableModel,
)
from velaris_shared.tenancy.identity import (
    caller_tenant_uuid,
    is_platform_operator,
    is_superadmin,
    resolve_tenant,
)


class OwnershipError(PermissionError):
    """A case-type write the caller's tenant does not permit."""


async def caller_tenant(session: AsyncSession, user: Any) -> uuid.UUID | None:
    """The caller's tenant id. No claim is the default tenant, as everywhere."""
    return await caller_tenant_uuid(session, user)


async def owner_for_create(
    session: AsyncSession, user: Any, requested: uuid.UUID | None,
) -> uuid.UUID | None:
    """The tenant a NEW case type is recorded under.

    The platform operator keeps the value as sent, NULL (a default) included —
    exactly what `POST /case-types` did before 8i. Everybody else creates in
    their own tenant; naming another tenant is refused.
    """
    if await is_platform_operator(session, user):
        return requested
    mine = await caller_tenant(session, user)
    if mine is None:
        raise OwnershipError("Your tenant could not be resolved.")
    if requested is not None and requested != mine:
        raise OwnershipError("A case type may only be created in your own tenant.")
    return mine


async def may_write_in_place(session: AsyncSession, user: Any, row: CaseTypeModel) -> bool:
    """May this caller change THIS row, rather than a fork of it?

    A default: the platform operator only. A tenant-owned row: its own tenant
    (a superadmin may reach any, as before).
    """
    if row.tenant_id is None:
        return await is_platform_operator(session, user)
    if is_superadmin(user):
        return True
    return row.tenant_id == await caller_tenant(session, user)


async def may_read(session: AsyncSession, user: Any, row: CaseTypeModel) -> bool:
    """`GET /case-types/{id}`'s rule: a default is everybody's; a tenant-owned
    row is its tenant's, and a caller with no tenant claim (a platform-level
    operator) or a superadmin reads any."""
    if row.tenant_id is None or is_superadmin(user) or not getattr(user, "tenant_id", None):
        return True
    return row.tenant_id == await caller_tenant(session, user)


def visible_to(tenant_id: uuid.UUID):
    """SQL: the case types `tenant_id` can use — its own, plus every default it
    has not forked. One implementation, in the shared repository."""
    return case_types_visible_to(tenant_id)


async def find_fork(
    session: AsyncSession, default: CaseTypeModel, tenant_id: uuid.UUID,
) -> CaseTypeModel | None:
    """The tenant's fork of exactly this default, if it has one."""
    return (await session.execute(
        select(CaseTypeModel).where(
            CaseTypeModel.tenant_id == tenant_id,
            CaseTypeModel.forked_from_id == default.id,
        ).order_by(CaseTypeModel.created_at.desc()).limit(1)
    )).scalars().first()


async def fork_for_use(
    session: AsyncSession, case_type: CaseTypeModel, tenant_id: uuid.UUID | None,
) -> CaseTypeModel:
    """The row a NEW case of `case_type` is created on, for `tenant_id` (8k).

    A default the tenant has forked means its fork — the fork is what that
    tenant sees and edits, so a case on the default would run a design the
    tenant replaced. Anything else (a tenant-owned row, a default nobody
    forked, no tenant) is `case_type` itself, exactly as before.
    """
    if case_type.tenant_id is not None or tenant_id is None:
        return case_type
    own = (await session.execute(
        select(CaseTypeModel).where(
            CaseTypeModel.tenant_id == tenant_id,
            CaseTypeModel.forked_from_id == case_type.id,
            (CaseTypeModel.is_deleted == False) | (CaseTypeModel.is_deleted == None),  # noqa: E712
        ).order_by(CaseTypeModel.created_at.desc()).limit(1)
    )).scalars().first()
    return own if own is not None else case_type


def _copy(model, row, **overrides):
    """A new instance of `model` carrying every column of `row` but its id."""
    values = {
        c.key: getattr(row, c.key)
        for c in inspect(model).column_attrs
        if c.key != "id"
    }
    values.update(overrides)
    return model(**values)


async def fork(
    session: AsyncSession, default: CaseTypeModel, tenant_id: uuid.UUID,
) -> CaseTypeModel:
    """Copy a default into `tenant_id` — its design, not its cases.

    Idempotent: a tenant that already has its own row of this name + version
    gets that row back, so two edits never make two forks.
    """
    if default.tenant_id is not None:
        raise ValueError("Only a default (tenant-less) case type is forked.")
    existing = await find_fork(session, default, tenant_id)
    if existing is not None:
        return existing
    clash = (await session.execute(
        select(CaseTypeModel.id).where(
            CaseTypeModel.tenant_id == tenant_id,
            CaseTypeModel.name == default.name,
            CaseTypeModel.version == default.version,
        ).limit(1)
    )).first()
    if clash is not None:
        # The tenant already made its OWN type of this name and version; an
        # edit of the default must not land on it, nor collide with it.
        raise OwnershipError(
            f"Your tenant already has its own '{default.name}' v{default.version}; "
            "edit that one, or give your copy of the default a new version.")

    now = datetime.now(timezone.utc)
    new = _copy(
        CaseTypeModel, default,
        id=uuid.uuid4(), tenant_id=tenant_id,
        tenant_key=str(tenant_id),
        forked_from_id=default.id, forked_at=now,
        created_at=now, updated_at=now,
        is_deleted=False, deleted_at=None, deleted_by=None,
        definition_json=dict(default.definition_json or {}),
        tags=list(default.tags or []),
    )
    session.add(new)
    await session.flush()

    stage_map: dict[uuid.UUID, uuid.UUID] = {}
    for st in (await session.execute(
        select(CaseTypeStageModel).where(CaseTypeStageModel.case_type_id == default.id)
    )).scalars().all():
        nid = uuid.uuid4()
        stage_map[st.id] = nid
        session.add(_copy(CaseTypeStageModel, st, id=nid, case_type_id=new.id))
    await session.flush()

    for sp in (await session.execute(
        select(CaseTypeStepModel).where(CaseTypeStepModel.case_type_id == default.id)
    )).scalars().all():
        if sp.stage_id not in stage_map:
            continue
        session.add(_copy(CaseTypeStepModel, sp, id=uuid.uuid4(),
                          case_type_id=new.id, stage_id=stage_map[sp.stage_id]))

    for v in (await session.execute(
        select(CaseTypeVariableModel).where(CaseTypeVariableModel.case_type_id == default.id)
    )).scalars().all():
        session.add(_copy(CaseTypeVariableModel, v, id=uuid.uuid4(), case_type_id=new.id))

    # Statuses and action configuration bound to the default: the platform's
    # rows and any this tenant had already layered on top. The tenant's own
    # row wins a name both define, as it did when resolving against the default.
    from case_service.case_actions.statuses import scope_key
    key = scope_key(tenant_id, new.id)
    for model, field in ((CaseStatusModel, "name"), (CaseActionConfigModel, "action")):
        rows = (await session.execute(
            select(model).where(
                model.case_type_id == default.id,
                or_(model.tenant_id.is_(None), model.tenant_id == tenant_id),
            )
        )).scalars().all()
        chosen: dict[str, Any] = {}
        for r in sorted(rows, key=lambda r: r.tenant_id is not None):
            chosen[getattr(r, field)] = r
        for r in chosen.values():
            session.add(_copy(model, r, id=uuid.uuid4(), tenant_id=tenant_id,
                              case_type_id=new.id, scope_key=key))

    await session.flush()
    return new


async def target_for_write(
    session: AsyncSession, user: Any, row: CaseTypeModel,
) -> CaseTypeModel:
    """The row an edit by this caller lands on: the row itself, or the caller's
    fork of it when it is a default the caller may not change for everyone.

    Raises `OwnershipError` for another tenant's row.
    """
    if await may_write_in_place(session, user, row):
        return row
    if row.tenant_id is not None:
        raise OwnershipError("You can only edit case types owned by your tenant.")
    mine = await caller_tenant(session, user)
    if mine is None:
        raise OwnershipError("Your tenant could not be resolved.")
    return await fork(session, row, mine)


async def updates_available(
    session: AsyncSession, rows: list[CaseTypeModel],
) -> set[uuid.UUID]:
    """Ids of forks whose default has changed since the fork was taken."""
    forks = [r for r in rows if r.forked_from_id is not None and r.forked_at is not None]
    if not forks:
        return set()
    parents = {
        p.id: p for p in (await session.execute(
            select(CaseTypeModel).where(
                CaseTypeModel.id.in_({r.forked_from_id for r in forks}))
        )).scalars().all()
    }
    out: set[uuid.UUID] = set()
    for r in forks:
        p = parents.get(r.forked_from_id)
        if p is None or p.updated_at is None:
            continue
        if _aware(p.updated_at) > _aware(r.forked_at):
            out.add(r.id)
    return out


def _aware(ts: datetime) -> datetime:
    return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)


# ── Backfill of legacy defaults (8i point 2) ─────────────────────────────────
#
# Every case type made before 8i is a default (17 of 18 on the dev box). Which
# of them really belong to one tenant is a DATA decision per install, so an
# update never makes it: the operator previews, then applies the ids it chose,
# and each is re-checked at apply time. "Never moved blind."

async def backfill_proposals(session: AsyncSession) -> list[dict]:
    """For each default: who uses it, and what moving it would mean.

    A case with no tenant counts as the default tenant's — the rule a caller
    with no tenant claim gets everywhere else.
    """
    from sqlalchemy import func
    from velaris_shared.db.models import CaseInstanceModel, TenantModel

    default = await resolve_tenant(session, "default")
    default_id = default[0] if default else None
    slugs = {t.id: t.slug for t in (await session.execute(select(TenantModel))).scalars().all()}

    rows = (await session.execute(
        select(CaseTypeModel).where(
            CaseTypeModel.tenant_id.is_(None),
            or_(CaseTypeModel.is_deleted.is_(None), CaseTypeModel.is_deleted.is_(False)),
        ).order_by(CaseTypeModel.name, CaseTypeModel.version)
    )).scalars().all()
    usage = (await session.execute(
        select(CaseInstanceModel.case_type_id, CaseInstanceModel.tenant_id, func.count())
        .where(CaseInstanceModel.case_type_id.in_([r.id for r in rows]))
        .group_by(CaseInstanceModel.case_type_id, CaseInstanceModel.tenant_id)
    )).all() if rows else []

    by_type: dict[uuid.UUID, dict[uuid.UUID | None, int]] = {}
    for ct_id, tid, n in usage:
        bucket = by_type.setdefault(ct_id, {})
        key = tid if tid is not None else default_id
        bucket[key] = bucket.get(key, 0) + n

    out = []
    for r in rows:
        users = by_type.get(r.id, {})
        if len(users) == 1 and next(iter(users)) is not None:
            tid = next(iter(users))
            proposal, reason = "move", f"Only tenant '{slugs.get(tid, tid)}' has cases of it."
            target = tid
        elif not users:
            proposal, reason, target = "keep", "No cases yet — stays a default (template).", None
        else:
            proposal, reason, target = "keep", "Used by several tenants — stays a default.", None
        out.append({
            "id": str(r.id), "name": r.name, "version": r.version,
            "cases_by_tenant": {slugs.get(k, str(k)): v for k, v in users.items()},
            "proposal": proposal, "reason": reason,
            "target_tenant_id": str(target) if target else None,
            "target_tenant": slugs.get(target) if target else None,
        })
    return out


async def apply_backfill(session: AsyncSession, ids: list[uuid.UUID]) -> dict:
    """Move the chosen defaults to the tenant that uses them, each re-checked."""
    current = {p["id"]: p for p in await backfill_proposals(session)}
    moved, skipped = [], []
    for cid in ids:
        p = current.get(str(cid))
        if p is None:
            skipped.append({"id": str(cid), "reason": "Not a default case type (any more)."})
            continue
        if p["proposal"] != "move":
            skipped.append({"id": str(cid), "reason": p["reason"]})
            continue
        target = uuid.UUID(p["target_tenant_id"])
        clash = (await session.execute(
            select(CaseTypeModel.id).where(
                CaseTypeModel.tenant_id == target,
                CaseTypeModel.name == p["name"], CaseTypeModel.version == p["version"],
            ).limit(1)
        )).first()
        if clash is not None:
            skipped.append({"id": str(cid), "reason":
                            f"Tenant '{p['target_tenant']}' already has its own "
                            f"'{p['name']}' v{p['version']}."})
            continue
        row = await session.get(CaseTypeModel, cid)
        row.tenant_id = target          # the `set` listener moves tenant_key
        moved.append({"id": str(cid), "name": p["name"], "version": p["version"],
                      "tenant": p["target_tenant"]})
    await session.flush()
    return {"moved": moved, "skipped": skipped}
