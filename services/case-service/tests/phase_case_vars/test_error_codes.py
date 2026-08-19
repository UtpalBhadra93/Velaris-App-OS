"""Case Variables — error catalog codes.

Most of this router's refusals are enum and regex shape checks and stay bare;
13 of 29 raise sites do. What is coded is the part a caller cannot deduce from
their own request — the reserved namespaces, the frozen-namespace rule, the
stricter-only sensitivity rule, and anything the variables service itself
refused. Those send the reader to namespace grants and definitions rather than
to the request body, which is the difference between a five-minute fix and an
afternoon.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import uuid

import pytest

from case_service.core import error_codes
from case_service.db.models import VariableNamespaceModel


def _code(resp) -> str | None:
    return (resp.json().get("error") or {}).get("code")


async def _namespace(session, *, name: str, sensitivity: str = "internal",
                     status: str = "active") -> VariableNamespaceModel:
    ns = VariableNamespaceModel(
        name=name, owner_type="connector", sensitivity=sensitivity, status=status,
    )
    session.add(ns)
    await session.commit()
    await session.refresh(ns)
    return ns


# ── reserved namespaces ───────────────────────────────────────────

class TestReserved:
    @pytest.mark.asyncio
    async def test_creating_a_reserved_namespace(self, client):
        r = await client.post("/api/v1/variables/namespaces",
                              json={"name": "velaris", "owner_type": "connector",
                                    "sensitivity": "internal"})
        assert r.status_code == 400
        assert _code(r) == error_codes.VAR_RESERVED_400

    @pytest.mark.asyncio
    async def test_every_reserved_name_is_refused_the_same_way(self, client):
        from case_service.case_vars.service import RESERVED_NAMESPACES
        for name in sorted(RESERVED_NAMESPACES):
            r = await client.post("/api/v1/variables/namespaces",
                                  json={"name": name, "owner_type": "connector",
                                        "sensitivity": "internal"})
            assert r.status_code == 400, name
            assert _code(r) == error_codes.VAR_RESERVED_400, name

    @pytest.mark.asyncio
    async def test_defining_a_velaris_variable_is_refused_as_reserved(self, client, session):
        """`velaris.*` is a projection over the case's native fields, not stored
        variables — defining one would create a second, diverging source of
        truth for the same value."""
        ns = await _namespace(session, name="velaris")
        r = await client.post(f"/api/v1/variables/case-types/{uuid.uuid4()}",
                              json={"namespace_id": str(ns.id), "name": "status",
                                    "var_type": "str"})
        assert r.status_code == 400
        assert _code(r) == error_codes.VAR_RESERVED_400


# ── namespace lifecycle ───────────────────────────────────────────

class TestNamespaceStatus:
    @pytest.mark.asyncio
    async def test_defining_into_a_frozen_namespace(self, client, session):
        """Nothing about the request is malformed — the target is closed for
        writes, which a bare 400 does not say."""
        ns = await _namespace(session, name="frozen_ns", status="frozen")
        r = await client.post(f"/api/v1/variables/case-types/{uuid.uuid4()}",
                              json={"namespace_id": str(ns.id), "name": "amount",
                                    "var_type": "int"})
        assert r.status_code == 400
        assert _code(r) == error_codes.VAR_NSSTATUS_400
        assert "frozen" in r.json()["detail"]

    @pytest.mark.asyncio
    async def test_an_active_namespace_accepts_the_same_definition(self, client, session):
        """The guard is about status, not about the payload."""
        ns = await _namespace(session, name="active_ns", status="active")
        r = await client.post(f"/api/v1/variables/case-types/{uuid.uuid4()}",
                              json={"namespace_id": str(ns.id), "name": "amount",
                                    "var_type": "int"})
        assert r.status_code == 201, r.text


# ── sensitivity ───────────────────────────────────────────────────

class TestSensitivity:
    @pytest.mark.asyncio
    async def test_a_looser_override_is_refused(self, client, session):
        """A variable may tighten its namespace's classification, never loosen
        it — redaction is driven from that classification, so one permissive
        variable would silently defeat it for the whole namespace."""
        ns = await _namespace(session, name="strict_ns", sensitivity="secret")
        r = await client.post(f"/api/v1/variables/case-types/{uuid.uuid4()}",
                              json={"namespace_id": str(ns.id), "name": "leaky",
                                    "var_type": "str",
                                    "sensitivity_override": "public"})
        assert r.status_code == 400
        assert _code(r) == error_codes.VAR_SENSITIVITY_400

    @pytest.mark.asyncio
    async def test_a_stricter_override_is_accepted(self, client, session):
        ns = await _namespace(session, name="loose_ns", sensitivity="internal")
        r = await client.post(f"/api/v1/variables/case-types/{uuid.uuid4()}",
                              json={"namespace_id": str(ns.id), "name": "tight",
                                    "var_type": "str",
                                    "sensitivity_override": "secret"})
        assert r.status_code == 201, r.text

    @pytest.mark.asyncio
    async def test_the_rule_and_the_enum_check_are_different_failures(self, client, session):
        """"not a sensitivity level at all" stays bare; "a level, but looser"
        gets the code. Merging them would tell someone who typed a valid level
        to check their spelling."""
        ns = await _namespace(session, name="enum_ns", sensitivity="secret")
        r = await client.post(f"/api/v1/variables/case-types/{uuid.uuid4()}",
                              json={"namespace_id": str(ns.id), "name": "x",
                                    "var_type": "str",
                                    "sensitivity_override": "not-a-level"})
        assert r.status_code == 400
        assert _code(r) != error_codes.VAR_SENSITIVITY_400


# ── lookup and collisions ─────────────────────────────────────────

class TestLookup:
    @pytest.mark.asyncio
    async def test_unknown_namespace_on_define(self, client):
        r = await client.post(f"/api/v1/variables/case-types/{uuid.uuid4()}",
                              json={"namespace_id": str(uuid.uuid4()), "name": "x",
                                    "var_type": "str"})
        assert r.status_code == 404
        assert _code(r) == error_codes.VAR_NOTFOUND_404

    @pytest.mark.asyncio
    async def test_unknown_namespace_on_patch(self, client):
        r = await client.patch(f"/api/v1/variables/namespaces/{uuid.uuid4()}",
                               json={"status": "frozen"})
        assert r.status_code == 404
        assert _code(r) == error_codes.VAR_NOTFOUND_404

    @pytest.mark.asyncio
    async def test_duplicate_namespace(self, client, session):
        await _namespace(session, name="taken_ns")
        r = await client.post("/api/v1/variables/namespaces",
                              json={"name": "taken_ns", "owner_type": "connector",
                                    "sensitivity": "internal"})
        assert r.status_code == 409
        assert _code(r) == error_codes.VAR_EXISTS_409

    @pytest.mark.asyncio
    async def test_duplicate_variable_definition(self, client, session):
        ns = await _namespace(session, name="dup_ns")
        ct = str(uuid.uuid4())
        body = {"namespace_id": str(ns.id), "name": "amount", "var_type": "int"}
        assert (await client.post(f"/api/v1/variables/case-types/{ct}", json=body)).status_code == 201
        r = await client.post(f"/api/v1/variables/case-types/{ct}", json=body)
        assert r.status_code == 409
        assert _code(r) == error_codes.VAR_EXISTS_409


# ── registry hygiene ──────────────────────────────────────────────

class TestRegistry:
    def test_every_var_code_is_registered(self):
        codes = {v for k, v in vars(error_codes).items()
                 if k.startswith("VAR_") and isinstance(v, str)}
        assert codes, "no VAR_* constants found"
        assert codes <= error_codes.KNOWN_CODES

    def test_the_router_raises_no_unregistered_code(self):
        import re
        from pathlib import Path
        src = (Path(__file__).resolve().parents[2] / "case_service" / "api"
               / "routers" / "case_variables.py")
        used = set(re.findall(r"coded\(\s*\d+,\s*([A-Z_0-9]+)", src.read_text()))
        assert used
        for name in used:
            assert getattr(error_codes, name) in error_codes.KNOWN_CODES, name

    def test_shape_validation_deliberately_stays_bare(self):
        """13 of 29 sites are enum/regex checks with no code, matching the
        earlier catalog phases. If this count moves, it was a decision — make
        it a deliberate one rather than drift."""
        from pathlib import Path
        src = (Path(__file__).resolve().parents[2] / "case_service" / "api"
               / "routers" / "case_variables.py").read_text()
        assert src.count("raise HTTPException(") == 13
        assert src.count("raise coded(") == 16
