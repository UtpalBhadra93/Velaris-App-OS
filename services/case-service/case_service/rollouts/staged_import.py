"""A staged rollout arriving through HxDeploy (Oct item 18a).

The source's `rollout_request` rides beside the sealed bundle. When it asks for
anything but `immediate`, this side:

1. **plans** before anything is written — refusing a bundle that would change
   something in place for every customer (a staged rollout can only split NEW
   cases between two case type versions; a shared form or rule edited in place
   reaches everyone at once, which is the opposite of what was asked), and
   refusing a case type that already has an open rollout here (the lock);
2. has `apply_bundle` install each case type **beside** the version here;
3. **opens** one rollout per case type, `requested`, awaiting an operator on
   THIS side. Turning it on is theirs, never the package's.

No `rollout_request`, or `immediate`, or an older sender that knows nothing of
it: `plan()` returns None and the import is exactly what it always was.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from velaris_shared.db.models import CaseTypeModel

from case_service.rollouts import service

#: What a staged bundle may carry: case types and what belongs to them.
STAGED_SECTIONS = frozenset({"case_types", "stages", "steps", "escalation_trees"})


class StagedImportRefused(ValueError):
    def __init__(self, message: str, **extra: Any):
        super().__init__(message)
        self.extra = extra

    def detail(self) -> dict[str, Any]:
        return {"message": str(self), **self.extra}


@dataclass
class Plan:
    tenant_uuid: uuid.UUID
    strategy: str
    percent: int
    request: dict[str, Any]
    #: name → baseline case type id, for names that already exist here
    baselines: dict[str, uuid.UUID] = field(default_factory=dict)

    @property
    def names(self) -> frozenset[str]:
        return frozenset(self.baselines)


async def plan(session: AsyncSession, bundle: dict[str, Any], tenant: str) -> Plan | None:
    request = bundle.get("rollout_request")
    if not isinstance(request, dict):
        return None
    strategy = str(request.get("strategy") or "immediate")
    if strategy == "immediate":
        return None
    if strategy not in service.STRATEGIES:
        raise StagedImportRefused(
            f"rollout strategy {strategy!r} is not one this environment knows "
            f"({', '.join(service.STRATEGIES)})")
    try:
        percent = service._check_percent(strategy, request.get("percent") or 0)
    except (service.RolloutError, TypeError, ValueError) as exc:
        raise StagedImportRefused(f"the rollout request's percent is not usable: {exc}") from exc
    if not await service._table_ready(session):
        raise StagedImportRefused(
            "a staged rollout was requested, but this environment has not run "
            "migration 210 yet; run the update, or promote it as immediate")
    tenant_uuid = await service._tenant_uuid(session, tenant)
    if tenant_uuid is None:
        raise StagedImportRefused(f"{tenant!r} is not a tenant here")

    shared = {k: len(v) for k, v in bundle.items()
              if isinstance(v, list) and v and k not in STAGED_SECTIONS}
    if shared:
        raise StagedImportRefused(
            "a staged rollout can only carry case types (with their stages, steps "
            "and escalation trees). This bundle also carries "
            + ", ".join(f"{n} {k}" for k, n in sorted(shared.items()))
            + ", which would change in place for every customer at once. Promote "
            "those first as an immediate package, then stage the case types.",
            shared_sections=shared)
    if not bundle.get("case_types"):
        raise StagedImportRefused("a staged rollout needs at least one case type")

    out = Plan(tenant_uuid=tenant_uuid, strategy=strategy, percent=percent,
               request={"strategy": strategy, "percent": percent,
                        "source_env": bundle.get("source_env"),
                        "deployment_run_id": bundle.get("deployment_run_id"),
                        "package_checksum": bundle.get("package_checksum")})
    from case_service.hxdeploy.packager import _current_case_type
    for ct in bundle["case_types"]:
        name = ct.get("name")
        same = list((await session.execute(
            select(CaseTypeModel).where(CaseTypeModel.name == name,
                                        CaseTypeModel.is_deleted.is_(False))
            .order_by(CaseTypeModel.created_at.desc()))).scalars().all())
        if not same:
            continue          # new here: nothing to roll out against; installs as new
        locked = await service.open_for(session, tenant_uuid, "case_type", name)
        if locked is not None:
            raise StagedImportRefused(str(service.RolloutLocked(locked)),
                                      open_rollout=service.to_json(locked),
                                      options=["promote", "rollback", "supersede"])
        current = await _current_case_type(session, same, tenant)
        out.baselines[name] = current.id
    return out


async def open_rollouts(session: AsyncSession, p: Plan, result: dict[str, Any], *,
                        actor: str) -> list[dict[str, Any]]:
    installed = result.get("installed_alongside") or {}
    missing = sorted(set(p.baselines) - set(installed))
    if missing:
        raise StagedImportRefused(
            "these case types could not be installed beside the current version, "
            "so no rollout was opened and nothing was imported: " + ", ".join(missing),
            errors=result.get("errors", []))
    out = []
    for name, baseline in sorted(p.baselines.items()):
        try:
            row = await service.start(
                session, tenant_uuid=p.tenant_uuid, kind="case_type", baseline_id=baseline,
                candidate_id=uuid.UUID(installed[name]), strategy=p.strategy,
                percent=p.percent, actor=actor, source="hxdeploy", request=p.request,
                note="requested by the source environment")
        except service.RolloutLocked as exc:
            raise StagedImportRefused(str(exc), **{k: v for k, v in exc.detail().items()
                                                    if k != "message"}) from exc
        out.append(service.to_json(row))
    return out
