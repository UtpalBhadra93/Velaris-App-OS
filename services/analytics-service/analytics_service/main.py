"""analytics-service — the read side, in its own process (roadmap 13c).

The first service extracted from case-service. It serves `/api/v1/analytics`
and `/api/v1/process-mining`: aggregate queries, saved reports, exports and
process mining — read-heavy work whose cost is exactly what §1.1 of the
architecture report wanted off the transactional connection pool.

Three things are deliberate here:

* **The same door.** `velaris_shared.app` applies the audit trail, request
  tracking, rate limit, body limit and security headers that case-service
  applies. An extracted service that answers authenticated routes with a
  thinner middleware stack is a weaker door on the same building, and the
  gateway routes traffic to it just as happily.

* **Its own pool, the same database.** Sessions come from
  `get_analytics_session` / `get_replica_session`, which is why the heavy
  endpoints could move at all: the read-replica seam already existed. Separate
  schemas per domain stay demand-driven.

* **No write side.** `process_mining.event_logger` — the per-transition write —
  stayed in the shared layer and runs in-process inside case-service. Putting a
  network hop on every case transition would buy isolation the case lifecycle
  does not want.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import logging

from fastapi import FastAPI

from velaris_shared.db.commit_route import install_commit_before_response
from velaris_shared.app import (
    apply_platform_error_handlers,
    apply_platform_middleware,
    mount_metrics,
    platform_lifespan,
)
from velaris_shared.config import get_settings
from velaris_shared.observability import configure_logging, configure_telemetry

from analytics_service.api.routers.analytics import router as analytics_router
from analytics_service.api.routers.process_mining import router as process_mining_router

logger = logging.getLogger("analytics_service")

SERVICE_NAME = "analytics-service"
API_PREFIX = "/api/v1"


def create_app() -> FastAPI:
    settings = get_settings()

    app = FastAPI(
        title="Velaris analytics-service",
        version="0.1.0",
        description="Aggregate analytics and process mining.",
        docs_url="/docs" if settings.debug else None,
        redoc_url=None,
        # The superadmin boot gate: the middleware only reads the flag this
        # lifespan sets, so the two must always be wired together.
        lifespan=platform_lifespan(SERVICE_NAME),
    )

    # Commit each request's database work BEFORE its response is sent. Must be
    # set before any include_router below: FastAPI captures the route class at
    # registration time. See velaris_shared/db/commit_route.py.
    install_commit_before_response(app)

    app.include_router(analytics_router, prefix=API_PREFIX)
    app.include_router(process_mining_router, prefix=API_PREFIX)

    mount_metrics(app)
    apply_platform_error_handlers(app, "analytics_service.errors")
    apply_platform_middleware(app)

    @app.get("/health", tags=["health"])
    async def health() -> dict[str, str]:
        return {"status": "healthy", "service": SERVICE_NAME}

    @app.get("/ready", tags=["health"])
    async def ready() -> dict[str, str]:
        """Ready means the database answers — this service is nothing without it."""
        from sqlalchemy import text

        from velaris_shared.db.session import get_analytics_session_factory

        factory = get_analytics_session_factory()
        async with factory() as session:
            await session.execute(text("SELECT 1"))
        return {"status": "ready", "service": SERVICE_NAME}

    configure_logging()
    try:
        configure_telemetry(SERVICE_NAME, app=app)
    except Exception:  # noqa: BLE001 - telemetry must never block startup
        logger.warning("telemetry not configured for %s", SERVICE_NAME, exc_info=True)

    return app


app = create_app()
