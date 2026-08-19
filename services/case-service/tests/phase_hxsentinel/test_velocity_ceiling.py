"""HxSentinel — the max_cases ceiling on identity_velocity.

`min_cases` is a floor: fire when a value recurs enough. The ceiling is the other
end. A digest sitting on hundreds of cases is not a person — it is a shared
support address or a template constant living in a variable whose name happened
to match an identity hint. Without a ceiling, every case carrying it fires, and
with the verify gate armed that holds all of them.

Set high (100) on purpose: the ceiling also hides a genuine ring sharing one
identity across more cases than that, so it must only catch values no plausible
individual could account for.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import uuid

import pytest

from case_service.db.models import CaseInstanceModel, CaseRiskSignalModel
from case_service.sentinel import engine as risk_engine
from case_service.sentinel import rules as rulecat
from tests.phase_hxsentinel._seed import seed_case

SHARED = "shared-identity-digest"


@pytest.fixture(autouse=True)
def _enable_hxsentinel():
    from case_service.api.routers.releases import _ENABLED_VERSIONS
    prior = _ENABLED_VERSIONS.get("hxsentinel")
    _ENABLED_VERSIONS["hxsentinel"] = "v1.9.0"
    yield
    if prior is None:
        _ENABLED_VERSIONS.pop("hxsentinel", None)
    else:
        _ENABLED_VERSIONS["hxsentinel"] = prior


async def _sharers(session, n, *, key=SHARED):
    """`n` cases all carrying the same identity digest. Returns the first."""
    first = None
    for _ in range(n):
        cid = await seed_case(session)
        session.add(CaseRiskSignalModel(
            tenant_id="default", case_id=cid, signal_type="identity_email",
            signal_key=key, hashed=True, value={"kind": "identity_email"}))
        first = first or cid
    await session.commit()
    return first


async def _run(session, case_id, **params):
    p = {"window_days": 30, "min_cases": 3, "max_cases": 100, **params}
    case = await session.get(CaseInstanceModel, case_id)
    return await risk_engine._rule_identity_velocity(session, case, "default", p)


class TestCeiling:
    async def test_the_default_is_one_hundred(self):
        rule = next(r for r in rulecat.RULES if r.key == "identity_velocity")
        assert rule.default_params["max_cases"] == 100

    async def test_normal_velocity_still_fires(self, session):
        """HAPPY PATH: the ceiling must not break the rule it guards."""
        first = await _sharers(session, 4)
        out = await _run(session, first)
        assert out is not None
        assert out["evidence"]["matches"][0]["case_count"] == 4

    async def test_a_value_over_the_ceiling_is_ignored(self, session):
        first = await _sharers(session, 8)
        assert await _run(session, first, max_cases=5) is None

    async def test_exactly_at_the_ceiling_still_fires(self, session):
        """The ceiling is `> max_cases`, not `>=` — 5 of 5 is still a finding."""
        first = await _sharers(session, 5)
        out = await _run(session, first, max_cases=5)
        assert out is not None

    async def test_a_real_identity_is_unaffected_by_a_common_one(self, session):
        """One over-common value must not suppress a genuine match on the same case."""
        first = await _sharers(session, 8)                      # over a ceiling of 5
        for _ in range(3):                                      # a real 4-case identity
            cid = await seed_case(session)
            session.add(CaseRiskSignalModel(
                tenant_id="default", case_id=cid, signal_type="identity_phone",
                signal_key="real-person", hashed=True, value={"kind": "identity_phone"}))
        session.add(CaseRiskSignalModel(
            tenant_id="default", case_id=first, signal_type="identity_phone",
            signal_key="real-person", hashed=True, value={"kind": "identity_phone"}))
        await session.commit()

        out = await _run(session, first, max_cases=5)
        assert out is not None
        types = {m["signal_type"] for m in out["evidence"]["matches"]}
        assert types == {"identity_phone"}, "only the plausible identity counts"

    async def test_the_finding_says_what_it_declined_to_count(self, session):
        """A silently narrowed finding is a misleading one."""
        first = await _sharers(session, 8)
        for _ in range(3):
            cid = await seed_case(session)
            session.add(CaseRiskSignalModel(
                tenant_id="default", case_id=cid, signal_type="identity_phone",
                signal_key="real-person", hashed=True, value={"kind": "identity_phone"}))
        session.add(CaseRiskSignalModel(
            tenant_id="default", case_id=first, signal_type="identity_phone",
            signal_key="real-person", hashed=True, value={"kind": "identity_phone"}))
        await session.commit()

        ev = (await _run(session, first, max_cases=5))["evidence"]
        assert ev["max_cases"] == 5
        assert ev["ignored_too_common"][0]["signal_type"] == "identity_email"
        assert ev["ignored_too_common"][0]["case_count"] == 8

    async def test_zero_disables_the_ceiling(self, session):
        """An escape hatch for a tenant that would rather see everything."""
        first = await _sharers(session, 8)
        assert await _run(session, first, max_cases=0) is not None


class TestRulesEditorContract:
    """What the Studio Rules tab depends on. It was API-only until 2026-08-15."""

    async def test_catalogue_exposes_defaults_and_effective_values(self, client):
        r = await client.get("/api/v1/risk/rules")
        assert r.status_code == 200, r.text
        rule = next(x for x in r.json()["rules"] if x["key"] == "identity_velocity")
        # The editor renders one input per default_params key and shows the
        # effective value from params — both have to be present.
        for field in ("display", "description", "default_severity",
                      "default_params", "enabled", "severity", "params"):
            assert field in rule, field
        assert rule["default_params"]["max_cases"] == 100
        assert rule["params"]["max_cases"] == 100

    async def test_params_round_trip(self, client):
        body = {"enabled": True, "severity": "high",
                "params": {"window_days": 30, "min_cases": 3, "max_cases": 250}}
        r = await client.put("/api/v1/risk/rules/identity_velocity", json=body)
        assert r.status_code == 200, r.text

        rule = next(x for x in (await client.get("/api/v1/risk/rules")).json()["rules"]
                    if x["key"] == "identity_velocity")
        assert rule["params"]["max_cases"] == 250
        assert rule["severity"] == "high"
        assert rule["default_params"]["max_cases"] == 100, "the default must not move"

    async def test_a_partial_params_write_resets_the_omitted_keys(self, client):
        """Why the editor always sends the whole object.

        The stored override is replaced wholesale while reads merge it over the
        code defaults, so omitting a key silently returns it to default.
        """
        await client.put("/api/v1/risk/rules/identity_velocity",
                         json={"params": {"min_cases": 9, "max_cases": 250}})
        await client.put("/api/v1/risk/rules/identity_velocity",
                         json={"params": {"max_cases": 250}})

        rule = next(x for x in (await client.get("/api/v1/risk/rules")).json()["rules"]
                    if x["key"] == "identity_velocity")
        assert rule["params"]["max_cases"] == 250
        assert rule["params"]["min_cases"] == 3, "omitted key fell back to the default"
