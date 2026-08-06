"""HxSentinel P3b — supervisor oversight: dashboard, reliability, disputes.

Pins: the dashboard aggregates per reviewer; reliability = outcome-agreement with
disputed scores excluded; a dispute is append-only + tenant-scoped; and the
`risk.scores.review` action gates both.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import uuid

import pytest

from case_service.db.models import (
    CaseRiskFeedbackModel,
    CaseRiskFindingModel,
    CaseRiskScoreDisputeModel,
)
from case_service.sentinel import oversight
from case_service.sentinel import service as sentinel
from tests.phase_hxsentinel._seed import seed_case

RISK = "/api/v1/risk"


@pytest.fixture(autouse=True)
def _enable_hxsentinel():
    from case_service.api.routers.releases import _ENABLED_VERSIONS
    prior = _ENABLED_VERSIONS.get("hxsentinel")
    _ENABLED_VERSIONS["hxsentinel"] = "v1.4.0"
    yield
    if prior is None:
        _ENABLED_VERSIONS.pop("hxsentinel", None)
    else:
        _ENABLED_VERSIONS["hxsentinel"] = prior


async def _finding(session, tenant="default"):
    # No client / real case: finding.case_id has no FK and the dashboard never
    # joins case_instances — plain UUIDs keep setup on the test session, immune to
    # the shared-connection churn that made the client-based version flaky.
    ca = await seed_case(session)   # backing case so set_outcome/dispute audit-log FK holds
    f = CaseRiskFindingModel(tenant_id=tenant, case_id=ca, rule_key="shared_doc_fraud",
                             severity="high", title="t", message="m", evidence={})
    session.add(f)
    await session.commit()
    return ca, f


async def _score(session, finding, case_id, reviewer, score, tenant="default"):
    session.add(CaseRiskFeedbackModel(tenant_id=tenant, case_id=case_id,
                finding_id=finding.id, reviewer=reviewer, score=score))
    await session.commit()


class TestDashboard:
    async def test_reliability_excludes_disputed(self, session, client):
        # reviewer scores 5 on two findings; one case is clean (miscalibrated),
        # the other fraud (ok). Reliability = 1/2 = 0.5. Dispute the bad one -> 1.0.
        ca1, f1 = await _finding(session)
        ca2, f2 = await _finding(session)
        await sentinel.set_outcome(session, case_id=ca1, tenant_id="default",
                                   outcome="clean", labelled_by="t", audit=False)
        await sentinel.set_outcome(session, case_id=ca2, tenant_id="default",
                                   outcome="fraud", labelled_by="t", audit=False)
        await _score(session, f1, ca1, "rev1", 5)   # miscalibrated (clean case)
        fb_bad = (await session.execute(
            __import__("sqlalchemy").select(CaseRiskFeedbackModel).where(
                CaseRiskFeedbackModel.finding_id == f1.id))).scalar_one()
        await _score(session, f2, ca2, "rev1", 5)   # ok (fraud case)
        await session.commit()

        dash = await oversight.reviewer_dashboard(session, "default")
        rev = next(r for r in dash["reviewers"] if r["reviewer"] == "rev1")
        assert rev["count"] == 2 and rev["reliability"] == 0.5

        await oversight.dispute_score(session, feedback_id=fb_bad.id, tenant="default",
                                      disputed_by="boss", note="rushed")
        await session.commit()
        dash2 = await oversight.reviewer_dashboard(session, "default")
        rev2 = next(r for r in dash2["reviewers"] if r["reviewer"] == "rev1")
        assert rev2["disputed"] == 1 and rev2["reliability"] == 1.0   # bad one excluded

    async def test_undetermined_scores_not_counted(self, session, client):
        ca, f = await _finding(session)          # case never labelled
        await _score(session, f, ca, "rev2", 5)
        await session.commit()
        dash = await oversight.reviewer_dashboard(session, "default")
        rev = next(r for r in dash["reviewers"] if r["reviewer"] == "rev2")
        assert rev["count"] == 1 and rev["determinable"] == 0 and rev["reliability"] is None

    async def test_tenant_scoped(self, session, client):
        ca, f = await _finding(session, tenant="tenantX")
        await _score(session, f, ca, "revX", 3, tenant="tenantX")
        await session.commit()
        dash = await oversight.reviewer_dashboard(session, "default")
        assert all(r["reviewer"] != "revX" for r in dash["reviewers"])


class TestEndpoints:
    async def test_dashboard_endpoint(self, session, client):
        ca, f = await _finding(session)
        await _score(session, f, ca, "revE", 4)
        await session.commit()
        r = await client.get(f"{RISK}/scores/dashboard")
        assert r.status_code == 200, r.text
        assert any(x["reviewer"] == "revE" for x in r.json()["reviewers"])

    async def test_dispute_endpoint_and_requires_note(self, session, client):
        ca, f = await _finding(session)
        await _score(session, f, ca, "revD", 5)
        fb = (await session.execute(
            __import__("sqlalchemy").select(CaseRiskFeedbackModel).where(
                CaseRiskFeedbackModel.finding_id == f.id))).scalar_one()
        await session.commit()
        assert (await client.post(f"{RISK}/feedback/{fb.id}/dispute",
                                 json={})).status_code == 422       # note required
        r = await client.post(f"{RISK}/feedback/{fb.id}/dispute", json={"note": "bulk 5s"})
        assert r.status_code == 201
        rows = (await session.execute(
            __import__("sqlalchemy").select(CaseRiskScoreDisputeModel).where(
                CaseRiskScoreDisputeModel.feedback_id == fb.id))).scalars().all()
        assert len(rows) == 1

    async def test_dispute_unknown_feedback_404(self, client):
        r = await client.post(f"{RISK}/feedback/{uuid.uuid4()}/dispute", json={"note": "x"})
        assert r.status_code == 404
