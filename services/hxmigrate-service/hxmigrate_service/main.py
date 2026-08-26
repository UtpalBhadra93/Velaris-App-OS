"""hxmigrate-service — the migration domain in its own process (roadmap 13e).

Last out, and the recipe said why: this is the one that *writes* case types, so
it had to be solid on the shared layer and on authorization before it left.

It serves five prefixes, because the domain is five routers over four packages
that call each other:

    /api/v1/hxmigrate     plan and execute a migration
    /api/v1/importer      BPM definition import (bpm_importer)
    /api/v1/scout         scan a legacy system
    /api/v1/scout-ai      the AI pass over a scan
    /api/v1/orchestrator  turn a scan into a project

`bpm_importer` imports `hxmigrate.security` and `hxmigrate.service` imports
`bpm_importer.pipeline` — they import each other, so they were never separable.
`scout` and `orchestrator` came with them for the same reason: `hxmigrate`
calls both, and they are migration-domain code, not platform infrastructure, so
the shared layer would have been the wrong home.

**How it writes case types.** It does not reach into case-service's tables.
`hxmigrate.creator` has always POSTed to the case API over HTTP, with the host
validated against a loopback allowlist (SEC-5). That was written as a
self-referential call inside one process; extraction turns it into a real
service-to-service call through the gateway, and nothing about it had to change.

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

from hxmigrate_service.api.routers.hxmigrate import router as hxmigrate_router
from hxmigrate_service.api.routers.importer import router as importer_router
from hxmigrate_service.api.routers.orchestrator import router as orchestrator_router
from hxmigrate_service.api.routers.scout import router as scout_router
from hxmigrate_service.api.routers.scout_ai import router as scout_ai_router

logger = logging.getLogger("hxmigrate_service")

SERVICE_NAME = "hxmigrate-service"
API_PREFIX = "/api/v1"


def create_app() -> FastAPI:
    settings = get_settings()

    app = FastAPI(
        title="Velaris hxmigrate-service",
        version="0.1.0",
        description="Migration: scan, import, plan, create.",
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

    for router in (hxmigrate_router, importer_router, scout_router,
                   scout_ai_router, orchestrator_router):
        app.include_router(router, prefix=API_PREFIX)

    mount_metrics(app)
    apply_platform_error_handlers(app, "hxmigrate_service.errors")
    apply_platform_middleware(app)

    @app.get("/health", tags=["health"])
    async def health() -> dict[str, str]:
        return {"status": "healthy", "service": SERVICE_NAME}

    @app.get("/ready", tags=["health"])
    async def ready() -> dict[str, str]:
        from sqlalchemy import text

        from velaris_shared.db.session import get_session_factory

        factory = get_session_factory()
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
