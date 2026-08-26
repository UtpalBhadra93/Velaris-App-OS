"""HxSentinel P2b — human score/note feedback + the outcome cross-check.

Pins: score 1–5 validation, append-only re-scoring, case-scoped authorization,
and the read-only calibration flag (a score that disagrees with the case's
CURRENT confirmed outcome is flagged, undetermined until the case is labelled).

Setup commits before client calls (shared StaticPool connection).

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

from velaris_shared.db.models import CaseRiskFeedbackModel, CaseRiskSignalModel
from case_service.sentinel import engine as risk_engine
from case_service.sentinel import service as sentinel
from tests.conftest import create_case, deploy_case_type

RISK = "/api/v1/risk"


@pytest.fixture(autouse=True)
def _enable_hxsentinel():
    from case_service.api.routers.releases import _ENABLED_VERSIONS
    prior = _ENABLED_VERSIONS.get("hxsentinel")
    _ENABLED_VERSIONS["hxsentinel"] = "v1.2.0"
    yield
    if prior is None:
        _ENABLED_VERSIONS.pop("hxsentinel", None)
    else:
        _ENABLED_VERSIONS["hxsentinel"] = prior


async def _finding_on_case(session, client):
    """Produce a shared_doc_fraud finding on case `ca`; return (ca, finding)."""
    ct = await deploy_case_type(client, name=f"F {uuid.uuid4().hex[:6]}")
    a = await create_case(client, ct["id"])
    b = await create_case(client, ct["id"])
    ca, cb = uuid.UUID(a["id"]), uuid.UUID(b["id"])
    for c in (ca, cb):
        session.add(CaseRiskSignalModel(tenant_id="default", case_id=c,
                    signal_type="doc_hash", signal_key="fb-doc", value={}))
    await sentinel.set_outcome(session, case_id=cb, tenant_id="default",
                               outcome="fraud", labelled_by="t", audit=False)
    await session.commit()
    active = await risk_engine.evaluate_case(session, ca)
    await session.commit()
    finding = next(f for f in active if f.rule_key == "shared_doc_fraud")
    return ca, finding


class TestFeedbackCollection:
    async def test_score_and_rescore_append_only(self, session, client):
        ca, finding = await _finding_on_case(session, client)
        r = await client.post(f"{RISK}/findings/{finding.id}/feedback",
                             json={"score": 4, "note": "solid catch"})
        assert r.status_code == 201, r.text
        r2 = await client.post(f"{RISK}/findings/{finding.id}/feedback", json={"score": 2})
        assert r2.status_code == 201

        rows = (await session.execute(
            select(CaseRiskFeedbackModel).where(
                CaseRiskFeedbackModel.finding_id == finding.id))).scalars().all()
        assert len(rows) == 2                       # append-only, both kept
        body = (await client.get(f"{RISK}/findings/{finding.id}/feedback")).json()
        assert body["latest"]["score"] == 2         # newest is current
        assert len(body["history"]) == 2

    async def test_score_out_of_range_rejected(self, session, client):
        ca, finding = await _finding_on_case(session, client)
        assert (await client.post(f"{RISK}/findings/{finding.id}/feedback",
                                 json={"score": 0})).status_code == 422
        assert (await client.post(f"{RISK}/findings/{finding.id}/feedback",
                                 json={"score": 6})).status_code == 422

    async def test_unknown_finding_404(self, client):
        r = await client.post(f"{RISK}/findings/{uuid.uuid4()}/feedback", json={"score": 3})
        assert r.status_code == 404


class TestOutcomeCrossCheck:
    async def _score_and_flag(self, session, client, finding, ca_outcome, score):
        if ca_outcome:
            await sentinel.set_outcome(session, case_id=finding.case_id, tenant_id="default",
                                       outcome=ca_outcome, labelled_by="t", audit=False)
            await session.commit()
        return await risk_engine._calibration(
            session, finding.case_id, "default", score)

    async def test_undetermined_without_outcome(self, session, client):
        _ca, finding = await _finding_on_case(session, client)
        assert await self._score_and_flag(session, client, finding, None, 5) == "undetermined"

    async def test_high_score_on_clean_case_is_miscalibrated(self, session, client):
        _ca, finding = await _finding_on_case(session, client)
        # The finding's own case turned out clean -> a 5 praised a false positive.
        assert await self._score_and_flag(session, client, finding, "clean", 5) == "miscalibrated"

    async def test_low_score_on_confirmed_bad_case_is_miscalibrated(self, session, client):
        _ca, finding = await _finding_on_case(session, client)
        # The case was confirmed fraud -> a 1 dismissed a finding that proved right.
        assert await self._score_and_flag(session, client, finding, "fraud", 1) == "miscalibrated"

    async def test_calibrated_scores_are_ok(self, session, client):
        _ca, finding = await _finding_on_case(session, client)
        assert await self._score_and_flag(session, client, finding, "fraud", 5) == "ok"

    async def test_calibration_surfaces_in_feedback_view(self, session, client):
        ca, finding = await _finding_on_case(session, client)
        await sentinel.set_outcome(session, case_id=ca, tenant_id="default",
                                   outcome="clean", labelled_by="t", audit=False)
        await session.commit()
        await client.post(f"{RISK}/findings/{finding.id}/feedback", json={"score": 5})
        body = (await client.get(f"{RISK}/findings/{finding.id}/feedback")).json()
        assert body["latest"]["calibration"] == "miscalibrated"
