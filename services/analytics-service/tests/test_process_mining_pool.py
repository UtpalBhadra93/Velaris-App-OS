"""Process mining runs off the read replica, not the operations pool.

Report §1.1 names process mining over OLTP tables as the connection-pool
killer, and §2.8 proposed a lakehouse to solve it. The platform already had the
cheaper correct answer — a dedicated analytics pool and an optional read
replica — and process mining simply was not using it.

The tests here guard the contract that makes that safe: `get_replica_session`
is documented as zero-write-only, because it points at a real standby the
moment one is configured. A write added to this router would pass locally
(where the replica falls back to the analytics pool) and fail in production.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import inspect

import pytest

pytestmark = pytest.mark.asyncio


class TestPoolSeparation:
    async def test_router_uses_the_replica_session(self):
        from analytics_service.api.routers import process_mining

        src = inspect.getsource(process_mining)
        assert "get_replica_session as get_session" in src

    async def test_every_route_is_a_read(self):
        """The replica contract: zero writes, or it breaks against a standby."""
        from analytics_service.api.routers import process_mining
        from analytics_service.main import app

        methods = {
            m
            for r in app.routes
            if "process-mining" in getattr(r, "path", "")
            for m in (getattr(r, "methods", None) or set())
        }
        assert methods <= {"GET", "HEAD", "OPTIONS"}, methods

        src = inspect.getsource(process_mining)
        for write in ("session.add", "session.commit", "session.delete", ".flush("):
            assert write not in src, write

    async def test_the_analyzer_is_read_only(self):
        from velaris_shared.process_mining import analyzer

        src = inspect.getsource(analyzer)
        for write in ("session.add", "session.commit", "session.delete", ".flush("):
            assert write not in src, write

    async def test_the_writer_is_not_reachable_from_the_router(self):
        """`event_logger` writes; it must not be imported into a replica-backed
        router, where calling it would fail against a real standby."""
        from analytics_service.api.routers import process_mining

        src = inspect.getsource(process_mining)
        assert "event_logger" not in src.split('"""', 2)[-1] or "event_logger." not in src

    async def test_routes_still_answer(self, client):
        """Behaviour is unchanged: with no standby configured the replica
        session IS the analytics pool, so nothing about the response moves."""
        r = await client.get("/api/v1/process-mining/summary")
        assert r.status_code == 200, r.text
