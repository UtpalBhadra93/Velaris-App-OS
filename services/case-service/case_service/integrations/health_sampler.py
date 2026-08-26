"""Periodic health sampler — the producer HxStatus needs (§8.8).

`health_check_results` has existed as a schema for some time and was **empty**:
0 rows, 0 components. The only writer sits inside `GET /health/deep`, which
nothing calls on a schedule, so the table filled in only when a human happened
to hit that endpoint.

§8.8 calls a status page "trivial to build on existing observability". It is
not, because there was no observability *history* to build on — only the
ability to observe on demand. This is that missing half: a background task that
samples component health on an interval and writes it down, so uptime over a
window is a real measurement rather than a single instantaneous reading.

Two things it deliberately does:

**Prunes.** One row per component per interval grows without limit — at a
60-second interval and two components that is ~1M rows a year, on the OLTP
database, to serve a page that only ever shows the last 30 days. Old samples
are deleted on a slow cadence.

**Never raises into the app.** A sampler that can crash the service it is
measuring is worse than no sampler.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import asyncio
import logging
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import delete, select

logger = logging.getLogger(__name__)

#: Component names are a fixed vocabulary. A status page must not become an
#: inventory of internals that changes shape as modules come and go.
DATABASE = "database"
TELEMETRY = "telemetry"

STATUS_OK = "ok"
STATUS_DEGRADED = "degraded"
STATUS_DOWN = "down"

#: Above this, the database is reachable but not healthy. A status page that
#: only distinguishes up from down tells an operator nothing before an outage.
DEGRADED_LATENCY_MS = 500.0


async def sample_once(session) -> list[dict[str, Any]]:
    """Measure every component once. Returns the rows written.

    The `detail` field may carry a raw exception message — it is diagnostic and
    **must never be served publicly**; `status.py` strips it.
    """
    from velaris_shared.db.models import HealthCheckResultModel

    components: list[dict[str, Any]] = []

    start = time.perf_counter()
    try:
        await session.execute(select(1))
        latency = round((time.perf_counter() - start) * 1000.0, 2)
        status = STATUS_OK if latency < DEGRADED_LATENCY_MS else STATUS_DEGRADED
        detail: dict[str, Any] = {}
    except Exception as exc:
        latency, status = None, STATUS_DOWN
        detail = {"error": str(exc)[:200]}
    components.append({
        "component": DATABASE, "status": status,
        "latency_ms": latency, "detail": detail,
    })

    try:
        from velaris_shared.observability.spans import recent_spans

        components.append({
            "component": TELEMETRY, "status": STATUS_OK, "latency_ms": 0.0,
            "detail": {"recent_spans": len(recent_spans(limit=500))},
        })
    except Exception as exc:
        components.append({
            "component": TELEMETRY, "status": STATUS_DEGRADED,
            "latency_ms": None, "detail": {"error": str(exc)[:200]},
        })

    for c in components:
        session.add(HealthCheckResultModel(
            id=uuid.uuid4(),
            component=c["component"], status=c["status"],
            latency_ms=c["latency_ms"], detail=c["detail"],
        ))
    return components


async def prune(session, *, older_than_days: int) -> int:
    """Delete samples past the retention window. Returns rows removed."""
    from velaris_shared.db.models import HealthCheckResultModel

    cutoff = datetime.now(timezone.utc) - timedelta(days=older_than_days)
    result = await session.execute(
        delete(HealthCheckResultModel).where(
            HealthCheckResultModel.checked_at < cutoff
        )
    )
    return int(result.rowcount or 0)


class HealthSampler:
    """Background task writing periodic health samples."""

    def __init__(
        self, session_factory, *, interval_seconds: int = 60,
        retention_days: int = 35,
    ) -> None:
        self._factory = session_factory
        self._interval = max(10, int(interval_seconds))
        self._retention_days = max(1, int(retention_days))
        self._task: asyncio.Task | None = None
        self._running = False
        self._cycles = 0

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._run())
        logger.info("health sampler started (every %ss)", self._interval)

    def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()

    async def _run(self) -> None:
        while self._running:
            try:
                async with self._factory() as session:
                    await sample_once(session)
                    # Prune roughly hourly rather than every cycle — a DELETE
                    # scan per sample would cost more than the samples do.
                    if self._cycles % max(1, (3600 // self._interval)) == 0:
                        removed = await prune(
                            session, older_than_days=self._retention_days,
                        )
                        if removed:
                            logger.info("health sampler pruned %d old sample(s)", removed)
                    await session.commit()
                self._cycles += 1
            except Exception as exc:
                # A sampler must never take down the thing it measures.
                logger.warning("health sample failed: %s", exc)
            await asyncio.sleep(self._interval)
