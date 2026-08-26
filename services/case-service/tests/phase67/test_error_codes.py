"""Error-catalog phase 6 — `VEL-DBM-*` codes on HxDBManager.

What is worth pinning in this router is not the happy grouping — it is the two
places where a code could quietly undo a security decision that was already
made carefully:

  * **`_require_table_access` has a three-way disclosure model.** Hidden tables
    answer 404 so their existence is never confirmed; superadmin-only tables
    answer 403 because admitting they exist is fine. A hidden table must
    therefore carry the *same* code as a table that does not exist at all.
  * **Four of these codes accompany breach events.** The abuse guard, the
    protected-write guard and the DDL/unknown-statement refusals all call
    `_fire_breach_event`, so the code is the join between what the admin saw
    and what HxShield recorded — it must not drift onto plain validation.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import uuid

import pytest
from httpx import AsyncClient

from velaris_shared.auth.dependencies import get_current_user
from velaris_shared.auth.models import ActiveAccessGroup, AuthenticatedUser
from velaris_shared.core import error_codes as ec
from case_service.main import app

DBM = "/api/v1/hxdbmanager"


def _code(response) -> str | None:
    return (response.json().get("error") or {}).get("code")


def _make_user(roles: list[str]) -> AuthenticatedUser:
    return AuthenticatedUser(
        user_id=f"test-{uuid.uuid4()}",
        roles=roles,
        active_access_group=ActiveAccessGroup(
            id=str(uuid.uuid4()), name="Test Group",
            portal_id=str(uuid.uuid4()), portal_type="admin",
            portal_name="Test Portal", modules=[], homepage="/",
            roles=roles, privileges=[],
            allowed_case_type_ids=["*"], allowed_queue_ids=["*"],
        ),
    )


@pytest.fixture
def as_admin():
    app.dependency_overrides[get_current_user] = lambda: _make_user(["admin"])
    yield
    app.dependency_overrides.pop(get_current_user, None)


@pytest.fixture
def as_superadmin():
    app.dependency_overrides[get_current_user] = lambda: _make_user(["admin", "superadmin"])
    yield
    app.dependency_overrides.pop(get_current_user, None)


@pytest.fixture
def as_worker():
    app.dependency_overrides[get_current_user] = lambda: _make_user(["case_worker"])
    yield
    app.dependency_overrides.pop(get_current_user, None)


@pytest.fixture
def flags_on():
    from case_service.api.routers import releases
    saved = dict(releases._ENABLED_VERSIONS)
    releases._ENABLED_VERSIONS["hxdbmanager"] = "v1.0.0"
    releases._ENABLED_VERSIONS["hxdbmanager_security"] = "v1.0.0"
    yield
    releases._ENABLED_VERSIONS.clear()
    releases._ENABLED_VERSIONS.update(saved)


@pytest.fixture
def flags_off():
    from case_service.api.routers import releases
    saved = dict(releases._ENABLED_VERSIONS)
    releases._ENABLED_VERSIONS.pop("hxdbmanager", None)
    yield
    releases._ENABLED_VERSIONS.clear()
    releases._ENABLED_VERSIONS.update(saved)


# ── The module hides rather than advertises ───────────────────────

class TestModuleGating:
    async def test_disabled_module_answers_404_not_403(
        self, client: AsyncClient, as_admin, flags_off,
    ):
        """The release flag conceals the module. A 403 would confirm it exists
        and is merely locked — the code must not undo that either."""
        r = await client.post(f"{DBM}/execute", json={"sql": "SELECT 1"})
        assert r.status_code == 404
        assert _code(r) == ec.DBM_DISABLED_404

    async def test_wrong_role_on_an_enabled_module_is_a_role_refusal(
        self, client: AsyncClient, as_worker, flags_on,
    ):
        r = await client.post(f"{DBM}/execute", json={"sql": "SELECT 1"})
        assert r.status_code == 403
        assert _code(r) == ec.DBM_ROLE_403

    def test_disabled_and_role_codes_are_distinct(self):
        assert ec.DBM_DISABLED_404 != ec.DBM_ROLE_403


# ── The table disclosure model survives being coded ───────────────

class TestTableConcealment:
    async def test_hidden_table_is_indistinguishable_from_an_unreal_one(
        self, client: AsyncClient, as_admin, flags_on,
    ):
        """The security property under test: a concealed table must not be
        tellable apart from a name that could never be a table. Both are
        refused by `_require_table_access` *before* any DB query, so this runs
        on SQLite; the message was already uniform, and this asserts the code
        did not reintroduce the difference.

        (Comparing against a valid-but-absent name needs a real introspector —
        that path is covered by the same single raise site.)"""
        from case_service.api.routers import hxdbmanager as mod

        hidden = next(iter(mod._HIDDEN_TABLES), None)
        if not hidden:
            pytest.skip("no hidden tables configured")

        concealed = await client.get(f"{DBM}/schema/{hidden}")
        unreal = await client.get(f"{DBM}/schema/not-a-valid-identifier")

        assert concealed.status_code == unreal.status_code == 404
        assert _code(concealed) == _code(unreal) == ec.DBM_TABLE_404
        assert concealed.json()["detail"].replace(hidden, "X") == \
               unreal.json()["detail"].replace("not-a-valid-identifier", "X")

    async def test_superadmin_only_table_admits_it_exists(
        self, client: AsyncClient, as_admin, flags_on,
    ):
        """The other side of the split: for these, admitting existence is fine,
        so the refusal is honest — a different code from the concealed set."""
        from case_service.api.routers import hxdbmanager as mod

        restricted = next(iter(mod._SUPERADMIN_ONLY_TABLES), None)
        if not restricted:
            pytest.skip("no superadmin-only tables configured")

        r = await client.get(f"{DBM}/schema/{restricted}")
        assert r.status_code == 403
        assert _code(r) == ec.DBM_TABLE_403

    async def test_an_invalid_identifier_is_refused_before_the_database(
        self, client: AsyncClient, as_admin, flags_on,
    ):
        """A NUL byte used to reach information_schema and surface as a 500."""
        r = await client.get(f"{DBM}/schema/not-a-valid-identifier!")
        assert r.status_code == 404
        assert _code(r) == ec.DBM_TABLE_404


# ── Codes that accompany a breach event ───────────────────────────

class TestSecurityRefusals:
    async def test_ddl_via_execute_is_a_statement_refusal(
        self, client: AsyncClient, as_admin, flags_on,
    ):
        r = await client.post(f"{DBM}/execute", json={"sql": "DROP TABLE cases"})
        assert r.status_code == 400
        assert _code(r) == ec.DBM_STMT_400

    async def test_unrecognised_statement_is_refused_by_allowlist(
        self, client: AsyncClient, as_admin, flags_on,
    ):
        """The classifier is an allowlist — anything it cannot positively
        identify is refused rather than passed through to the database."""
        r = await client.post(f"{DBM}/execute", json={"sql": "VACUUM FULL"})
        assert r.status_code == 400
        assert _code(r) == ec.DBM_STMT_400

    async def test_ddl_without_confirmation_is_its_own_code(
        self, client: AsyncClient, as_superadmin, flags_on,
    ):
        """Distinct from STMT: the statement is at the right endpoint and the
        fix is to supply a reason, not to move it."""
        r = await client.post(f"{DBM}/ddl", json={"sql": "CREATE TABLE t (id int)"})
        assert r.status_code == 400
        assert _code(r) == ec.DBM_CONFIRM_400

    async def test_non_ddl_at_the_ddl_endpoint_is_a_statement_refusal(
        self, client: AsyncClient, as_superadmin, flags_on,
    ):
        # The reason must clear the 5-char minimum, or CONFIRM fires first and
        # this asserts the wrong gate.
        r = await client.post(f"{DBM}/ddl",
                              json={"sql": "SELECT 1", "confirm": True,
                                    "reason": "checking endpoint separation"})
        assert r.status_code == 400
        assert _code(r) == ec.DBM_STMT_400

    async def test_oversized_sql_is_rejected_by_size_not_content(
        self, client: AsyncClient, as_admin, flags_on,
    ):
        r = await client.post(f"{DBM}/execute", json={"sql": "SELECT " + "1," * 30_000})
        assert r.status_code == 413
        assert _code(r) == ec.DBM_TOOLARGE_413


# ── AI down vs AI talking nonsense ────────────────────────────────

class TestAiSplit:
    async def test_unavailable_backend_is_503(
        self, client: AsyncClient, as_admin, flags_on, monkeypatch,
    ):
        async def _unavailable():
            return False
        # The router imports this *inside* the handler, so the patch has to land
        # on the factory module — patching the router's namespace does nothing
        # and the test sails past the gate into the introspector.
        monkeypatch.setattr("velaris_shared.llm.factory.check_ai_available",
                            _unavailable)

        r = await client.post(f"{DBM}/ai/sql", json={"question": "how many cases"})
        assert r.status_code == 503
        assert _code(r) == ec.DBM_AI_503

    def test_the_two_ai_codes_are_not_interchangeable(self):
        """503 = nothing answered (check /hxnexus/status, incl. the new
        `misconfigured` field). 502 = it answered with garbage. Collapsing them
        would send an operator to restart a backend that is running fine."""
        assert ec.DBM_AI_503 != ec.DBM_AI_502
        assert ec.DBM_PGSTAT_503 != ec.DBM_AI_503


# ── Registry hygiene ──────────────────────────────────────────────

class TestRegistry:
    def test_every_dbm_code_is_documented(self):
        declared = {v for k, v in vars(ec).items()
                    if k.startswith("DBM_") and isinstance(v, str)}
        assert len(declared) == 18
        assert declared <= ec.KNOWN_CODES

    def test_every_code_carries_its_http_status(self):
        """The registry-wide invariant that decided the preview naming: a code
        in a log line always says what the client actually received. The first
        cut used a suffix-less VEL-DBM-PREVIEW-STALE and this rejected it."""
        import re
        dbm = {v for k, v in vars(ec).items()
               if k.startswith("DBM_") and isinstance(v, str)}
        bad = [c for c in dbm if not re.fullmatch(r"VEL-DBM-[A-Z0-9-]+-\d{3}", c)]
        assert bad == []

    def test_bare_sites_were_left_bare(self):
        """3 uncoded raises: 'Empty query' twice and 'Unknown column'."""
        from pathlib import Path
        src = (Path(__file__).resolve().parents[2]
               / "case_service" / "api" / "routers" / "hxdbmanager.py")
        assert src.read_text().count("raise HTTPException") == 3
