"""
FastAPI entry point for the Helix Flow Engine.
"""

from contextlib import asynccontextmanager
from collections.abc import AsyncGenerator
from pathlib import Path

import structlog
import yaml
from fastapi import FastAPI, Response

from velaris_sdk.config.brand import BRAND_NAME
from helix_engine.compiler import BPMNCompiler
from helix_engine.api.routers.process import router as process_router, init_router

logger = structlog.get_logger()


def load_config() -> dict:
    for path in [Path("velaris.yaml"), Path("../velaris.yaml"), Path("../../velaris.yaml")]:
        if path.exists():
            with open(path) as f:
                return yaml.safe_load(f) or {}
    return {}


_CONFIG = load_config()
_VERSION: str = _CONFIG.get("velaris", {}).get("version", "1.0.0")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    config = _CONFIG
    logger.info(f"{BRAND_NAME}_engine_starting", version=_VERSION,
                plugins=config.get("velaris", {}))

    is_pg = False
    store = None
    try:
        from helix_engine.db.session import init_db
        from helix_engine.db.pg_store import PgProcessStore
        await init_db()
        store = PgProcessStore()
        is_pg = True
        logger.info(f"{BRAND_NAME}_database_connected", backend="postgresql")
    except Exception as e:
        logger.warning(f"{BRAND_NAME}_database_unavailable",
                        error=str(e), hint="Falling back to in-memory store")
        from helix_engine.db.store import ProcessStore
        store = ProcessStore()

    temporal_worker = None
    try:
        from helix_engine.temporal.client import connect
        from helix_engine.temporal.worker import start_worker
        client = await connect()
        temporal_worker = await start_worker(client)
        app.state.temporal_client = client
        app.state.temporal_worker = temporal_worker
        logger.info(f"{BRAND_NAME}_temporal_connected")
    except Exception as e:
        logger.warning(f"{BRAND_NAME}_temporal_unavailable",
                        error=str(e), hint="Engine will run without Temporal")
        app.state.temporal_client = None

    compiler = BPMNCompiler()
    init_router(store=store, compiler=compiler)
    app.state.compiler = compiler
    app.state.store = store
    app.state.is_pg = is_pg

    logger.info(f"{BRAND_NAME}_engine_ready",
                database="postgresql" if is_pg else "in-memory",
                temporal="connected" if app.state.temporal_client else "unavailable",
                routes=len(app.routes))
    yield

    if temporal_worker is not None:
        from helix_engine.temporal.worker import stop_worker
        await stop_worker(temporal_worker)
    if is_pg:
        from helix_engine.db.session import close_db
        await close_db()
    logger.info(f"{BRAND_NAME}_engine_stopping")


app = FastAPI(
    title=f"{BRAND_NAME} Flow Engine",
    description="BPMN 2.0 process orchestration — your stack, your rules",
    version=_VERSION,
    lifespan=lifespan,
)
app.include_router(process_router)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "healthy", "service": f"{BRAND_NAME}-engine", "version": _VERSION}


@app.get("/ready")
async def ready(response: Response) -> dict[str, str]:
    """Readiness — and it ANSWERS 503 when the engine is not ready.

    Until 2026-09-17 this always returned 200 with a status word in the body, so
    a Kubernetes readiness probe pointed at it passed no matter what: an engine
    that had fallen back to the IN-MEMORY store (no database, no persistence)
    reported itself ready and took traffic. A probe reads the status code.

    **The database is what decides.** Without it the engine keeps deployed
    processes in memory, loses them on restart, and every pod holds a different
    set — so it must not be in the load balancer. Temporal is reported and
    retried here (the reconnect below), but it does not fail readiness on its
    own: the engine still compiles, deploys and lists processes without it, and
    flapping every pod out of service because a workflow backend blipped is the
    failure mode liveness probes are warned about.
    """
    from helix_engine.temporal.client import is_connected, connect
    from helix_engine.temporal.worker import start_worker
    from helix_engine.db.session import is_db_connected

    # Auto-heal: if Temporal was unavailable at startup, try once to reconnect
    if not is_connected() and not getattr(app.state, "temporal_reconnecting", False):
        app.state.temporal_reconnecting = True
        try:
            client = await connect()
            app.state.temporal_client = client
            if not getattr(app.state, "temporal_worker", None):
                app.state.temporal_worker = await start_worker(client)
            logger.info(f"{BRAND_NAME}_temporal_reconnected")
        except Exception:
            pass
        finally:
            app.state.temporal_reconnecting = False

    temporal_ok = is_connected()
    # A REAL check, not "was an engine object created".
    #
    # `is_db_connected()` returns True as soon as `create_async_engine` has been
    # called — which happens even when the database is unreachable — and the
    # startup path falls back to the in-memory store on failure. So the two
    # signals that matter are: did we fall back (`app.state.is_pg`), and does the
    # database answer right now. Without both, a pod holding its processes in RAM
    # reported "database: connected".
    db_ok = bool(getattr(app.state, "is_pg", False)) and is_db_connected()
    if db_ok:
        try:
            from sqlalchemy import text
            from helix_engine.db.session import get_session

            async with get_session() as session:
                await session.execute(text("SELECT 1"))
        except Exception as exc:                                  # noqa: BLE001
            logger.warning(f"{BRAND_NAME}_readiness_db_check_failed", error=str(exc))
            db_ok = False
    if temporal_ok and db_ok:
        status = "ready"
    elif temporal_ok or db_ok:
        status = "degraded"
    else:
        status = "unavailable"
    if not db_ok:
        response.status_code = 503
    return {"status": status, "database": "connected" if db_ok else "unavailable",
            "temporal": "connected" if temporal_ok else "unavailable"}


@app.get("/config")
async def get_config() -> dict:
    config = load_config()
    velaris = config.get("velaris", {})
    return {
        "git": velaris.get("git"), "auth": velaris.get("auth"),
        "database": velaris.get("database"), "cache": velaris.get("cache"),
        "events": velaris.get("events"), "search": velaris.get("search"),
        "ai_providers": velaris.get("ai", {}).get("providers", []),
        "channels": velaris.get("channels", []),
        "telephony": velaris.get("telephony", []),
        "integrations": velaris.get("integrations", []),
    }
