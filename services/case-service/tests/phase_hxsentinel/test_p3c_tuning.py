"""HxSentinel P3c-1 — score-driven rule tuning (deterministic).

Pins: reliability-weighted aggregation (disputed excluded), the min-reviews floor,
low->deactivate / high->raise-severity proposals, idempotent generation, and that
approval (only) applies the change to case_risk_rules — never silent.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

from case_service.db.models import (
    CaseRiskFeedbackModel,
    CaseRiskFindingModel,
    CaseRiskRuleModel,
    CaseRiskRuleProposalModel,
)
from case_service.sentinel import tuning
from tests.phase_hxsentinel._seed import seed_case

RISK = "/api/v1/risk"


@pytest.fixture(autouse=True)
def _enable_hxsentinel():
    from case_service.api.routers.releases import _ENABLED_VERSIONS
    prior = _ENABLED_VERSIONS.get("hxsentinel")
    _ENABLED_VERSIONS["hxsentinel"] = "v1.5.0"
    yield
    if prior is None:
        _ENABLED_VERSIONS.pop("hxsentinel", None)
    else:
        _ENABLED_VERSIONS["hxsentinel"] = prior


async def _score_rule(session, rule_key, scores, *, tenant="default"):
    """Create `len(scores)` findings for `rule_key`, one score each (distinct
    reviewers), cases unlabelled (reviewer reliability unknown -> neutral 0.5).

    No client / real cases needed: findings.case_id has no FK and tuning is
    tenant-scoped (never joins case_instances) — using plain UUIDs keeps this
    setup entirely on the test session, immune to the shared-connection churn.
    """
    for i, sc in enumerate(scores):
        ca = await seed_case(session)   # backing case so dispute_score audit-log FK holds
        f = CaseRiskFindingModel(tenant_id=tenant, case_id=ca, rule_key=rule_key,
                                 severity="medium", title="t", message="m", evidence={})
        session.add(f)
        await session.commit()
        session.add(CaseRiskFeedbackModel(tenant_id=tenant, case_id=ca, finding_id=f.id,
                    reviewer=f"rev{i}", score=sc))
    await session.commit()


class TestAnalysis:
    async def test_weighted_avg_and_reviews(self, session, client):
        await _score_rule(session, "amount_anomaly", [1, 1, 2, 2, 2])
        a = await tuning.analyze_rule_scores(session, "default")
        assert a["amount_anomaly"]["reviews"] == 5
        assert a["amount_anomaly"]["weighted_avg"] == 1.6

    async def test_disputed_excluded(self, session, client):
        await _score_rule(session, "amount_anomaly", [5, 5])
        fb = (await session.execute(select(CaseRiskFeedbackModel).limit(1))).scalar_one()
        from case_service.sentinel import oversight
        await oversight.dispute_score(session, feedback_id=fb.id, tenant="default",
                                      disputed_by="boss", note="x")
        await session.commit()
        a = await tuning.analyze_rule_scores(session, "default")
        assert a["amount_anomaly"]["reviews"] == 1   # the disputed one is gone


class TestProposals:
    async def test_low_scores_propose_deactivate(self, session, client):
        await _score_rule(session, "amount_anomaly", [1, 1, 1, 2, 1])
        created = await tuning.generate_proposals(session, "default")
        assert len(created) == 1
        assert created[0].proposed_change == {"enabled": False}

    async def test_high_scores_propose_raise_severity(self, session, client):
        await _score_rule(session, "amount_anomaly", [5, 5, 5, 4, 5])
        created = await tuning.generate_proposals(session, "default")
        assert created and created[0].proposed_change == {"severity": "medium"}  # low->medium

    async def test_below_min_reviews_no_proposal(self, session, client):
        await _score_rule(session, "amount_anomaly", [1, 1])   # < 5
        assert await tuning.generate_proposals(session, "default") == []

    async def test_generation_is_idempotent(self, session, client):
        await _score_rule(session, "amount_anomaly", [1, 1, 1, 1, 1])
        await tuning.generate_proposals(session, "default")
        again = await tuning.generate_proposals(session, "default")
        assert again == []                          # no second pending proposal
        rows = (await session.execute(select(CaseRiskRuleProposalModel))).scalars().all()
        assert len(rows) == 1

    async def test_no_reproposal_after_approval(self, session, client):
        """After a deactivate is APPROVED, re-analysing must not re-propose it —
        the change is already in effect (the old low scores don't disappear)."""
        await _score_rule(session, "amount_anomaly", [1, 1, 1, 1, 1])
        [p] = await tuning.generate_proposals(session, "default")
        await tuning.decide_proposal(session, proposal_id=p.id, tenant="default",
                                     decision="approved", decided_by="boss", note="ok")
        await session.commit()
        again = await tuning.generate_proposals(session, "default")
        assert again == []                          # rule already disabled — nothing to propose


class TestDecision:
    async def test_approve_applies_to_rules(self, session, client):
        await _score_rule(session, "amount_anomaly", [1, 1, 1, 1, 1])
        [p] = await tuning.generate_proposals(session, "default")
        await session.commit()
        r = await client.post(f"{RISK}/rules/proposals/{p.id}/decision",
                             json={"decision": "approved", "note": "agreed"})
        assert r.status_code == 200
        rule = (await session.execute(select(CaseRiskRuleModel).where(
            CaseRiskRuleModel.rule_key == "amount_anomaly"))).scalar_one()
        assert rule.enabled is False                # applied

    async def test_reject_does_not_apply(self, session, client):
        await _score_rule(session, "amount_anomaly", [1, 1, 1, 1, 1])
        [p] = await tuning.generate_proposals(session, "default")
        await session.commit()
        await client.post(f"{RISK}/rules/proposals/{p.id}/decision",
                         json={"decision": "rejected", "note": "keep it"})
        rule = (await session.execute(select(CaseRiskRuleModel).where(
            CaseRiskRuleModel.rule_key == "amount_anomaly"))).scalar_one_or_none()
        assert rule is None                         # no override written

    async def test_endpoints_list_and_analyze(self, session, client):
        await _score_rule(session, "amount_anomaly", [1, 1, 1, 1, 1])
        a = await client.post(f"{RISK}/rules/tuning/analyze")
        assert a.status_code == 200 and len(a.json()["created"]) == 1
        lst = await client.get(f"{RISK}/rules/proposals")
        assert lst.status_code == 200 and len(lst.json()["proposals"]) == 1

    async def test_bad_decision_rejected(self, session, client):
        await _score_rule(session, "amount_anomaly", [1, 1, 1, 1, 1])
        [p] = await tuning.generate_proposals(session, "default")
        await session.commit()
        assert (await client.post(f"{RISK}/rules/proposals/{p.id}/decision",
                                 json={"decision": "maybe"})).status_code == 422
