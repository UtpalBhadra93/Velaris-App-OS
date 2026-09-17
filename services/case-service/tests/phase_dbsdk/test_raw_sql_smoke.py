"""DB SDK Phase 1b — raw-SQL conversion smokes for permissions / hxguard tuples /
user-directory. Each drives the converted path on whatever dialect the harness URL
selects (SQLite default; MySQL/Postgres via VELARIS_TEST_DATABASE_URL), so the
ORM/Core rewrites are proven on every backend — these sites were invisible to the
schema tests because they only fire on live endpoints/helpers.
"""
from __future__ import annotations

import uuid

import pytest

from tests.conftest import ADMIN_HEADERS


# ── permissions.py: system_config get + get-or-create (was SELECT key= / ON CONFLICT) ──
@pytest.mark.asyncio
async def test_permissions_get_default_then_upsert(client):
    # Absent row → defaults (the get_or_none branch).
    first = await client.get("/api/v1/admin/permissions", headers=ADMIN_HEADERS)
    assert first.status_code == 200, first.text
    assert "/admin" in first.json()["permissions"]

    # PUT (insert branch), then PUT again (update branch) — both must persist.
    #
    # **The stored map does not REPLACE the defaults; the defaults merge under
    # it.** This assertion was `== payload` until 2026-09-07, which encoded the
    # bug rather than the behaviour: a stored row replaced `_DEFAULTS` outright,
    # so on any install that had ever saved this screen, every route added from
    # that point on was permanently invisible — `getRouteRoles` returned `[]`
    # and `RequireRole` denied everybody while the route, the nav entry and the
    # sitemap all looked correct. It was latent for every FUTURE route, not just
    # the one that exposed it. So what is pinned here is both halves: an
    # operator's own choice wins, and a route they never touched keeps its
    # default.
    for payload in ({"/widget": ["admin"]}, {"/widget": ["admin", "designer"]}):
        put = await client.put("/api/v1/admin/permissions",
                               json={"permissions": payload}, headers=ADMIN_HEADERS)
        assert put.status_code == 200, put.text
        got = await client.get("/api/v1/admin/permissions", headers=ADMIN_HEADERS)
        assert got.status_code == 200, got.text
        stored = got.json()["permissions"]
        # The operator's choice wins…
        assert stored["/widget"] == payload["/widget"]
        # …and a route they never named still has its default.
        assert stored["/admin"] == first.json()["permissions"]["/admin"]
        assert set(first.json()["permissions"]) <= set(stored)


# ── hxguard/tuples.py: portable on-conflict-do-nothing upsert ──────────────────
@pytest.mark.asyncio
async def test_write_tuple_is_idempotent(session):
    from velaris_shared.hxguard.tuples import write_tuple, list_tuples

    obj_id = uuid.uuid4()
    kw = dict(object_type="case", object_id=obj_id, relation="viewer",
              subject_type="user", subject_id="alice")

    # Writing the same tuple twice must NOT raise and must leave exactly one row.
    # This asserts BOTH the dialect-portable upsert AND that uq_hxguard_tuple
    # actually exists on the metadata-built schema (a missing constraint would
    # silently create two rows and still not error).
    await write_tuple(session, **kw)
    await write_tuple(session, **kw)
    await session.commit()

    rows = await list_tuples(session, object_type="case", object_id=obj_id)
    assert len(rows) == 1, f"expected exactly one tuple, got {len(rows)}"


# ── user_directory.py: access_group_id filter (was JSONB @>, now Python filter) ──
@pytest.mark.asyncio
async def test_directory_access_group_filter(client, session):
    from velaris_shared.db.models import UserDirectoryModel

    gid = str(uuid.uuid4())
    other = str(uuid.uuid4())
    session.add_all([
        UserDirectoryModel(user_id=f"in_{uuid.uuid4().hex[:6]}", email="in@x.test",
                           display_name="In Group", access_group_ids=[gid], is_active=True),
        UserDirectoryModel(user_id=f"out_{uuid.uuid4().hex[:6]}", email="out@x.test",
                           display_name="Out Group", access_group_ids=[other], is_active=True),
    ])
    await session.commit()

    resp = await client.get(f"/api/v1/user-directory?access_group_id={gid}", headers=ADMIN_HEADERS)
    assert resp.status_code == 200, resp.text
    entries = resp.json()
    assert len(entries) == 1, entries
    assert gid in entries[0]["access_group_ids"]
