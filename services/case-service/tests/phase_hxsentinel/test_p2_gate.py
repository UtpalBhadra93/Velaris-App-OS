"""HxSentinel P2a — the optional verify gate (opt-in per tenant).

Pins the properties that keep the gate from ever wedging the core workflow:
  * OFF by default — no config, no hold, advance proceeds;
  * a gating-severity finding opens exactly ONE hold, and re-evaluation never
    stacks or silently re-opens/clears it (decoupled from the finding);
  * an open hold blocks BOTH the auto-advance and the manual transition (409);
  * a human clearing the hold (with a note) releases the gate;
  * the check fails OPEN when the flag is off.

Setup is committed BEFORE any client call (the advance paths run through the
app), so these don't add the flush->client-call StaticPool shape.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import uuid

import pytest
import pytest_asyncio
from sqlalchemy import select

from case_service.db.models import CaseRiskConfigModel, CaseRiskHoldModel, CaseRiskSignalModel
from case_service.sentinel import engine as risk_engine
from case_service.sentinel import service as sentinel
from tests.conftest import create_case, deploy_case_type
from tests.phase_hxsentinel._seed import seed_case

RISK = "/api/v1/risk"
CASES = "/api/v1/cases"


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


async def _high_finding_case(session, client):
    """A case that fires the high-severity shared_doc_fraud rule."""
    ct = await deploy_case_type(client, name=f"G {uuid.uuid4().hex[:6]}")
    a = await create_case(client, ct["id"])
    b = await create_case(client, ct["id"])
    ca, cb = uuid.UUID(a["id"]), uuid.UUID(b["id"])
    for c in (ca, cb):
        session.add(CaseRiskSignalModel(tenant_id="default", case_id=c,
                    signal_type="doc_hash", signal_key="shared-doc", value={}))
    await sentinel.set_outcome(session, case_id=cb, tenant_id="default",
                               outcome="fraud", labelled_by="t", audit=False)
    await session.commit()      # durable setup — survives the shared-conn hazard
    return ct, ca, cb


async def _set_gate(session, severity):
    session.add(CaseRiskConfigModel(tenant_id="default",
                verify_gate_severity=severity, updated_by="admin"))
    await session.commit()


class TestGate:
    async def test_off_by_default_opens_no_hold(self, session, client):
        _ct, ca, _cb = await _high_finding_case(session, client)
        await risk_engine.evaluate_case(session, ca)
        holds = await risk_engine.list_holds(session, ca)
        assert holds == []

    async def test_gating_finding_opens_one_hold(self, session, client):
        _ct, ca, _cb = await _high_finding_case(session, client)
        await _set_gate(session, "high")
        await risk_engine.evaluate_case(session, ca)
        holds = await risk_engine.list_holds(session, ca)
        assert len(holds) == 1 and holds[0].severity == "high"

    async def test_below_threshold_opens_no_hold(self, session, client):
        """A finding below the gate threshold must not open a hold."""
        from case_service.db.models import CaseRiskRuleModel
        _ct, ca, _cb = await _high_finding_case(session, client)
        # Downgrade the rule so its finding fires at 'low', below the 'high' gate.
        session.add(CaseRiskRuleModel(tenant_id="default", rule_key="shared_doc_fraud",
                    severity="low", updated_by="admin"))
        await _set_gate(session, "high")            # commits pending setup
        await risk_engine.evaluate_case(session, ca)
        assert await risk_engine.list_holds(session, ca) == []

    async def test_reeval_does_not_stack_holds(self, session, client):
        _ct, ca, _cb = await _high_finding_case(session, client)
        await _set_gate(session, "high")
        await risk_engine.evaluate_case(session, ca)
        await risk_engine.evaluate_case(session, ca)
        rows = (await session.execute(
            select(CaseRiskHoldModel).where(CaseRiskHoldModel.case_id == ca))).scalars().all()
        assert len(rows) == 1

    async def test_reeval_does_not_reopen_a_cleared_hold(self, session, client):
        """A finding going stale/re-firing must not touch a human-cleared hold."""
        _ct, ca, _cb = await _high_finding_case(session, client)
        await _set_gate(session, "high")
        await risk_engine.evaluate_case(session, ca)
        hold = (await risk_engine.list_holds(session, ca))[0]
        await risk_engine.resolve_hold(session, hold.id, resolved_by="worker",
                                       status="cleared", note="verified in person")
        await risk_engine.evaluate_case(session, ca)          # re-fires
        assert await risk_engine.list_holds(session, ca, status="open") == []

    async def test_advance_blocked_true_only_with_open_hold(self, session, client):
        _ct, ca, _cb = await _high_finding_case(session, client)
        assert await risk_engine.advance_blocked(session, ca) is False
        await _set_gate(session, "high")
        await risk_engine.evaluate_case(session, ca)
        assert await risk_engine.advance_blocked(session, ca) is True

    async def test_gate_arms_on_advance_without_a_panel_view(self, session, client):
        """The gate must fire in the real flow — NOT only if a human opened the
        risk panel first. No explicit evaluate_case here; the advance arms it."""
        ct, ca, _cb = await _high_finding_case(session, client)
        await _set_gate(session, "high")
        await session.commit()                       # setup committed; NO evaluate
        stages = [{"id": "s1", "name": "Open", "order": 0},
                  {"id": "s2", "name": "Closed", "order": 1}]
        assert (await client.patch(f"/api/v1/case-types/{ct['id']}",
                json={"definition_json": {"stages": stages}})).status_code == 200
        blocked = await client.post(f"{CASES}/{ca}/stage", json={"target_stage_id": "s2"})
        assert blocked.status_code == 409, blocked.text

    async def test_manual_transition_409_then_ok_after_clear(self, session, client):
        ct, ca, _cb = await _high_finding_case(session, client)
        await _set_gate(session, "high")
        await session.commit()                       # commit BEFORE client calls; no pre-eval

        stages = [{"id": "s1", "name": "Open", "order": 0},
                  {"id": "s2", "name": "Closed", "order": 1}]
        r = await client.patch(f"/api/v1/case-types/{ct['id']}",
                               json={"definition_json": {"stages": stages}})
        assert r.status_code == 200, r.text

        # Real flow: the advance arms the gate and is refused (409). The 409
        # rolls its transaction back, so the worker opens the risk panel — which
        # persists the hold — then clears it, then the advance succeeds.
        blocked = await client.post(f"{CASES}/{ca}/stage", json={"target_stage_id": "s2"})
        assert blocked.status_code == 409, blocked.text

        # The panel loads findings first (which evaluates and persists the hold),
        # then the holds — mirror that order.
        assert (await client.get(f"{RISK}/cases/{ca}/findings")).status_code == 200
        holds = (await client.get(f"{RISK}/cases/{ca}/holds")).json()["holds"]
        assert holds, "opening the panel should surface the hold"
        res = await client.post(f"{RISK}/holds/{holds[0]['id']}/resolve",
                               json={"status": "cleared", "note": "checked ID in person"})
        assert res.status_code == 200

        ok = await client.post(f"{CASES}/{ca}/stage", json={"target_stage_id": "s2"})
        assert ok.status_code == 200, ok.text

    async def test_auto_advance_is_held_and_persists_the_hold(self, session, client):
        """The primary flow: completing a stage's steps must NOT auto-advance a
        gated case, and (unlike the 409 manual path) the hold persists."""
        from case_service.api.routers.cases import _auto_advance_if_complete
        from case_service.db.models import CaseInstanceModel, CaseStepCompletionModel
        ct, ca, _cb = await _high_finding_case(session, client)
        await _set_gate(session, "high")
        # Put the case in a stage whose single required step is already complete.
        ct_def = {"stages": [
            {"id": "s1", "order": 0, "steps": [
                {"id": "st1", "required": True, "step_type": "user_task"}]},
            {"id": "s2", "order": 1}]}
        await session.execute(CaseInstanceModel.__table__.update()
                              .where(CaseInstanceModel.id == ca).values(current_stage_id="s1"))
        session.add(CaseStepCompletionModel(case_id=ca, stage_id="s1", step_id="st1",
                                            status="completed"))
        await session.commit()

        advanced = await _auto_advance_if_complete(session, ca, "s1", ct_def)
        assert advanced is None                       # gate held the advance
        case = await session.get(CaseInstanceModel, ca)
        assert case.current_stage_id == "s1"          # did not move
        assert await risk_engine.list_holds(session, ca, status="open")  # hold persisted

    async def test_resolve_requires_a_note(self, session, client):
        _ct, ca, _cb = await _high_finding_case(session, client)
        await _set_gate(session, "high")
        await risk_engine.evaluate_case(session, ca)
        await session.commit()
        holds = (await client.get(f"{RISK}/cases/{ca}/holds")).json()["holds"]
        r = await client.post(f"{RISK}/holds/{holds[0]['id']}/resolve", json={"status": "cleared"})
        assert r.status_code == 422       # note is required

    async def test_config_endpoint_roundtrip(self, client):
        assert (await client.get(f"{RISK}/config")).json()["verify_gate_severity"] is None
        r = await client.put(f"{RISK}/config", json={"verify_gate_severity": "medium"})
        assert r.status_code == 200 and r.json()["verify_gate_severity"] == "medium"
        assert (await client.put(f"{RISK}/config",
                                json={"verify_gate_severity": "off"})).json()["verify_gate_severity"] is None
        assert (await client.put(f"{RISK}/config",
                                json={"verify_gate_severity": "bogus"})).status_code == 400

    async def test_gate_fires_for_a_real_uuid_tenant(self, session, client):
        """The config write-key (_tenant(user)) and the engine read-key
        (norm_tenant(case.tenant_id)) must resolve to the SAME string for a real
        UUID tenant — otherwise a configured gate silently never fires."""
        import uuid as _uuid
        tid = _uuid.uuid4()
        # This test calls evaluate_case/list_holds directly (no stage-advance
        # endpoint), so `ca` just needs to be a row evaluate_case can load — insert
        # it on the test session (no client). Fully client-free -> deterministic.
        # seed_case also seeds the case_type + tenants FK parents (enforced on PG/MySQL).
        cb = _uuid.uuid4()
        ca = await seed_case(session, tenant=tid)
        for c in (ca, cb):
            session.add(CaseRiskSignalModel(tenant_id=str(tid), case_id=c,
                        signal_type="doc_hash", signal_key="t-doc", value={}))
        await sentinel.set_outcome(session, case_id=cb, tenant_id=str(tid),
                                   outcome="fraud", labelled_by="t", audit=False)
        # Config keyed the way the PUT /config endpoint writes it: str(tenant).
        session.add(CaseRiskConfigModel(tenant_id=str(tid),
                    verify_gate_severity="high", updated_by="admin"))
        await session.commit()

        await risk_engine.evaluate_case(session, ca)
        assert await risk_engine.list_holds(session, ca, status="open"), \
            "gate must fire for a real UUID tenant (config key must match read key)"

    async def test_gate_off_when_flag_disabled(self, session, client):
        _ct, ca, _cb = await _high_finding_case(session, client)
        await _set_gate(session, "high")
        await risk_engine.evaluate_case(session, ca)
        from case_service.api.routers.releases import _ENABLED_VERSIONS
        _ENABLED_VERSIONS.pop("hxsentinel", None)     # flag off platform-wide
        assert await risk_engine.advance_blocked(session, ca) is False
