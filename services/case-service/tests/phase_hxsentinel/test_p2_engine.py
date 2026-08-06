"""HxSentinel P2a — the deterministic rule engine, findings, and rule config.

These pin the correctness invariants that make a finding trustworthy:
  * a match uses the OTHER case's CURRENT outcome (a fraud->clean re-label stops
    the fraud rules firing);
  * matching is tenant-scoped and never spans tenants;
  * a case never matches itself, and an unlabelled case is not evidence;
  * amount anomaly skips honestly when there is too little history;
  * re-evaluation is idempotent and retires a rule that stops firing.

The engine is driven directly (no client sub-request) so these tests don't add
the flush->client-call StaticPool shape to the suite; endpoint tests commit
their setup BEFORE any client call.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import uuid

import pytest
import pytest_asyncio
from sqlalchemy import select

from case_service.db.models import (
    CaseInstanceVariableModel,
    CaseRiskFindingModel,
    CaseRiskSignalModel,
)
from case_service.sentinel import engine as risk_engine
from case_service.sentinel import service as sentinel
from tests.conftest import create_case, deploy_case_type

RISK = "/api/v1/risk"


@pytest.fixture(autouse=True)
def _enable_hxsentinel():
    from case_service.api.routers.releases import _ENABLED_VERSIONS
    prior = _ENABLED_VERSIONS.get("hxsentinel")
    _ENABLED_VERSIONS["hxsentinel"] = "v1.1.0"
    yield
    if prior is None:
        _ENABLED_VERSIONS.pop("hxsentinel", None)
    else:
        _ENABLED_VERSIONS["hxsentinel"] = prior


# Setup writes COMMIT (not just flush): this suite shares one StaticPool
# connection with the client sub-requests used to create cases, so a
# flushed-but-uncommitted setup row can be rolled back out from under the test.
# Committing makes the ground truth durable before the engine reads it.
async def _add_signal(session, case_id, stype, skey, *, tenant="default", hashed=False):
    session.add(CaseRiskSignalModel(
        tenant_id=tenant, case_id=case_id, signal_type=stype,
        signal_key=skey, hashed=hashed, value={"kind": stype}))
    await session.commit()


async def _label(session, case_id, outcome):
    await sentinel.set_outcome(
        session, case_id=case_id, tenant_id="default", outcome=outcome,
        labelled_by="tester", audit=False)
    await session.commit()


async def _two_cases_sharing(session, client, stype, skey):
    ct = await deploy_case_type(client, name=f"S {uuid.uuid4().hex[:6]}")
    a = await create_case(client, ct["id"])
    b = await create_case(client, ct["id"])
    ca, cb = uuid.UUID(a["id"]), uuid.UUID(b["id"])
    await _add_signal(session, ca, stype, skey)
    await _add_signal(session, cb, stype, skey)
    return ct, ca, cb


# ── shared_doc_fraud ─────────────────────────────────────────────────────────


class TestSharedDocFraud:
    async def test_fires_when_shared_doc_on_confirmed_fraud_case(self, session, client):
        _ct, ca, cb = await _two_cases_sharing(session, client, "doc_hash", "sha-abc")
        await _label(session, cb, "fraud")
        active = await risk_engine.evaluate_case(session, ca)
        keys = {f.rule_key for f in active}
        assert "shared_doc_fraud" in keys
        finding = next(f for f in active if f.rule_key == "shared_doc_fraud")
        assert finding.severity == "high"
        assert finding.evidence["matched_cases"][0]["outcome"] == "fraud"

    async def test_latest_outcome_wins_fraud_then_clean_does_not_fire(self, session, client):
        """A fraud->clean re-label must stop tripping the fraud rule."""
        _ct, ca, cb = await _two_cases_sharing(session, client, "doc_hash", "sha-relabel")
        await _label(session, cb, "fraud")
        await _label(session, cb, "clean")     # append-only: latest wins
        active = await risk_engine.evaluate_case(session, ca)
        assert "shared_doc_fraud" not in {f.rule_key for f in active}

    async def test_unlabelled_match_is_not_evidence(self, session, client):
        _ct, ca, _cb = await _two_cases_sharing(session, client, "doc_hash", "sha-unl")
        active = await risk_engine.evaluate_case(session, ca)
        assert "shared_doc_fraud" not in {f.rule_key for f in active}

    async def test_never_matches_itself(self, session, client):
        ct = await deploy_case_type(client, name=f"S {uuid.uuid4().hex[:6]}")
        a = await create_case(client, ct["id"])
        ca = uuid.UUID(a["id"])
        await _add_signal(session, ca, "doc_hash", "sha-solo")
        await _label(session, ca, "fraud")      # this very case is fraud
        active = await risk_engine.evaluate_case(session, ca)
        assert "shared_doc_fraud" not in {f.rule_key for f in active}

    async def test_tenant_isolation(self, session, client):
        _ct, ca, cb = await _two_cases_sharing(session, client, "doc_hash", "sha-ten")
        # Same digest, but the fraud case is stored under a DIFFERENT tenant.
        await session.execute(
            CaseRiskSignalModel.__table__.update()
            .where(CaseRiskSignalModel.case_id == cb)
            .values(tenant_id="other-tenant"))
        await _label(session, cb, "fraud")
        await session.commit()
        active = await risk_engine.evaluate_case(session, ca)
        assert "shared_doc_fraud" not in {f.rule_key for f in active}


# ── identity_velocity ────────────────────────────────────────────────────────


class TestIdentityVelocity:
    async def test_fires_at_threshold(self, session, client):
        ct = await deploy_case_type(client, name=f"V {uuid.uuid4().hex[:6]}")
        cases = [uuid.UUID((await create_case(client, ct["id"]))["id"]) for _ in range(3)]
        for c in cases:
            await _add_signal(session, c, "identity_phone", "digest-XYZ", hashed=True)
        active = await risk_engine.evaluate_case(session, cases[0])
        vel = [f for f in active if f.rule_key == "identity_velocity"]
        assert vel and vel[0].evidence["matches"][0]["case_count"] == 3

    async def test_below_threshold_does_not_fire(self, session, client):
        ct = await deploy_case_type(client, name=f"V {uuid.uuid4().hex[:6]}")
        cases = [uuid.UUID((await create_case(client, ct["id"]))["id"]) for _ in range(2)]
        for c in cases:
            await _add_signal(session, c, "identity_phone", "digest-LOW", hashed=True)
        active = await risk_engine.evaluate_case(session, cases[0])
        assert "identity_velocity" not in {f.rule_key for f in active}


# ── amount_anomaly ───────────────────────────────────────────────────────────


class TestAmountAnomaly:
    async def _amount_case(self, session, client, ct_id, value):
        c = uuid.UUID((await create_case(client, ct_id))["id"])
        session.add(CaseInstanceVariableModel(
            case_id=c, full_key="claim.amount", value_num=float(value), written_by="t"))
        await session.commit()
        return c

    async def test_outlier_fires(self, session, client):
        ct = await deploy_case_type(client, name=f"A {uuid.uuid4().hex[:6]}")
        # A baseline WITH spread (std > 0) so a z-score is defined.
        for v in (90, 95, 98, 100, 102, 105, 108, 110, 96, 104):
            await self._amount_case(session, client, ct["id"], v)
        target = await self._amount_case(session, client, ct["id"], 100000.0)
        active = await risk_engine.evaluate_case(session, target)
        anom = [f for f in active if f.rule_key == "amount_anomaly"]
        assert anom, "outlier should fire"
        assert anom[0].evidence["metric"]["full_key"] == "claim.amount"

    async def test_too_little_history_skips(self, session, client):
        ct = await deploy_case_type(client, name=f"A {uuid.uuid4().hex[:6]}")
        for _ in range(3):    # below default min_samples=8
            await self._amount_case(session, client, ct["id"], 100.0)
        target = await self._amount_case(session, client, ct["id"], 100000.0)
        active = await risk_engine.evaluate_case(session, target)
        assert "amount_anomaly" not in {f.rule_key for f in active}

    async def test_constant_history_no_z(self, session, client):
        """std == 0 -> z undefined -> honest skip, not a divide-by-zero."""
        ct = await deploy_case_type(client, name=f"A {uuid.uuid4().hex[:6]}")
        for _ in range(10):
            await self._amount_case(session, client, ct["id"], 100.0)
        target = await self._amount_case(session, client, ct["id"], 100.0)
        active = await risk_engine.evaluate_case(session, target)
        assert "amount_anomaly" not in {f.rule_key for f in active}


# ── idempotency + retirement ─────────────────────────────────────────────────


class TestIdempotencyAndConfig:
    async def test_reeval_idempotent_no_duplicate(self, session, client):
        _ct, ca, cb = await _two_cases_sharing(session, client, "doc_hash", "sha-idem")
        await _label(session, cb, "fraud")
        await risk_engine.evaluate_case(session, ca)
        await risk_engine.evaluate_case(session, ca)
        rows = (await session.execute(
            select(CaseRiskFindingModel).where(
                CaseRiskFindingModel.case_id == ca,
                CaseRiskFindingModel.rule_key == "shared_doc_fraud"))).scalars().all()
        assert len(rows) == 1

    async def test_rule_that_stops_firing_goes_stale(self, session, client):
        _ct, ca, cb = await _two_cases_sharing(session, client, "doc_hash", "sha-stale")
        await _label(session, cb, "fraud")
        await risk_engine.evaluate_case(session, ca)
        await _label(session, cb, "clean")      # no longer bad
        active = await risk_engine.evaluate_case(session, ca)
        assert "shared_doc_fraud" not in {f.rule_key for f in active}
        row = (await session.execute(
            select(CaseRiskFindingModel).where(
                CaseRiskFindingModel.case_id == ca,
                CaseRiskFindingModel.rule_key == "shared_doc_fraud"))).scalar_one()
        assert row.status == "stale"          # kept, so P2b scores never orphan

    async def test_disabled_rule_does_not_fire(self, session, client):
        from case_service.db.models import CaseRiskRuleModel
        _ct, ca, cb = await _two_cases_sharing(session, client, "doc_hash", "sha-off")
        await _label(session, cb, "fraud")
        session.add(CaseRiskRuleModel(
            tenant_id="default", rule_key="shared_doc_fraud",
            enabled=False, updated_by="admin"))
        await session.commit()
        active = await risk_engine.evaluate_case(session, ca)
        assert "shared_doc_fraud" not in {f.rule_key for f in active}


# ── Endpoints ────────────────────────────────────────────────────────────────


class TestEndpoints:
    async def test_findings_endpoint_evaluates_and_lists(self, session, client):
        _ct, ca, cb = await _two_cases_sharing(session, client, "doc_hash", "sha-ep")
        await _label(session, cb, "fraud")
        await session.commit()               # commit BEFORE the client call
        r = await client.get(f"{RISK}/cases/{ca}/findings")
        assert r.status_code == 200, r.text
        keys = {f["rule_key"] for f in r.json()["findings"]}
        assert "shared_doc_fraud" in keys

    async def test_rules_catalogue_and_override(self, client):
        r = await client.get(f"{RISK}/rules")
        assert r.status_code == 200
        assert {"shared_doc_fraud", "identity_velocity", "amount_anomaly",
                "shared_signal_negative_outcome"} <= {x["key"] for x in r.json()["rules"]}

        r = await client.put(f"{RISK}/rules/amount_anomaly",
                             json={"enabled": False, "severity": "medium"})
        assert r.status_code == 200
        assert r.json()["enabled"] is False and r.json()["severity"] == "medium"

    async def test_rule_override_rejects_bad_severity_and_key(self, client):
        assert (await client.put(f"{RISK}/rules/amount_anomaly",
                                json={"severity": "critical"})).status_code == 400
        assert (await client.put(f"{RISK}/rules/nope", json={"enabled": True})).status_code == 404
