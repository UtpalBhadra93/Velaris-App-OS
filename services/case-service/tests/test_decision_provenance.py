"""§5.5 GDPR Article 22 provenance on AI decisions.

Article 22 requires an automated decision to be explainable after the fact.
The decision, its confidence and the policy alternative were already audited;
**which model produced it** and **what input it saw** were not, so a disputed
decision could not be reproduced. These tests pin the two missing fields.

The prompt itself is deliberately never stored — only its SHA-256 — because
the prompt carries case context and the audit trail should not duplicate it.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import hashlib
import uuid
from typing import Any

import pytest

from velaris_shared.db import repository as repo
from case_service.decisions.service import DecisionPoint

pytestmark = pytest.mark.asyncio


class _Policy:
    name = "policy-stub"

    async def resolve(self, session, context) -> Any:
        return {"assignee_type": "queue", "assignee_id": "fallback-q"}


class _AIWithProvenance:
    """A resolver that can name the model it used."""

    name = "ai-stub"

    def __init__(self, confidence: float = 0.9) -> None:
        self._confidence = confidence

    async def resolve(self, session, context):
        return (
            {"assignee_type": "user", "assignee_id": "u1"},
            self._confidence,
            {
                "model": "llama3.2",
                "provider": "ollama",
                "prompt_sha256": hashlib.sha256(b"the-prompt").hexdigest(),
            },
        )


class _AILegacy:
    """A resolver on the old 2-tuple contract — must still work."""

    name = "ai-legacy"

    async def resolve(self, session, context):
        return {"assignee_type": "user", "assignee_id": "u2"}, 0.95


async def _resolve(session, ai, *, case_id=None, threshold=0.5):
    dp = DecisionPoint(name="test_dp", policy_resolver=_Policy(), ai_resolver=ai)
    return await dp.resolve(
        session, {}, case_id=case_id, ai_enabled=True,
        threshold=threshold, cognitive_mode="automatic",
    )


class TestOutcomeFields:
    async def test_ai_outcome_carries_model_and_prompt_hash(self, session):
        outcome = await _resolve(session, _AIWithProvenance())
        assert outcome.source == "ai"
        assert outcome.model == "llama3.2"
        assert outcome.provider == "ollama"
        assert outcome.prompt_sha256 == hashlib.sha256(b"the-prompt").hexdigest()

    async def test_policy_outcome_has_no_model(self, session):
        """A deterministic decision names no model — it is reproducible from code."""
        outcome = await _resolve(session, _AIWithProvenance(confidence=0.1))
        assert outcome.source == "policy"
        assert outcome.model is None
        assert outcome.prompt_sha256 is None

    async def test_legacy_two_tuple_resolver_still_works(self, session):
        """A resolver that cannot name its model records none, rather than a wrong one."""
        outcome = await _resolve(session, _AILegacy())
        assert outcome.source == "ai"
        assert outcome.decision["assignee_id"] == "u2"
        assert outcome.model is None


class TestAuditTrail:
    async def _audit_details(self, session, client, ai):
        from tests.conftest import create_case, deploy_case_type

        ct = await deploy_case_type(client, name=f"DP {uuid.uuid4().hex[:8]}")
        case = await create_case(client, ct["id"])
        case_id = uuid.UUID(case["id"])

        await _resolve(session, ai, case_id=case_id)
        await session.commit()

        entries = await repo.get_audit_log(session, case_id)
        rows = [e for e in entries if e.action == "decision_point"]
        assert rows, "no decision_point audit entry was written"
        return rows[0].details

    async def test_ai_decision_is_auditable(self, session, client):
        details = await self._audit_details(session, client, _AIWithProvenance())
        assert details["source"] == "ai"
        assert details["model"] == "llama3.2"
        assert details["provider"] == "ollama"
        assert details["prompt_sha256"] == hashlib.sha256(b"the-prompt").hexdigest()
        # The prompt itself must never be written to the trail.
        assert "prompt" not in details

    async def test_policy_decision_omits_ai_fields(self, session, client):
        """Policy rows carry no null AI columns — the keys are absent entirely."""
        details = await self._audit_details(
            session, client, _AIWithProvenance(confidence=0.1)
        )
        assert details["source"] == "policy"
        assert "model" not in details
        assert "provider" not in details
        assert "prompt_sha256" not in details


class TestActiveModel:
    async def test_reports_the_side_that_actually_answered(self):
        """The egress ladder answers locally first — provenance must follow it."""
        from velaris_shared.llm.factory import active_model

        class _B:
            backend_name = "ollama"
            _model = "llama3.2"

        class _E:
            backend_name = "anthropic"
            _model = "claude-haiku-4-5"

        class _Ladder:
            def __init__(self, route):
                self._local, self._external, self.last_route = _B(), _E(), route

        assert active_model(_Ladder("local"))["model"] == "llama3.2"
        assert active_model(_Ladder("external"))["provider"] == "anthropic"

    async def test_never_raises(self):
        """Audit metadata must not be able to fail a decision."""
        from velaris_shared.llm.factory import active_model

        class _Hostile:
            def __getattr__(self, name):
                raise RuntimeError("boom")

        assert active_model(_Hostile())["model"] == "unknown"
