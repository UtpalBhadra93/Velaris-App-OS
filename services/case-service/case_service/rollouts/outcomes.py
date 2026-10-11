"""How the two arms of a rollout are doing — the A/B comparison (Oct 18a, phase 3).

Computed from the cases themselves, never stored: the arm a case landed in
(`content_rollout_cases`), when it was resolved, whether an SLA on it breached,
and whether it was reopened. Per metric the answer is one of `better`, `worse`,
`no clear difference` or `not enough cases` — and a difference that could be
noise (p ≥ 0.05) is reported as no clear difference, not as a finding.

The statistics are deliberately plain: a two-proportion z-test for the rates and
Welch's t (normal approximation) for resolution time. They are enough to stop an
operator reading a 3-case arm as a verdict, which is their job here.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import math
import statistics
import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from velaris_shared.db.models import (
    CaseAuditLogModel,
    CaseInstanceModel,
    CaseSLAInstanceModel,
    ContentRolloutCaseModel,
)

#: Per arm, at most this many cases are read. A rollout past it is judged on
#: its most recent cases, and says so.
MAX_PER_ARM = 20_000
MIN_CASES = 30
ALPHA = 0.05


def _utc(d):
    """Naive and aware columns both appear here (see `core/timestamps`)."""
    from datetime import timezone
    return d.replace(tzinfo=timezone.utc) if d.tzinfo is None else d.astimezone(timezone.utc)


def _p_two_sided(z: float) -> float:
    return math.erfc(abs(z) / math.sqrt(2))


def compare_rates(k_a: int, n_a: int, k_b: int, n_b: int) -> dict[str, Any]:
    """Arm a = baseline, arm b = candidate. Delta is candidate minus baseline."""
    if n_a == 0 or n_b == 0:
        return {"baseline": None, "candidate": None, "delta": None, "p_value": None}
    pa, pb = k_a / n_a, k_b / n_b
    pooled = (k_a + k_b) / (n_a + n_b)
    se = math.sqrt(pooled * (1 - pooled) * (1 / n_a + 1 / n_b))
    p = 1.0 if se == 0 else _p_two_sided((pb - pa) / se)
    return {"baseline": round(pa, 4), "candidate": round(pb, 4),
            "delta": round(pb - pa, 4), "p_value": round(p, 4)}


def compare_means(a: list[float], b: list[float]) -> dict[str, Any]:
    if not a or not b:
        return {"baseline": None, "candidate": None, "delta": None, "p_value": None}
    ma, mb = statistics.fmean(a), statistics.fmean(b)
    va = statistics.variance(a) if len(a) > 1 else 0.0
    vb = statistics.variance(b) if len(b) > 1 else 0.0
    se = math.sqrt(va / len(a) + vb / len(b))
    p = 1.0 if se == 0 else _p_two_sided((mb - ma) / se)
    return {"baseline": round(ma, 2), "candidate": round(mb, 2),
            "baseline_median": round(statistics.median(a), 2),
            "candidate_median": round(statistics.median(b), 2),
            "delta": round(mb - ma, 2), "p_value": round(p, 4)}


def verdict(cmp: dict[str, Any], n_a: int, n_b: int, *, min_cases: int = MIN_CASES,
            lower_is_better: bool = True) -> str:
    if min(n_a, n_b) < min_cases or cmp.get("delta") is None:
        return "not enough cases"
    if cmp["p_value"] is None or cmp["p_value"] >= ALPHA or cmp["delta"] == 0:
        return "no clear difference"
    worse = cmp["delta"] > 0 if lower_is_better else cmp["delta"] < 0
    return "worse" if worse else "better"


async def _arm(session: AsyncSession, rollout_id: uuid.UUID, arm: str) -> dict[str, Any]:
    rows = (await session.execute(
        select(CaseInstanceModel.id, CaseInstanceModel.created_at,
               CaseInstanceModel.resolved_at, CaseInstanceModel.closed_at)
        .join(ContentRolloutCaseModel, ContentRolloutCaseModel.case_id == CaseInstanceModel.id)
        .where(ContentRolloutCaseModel.rollout_id == rollout_id,
               ContentRolloutCaseModel.arm == arm)
        .order_by(CaseInstanceModel.created_at.desc()).limit(MAX_PER_ARM + 1))).all()
    truncated = len(rows) > MAX_PER_ARM
    rows = rows[:MAX_PER_ARM]
    ids = [r[0] for r in rows]
    hours = []
    for _id, created, resolved, closed in rows:
        done = resolved or closed
        if done and created:
            hours.append(max(0.0, (_utc(done) - _utc(created)).total_seconds() / 3600))
    breached: set = set()
    reopened: set = set()
    if ids:
        for i in range(0, len(ids), 500):
            chunk = ids[i:i + 500]
            breached |= set((await session.execute(
                select(CaseSLAInstanceModel.case_id).where(
                    CaseSLAInstanceModel.case_id.in_(chunk),
                    CaseSLAInstanceModel.status == "breached").distinct())).scalars().all())
            reopened |= set((await session.execute(
                select(CaseAuditLogModel.case_id).where(
                    CaseAuditLogModel.case_id.in_(chunk),
                    CaseAuditLogModel.action == "reopened").distinct())).scalars().all())
    return {"cases": len(ids), "resolved": len(hours), "open": len(ids) - len(hours),
            "sla_breached": len(breached), "reopened": len(reopened),
            "resolution_hours": hours, "truncated": truncated}


async def compare(session: AsyncSession, rollout_id: uuid.UUID, *,
                  min_cases: int = MIN_CASES) -> dict[str, Any]:
    a = await _arm(session, rollout_id, "baseline")
    b = await _arm(session, rollout_id, "candidate")
    sla = compare_rates(a["sla_breached"], a["cases"], b["sla_breached"], b["cases"])
    reopen = compare_rates(a["reopened"], a["cases"], b["reopened"], b["cases"])
    res_rate = compare_rates(a["resolved"], a["cases"], b["resolved"], b["cases"])
    hours = compare_means(a["resolution_hours"], b["resolution_hours"])
    metrics = {
        "sla_breach_rate": {**sla, "verdict": verdict(sla, a["cases"], b["cases"],
                                                     min_cases=min_cases)},
        "reopen_rate": {**reopen, "verdict": verdict(reopen, a["cases"], b["cases"],
                                                     min_cases=min_cases)},
        "resolved_rate": {**res_rate, "verdict": verdict(
            res_rate, a["cases"], b["cases"], min_cases=min_cases, lower_is_better=False)},
        "avg_resolution_hours": {**hours, "verdict": verdict(
            hours, a["resolved"], b["resolved"], min_cases=min_cases)},
    }
    strip = ("resolution_hours",)
    return {"arms": {"baseline": {k: v for k, v in a.items() if k not in strip},
                     "candidate": {k: v for k, v in b.items() if k not in strip}},
            "metrics": metrics, "min_cases": min_cases, "alpha": ALPHA,
            "note": ("Computed from the cases now. A candidate case that is still open "
                     "counts in the rates but not in resolution time, so a young arm "
                     "looks faster than it is — wait for cases to close.")}


def guard_breach(result: dict[str, Any], guard: dict[str, Any]) -> dict[str, Any] | None:
    """The guard's finding, or None when it holds."""
    m = result["metrics"].get(guard["metric"]) or {}
    arms = result["arms"]
    n_a = arms["baseline"]["resolved" if guard["metric"] == "avg_resolution_hours" else "cases"]
    n_b = arms["candidate"]["resolved" if guard["metric"] == "avg_resolution_hours" else "cases"]
    if min(n_a, n_b) < guard["min_cases"] or m.get("delta") is None:
        return None
    if m["delta"] <= guard["max_delta"]:
        return None
    # `is None`, not `or`: a p-value of exactly 0.0 is the STRONGEST evidence,
    # and `0.0 or 1` would have read it as no evidence at all.
    p = m.get("p_value")
    if guard.get("require_significance", True) and (p is None or p >= ALPHA):
        return None
    return {"metric": guard["metric"], "baseline": m["baseline"], "candidate": m["candidate"],
            "delta": m["delta"], "max_delta": guard["max_delta"], "p_value": m.get("p_value"),
            "cases": {"baseline": n_a, "candidate": n_b}}
