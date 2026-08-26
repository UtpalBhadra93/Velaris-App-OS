"""Statistical SLA prediction (roadmap #40, report §4.5).

Answers one question: *given how long cases like this have historically taken,
how unusual is this one's current age?* A case sitting past the 85th percentile
of its own history is very likely to breach, and it can be flagged before any
SLA timer fires.

Deliberately **no model and no training** — §4.5 is explicit that this is
percentile arithmetic over history the platform already records. It is the
policy path of the two-path architecture: useful with the AI switched off.

Two grains:

* **case** — age since `created_at` against the durations of resolved cases of
  the same case type. Works today.
* **stage** — age in the current stage against historical stage durations, from
  `stage_enter` / `stage_exit` events. Only meaningful once those events exist;
  `case_lifecycle.on_stage_entered` began recording them in 2026-08 (roadmap
  #40), so older cases contribute nothing and thin history reports as such.

Both refuse to answer below :data:`MIN_SAMPLES`. A percentile computed from
three cases is not a weak signal, it is a misleading one — the caller gets
``insufficient_data`` and can say so, rather than rendering a confident number.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from velaris_shared.db.models import CaseEventLogModel, CaseInstanceModel

logger = logging.getLogger(__name__)

#: Below this many historical samples, no percentile is reported.
MIN_SAMPLES = 50

#: Age at or above this percentile of history is flagged as at risk.
AT_RISK_PERCENTILE = 85.0

#: Cap on rows pulled for a single distribution — a pathological case type
#: must not drag the whole request.
_MAX_SAMPLES = 20_000


def percentile_of(sorted_values: list[float], value: float) -> float:
    """Percent of `sorted_values` strictly below `value`, 0.0–100.0.

    Uses a strict "below" count rather than interpolating between neighbours:
    the question is "what share of history finished faster than this case has
    been open", and that is a plain rank, not an estimate of a quantile.
    """
    if not sorted_values:
        return 0.0
    lo, hi = 0, len(sorted_values)
    while lo < hi:                      # bisect_left, without the import
        mid = (lo + hi) // 2
        if sorted_values[mid] < value:
            lo = mid + 1
        else:
            hi = mid
    return round(100.0 * lo / len(sorted_values), 2)


def value_at_percentile(sorted_values: list[float], pct: float) -> float:
    """The value standing at `pct` of a sorted list (nearest-rank)."""
    if not sorted_values:
        return 0.0
    idx = int(round((pct / 100.0) * (len(sorted_values) - 1)))
    return sorted_values[max(0, min(idx, len(sorted_values) - 1))]


def _insufficient(grain: str, n: int, **extra: Any) -> dict[str, Any]:
    return {
        "grain": grain,
        "status": "insufficient_data",
        "samples": n,
        "min_samples": MIN_SAMPLES,
        "at_risk": False,
        "percentile": None,
        "reason": (
            f"only {n} historical sample(s); {MIN_SAMPLES} required before a "
            f"percentile means anything"
        ),
        **extra,
    }


async def case_type_durations(
    session: AsyncSession, case_type_id: uuid.UUID,
) -> list[float]:
    """Seconds from creation to resolution for resolved cases of this type."""
    rows = (await session.execute(
        select(CaseInstanceModel.created_at, CaseInstanceModel.resolved_at)
        .where(
            CaseInstanceModel.case_type_id == case_type_id,
            CaseInstanceModel.resolved_at.is_not(None),
        )
        .limit(_MAX_SAMPLES)
    )).all()

    out = []
    for created, resolved in rows:
        if not created or not resolved:
            continue
        delta = (_aware(resolved) - _aware(created)).total_seconds()
        if delta >= 0:          # clock skew / backdated import — not a sample
            out.append(delta)
    out.sort()
    return out


async def stage_durations(
    session: AsyncSession, case_type_id: uuid.UUID, stage_id: str,
) -> list[float]:
    """Seconds spent in `stage_id`, from paired enter/exit events.

    Pairs each `stage_enter` with the next `stage_exit` for the same case, so
    a case that re-enters a stage (loopback) contributes one sample per visit
    rather than one merged span.
    """
    rows = (await session.execute(
        select(
            CaseEventLogModel.case_id,
            CaseEventLogModel.activity_type,
            CaseEventLogModel.timestamp,
        )
        .where(
            CaseEventLogModel.case_type_id == case_type_id,
            CaseEventLogModel.stage_id == stage_id,
            CaseEventLogModel.activity_type.in_(("stage_enter", "stage_exit")),
        )
        .order_by(CaseEventLogModel.case_id, CaseEventLogModel.timestamp)
        .limit(_MAX_SAMPLES)
    )).all()

    out: list[float] = []
    open_enter: dict[Any, datetime] = {}
    for case_id, activity_type, ts in rows:
        if activity_type == "stage_enter":
            open_enter[case_id] = ts
        elif activity_type == "stage_exit":
            entered = open_enter.pop(case_id, None)
            if entered is not None:
                delta = (_aware(ts) - _aware(entered)).total_seconds()
                if delta >= 0:
                    out.append(delta)
    out.sort()
    return out


def _aware(dt: datetime) -> datetime:
    """SQLite hands back naive datetimes; treat those as UTC."""
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _assess(grain: str, samples: list[float], age_seconds: float,
            **extra: Any) -> dict[str, Any]:
    if len(samples) < MIN_SAMPLES:
        return _insufficient(grain, len(samples), **extra)

    pct = percentile_of(samples, age_seconds)
    return {
        "grain": grain,
        "status": "ok",
        "samples": len(samples),
        "age_seconds": round(age_seconds, 2),
        "percentile": pct,
        "at_risk": pct >= AT_RISK_PERCENTILE,
        "at_risk_percentile": AT_RISK_PERCENTILE,
        "median_seconds": round(value_at_percentile(samples, 50.0), 2),
        "p85_seconds": round(value_at_percentile(samples, 85.0), 2),
        "p95_seconds": round(value_at_percentile(samples, 95.0), 2),
        **extra,
    }


async def predict_for_case(
    session: AsyncSession, case_id: uuid.UUID, *, now: datetime | None = None,
) -> dict[str, Any]:
    """Percentile assessment for one open case, at both grains.

    A resolved case reports its final position rather than an age that keeps
    growing — the question stops being predictive once the case is done.
    """
    now = now or datetime.now(timezone.utc)
    case = await session.get(CaseInstanceModel, case_id)
    if case is None:
        raise ValueError(f"Case {case_id} not found")

    end = _aware(case.resolved_at) if case.resolved_at else now
    age = (end - _aware(case.created_at)).total_seconds()

    case_grain = _assess(
        "case", await case_type_durations(session, case.case_type_id), age,
        case_type_id=str(case.case_type_id),
    )

    stage_grain: dict[str, Any]
    if case.current_stage_id:
        entered = await _current_stage_entered_at(session, case_id, case.current_stage_id)
        if entered is None:
            stage_grain = _insufficient(
                "stage", 0, stage_id=case.current_stage_id,
            ) | {"reason": "no stage_enter event recorded for the current stage"}
        else:
            stage_grain = _assess(
                "stage",
                await stage_durations(session, case.case_type_id, case.current_stage_id),
                (end - _aware(entered)).total_seconds(),
                stage_id=case.current_stage_id,
            )
    else:
        stage_grain = _insufficient("stage", 0) | {"reason": "case is not in a stage"}

    return {
        "case_id": str(case_id),
        "resolved": case.resolved_at is not None,
        "case": case_grain,
        "stage": stage_grain,
        # One flag for a caller that does not want to reason about grains:
        # either grain saying "at risk" is enough to surface the case.
        "at_risk": bool(case_grain.get("at_risk") or stage_grain.get("at_risk")),
    }


async def _current_stage_entered_at(
    session: AsyncSession, case_id: uuid.UUID, stage_id: str,
) -> datetime | None:
    return (await session.execute(
        select(CaseEventLogModel.timestamp)
        .where(
            CaseEventLogModel.case_id == case_id,
            CaseEventLogModel.stage_id == stage_id,
            CaseEventLogModel.activity_type == "stage_enter",
        )
        .order_by(CaseEventLogModel.timestamp.desc())
        .limit(1)
    )).scalar_one_or_none()
