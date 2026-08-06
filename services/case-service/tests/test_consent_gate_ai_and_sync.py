"""HxConsent as a control for `ai_processing` and `data_sharing`.

Email was the first gated path (`tests/test_consent_gate.py`). It was also the
easy one: the subject of a marketing email is the address it is sent to, so
there is never any doubt who to ask about. These two paths are harder, and the
hard part is the same in both — **often there is no subject at all.**

`case_instances.portal_submitter_email` is NULL on every staff-created case. A
gate that failed closed on an absent subject would disable AI and warehouse
sync across most of the platform while protecting nobody, because there is no
person there whose consent could have been required. So a missing subject is
allowed through and a *recorded refusal* is not. Decided 2026-08-06.

The limit of that rule, stated because it is real: a staff-created case can
still contain personal data about someone the platform never identified. This
gate does not protect them. Nothing here knows who they are — finding them is
HxPrivacy's job.

What would go wrong with a plausible implementation:

* failing closed on a missing subject — AI dies on every internal case;
* failing *open* on a ledger error, which is not a gate;
* gating the recording rather than the inference in the voice path, which
  would be a second mechanism disagreeing with HxMeet's participant check;
* letting a refused case still produce a prompt, a proposal row, or a
  warehouse row — the refusal has to come before the data is gathered;
* collapsing "refused on consent grounds" into the same counter as "a customer
  transform crashed", sending an operator hunting for a fault that is not there.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import uuid

import pytest

from case_service.compliance import consent as c

pytestmark = pytest.mark.asyncio


SUBJECT = "subject@example.test"


async def _grant(session, subject=SUBJECT, purpose=c.AI_PROCESSING):
    await c.record(session, subject_ref=subject, purpose=purpose,
                   action=c.GRANTED, basis=c.CONSENT)
    await session.flush()


async def _withdraw(session, subject=SUBJECT, purpose=c.AI_PROCESSING):
    await c.record(session, subject_ref=subject, purpose=purpose,
                   action=c.WITHDRAWN, basis=c.CONSENT)
    await session.flush()


# ── The shared rule ───────────────────────────────────────────────


class TestAllowsSubject:
    async def test_no_subject_is_allowed_and_says_so(self, session):
        allowed, reason = await c.allows_subject(session, None, c.AI_PROCESSING)
        assert allowed is True
        assert reason == c.NO_SUBJECT, (
            "the reason must distinguish 'nobody to ask' from 'they said yes' — "
            "they are the same outcome but not the same fact"
        )

    async def test_blank_subject_is_treated_as_absent(self, session):
        allowed, reason = await c.allows_subject(session, "   ", c.AI_PROCESSING)
        assert (allowed, reason) == (True, c.NO_SUBJECT)

    async def test_unknown_subject_is_refused(self, session):
        """Present but unrecorded is a refusal — absence of a basis is not permission."""
        allowed, reason = await c.allows_subject(
            session, "never-recorded@example.test", c.AI_PROCESSING)
        assert allowed is False
        assert reason == "no_lawful_basis"

    async def test_granted_subject_is_allowed(self, session):
        await _grant(session)
        allowed, reason = await c.allows_subject(session, SUBJECT, c.AI_PROCESSING)
        assert (allowed, reason) == (True, "granted")

    async def test_withdrawal_flips_it_back(self, session):
        await _grant(session)
        await _withdraw(session)
        allowed, _ = await c.allows_subject(session, SUBJECT, c.AI_PROCESSING)
        assert allowed is False

    async def test_a_grant_for_one_purpose_is_not_a_grant_for_another(self, session):
        await _grant(session, purpose=c.MARKETING)
        allowed, _ = await c.allows_subject(session, SUBJECT, c.AI_PROCESSING)
        assert allowed is False, "purpose limitation is the whole point of the ledger"


class TestRequireCasePurpose:
    async def test_raises_for_a_refused_subject(self, session):
        case = type("C", (), {"portal_submitter_email": SUBJECT, "tenant_id": None})()
        with pytest.raises(c.ProcessingRefused) as e:
            await c.require_case_purpose(session, case, c.AI_PROCESSING)
        assert e.value.purpose == c.AI_PROCESSING
        assert e.value.reason == "no_lawful_basis"

    async def test_returns_for_a_staff_case(self, session):
        case = type("C", (), {"portal_submitter_email": None, "tenant_id": None})()
        await c.require_case_purpose(session, case, c.AI_PROCESSING)  # must not raise

    async def test_a_failing_ledger_refuses_rather_than_allows(self, session, monkeypatch):
        async def _boom(*_a, **_k):
            raise RuntimeError("ledger unreachable")

        monkeypatch.setattr(c, "allows_subject", _boom)
        case = type("C", (), {"portal_submitter_email": SUBJECT, "tenant_id": None})()
        with pytest.raises(c.ProcessingRefused) as e:
            await c.require_case_purpose(session, case, c.AI_PROCESSING)
        assert e.value.reason == "check_failed", (
            "a gate that opens when the ledger is down is not a gate"
        )


class TestAllowsCasePurpose:
    async def test_unknown_case_is_refused(self, session):
        allowed, reason = await c.allows_case_purpose(
            session, uuid.uuid4(), c.AI_PROCESSING)
        assert allowed is False
        assert reason == "unknown_case", (
            "an unresolvable case is not evidence of a lawful basis"
        )


# ── The purposes are actually registered as controls ──────────────


class TestGatedSet:
    async def test_ai_and_data_sharing_are_controls_now(self):
        assert c.AI_PROCESSING in c.GATED_PURPOSES
        assert c.DATA_SHARING in c.GATED_PURPOSES

    async def test_case_processing_is_still_never_gated(self):
        assert c.CASE_PROCESSING not in c.GATED_PURPOSES, (
            "case correspondence is lawful under contract; gating it would stop "
            "the platform doing the job it was engaged to do"
        )

    async def test_voice_and_recording_stay_ungated(self):
        """Not an oversight in either case — see the comments on GATED_PURPOSES."""
        assert c.VOICE_PROCESSING not in c.GATED_PURPOSES
        assert c.RECORDING not in c.GATED_PURPOSES
        assert c.BIOMETRIC not in c.GATED_PURPOSES


# ── ai_action: the gate on an unattended write path ───────────────


async def _case(session, submitter: str | None):
    from case_service.db.models import CaseInstanceModel, CaseTypeModel

    ct = CaseTypeModel(name=f"AI {uuid.uuid4().hex[:6]}", version="1.0.0",
                       definition_json={"stages": []})
    session.add(ct)
    await session.flush()
    case = CaseInstanceModel(
        case_type_id=ct.id, case_type_version=ct.version, status="open",
        case_number=f"C-{uuid.uuid4().hex[:8]}",
        portal_submitter_email=submitter,
    )
    session.add(case)
    await session.flush()
    return case


_STEP = {"id": "s1", "ai_action_config": {
    "prompt": "Classify this case", "output_variable": "band", "namespace": "ai",
}}


class TestAiActionGate:
    async def test_refused_case_never_calls_the_model(self, session, monkeypatch):
        """The refusal has to land before a prompt exists, not after."""
        from case_service.core import ai_action

        called = False

        async def _never(*_a, **_k):
            nonlocal called
            called = True
            return {"value": "x"}

        monkeypatch.setattr(
            "case_service.hxnexus.factory.generate_json", _never, raising=False)

        case = await _case(session, SUBJECT)          # present, unrecorded
        out = await ai_action.execute(session, case_id=case.id, step=_STEP)

        assert out["refused"] is True
        assert out["status"] == "failed"
        assert called is False, (
            "case data reached a model for a purpose with no lawful basis"
        )

    async def test_refusal_is_recorded_as_a_proposal_row(self, session):
        """An unattended refusal that leaves no trace is as bad as a silent write."""
        from sqlalchemy import select

        from case_service.core import ai_action
        from case_service.db.models import AIActionProposalModel

        case = await _case(session, SUBJECT)
        await ai_action.execute(session, case_id=case.id, step=_STEP)

        rows = (await session.execute(
            select(AIActionProposalModel).where(
                AIActionProposalModel.case_id == case.id)
        )).scalars().all()
        assert len(rows) == 1
        assert rows[0].status == "failed"
        assert "ai_processing" in (rows[0].error or "")

    async def test_staff_case_is_not_refused(self, session, monkeypatch):
        from case_service.core import ai_action

        async def _answer(*_a, **_k):
            return {"value": "high", "reasoning": "r"}

        monkeypatch.setattr(
            "case_service.hxnexus.factory.generate_json", _answer, raising=False)

        case = await _case(session, None)
        out = await ai_action.execute(session, case_id=case.id, step=_STEP)

        assert out.get("refused") is not True

    async def test_granted_case_is_not_refused(self, session, monkeypatch):
        from case_service.core import ai_action

        async def _answer(*_a, **_k):
            return {"value": "high", "reasoning": "r"}

        monkeypatch.setattr(
            "case_service.hxnexus.factory.generate_json", _answer, raising=False)

        case = await _case(session, SUBJECT)
        await _grant(session)
        out = await ai_action.execute(session, case_id=case.id, step=_STEP)

        assert out.get("refused") is not True
