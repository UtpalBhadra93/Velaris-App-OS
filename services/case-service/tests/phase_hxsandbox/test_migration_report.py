"""Per-case-type WASM migration report (#28, §7.4).

`/rules/lint` answers "which rules need work". This answers "which case types
can move", which is the question that actually gates making WASM the default
for new rules — that switch is per case type, and one unmigrated rule blocks
the case type it belongs to.

The scope arithmetic is the part worth pinning: a **global** rule applies to
every case type (so one bad global blocks all of them), and a **stage**-scoped
rule belongs to the case type owning that stage. A report that counted only
`scope="case_type"` rules would read as ready when it is not.

Rules are inserted directly rather than through the API: `POST /rules` hard-
rejects anything non-CONFORMING, so the grandfathered rules this report exists
to find cannot be created through it.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import uuid

import pytest

from case_service.db.models import (
    CaseTypeModel,
    CaseTypeStageModel,
    RuleDefinitionModel,
)

pytestmark = pytest.mark.asyncio

CONFORMING = "a + 1"
NEEDS_MIGRATION = "[x for x in y]"
REJECTED = "a.b"


async def _case_type(session, name: str) -> CaseTypeModel:
    ct = CaseTypeModel(name=name, version="1.0.0", definition_json={"stages": []})
    session.add(ct)
    await session.flush()
    return ct


async def _stage(session, case_type_id, name="s1") -> CaseTypeStageModel:
    st = CaseTypeStageModel(
        case_type_id=case_type_id,
        stage_id=f"stg-{uuid.uuid4().hex[:6]}",
        name=name,
        stage_type="sequential",
        order=1,
    )
    session.add(st)
    await session.flush()
    return st


async def _rule(session, *, expression, scope="global", target=None, name=None):
    r = RuleDefinitionModel(
        name=name or f"r-{uuid.uuid4().hex[:6]}",
        version="1.0.0",
        rule_type="expression",
        scope=scope,
        scope_target_id=str(target) if target else None,
        definition_json={"expression": expression},
        enabled=True,
    )
    session.add(r)
    await session.flush()
    return r


async def _report(client):
    resp = await client.get("/api/v1/rules/lint/report")
    assert resp.status_code == 200, resp.text
    return resp.json()


def _find(report, case_type_id):
    return next(c for c in report["case_types"] if c["case_type_id"] == str(case_type_id))


class TestMigrationReport:
    async def test_clean_case_type_is_ready(self, session, client):
        ct = await _case_type(session, f"Clean {uuid.uuid4().hex[:6]}")
        await _rule(session, expression=CONFORMING, scope="case_type", target=ct.id)
        await session.commit()

        entry = _find(await _report(client), ct.id)
        assert entry["ready_for_wasm_default"] is True
        assert entry["blocking_rules"] == []

    async def test_unmigrated_rule_blocks_its_case_type(self, session, client):
        ct = await _case_type(session, f"Blocked {uuid.uuid4().hex[:6]}")
        await _rule(session, expression=NEEDS_MIGRATION, scope="case_type",
                    target=ct.id, name="legacy-comprehension")
        await session.commit()

        entry = _find(await _report(client), ct.id)
        assert entry["ready_for_wasm_default"] is False
        assert [b["name"] for b in entry["blocking_rules"]] == ["legacy-comprehension"]
        assert entry["summary"]["needs_migration"] == 1

    async def test_stage_scoped_rule_is_attributed_to_its_case_type(self, session, client):
        """The trap: counting only case_type-scoped rules would call this ready."""
        ct = await _case_type(session, f"Staged {uuid.uuid4().hex[:6]}")
        st = await _stage(session, ct.id)
        await _rule(session, expression=REJECTED, scope="stage", target=st.id)
        await session.commit()

        entry = _find(await _report(client), ct.id)
        assert entry["ready_for_wasm_default"] is False
        assert entry["summary"]["rejected"] == 1

    async def test_stage_rule_by_string_stage_id_is_also_attributed(self, session, client):
        """Stage rules may store the business key rather than the row UUID."""
        ct = await _case_type(session, f"StagedKey {uuid.uuid4().hex[:6]}")
        st = await _stage(session, ct.id)
        await _rule(session, expression=NEEDS_MIGRATION, scope="stage", target=st.stage_id)
        await session.commit()

        entry = _find(await _report(client), ct.id)
        assert entry["ready_for_wasm_default"] is False

    async def test_bad_global_rule_blocks_every_case_type(self, session, client):
        a = await _case_type(session, f"GA {uuid.uuid4().hex[:6]}")
        b = await _case_type(session, f"GB {uuid.uuid4().hex[:6]}")
        await _rule(session, expression=NEEDS_MIGRATION, scope="global", name="bad-global")
        await session.commit()

        report = await _report(client)
        for ct in (a, b):
            entry = _find(report, ct.id)
            assert entry["ready_for_wasm_default"] is False
            assert "bad-global" in [x["name"] for x in entry["blocking_rules"]]
        assert "bad-global" in [x["name"] for x in report["global_rules"]["blocking_rules"]]

    async def test_orphan_rule_is_reported_not_dropped(self, session, client):
        """A rule pointing at a deleted case type must not vanish from the report."""
        await _rule(session, expression=NEEDS_MIGRATION, scope="case_type",
                    target=uuid.uuid4(), name="orphan-rule")
        await session.commit()

        report = await _report(client)
        names = [x["name"] for x in report["unattributed_rules"]["blocking_rules"]]
        assert "orphan-rule" in names

    async def test_counts_and_ordering(self, session, client):
        """Blocked case types sort first — the report is a work list."""
        report = await _report(client)
        assert report["total_case_types"] == len(report["case_types"])
        assert report["ready_count"] == sum(
            1 for c in report["case_types"] if c["ready_for_wasm_default"]
        )
        readiness = [c["ready_for_wasm_default"] for c in report["case_types"]]
        assert readiness == sorted(readiness)

    async def test_route_is_not_swallowed_by_the_rule_id_converter(self):
        """`/lint/report` must be declared before `/{rule_id}`."""
        from case_service.main import app

        paths = [r.path for r in app.routes if getattr(r, "path", "").startswith("/api/v1/rules")]
        assert paths.index("/api/v1/rules/lint/report") < paths.index("/api/v1/rules/{rule_id}")
