"""Shadow — what the candidate would have done, with no effect (Oct 18a, phase 2).

A baseline case is replayed through the CANDIDATE's rule set by HxReplay's
engine, which is read-only by construction: it reconstructs the inputs each
decision saw and re-evaluates the rules in memory. Nothing about the case
changes and no customer sees anything; the only write is the result row here.

Two things are compared:

* **the rules** — HxReplay's divergence: the first point at which the candidate's
  rules would have decided differently, and what they would have done instead;
* **the shape** — stages the case passed through that the candidate does not
  define (a case at such a stage could not have continued on the candidate).

A case is evaluated once, after it has closed or has been open long enough to
have a history (`SHADOW_AFTER_MINUTES`). What HxReplay cannot determine is
recorded as indeterminate with its reason — never guessed into a verdict.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from velaris_shared.db.models import (
    CaseInstanceModel,
    CaseStepCompletionModel,
    CaseTypeModel,
    ContentRolloutCaseModel,
    ContentRolloutModel,
    ContentRolloutShadowModel,
)

log = logging.getLogger(__name__)

SHADOW_AFTER_MINUTES = 60
BATCH = 25


def _stage_ids(definition: dict[str, Any] | None) -> set[str]:
    return {str(s.get("id")) for s in ((definition or {}).get("stages") or []) if s.get("id")}


async def evaluate(session: AsyncSession, *, rollout: ContentRolloutModel,
                   case: CaseInstanceModel) -> ContentRolloutShadowModel:
    from case_service.hxreplay import engine, runner

    cand = await session.get(CaseTypeModel, rollout.candidate_id)
    detail: dict[str, Any] = {}
    try:
        baseline_rules = await runner.baseline_rule_set(session, rollout.baseline_id)
        candidate_rules = await runner.baseline_rule_set(session, rollout.candidate_id)
        out = await engine.replay_case(
            session, case.id, baseline_rules, candidate_rules,
            case_type_id=rollout.baseline_id,
            definition_json=cand.definition_json if cand else None,
            tenant_id=str(case.tenant_id) if case.tenant_id else None)
        determinacy = str(out.get("determinacy") or "indeterminate")
        diverged = out.get("divergence_point") is not None
        detail = {
            "divergence_point": out.get("divergence_point"),
            "diverging_actions": ((out.get("trace") or {}).get("diverging_actions") or [])[:10],
            "note": (out.get("trace") or {}).get("note"),
            "exclusion_reason": out.get("exclusion_reason"),
            "baseline_metrics": out.get("baseline_metrics"),
            "candidate_metrics": out.get("counterfactual_metrics"),
        }
    except Exception as exc:                                     # noqa: BLE001
        determinacy, diverged = "error", False
        detail = {"error": f"{type(exc).__name__}: {str(exc)[:300]}"}

    # The shape: stages this case stood in that the candidate does not have.
    visited = {str(s) for s in (await session.execute(
        select(CaseStepCompletionModel.stage_id).where(
            CaseStepCompletionModel.case_id == case.id).distinct())).scalars().all() if s}
    if case.current_stage_id:
        visited.add(str(case.current_stage_id))
    missing = sorted(visited - _stage_ids(cand.definition_json if cand else None))
    if missing:
        detail["stages_missing_in_candidate"] = missing
        diverged = True

    row = ContentRolloutShadowModel(
        case_id=case.id, rollout_id=rollout.id, tenant_id=rollout.tenant_id,
        determinacy=determinacy[:20], diverged=bool(diverged), detail=detail,
        evaluated_at=datetime.now(timezone.utc))
    session.add(row)
    return row


async def pending(session: AsyncSession, rollout: ContentRolloutModel, *,
                  now: datetime | None = None, limit: int = BATCH) -> list[CaseInstanceModel]:
    """Baseline cases of this rollout not yet shadowed, and old enough to judge."""
    moment = now or datetime.now(timezone.utc)
    cutoff = moment - timedelta(minutes=SHADOW_AFTER_MINUTES)
    done = select(ContentRolloutShadowModel.case_id).where(
        ContentRolloutShadowModel.rollout_id == rollout.id)
    q = (select(CaseInstanceModel)
         .join(ContentRolloutCaseModel, ContentRolloutCaseModel.case_id == CaseInstanceModel.id)
         .where(ContentRolloutCaseModel.rollout_id == rollout.id,
                ContentRolloutCaseModel.arm == "baseline",
                CaseInstanceModel.id.notin_(done),
                or_(CaseInstanceModel.resolved_at.isnot(None),
                    CaseInstanceModel.closed_at.isnot(None),
                    CaseInstanceModel.created_at <= cutoff))
         .order_by(CaseInstanceModel.created_at).limit(limit))
    return list((await session.execute(q)).scalars().all())


async def summary(session: AsyncSession, rollout_id: uuid.UUID) -> dict[str, Any]:
    rows = (await session.execute(
        select(ContentRolloutShadowModel.determinacy, ContentRolloutShadowModel.diverged,
               func.count())
        .where(ContentRolloutShadowModel.rollout_id == rollout_id)
        .group_by(ContentRolloutShadowModel.determinacy,
                  ContentRolloutShadowModel.diverged))).all()
    out = {"evaluated": 0, "diverged": 0, "by_determinacy": {}}
    for det, div, n in rows:
        out["evaluated"] += int(n)
        out["diverged"] += int(n) if div else 0
        out["by_determinacy"][det] = out["by_determinacy"].get(det, 0) + int(n)
    examples = (await session.execute(
        select(ContentRolloutShadowModel).where(
            ContentRolloutShadowModel.rollout_id == rollout_id,
            ContentRolloutShadowModel.diverged.is_(True))
        .order_by(ContentRolloutShadowModel.evaluated_at.desc()).limit(10))).scalars().all()
    out["examples"] = [{"case_id": str(e.case_id), "determinacy": e.determinacy,
                        "detail": e.detail} for e in examples]
    return out
