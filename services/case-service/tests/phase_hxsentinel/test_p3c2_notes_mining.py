"""HxSentinel P3c-2 — notes mining (LOCAL LLM clusters feedback notes).

Pins: notes -> a rule-change proposal marked source=notes_mining; the suggestion
maps to a machine change (deactivate / lower_severity) or stays advisory; honest
skip (no proposals) when the local model is unavailable; min-notes floor; and no
duplicate while a proposal is already pending.

The LOCAL LLM call (`tuning._local_llm_json`) is patched — these tests never
touch Ollama, matching the codebase's "[] when AI is unavailable" pattern.

Copyright (c) 2024-2026 HELIX Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select

from case_service.db.models import (
    CaseRiskFeedbackModel,
    CaseRiskFindingModel,
    CaseRiskRuleModel,
    CaseRiskRuleProposalModel,
)
from case_service.sentinel import tuning

RISK = "/api/v1/risk"


@pytest.fixture(autouse=True)
def _enable_hxsentinel():
    from case_service.api.routers.releases import _ENABLED_VERSIONS
    prior = _ENABLED_VERSIONS.get("hxsentinel")
    _ENABLED_VERSIONS["hxsentinel"] = "v1.6.0"
    yield
    if prior is None:
        _ENABLED_VERSIONS.pop("hxsentinel", None)
    else:
        _ENABLED_VERSIONS["hxsentinel"] = prior


async def _notes(session, rule_key, notes, *, tenant="default"):
    """Findings + feedback-with-notes for a rule. No client/real cases needed
    (case_id has no FK, mining never joins case_instances)."""
    for i, note in enumerate(notes):
        ca = uuid.uuid4()
        f = CaseRiskFindingModel(tenant_id=tenant, case_id=ca, rule_key=rule_key,
                                 severity="medium", title="t", message="m", evidence={})
        session.add(f)
        await session.commit()
        session.add(CaseRiskFeedbackModel(tenant_id=tenant, case_id=ca, finding_id=f.id,
                    reviewer=f"rev{i}", score=2, note=note))
    await session.commit()


def _llm(suggestion, theme="returning customers flagged", rationale="notes agree"):
    return AsyncMock(return_value={"theme": theme, "suggestion": suggestion, "rationale": rationale})


class TestMining:
    async def test_deactivate_suggestion_becomes_change(self, session, client):
        await _notes(session, "identity_velocity",
                     ["flags every returning customer", "too many repeat-customer false hits", "noisy"])
        with patch.object(tuning, "_local_llm_json", _llm("deactivate")):
            created = await tuning.mine_notes(session, "default")
        assert len(created) == 1
        p = created[0]
        assert p.proposed_change == {"enabled": False}
        assert p.evidence["source"] == "notes_mining" and p.evidence["note_count"] == 3

    async def test_lower_severity_suggestion(self, session, client):
        # identity_velocity defaults to 'medium', so it can be lowered to 'low'.
        await _notes(session, "identity_velocity", ["a", "b", "c"])
        with patch.object(tuning, "_local_llm_json", _llm("lower_severity")):
            created = await tuning.mine_notes(session, "default")
        assert created and created[0].proposed_change == {"severity": "low"}   # medium -> low

    async def test_advisory_suggestion_has_no_change(self, session, client):
        await _notes(session, "amount_anomaly", ["a", "b", "c"])
        with patch.object(tuning, "_local_llm_json", _llm("advisory")):
            created = await tuning.mine_notes(session, "default")
        assert created and created[0].proposed_change == {}

    async def test_honest_skip_when_llm_unavailable(self, session, client):
        await _notes(session, "amount_anomaly", ["a", "b", "c"])
        with patch.object(tuning, "_local_llm_json", AsyncMock(return_value=None)):
            created = await tuning.mine_notes(session, "default")
        assert created == []

    async def test_below_min_notes_skipped(self, session, client):
        await _notes(session, "amount_anomaly", ["only", "two"])   # < 3
        with patch.object(tuning, "_local_llm_json", _llm("deactivate")):
            assert await tuning.mine_notes(session, "default") == []

    async def test_no_duplicate_while_pending(self, session, client):
        await _notes(session, "amount_anomaly", ["a", "b", "c"])
        with patch.object(tuning, "_local_llm_json", _llm("advisory")):
            await tuning.mine_notes(session, "default")
            again = await tuning.mine_notes(session, "default")
        assert again == []

    async def test_deactivate_skipped_if_already_disabled(self, session, client):
        session.add(CaseRiskRuleModel(tenant_id="default", rule_key="amount_anomaly",
                    enabled=False, updated_by="admin"))
        await _notes(session, "amount_anomaly", ["a", "b", "c"])
        with patch.object(tuning, "_local_llm_json", _llm("deactivate")):
            created = await tuning.mine_notes(session, "default")
        # Already disabled -> deactivate maps to no change -> advisory (still surfaced).
        assert created and created[0].proposed_change == {}


class TestEndpoint:
    async def test_mine_endpoint(self, session, client):
        await _notes(session, "amount_anomaly", ["a", "b", "c"])
        with patch.object(tuning, "_local_llm_json", _llm("deactivate")):
            r = await client.post(f"{RISK}/rules/tuning/mine-notes")
        assert r.status_code == 200, r.text
        assert len(r.json()["created"]) == 1

    async def test_mine_endpoint_honest_skip(self, session, client):
        await _notes(session, "amount_anomaly", ["a", "b", "c"])
        with patch.object(tuning, "_local_llm_json", AsyncMock(return_value=None)):
            r = await client.post(f"{RISK}/rules/tuning/mine-notes")
        assert r.status_code == 200 and r.json()["created"] == []
