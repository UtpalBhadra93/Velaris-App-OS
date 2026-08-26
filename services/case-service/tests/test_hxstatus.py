"""HxStatus — public status page + SLA reporting (§8.8).

§8.8 calls this "trivial to build on existing observability". It was not:
`health_check_results` was an **empty schema** — 0 rows, 0 components — because
its only writer lived inside `GET /health/deep`, which nothing calls on a
schedule. Uptime over a window cannot be computed from a table nobody fills, so
the sampler had to be built first.

The tests that matter are the leak tests. This is the platform's only
unauthenticated read of internal state, and a health sample's `detail` field
carries **raw exception text** — hostnames, table names, driver messages. The
public payload is built from a whitelist precisely so that adding a column
cannot start leaking; these pin that.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest

from velaris_shared.db.models import HealthCheckResultModel
from case_service.integrations import health_sampler as hs

pytestmark = pytest.mark.asyncio

SECRET = "postgresql://velaris:hunter2@db.internal:5432/velaris"


@pytest.fixture(autouse=True)
def _clear_status_cache():
    """The public payload cache is module-level and process-wide.

    That is correct in production — it is what removes the DoS lever — but it
    would otherwise let one test serve another test's stale payload, making
    results depend on execution order.
    """
    from case_service.api.routers import status as status_router

    status_router._cache["payload"] = None
    status_router._cache["at"] = 0.0
    yield
    status_router._cache["payload"] = None
    status_router._cache["at"] = 0.0


async def _sample(session, component, status, *, detail=None, ago_days=0, latency=1.0):
    session.add(HealthCheckResultModel(
        id=uuid.uuid4(), component=component, status=status,
        latency_ms=latency, detail=detail or {},
        checked_at=datetime.now(timezone.utc) - timedelta(days=ago_days),
    ))
    await session.flush()


class TestSampler:
    async def test_sample_writes_a_row_per_component(self, session):
        rows = await hs.sample_once(session)
        names = {r["component"] for r in rows}
        assert names == {hs.DATABASE, hs.TELEMETRY}

    async def test_healthy_database_is_ok(self, session):
        rows = await hs.sample_once(session)
        db = next(r for r in rows if r["component"] == hs.DATABASE)
        assert db["status"] == hs.STATUS_OK
        assert db["detail"] == {}          # no diagnostics when nothing is wrong

    async def test_prune_removes_only_old_samples(self, session):
        await _sample(session, hs.DATABASE, "ok", ago_days=90)
        await _sample(session, hs.DATABASE, "ok", ago_days=1)
        removed = await hs.prune(session, older_than_days=35)
        assert removed >= 1

        from sqlalchemy import select
        left = (await session.execute(
            select(HealthCheckResultModel).where(
                HealthCheckResultModel.checked_at
                >= datetime.now(timezone.utc) - timedelta(days=2)
            )
        )).scalars().all()
        assert len(left) >= 1              # the recent one survived

    async def test_sampler_interval_has_a_floor(self):
        """A one-second sampler would be a self-inflicted load test."""
        s = hs.HealthSampler(None, interval_seconds=1)
        assert s._interval >= 10


class TestPublicPayloadLeaks:
    """The whole reason this endpoint is dangerous."""

    async def test_public_never_returns_detail(self, anon_client, session):
        await _sample(session, hs.DATABASE, "down", detail={"error": SECRET})
        await session.commit()

        r = await anon_client.get("/api/v1/status/public")
        assert r.status_code == 200, r.text
        body = r.text
        assert SECRET not in body
        assert "detail" not in body
        assert "hunter2" not in body and "db.internal" not in body

    async def test_public_component_fields_are_whitelisted(self, anon_client, session):
        await _sample(session, hs.DATABASE, "ok")
        await session.commit()

        r = await anon_client.get("/api/v1/status/public")
        for c in r.json()["components"]:
            assert set(c) == {"component", "status", "last_checked", "uptime"}

    async def test_public_reports_only_fixed_component_names(self, anon_client, session):
        """A status page must not double as an inventory of internals."""
        await _sample(session, "secret_internal_module", "ok")
        await session.commit()

        r = await anon_client.get("/api/v1/status/public")
        names = {c["component"] for c in r.json()["components"]}
        assert names == {hs.DATABASE, hs.TELEMETRY}
        assert "secret_internal_module" not in r.text

    async def test_public_requires_no_authentication(self, anon_client):
        r = await anon_client.get("/api/v1/status/public")
        assert r.status_code == 200

    async def test_internal_route_requires_admin(self, anon_client):
        r = await anon_client.get("/api/v1/status/internal")
        assert r.status_code in (401, 403)

    async def test_internal_route_does_expose_detail(self, client, session):
        """The privileged companion is where diagnostics are allowed."""
        await _sample(session, hs.DATABASE, "down", detail={"error": SECRET})
        await session.commit()

        r = await client.get("/api/v1/status/internal")
        assert r.status_code == 200, r.text
        assert SECRET in r.text


class TestUptimeHonesty:
    async def test_no_samples_reports_unknown_not_healthy(self, anon_client, session):
        """Claiming 100% uptime from zero measurements is a lie."""
        from sqlalchemy import delete

        await session.execute(delete(HealthCheckResultModel))
        await session.commit()

        body = (await anon_client.get("/api/v1/status/public")).json()
        assert body["status"] == "unknown"
        for c in body["components"]:
            assert c["status"] == "unknown"
            assert c["uptime"]["24h"] is None

    async def test_degraded_counts_against_uptime(self, session):
        from sqlalchemy import delete

        await session.execute(delete(HealthCheckResultModel))
        for s in ("ok", "ok", "degraded", "down"):
            await _sample(session, hs.DATABASE, s)
        await session.commit()

        from case_service.api.routers.status import _uptime_by_component
        out = await _uptime_by_component(session, days=1)
        assert out[hs.DATABASE]["uptime_pct"] == 50.0      # 2 of 4

    async def test_down_component_drives_overall_status(self, anon_client, session):
        from sqlalchemy import delete

        await session.execute(delete(HealthCheckResultModel))
        await _sample(session, hs.DATABASE, "down")
        await _sample(session, hs.TELEMETRY, "ok")
        await session.commit()

        body = (await anon_client.get("/api/v1/status/public")).json()
        assert body["status"] == "major_outage"


class TestCaching:
    async def test_public_response_is_cached(self, anon_client, session):
        """An unauthenticated aggregate query per request is a DoS lever."""
        from case_service.api.routers import status as status_router

        status_router._cache["payload"] = None
        await _sample(session, hs.DATABASE, "ok")
        await session.commit()

        first = (await anon_client.get("/api/v1/status/public")).json()
        assert status_router._cache["payload"] is not None
        second = (await anon_client.get("/api/v1/status/public")).json()
        assert first["generated_at"] == second["generated_at"]   # served from cache
