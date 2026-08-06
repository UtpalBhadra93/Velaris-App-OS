"""HxStatus — public status page + SLA reporting (§8.8).

External-facing uptime, fed by the health samples `integrations/health_sampler`
writes. Enterprise buyers expect a status page; the value is that it is
*public*, which is also the whole security problem.

## What makes the public route safe

**`detail` is never served publicly.** A health sample's `detail` carries the
raw exception text when a component fails — which can contain hostnames, table
names, driver messages and connection fragments. The public payload is built
field by field from a whitelist (`component`, `status`, uptime percentages,
`last_checked`); it is not the stored row with keys removed, because a
subtractive filter silently starts leaking the moment a new column is added.

**It is cached.** An unauthenticated endpoint that runs aggregate queries per
request is a free DoS lever against the OLTP database. Results are held for a
short window, so a flood costs one query rather than thousands.

**It is rate limited** per IP, using the same limiter the other public routes
use.

**It reports only fixed component names**, never a live inventory of internal
modules — a status page should not double as reconnaissance.

The authenticated companion route returns everything, including `detail`, for
operators.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from case_service.auth.dependencies import require_role
from case_service.auth.models import AuthenticatedUser
from case_service.db.session import get_replica_session
from case_service.middleware.endpoint_rate_limit import SlidingWindowLimiter

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/status", tags=["status"])

#: Public and unauthenticated — the same posture the storefront public routes
#: take. Generous enough for a browser that polls, tight enough that it is not
#: a lever against the database.
_status_rl = SlidingWindowLimiter(max_calls=60, window_seconds=60.0, name="status_public")

#: Windows the page reports. Anything longer than the sampler's retention
#: would silently under-report, so 30d is the ceiling (retention is 35d).
WINDOWS: dict[str, int] = {"24h": 1, "7d": 7, "30d": 30}

#: Cache TTL. A status page is not a real-time console; a minute of staleness
#: is invisible to a reader and removes the DoS lever entirely.
_CACHE_TTL_SECONDS = 30.0
_cache: dict[str, Any] = {"at": 0.0, "payload": None}


async def _uptime_by_component(
    session: AsyncSession, *, days: int,
) -> dict[str, dict[str, Any]]:
    """Percentage of samples that were `ok`, per component, over a window.

    `degraded` counts against uptime deliberately: a component answering
    slowly is not up as far as a customer is concerned, and a status page that
    calls it green is worse than no page.
    """
    from case_service.db.models import HealthCheckResultModel

    since = datetime.now(timezone.utc) - timedelta(days=days)
    rows = (await session.execute(
        select(
            HealthCheckResultModel.component,
            HealthCheckResultModel.status,
            func.count().label("n"),
        )
        .where(HealthCheckResultModel.checked_at >= since)
        .group_by(HealthCheckResultModel.component, HealthCheckResultModel.status)
    )).all()

    totals: dict[str, dict[str, int]] = {}
    for component, status, n in rows:
        bucket = totals.setdefault(component, {"ok": 0, "total": 0})
        bucket["total"] += int(n)
        if status == "ok":
            bucket["ok"] += int(n)

    return {
        component: {
            "samples": b["total"],
            # None, not 100, when nothing was sampled. Reporting perfect
            # uptime from zero measurements is a lie a status page must not
            # tell.
            "uptime_pct": (
                round(100.0 * b["ok"] / b["total"], 3) if b["total"] else None
            ),
        }
        for component, b in totals.items()
    }


async def _build_public_payload(session: AsyncSession) -> dict[str, Any]:
    from case_service.db.models import HealthCheckResultModel
    from case_service.integrations import health_sampler as hs

    windows = {
        label: await _uptime_by_component(session, days=days)
        for label, days in WINDOWS.items()
    }

    components: list[dict[str, Any]] = []
    for name in (hs.DATABASE, hs.TELEMETRY):
        latest = (await session.execute(
            select(HealthCheckResultModel.status, HealthCheckResultModel.checked_at)
            .where(HealthCheckResultModel.component == name)
            .order_by(HealthCheckResultModel.checked_at.desc())
            .limit(1)
        )).first()

        # Whitelist construction — every field is named here on purpose. A
        # subtractive filter over the ORM row would start leaking the day
        # someone adds a column.
        components.append({
            "component": name,
            "status": latest.status if latest else "unknown",
            "last_checked": (
                latest.checked_at.isoformat() if latest and latest.checked_at else None
            ),
            "uptime": {
                label: windows[label].get(name, {}).get("uptime_pct")
                for label in WINDOWS
            },
        })

    overall = "operational"
    if any(c["status"] == "down" for c in components):
        overall = "major_outage"
    elif any(c["status"] == "degraded" for c in components):
        overall = "degraded"
    elif all(c["status"] == "unknown" for c in components):
        # No samples at all — say so rather than claiming health.
        overall = "unknown"

    return {
        "status": overall,
        "components": components,
        "windows": list(WINDOWS),
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }


@router.get("/public")
async def public_status(
    request: Request,
    session: AsyncSession = Depends(get_replica_session),
):
    """Public, unauthenticated status page data (§8.8).

    Carries no diagnostic detail, no internal inventory and no tenant data —
    only fixed component names, a coarse status, and uptime percentages.
    """
    ip = (request.client.host if request.client else "unknown") or "unknown"
    allowed, _retry = _status_rl.allow(ip)
    if not allowed:
        raise HTTPException(429, "Too many requests — please try again shortly")

    now = time.monotonic()
    if _cache["payload"] is not None and (now - _cache["at"]) < _CACHE_TTL_SECONDS:
        return _cache["payload"]

    payload = await _build_public_payload(session)
    _cache["at"], _cache["payload"] = now, payload
    return payload


@router.get("/internal")
async def internal_status(
    days: int = 7,
    session: AsyncSession = Depends(get_replica_session),
    _user: AuthenticatedUser = Depends(require_role("admin")),
):
    """Full status including diagnostic detail — admin only.

    This is where `detail` (raw exception text) is allowed to surface, because
    the caller is authenticated and privileged. It is the reason the public
    route builds its payload from a whitelist instead of reusing this one.
    """
    from case_service.db.models import HealthCheckResultModel

    days = max(1, min(int(days), 30))
    since = datetime.now(timezone.utc) - timedelta(days=days)

    recent = (await session.execute(
        select(HealthCheckResultModel)
        .where(HealthCheckResultModel.checked_at >= since)
        .order_by(HealthCheckResultModel.checked_at.desc())
        .limit(500)
    )).scalars().all()

    return {
        "window_days": days,
        "uptime": await _uptime_by_component(session, days=days),
        "samples": [{
            "component": r.component,
            "status": r.status,
            "latency_ms": r.latency_ms,
            "detail": r.detail,
            "checked_at": r.checked_at.isoformat() if r.checked_at else None,
        } for r in recent],
    }
