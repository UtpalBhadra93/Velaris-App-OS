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
    from velaris_shared.db.models import CaseInstanceModel, CaseTypeModel

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
            "velaris_shared.llm.factory.generate_json", _never, raising=False)

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
        from velaris_shared.db.models import AIActionProposalModel

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
            "velaris_shared.llm.factory.generate_json", _answer, raising=False)

        case = await _case(session, None)
        out = await ai_action.execute(session, case_id=case.id, step=_STEP)

        assert out.get("refused") is not True

    async def test_granted_case_is_not_refused(self, session, monkeypatch):
        from case_service.core import ai_action

        async def _answer(*_a, **_k):
            return {"value": "high", "reasoning": "r"}

        monkeypatch.setattr(
            "velaris_shared.llm.factory.generate_json", _answer, raising=False)

        case = await _case(session, SUBJECT)
        await _grant(session)
        out = await ai_action.execute(session, case_id=case.id, step=_STEP)

        assert out.get("refused") is not True


class TestAnonymizedCaseIsRefused:
    """An erased case must not read as an unowned one.

    Both have a NULL `portal_submitter_email`, and they mean opposite things. A
    staff case never had a data subject, so nobody's consent could be required —
    that is why NO_SUBJECT is allowed. An anonymized case HAD one and erased them.
    Letting it share the NO_SUBJECT path would mean erasure silently UNLOCKS the
    AI and warehouse processing that consent had been gating.
    """

    async def _anon_case(self, session):
        from case_service.compliance.retention import ANON_MARKER
        case = await _case(session, None)              # erasure NULLs the column
        case.data = {ANON_MARKER: "2026-01-01T00:00:00+00:00"}
        await session.flush()
        return case

    async def test_allows_case_purpose_refuses_an_anonymized_case(self, session):
        case = await self._anon_case(session)
        allowed, reason = await c.allows_case_purpose(
            session, case.id, c.AI_PROCESSING)
        assert (allowed, reason) == (False, c.ANONYMIZED)

    async def test_require_case_purpose_raises_for_an_anonymized_case(self, session):
        case = await self._anon_case(session)
        with pytest.raises(Exception):
            await c.require_case_purpose(session, case, c.AI_PROCESSING)

    async def test_the_reason_is_distinct_from_no_subject(self, session):
        """"Erased" and "there was nobody to ask" must not look the same."""
        assert c.ANONYMIZED != c.NO_SUBJECT

    async def test_happy_path_staff_case_is_still_allowed(self, session):
        """HAPPY PATH: the NO_SUBJECT allowance must survive intact."""
        case = await _case(session, None)
        allowed, reason = await c.allows_case_purpose(
            session, case.id, c.AI_PROCESSING)
        assert (allowed, reason) == (True, c.NO_SUBJECT)

    async def test_happy_path_a_live_portal_case_is_still_gated(self, session):
        """HAPPY PATH: an ordinary portal case is refused for lack of a grant,
        by the ordinary rule — not by the new anonymized branch."""
        case = await _case(session, "live@example.com")
        allowed, reason = await c.allows_case_purpose(
            session, case.id, c.AI_PROCESSING)
        assert (allowed, reason) == (False, "no_lawful_basis")


class TestWarehouseSyncGateUsesTheCaseRule:
    """The warehouse gate read the column directly and missed the erasure rule.

    It is the path that physically copies a row OUT of the platform, so an erased
    case slipping through it is the worst version of this bug.
    """

    async def test_hxsync_calls_allows_case(self):
        import inspect
        from case_service.hxsync import pipeline
        src = inspect.getsource(pipeline.run_sync) if hasattr(pipeline, "run_sync") \
            else inspect.getsource(pipeline)
        assert "allows_case(" in src
        assert "allows_subject(" not in src, (
            "the sync gate must go through the case-aware rule")

    async def test_anonymized_case_is_refused_for_data_sharing(self, session):
        from case_service.compliance.retention import ANON_MARKER
        case = await _case(session, None)
        case.data = {ANON_MARKER: "2026-01-01T00:00:00+00:00"}
        await session.flush()

        allowed, reason = await c.allows_case(session, case, c.DATA_SHARING)
        assert (allowed, reason) == (False, c.ANONYMIZED)

    async def test_happy_path_staff_case_still_syncs(self, session):
        """HAPPY PATH: staff cases must keep flowing to the warehouse."""
        case = await _case(session, None)
        allowed, reason = await c.allows_case(session, case, c.DATA_SHARING)
        assert (allowed, reason) == (True, c.NO_SUBJECT)


class TestConsentDoesNotCrossTenants:
    """A grant recorded in one tenant is not a lawful basis in another.

    `current_state` applies NO tenant filter when `tenant_id` is None, so the old
    `allows_case_purpose` — whose only caller (`core.ai_action`) passes none —
    was satisfied by a grant from ANY tenant. `require_case_purpose` already fell
    back to `case.tenant_id`; the two entry points disagreed. Both now scope to
    the case's own tenant.
    """

    async def _tenant(self, session, slug):
        from velaris_shared.db.models import TenantModel
        t = TenantModel(slug=slug, name=slug)
        session.add(t)
        await session.flush()
        return t

    async def _case_in(self, session, tenant, submitter):
        case = await _case(session, submitter)
        case.tenant_id = tenant.id
        await session.flush()
        return case

    async def test_a_grant_in_another_tenant_does_not_authorize(self, session):
        other = await self._tenant(session, f"other-{uuid.uuid4().hex[:6]}")
        mine = await self._tenant(session, f"mine-{uuid.uuid4().hex[:6]}")

        await c.record(session, subject_ref=SUBJECT, purpose=c.AI_PROCESSING,
                       action=c.GRANTED, basis=c.CONSENT, tenant_id=str(other.id))
        await session.flush()

        case = await self._case_in(session, mine, SUBJECT)
        allowed, reason = await c.allows_case_purpose(
            session, case.id, c.AI_PROCESSING)
        assert (allowed, reason) == (False, "no_lawful_basis")

    async def test_happy_path_the_same_tenants_grant_does_authorize(self, session):
        """HAPPY PATH: consent must still work inside its own tenant."""
        mine = await self._tenant(session, f"mine-{uuid.uuid4().hex[:6]}")
        await c.record(session, subject_ref=SUBJECT, purpose=c.AI_PROCESSING,
                       action=c.GRANTED, basis=c.CONSENT, tenant_id=str(mine.id))
        await session.flush()

        case = await self._case_in(session, mine, SUBJECT)
        allowed, reason = await c.allows_case_purpose(
            session, case.id, c.AI_PROCESSING)
        assert (allowed, reason) == (True, "granted")


class TestRequireCasePurposeFindsATenantedGrant:
    """`require_case_purpose` refused every tenanted case with a valid grant.

    It has fallen back to `case.tenant_id` for a long time, but
    `consent_records.tenant_id` is a String and `case_instances.tenant_id` is a
    GUID — filtering the string column by a UUID object matched nothing. Because
    the gate fails closed, it read as "the customer never consented".
    """

    async def test_a_uuid_tenant_still_finds_its_grant(self, session):
        from velaris_shared.db.models import TenantModel

        t = TenantModel(slug=f"t-{uuid.uuid4().hex[:6]}", name="T")
        session.add(t)
        await session.flush()

        await c.record(session, subject_ref=SUBJECT, purpose=c.AI_PROCESSING,
                       action=c.GRANTED, basis=c.CONSENT, tenant_id=str(t.id))
        case = await _case(session, SUBJECT)
        case.tenant_id = t.id                 # a UUID, as the ORM returns it
        await session.flush()

        # Must not raise: the grant exists and belongs to this tenant.
        await c.require_case_purpose(session, case, c.AI_PROCESSING)

    async def test_a_withdrawal_in_the_same_tenant_still_refuses(self, session):
        """The coercion must not turn the gate into a rubber stamp."""
        from velaris_shared.db.models import TenantModel

        t = TenantModel(slug=f"t-{uuid.uuid4().hex[:6]}", name="T")
        session.add(t)
        await session.flush()

        await c.record(session, subject_ref=SUBJECT, purpose=c.AI_PROCESSING,
                       action=c.WITHDRAWN, basis=c.CONSENT, tenant_id=str(t.id))
        case = await _case(session, SUBJECT)
        case.tenant_id = t.id
        await session.flush()

        with pytest.raises(Exception):
            await c.require_case_purpose(session, case, c.AI_PROCESSING)
