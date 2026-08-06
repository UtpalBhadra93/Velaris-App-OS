"""HxSentinel P3a — the similarity layer (resembles_confirmed_cases).

Stronger than a single shared signal: fires only when a case shares MULTIPLE
signals with a confirmed-bad case, uses the neighbour's CURRENT outcome, stays
tenant-scoped, and cites which signals overlap.

Setup commits before the engine reads (shared StaticPool connection).

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import uuid

import pytest

from case_service.db.models import CaseRiskSignalModel
from case_service.sentinel import engine as risk_engine
from case_service.sentinel import service as sentinel
from tests.conftest import create_case, deploy_case_type
from tests.phase_hxsentinel._seed import seed_case


@pytest.fixture(autouse=True)
def _enable_hxsentinel():
    from case_service.api.routers.releases import _ENABLED_VERSIONS
    prior = _ENABLED_VERSIONS.get("hxsentinel")
    _ENABLED_VERSIONS["hxsentinel"] = "v1.3.0"
    yield
    if prior is None:
        _ENABLED_VERSIONS.pop("hxsentinel", None)
    else:
        _ENABLED_VERSIONS["hxsentinel"] = prior


async def _sig(session, case_id, stype, skey, tenant="default"):
    session.add(CaseRiskSignalModel(tenant_id=tenant, case_id=case_id,
                signal_type=stype, signal_key=skey, value={}))


async def _pair_sharing(session, client, shared: list[tuple[str, str]]):
    """Two cases sharing every (type, key) in `shared`; label the second.

    These tests call evaluate_case directly (no endpoint), so `ca` is inserted on
    the test session — evaluate_case only needs the row to load — and `cb` is a
    plain UUID neighbour. Fully client-free -> deterministic (no shared-conn churn).
    """
    ca = await seed_case(session)   # backing case row + case_type FK parent
    cb = uuid.uuid4()               # neighbour: referenced only by signals (no case FK)
    for stype, skey in shared:
        await _sig(session, ca, stype, skey)
        await _sig(session, cb, stype, skey)
    return ca, cb


class TestResembles:
    async def test_fires_on_two_shared_signals_with_a_bad_case(self, session, client):
        ca, cb = await _pair_sharing(session, client,
                                     [("doc_hash", "d1"), ("identity_phone", "p1")])
        await sentinel.set_outcome(session, case_id=cb, tenant_id="default",
                                   outcome="fraud", labelled_by="t", audit=False)
        await session.commit()
        active = await risk_engine.evaluate_case(session, ca)
        f = [x for x in active if x.rule_key == "resembles_confirmed_cases"]
        assert f, "two shared signals with a fraud case should resemble"
        nb = f[0].evidence["neighbours"][0]
        assert nb["shared_count"] == 2 and nb["outcome"] == "fraud"

    async def test_single_shared_signal_does_not_resemble(self, session, client):
        ca, cb = await _pair_sharing(session, client, [("doc_hash", "only1")])
        await sentinel.set_outcome(session, case_id=cb, tenant_id="default",
                                   outcome="fraud", labelled_by="t", audit=False)
        await session.commit()
        active = await risk_engine.evaluate_case(session, ca)
        assert "resembles_confirmed_cases" not in {x.rule_key for x in active}

    async def test_latest_outcome_wins_clean_neighbour_does_not_resemble(self, session, client):
        ca, cb = await _pair_sharing(session, client,
                                     [("doc_hash", "d2"), ("identity_email", "e2")])
        await sentinel.set_outcome(session, case_id=cb, tenant_id="default",
                                   outcome="fraud", labelled_by="t", audit=False)
        await sentinel.set_outcome(session, case_id=cb, tenant_id="default",
                                   outcome="clean", labelled_by="t", audit=False)
        await session.commit()
        active = await risk_engine.evaluate_case(session, ca)
        assert "resembles_confirmed_cases" not in {x.rule_key for x in active}

    async def test_unlabelled_neighbour_does_not_resemble(self, session, client):
        ca, _cb = await _pair_sharing(session, client,
                                      [("doc_hash", "d3"), ("identity_phone", "p3")])
        await session.commit()
        active = await risk_engine.evaluate_case(session, ca)
        assert "resembles_confirmed_cases" not in {x.rule_key for x in active}

    async def test_tenant_isolation(self, session, client):
        ca, cb = await _pair_sharing(session, client,
                                     [("doc_hash", "d4"), ("identity_phone", "p4")])
        # Move the bad neighbour's signals to another tenant.
        await session.execute(CaseRiskSignalModel.__table__.update()
                              .where(CaseRiskSignalModel.case_id == cb)
                              .values(tenant_id="other"))
        await sentinel.set_outcome(session, case_id=cb, tenant_id="other",
                                   outcome="fraud", labelled_by="t", audit=False)
        await session.commit()
        active = await risk_engine.evaluate_case(session, ca)
        assert "resembles_confirmed_cases" not in {x.rule_key for x in active}

    async def test_configurable_min_shared(self, session, client):
        from case_service.db.models import CaseRiskRuleModel
        ca, cb = await _pair_sharing(session, client,
                                     [("doc_hash", "d5"), ("identity_phone", "p5")])
        await sentinel.set_outcome(session, case_id=cb, tenant_id="default",
                                   outcome="fraud", labelled_by="t", audit=False)
        # Raise the threshold to 3 shared signals -> 2 shared no longer resembles.
        session.add(CaseRiskRuleModel(tenant_id="default", rule_key="resembles_confirmed_cases",
                    params={"min_shared": 3}, updated_by="admin"))
        await session.commit()
        active = await risk_engine.evaluate_case(session, ca)
        assert "resembles_confirmed_cases" not in {x.rule_key for x in active}
