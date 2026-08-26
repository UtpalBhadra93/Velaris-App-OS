"""HxSentinel P3b — supervisor oversight of the score signal.

Scores are the engine's training signal (P3c), so they have to be honest. This
module lets a supervisor SEE how each reviewer scores (distribution, agreement
with confirmed outcomes, how fast they score) and DISPUTE a specific score to
exclude it from reliability and training.

Everything here is read-only aggregation plus append-only dispute records — no
score is ever mutated. Reliability is computed, not stored, so it is always
current and there is nothing to keep in sync.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import statistics
import uuid
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from velaris_shared.db.models import (
    CaseAuditLogModel,
    CaseRiskFeedbackModel,
    CaseRiskFindingModel,
    CaseRiskScoreDisputeModel,
)
from case_service.sentinel.engine import _classify_calibration, _current_outcomes, _negative_keys
from case_service.sentinel.service import norm_tenant

#: A reviewer whose scores agree with confirmed outcomes less than this (once
#: there are enough determinable scores) is flagged for a supervisor's attention.
_LOW_RELIABILITY = 0.6
#: A median time-to-score below this (seconds) with several scores looks like
#: rubber-stamping rather than review.
_FAST_SCORE_SECONDS = 3.0
_MIN_FOR_FLAG = 4


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


async def dispute_score(
    session: AsyncSession, *, feedback_id: uuid.UUID, tenant: str,
    disputed_by: str, note: str,
) -> CaseRiskScoreDisputeModel | None:
    """Record a supervisor's dispute of a score. Idempotent-ish: a second
    dispute of the same score just adds another note-bearing record."""
    fb = await session.get(CaseRiskFeedbackModel, feedback_id)
    if fb is None:
        return None
    row = CaseRiskScoreDisputeModel(
        tenant_id=norm_tenant(fb.tenant_id), feedback_id=feedback_id,
        disputed_by=disputed_by, note=note)
    session.add(row)
    session.add(CaseAuditLogModel(
        case_id=fb.case_id, action="risk.score.disputed", actor_id=disputed_by,
        details={"feedback_id": str(feedback_id), "reviewer": fb.reviewer, "note": note}))
    await session.flush()
    return row


async def reviewer_dashboard(session: AsyncSession, tenant: str) -> dict:
    """Per-reviewer scoring stats for a tenant — the supervisor dashboard.

    For each reviewer: score count + distribution, outcome-agreement reliability
    (over scores whose case now has a confirmed outcome, disputed ones excluded),
    median time-to-score, and whether they are flagged for review.
    """
    # All feedback in the tenant, with the finding's created_at for time-to-score.
    rows = (await session.execute(
        select(CaseRiskFeedbackModel, CaseRiskFindingModel.created_at)
        .join(CaseRiskFindingModel, CaseRiskFindingModel.id == CaseRiskFeedbackModel.finding_id)
        .where(CaseRiskFeedbackModel.tenant_id == tenant)
    )).all()
    if not rows:
        return {"reviewers": []}

    disputed_ids = set((await session.execute(
        select(CaseRiskScoreDisputeModel.feedback_id)
        .where(CaseRiskScoreDisputeModel.tenant_id == tenant)
    )).scalars().all())

    case_ids = {fb.case_id for fb, _ in rows}
    outcomes = await _current_outcomes(session, case_ids)
    negatives = await _negative_keys(session, tenant)

    agg: dict[str, dict] = {}
    for fb, finding_created in rows:
        r = agg.setdefault(fb.reviewer, {
            "reviewer": fb.reviewer, "count": 0, "disputed": 0,
            "distribution": {n: 0 for n in range(1, 6)},
            "_ok": 0, "_mis": 0, "_times": []})
        r["count"] += 1
        r["distribution"][fb.score] = r["distribution"].get(fb.score, 0) + 1
        if fb.id in disputed_ids:
            r["disputed"] += 1
            continue                       # disputed scores don't count toward reliability
        cal = _classify_calibration(outcomes.get(fb.case_id), negatives, fb.score)
        if cal == "ok":
            r["_ok"] += 1
        elif cal == "miscalibrated":
            r["_mis"] += 1
        if finding_created and fb.created_at:
            r["_times"].append(max(0.0, (fb.created_at - finding_created).total_seconds()))

    reviewers = []
    for r in agg.values():
        determinable = r["_ok"] + r["_mis"]
        reliability = round(r["_ok"] / determinable, 3) if determinable else None
        median_tts = round(statistics.median(r["_times"]), 1) if r["_times"] else None
        flagged = bool(
            (reliability is not None and determinable >= _MIN_FOR_FLAG and reliability < _LOW_RELIABILITY)
            or (median_tts is not None and len(r["_times"]) >= _MIN_FOR_FLAG and median_tts < _FAST_SCORE_SECONDS)
        )
        reviewers.append({
            "reviewer": r["reviewer"],
            "count": r["count"],
            "disputed": r["disputed"],
            "distribution": r["distribution"],
            "determinable": determinable,
            "reliability": reliability,               # outcome-agreement rate, disputed excluded
            "median_time_to_score_s": median_tts,
            "flagged": flagged,
        })
    reviewers.sort(key=lambda x: (x["reliability"] is None, x["reliability"] or 0))
    return {"reviewers": reviewers}


async def reviewer_reliability(session: AsyncSession, tenant: str, reviewer: str) -> float | None:
    """A single reviewer's reliability factor (for P3c training weights)."""
    dash = await reviewer_dashboard(session, tenant)
    for r in dash["reviewers"]:
        if r["reviewer"] == reviewer:
            return r["reliability"]
    return None
