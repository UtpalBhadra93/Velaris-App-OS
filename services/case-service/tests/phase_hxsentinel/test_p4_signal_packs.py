"""HxSentinel P4a — marketplace signal packs (declarative rules as content).

Pins: manifest validation REJECTS unsafe/malformed expressions (the sandbox
boundary), install stores the pack + its taxonomy, the in-core engine evaluates
enabled pack rules against the feature context and fires namespaced findings,
pack rules are tenant-scoped, and uninstall removes them.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

from case_service.db.models import (
    CaseInstanceModel,
    CaseOutcomeLabelModel,
    CaseRiskSignalModel,
)
from case_service.sentinel import engine as risk_engine
from case_service.sentinel import packs
from tests.conftest import create_case, deploy_case_type
from tests.phase_hxsentinel._seed import seed_case, seed_tenant

RISK = "/api/v1/risk"


@pytest.fixture(autouse=True)
def _enable_hxsentinel():
    from case_service.api.routers.releases import _ENABLED_VERSIONS
    prior = _ENABLED_VERSIONS.get("hxsentinel")
    _ENABLED_VERSIONS["hxsentinel"] = "v1.7.0"
    yield
    if prior is None:
        _ENABLED_VERSIONS.pop("hxsentinel", None)
    else:
        _ENABLED_VERSIONS["hxsentinel"] = prior


def _rule(key, when, severity="high"):
    return {"key": key, "when": when, "severity": severity, "title": "T", "message": "M"}


def _manifest(rules, taxonomy=None, pack_id="telco", version="1.0.0"):
    return {"id": pack_id, "name": "Telco Pack", "vertical": "telco",
            "version": version, "rules": rules, "taxonomy": taxonomy or []}


class TestManifestValidation:
    def test_valid_manifest_ok(self):
        packs.validate_manifest(_manifest([_rule("r1", "phone_velocity >= 3")]))

    def test_unsafe_dunder_rejected(self):
        with pytest.raises(packs.ManifestError):
            packs.validate_manifest(_manifest([_rule("r1", "amount.__class__ == 1")]))

    def test_import_call_rejected(self):
        with pytest.raises(packs.ManifestError):
            packs.validate_manifest(_manifest([_rule("r1", "__import__('os')")]))

    def test_attribute_access_rejected(self):
        with pytest.raises(packs.ManifestError):
            packs.validate_manifest(_manifest([_rule("r1", "amount.real > 1")]))

    def test_bad_severity_rejected(self):
        with pytest.raises(packs.ManifestError):
            packs.validate_manifest(_manifest([_rule("r1", "amount > 1", severity="critical")]))

    def test_empty_rules_rejected(self):
        with pytest.raises(packs.ManifestError):
            packs.validate_manifest(_manifest([]))

    def test_missing_when_rejected(self):
        with pytest.raises(packs.ManifestError):
            packs.validate_manifest(_manifest([{"key": "r1", "severity": "high",
                                                "title": "t", "message": "m"}]))


class TestInstall:
    async def test_install_list_and_taxonomy(self, session, client):
        m = _manifest([_rule("r1", "phone_velocity >= 3")],
                      taxonomy=[{"key": "sim_swap", "display": "SIM swap", "is_negative": True}])
        r = await client.post(f"{RISK}/packs/install", json={"manifest": m})
        assert r.status_code == 201, r.text
        assert r.json()["pack_id"] == "telco" and r.json()["rule_count"] == 1
        lst = await client.get(f"{RISK}/packs")
        assert any(p["pack_id"] == "telco" for p in lst.json()["packs"])
        # taxonomy extension landed as a tenant label
        lab = (await session.execute(select(CaseOutcomeLabelModel).where(
            CaseOutcomeLabelModel.key == "sim_swap"))).scalars().first()
        assert lab is not None and lab.is_negative is True

    async def test_install_rejects_unsafe(self, client):
        m = _manifest([_rule("r1", "__import__('os').system('x')")])
        r = await client.post(f"{RISK}/packs/install", json={"manifest": m})
        assert r.status_code == 400

    async def test_uninstall(self, client):
        await client.post(f"{RISK}/packs/install", json={"manifest": _manifest([_rule("r1", "amount > 1")])})
        assert (await client.delete(f"{RISK}/packs/telco")).status_code == 204
        assert (await client.get(f"{RISK}/packs")).json()["packs"] == []
        assert (await client.delete(f"{RISK}/packs/telco")).status_code == 404


class TestPackEvaluation:
    async def _real_case(self, client, session=None):
        # evaluate_case only needs a loadable row; insert directly (no client) so
        # the pack-evaluation tests don't inherit the shared-connection flake.
        return await seed_case(session)   # backing case row + case_type FK parent

    async def test_pack_rule_fires(self, session, client):
        m = _manifest([_rule("high_velocity", "phone_velocity >= 3", severity="high")])
        await client.post(f"{RISK}/packs/install", json={"manifest": m})
        ca = await self._real_case(client, session)
        # ca + two other cases share the same phone digest -> phone_velocity == 3
        for cid in (ca, uuid.uuid4(), uuid.uuid4()):
            session.add(CaseRiskSignalModel(tenant_id="default", case_id=cid,
                        signal_type="identity_phone", signal_key="digest-1", hashed=True, value={}))
        await session.commit()
        active = await risk_engine.evaluate_case(session, ca)
        keys = {f.rule_key for f in active}
        assert "pack:telco:high_velocity" in keys
        f = next(f for f in active if f.rule_key == "pack:telco:high_velocity")
        assert f.severity == "high" and f.evidence["source"] == "signal_pack"

    async def test_pack_rule_below_threshold_does_not_fire(self, session, client):
        await client.post(f"{RISK}/packs/install",
                          json={"manifest": _manifest([_rule("hv", "phone_velocity >= 5")])})
        ca = await self._real_case(client, session)
        for cid in (ca, uuid.uuid4()):
            session.add(CaseRiskSignalModel(tenant_id="default", case_id=cid,
                        signal_type="identity_phone", signal_key="digest-2", hashed=True, value={}))
        await session.commit()
        active = await risk_engine.evaluate_case(session, ca)
        assert not any(f.rule_key.startswith("pack:") for f in active)

    async def test_uninstall_retires_pack_findings_and_releases_hold(self, session, client):
        from case_service.db.models import (
            CaseRiskConfigModel, CaseRiskFindingModel, CaseRiskHoldModel)
        # Gate on 'high' so the pack finding opens a hold.
        session.add(CaseRiskConfigModel(tenant_id="default", verify_gate_severity="high",
                    updated_by="admin"))
        await client.post(f"{RISK}/packs/install",
                          json={"manifest": _manifest([_rule("hv", "phone_velocity >= 3", "high")])})
        ca = await self._real_case(client, session)
        for cid in (ca, uuid.uuid4(), uuid.uuid4()):
            session.add(CaseRiskSignalModel(tenant_id="default", case_id=cid,
                        signal_type="identity_phone", signal_key="d-un", hashed=True, value={}))
        await session.commit()
        await risk_engine.evaluate_case(session, ca)
        await session.commit()
        assert await risk_engine.list_holds(session, ca, status="open")   # armed

        # Uninstall must retire the pack's finding AND release its hold now,
        # not lazily on some later re-eval.
        assert await packs.uninstall_pack(session, "default", "telco") is True
        await session.commit()
        f = (await session.execute(select(CaseRiskFindingModel).where(
            CaseRiskFindingModel.case_id == ca,
            CaseRiskFindingModel.rule_key == "pack:telco:hv"))).scalar_one()
        assert f.status == "stale"
        assert await risk_engine.list_holds(session, ca, status="open") == []   # released
        h = (await session.execute(select(CaseRiskHoldModel).where(
            CaseRiskHoldModel.case_id == ca))).scalar_one()
        assert h.status == "cleared" and h.resolved_by == "system"

    async def test_evidence_minimises_features(self, session, client):
        # Rule references only phone_velocity -> amount must NOT be in evidence.
        await client.post(f"{RISK}/packs/install",
                          json={"manifest": _manifest([_rule("hv", "phone_velocity >= 3")])})
        ca = await self._real_case(client, session)
        for cid in (ca, uuid.uuid4(), uuid.uuid4()):
            session.add(CaseRiskSignalModel(tenant_id="default", case_id=cid,
                        signal_type="identity_phone", signal_key="d-min", hashed=True, value={}))
        await session.commit()
        active = await risk_engine.evaluate_case(session, ca)
        f = next(f for f in active if f.rule_key == "pack:telco:hv")
        assert "phone_velocity" in f.evidence["features"]
        assert "amount" not in f.evidence["features"]      # not referenced -> not stored

    async def test_pack_is_tenant_scoped(self, session, client):
        # Pack installed for the caller's tenant ("default"); a case in another
        # tenant must not see it.
        await client.post(f"{RISK}/packs/install",
                          json={"manifest": _manifest([_rule("hv", "phone_velocity >= 1")])})
        ca = await self._real_case(client, session)
        other_tenant = uuid.uuid4()
        await seed_tenant(session, other_tenant)   # tenants FK parent for the case's new tenant
        await session.execute(CaseInstanceModel.__table__.update()
                              .where(CaseInstanceModel.id == ca).values(tenant_id=other_tenant))
        session.add(CaseRiskSignalModel(tenant_id="other", case_id=ca,
                    signal_type="identity_phone", signal_key="d3", hashed=True, value={}))
        await session.commit()
        active = await risk_engine.evaluate_case(session, ca)
        assert not any(f.rule_key.startswith("pack:") for f in active)
