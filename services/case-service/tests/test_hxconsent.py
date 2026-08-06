"""HxConsent — consent & purpose-limitation ledger (§8.1).

The tests that matter are the ones where a plausible-looking implementation
would quietly misstate the legal position:

* an overwritten grant cannot answer "was this lawful at the time";
* `basis` is not a synonym for consent — a legal obligation is not withdrawable
  by the subject, and recording that it was would claim processing stopped when
  it lawfully continues;
* a typo'd purpose must be refused, because `has_consent(x, "marketng")`
  returning False forever is indistinguishable from a lawful refusal;
* the absence of a record is not permission.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import uuid

import pytest

from case_service.compliance import consent as c

pytestmark = pytest.mark.asyncio

SUBJECT = "alice@example.com"


class TestLedgerIsAppendOnly:
    async def test_withdrawal_adds_a_row_and_keeps_the_grant(self, session):
        await c.record(session, subject_ref=SUBJECT, purpose=c.MARKETING)
        await c.record(session, subject_ref=SUBJECT, purpose=c.MARKETING,
                       action=c.WITHDRAWN)

        rows = await c.history(session, SUBJECT)
        actions = [r.action for r in rows]
        assert c.GRANTED in actions and c.WITHDRAWN in actions
        assert len(rows) == 2          # the grant was not overwritten

    async def test_current_state_reflects_the_latest_decision(self, session):
        subj = f"state-{uuid.uuid4().hex[:6]}@x.com"
        await c.record(session, subject_ref=subj, purpose=c.MARKETING)
        assert (await c.current_state(session, subj))[c.MARKETING]["granted"] is True

        await c.record(session, subject_ref=subj, purpose=c.MARKETING,
                       action=c.WITHDRAWN)
        assert (await c.current_state(session, subj))[c.MARKETING]["granted"] is False

    async def test_regrant_after_withdrawal_works(self, session):
        subj = f"regrant-{uuid.uuid4().hex[:6]}@x.com"
        await c.record(session, subject_ref=subj, purpose=c.MARKETING)
        await c.record(session, subject_ref=subj, purpose=c.MARKETING, action=c.WITHDRAWN)
        await c.record(session, subject_ref=subj, purpose=c.MARKETING)
        assert await c.has_consent(session, subj, c.MARKETING) is True


class TestLawfulBasisIsNotConsent:
    async def test_legal_obligation_cannot_be_withdrawn(self, session):
        """Recording this would claim processing stopped when it lawfully continues."""
        with pytest.raises(c.ConsentError, match="cannot be withdrawn"):
            await c.record(session, subject_ref=SUBJECT, purpose=c.CASE_PROCESSING,
                           action=c.WITHDRAWN, basis=c.LEGAL_OBLIGATION)

    async def test_contract_cannot_be_withdrawn(self, session):
        with pytest.raises(c.ConsentError, match="cannot be withdrawn"):
            await c.record(session, subject_ref=SUBJECT, purpose=c.CASE_PROCESSING,
                           action=c.WITHDRAWN, basis=c.CONTRACT)

    async def test_consent_can_be_withdrawn(self, session):
        row = await c.record(session, subject_ref=SUBJECT, purpose=c.MARKETING,
                             action=c.WITHDRAWN, basis=c.CONSENT)
        assert row.action == c.WITHDRAWN

    async def test_basis_is_recorded_not_flattened(self, session):
        subj = f"basis-{uuid.uuid4().hex[:6]}@x.com"
        await c.record(session, subject_ref=subj, purpose=c.CASE_PROCESSING,
                       basis=c.LEGITIMATE_INTEREST)
        state = await c.current_state(session, subj)
        assert state[c.CASE_PROCESSING]["basis"] == c.LEGITIMATE_INTEREST

    async def test_unknown_basis_is_refused(self, session):
        with pytest.raises(c.ConsentError, match="lawful basis"):
            await c.record(session, subject_ref=SUBJECT, purpose=c.MARKETING,
                           basis="because_we_felt_like_it")


class TestClosedPurposeVocabulary:
    async def test_typo_purpose_is_refused_at_write(self, session):
        with pytest.raises(c.ConsentError, match="not a known purpose"):
            await c.record(session, subject_ref=SUBJECT, purpose="marketng")

    async def test_typo_purpose_check_fails_closed(self, session):
        """And a check against one is False, not an exception that gets caught."""
        assert await c.has_consent(session, SUBJECT, "marketng") is False


class TestFailsClosed:
    async def test_no_record_means_no_consent(self, session):
        assert await c.has_consent(
            session, f"nobody-{uuid.uuid4().hex[:6]}@x.com", c.MARKETING,
        ) is False

    async def test_empty_subject_is_refused(self, session):
        with pytest.raises(c.ConsentError, match="subject_ref is required"):
            await c.record(session, subject_ref="  ", purpose=c.MARKETING)


class TestSubjectMatching:
    async def test_subject_is_case_insensitive(self, session):
        """`Alice@x.com` withdrawing must affect `alice@x.com`'s record."""
        subj = f"Case-{uuid.uuid4().hex[:6]}@Example.COM"
        await c.record(session, subject_ref=subj, purpose=c.MARKETING)
        assert await c.has_consent(session, subj.lower(), c.MARKETING) is True

        await c.record(session, subject_ref=subj.upper(), purpose=c.MARKETING,
                       action=c.WITHDRAWN)
        assert await c.has_consent(session, subj.lower(), c.MARKETING) is False


class TestTenantIsolation:
    async def test_one_tenant_cannot_read_anothers_basis(self, session):
        subj = f"multi-{uuid.uuid4().hex[:6]}@x.com"
        await c.record(session, subject_ref=subj, purpose=c.MARKETING, tenant_id="t1")

        assert await c.has_consent(session, subj, c.MARKETING, tenant_id="t1") is True
        assert await c.has_consent(session, subj, c.MARKETING, tenant_id="t2") is False


class TestApiSurface:
    async def test_endpoints_require_privilege(self, anon_client):
        r = await anon_client.get(f"/api/v1/compliance/consent/{SUBJECT}")
        assert r.status_code in (401, 403)

    async def test_record_and_read_back(self, client):
        subj = f"api-{uuid.uuid4().hex[:6]}@x.com"
        r = await client.post("/api/v1/compliance/consent", json={
            "subject_ref": subj, "purpose": c.RECORDING, "basis": c.CONSENT,
        })
        assert r.status_code == 200, r.text

        got = await client.get(f"/api/v1/compliance/consent/{subj}")
        assert got.status_code == 200, got.text
        body = got.json()
        assert body["purposes"][c.RECORDING]["granted"] is True
        assert len(body["ledger"]) == 1

    async def test_api_rejects_a_bad_purpose(self, client):
        r = await client.post("/api/v1/compliance/consent", json={
            "subject_ref": "x@y.com", "purpose": "nonsense",
        })
        assert r.status_code == 400
        assert "not a known purpose" in r.text

    async def test_check_endpoint_fails_closed(self, client):
        r = await client.get(
            f"/api/v1/compliance/consent/never-{uuid.uuid4().hex[:6]}@x.com/check",
            params={"purpose": c.MARKETING},
        )
        assert r.status_code == 200
        assert r.json()["lawful"] is False
