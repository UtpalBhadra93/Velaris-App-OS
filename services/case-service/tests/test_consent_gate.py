"""HxConsent as a *control* — the ledger now stops processing, not just records it.

Until 2026-08-05 `has_consent()` existed and no processing path called it, so a
withdrawal changed nothing about what the platform did. These tests pin the
first enforcement point: outbound email for a gated purpose.

What would go wrong with a plausible implementation:

* gating everything — case correspondence is lawful under contract, and a
  platform that stops sending case email because nobody recorded a consent row
  has broken the job it was engaged to do;
* refusing the whole send when one recipient of fifty withdrew;
* treating "the ledger is unreachable" as permission;
* dropping recipients silently, which is the same outcome as the gate not
  existing the moment anyone asks for proof.

The SMTP layer is patched out throughout: this is about who we are permitted to
write to, not about delivery.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest

from case_service.compliance import consent as c
from case_service.mail.service import ConsentRefused, EmailService

pytestmark = pytest.mark.asyncio


def _account():
    """Minimal stand-in for an EmailAccountModel row."""
    return SimpleNamespace(
        id=uuid.uuid4(), address="noreply@velaris.test",
        smtp_host="localhost", smtp_port=1025,
        smtp_username=None, smtp_password=None, smtp_use_tls=False,
    )


@pytest.fixture(autouse=True)
def _no_smtp(monkeypatch):
    async def _fake_send(**_kwargs):
        return None

    monkeypatch.setattr("case_service.mail.service.send_via_smtp", _fake_send)


async def _send(session, to, *, purpose=None, cc=None):
    return await EmailService().send(
        session, case_id=None, account=_account(),
        to_addresses=list(to), cc_addresses=list(cc or []),
        subject="Hello", body_text="Body",
        purpose=purpose,
    )


class TestGatedPurposeIsEnforced:
    async def test_granted_consent_is_delivered(self, session):
        subj = f"ok-{uuid.uuid4().hex[:6]}@x.com"
        await c.record(session, subject_ref=subj, purpose=c.MARKETING)

        row = await _send(session, [subj], purpose=c.MARKETING)
        assert row.to_addresses == [subj]

    async def test_withdrawn_consent_is_refused(self, session):
        subj = f"gone-{uuid.uuid4().hex[:6]}@x.com"
        await c.record(session, subject_ref=subj, purpose=c.MARKETING)
        await c.record(session, subject_ref=subj, purpose=c.MARKETING,
                       action=c.WITHDRAWN)

        with pytest.raises(ConsentRefused):
            await _send(session, [subj], purpose=c.MARKETING)

    async def test_unknown_subject_is_refused(self, session):
        """The absence of a recorded basis is not permission."""
        with pytest.raises(ConsentRefused):
            await _send(session, [f"never-seen-{uuid.uuid4().hex[:6]}@x.com"],
                        purpose=c.MARKETING)

    async def test_partial_drop_still_sends_to_the_lawful_ones(self, session):
        """One withdrawal must not cancel everyone else's mail."""
        ok = f"ok-{uuid.uuid4().hex[:6]}@x.com"
        no = f"no-{uuid.uuid4().hex[:6]}@x.com"
        await c.record(session, subject_ref=ok, purpose=c.MARKETING)
        await c.record(session, subject_ref=no, purpose=c.MARKETING,
                       action=c.WITHDRAWN)

        row = await _send(session, [ok, no], purpose=c.MARKETING)
        assert row.to_addresses == [ok]
        assert no not in row.to_addresses

    async def test_cc_is_filtered_too(self, session):
        to = f"to-{uuid.uuid4().hex[:6]}@x.com"
        cc = f"cc-{uuid.uuid4().hex[:6]}@x.com"
        await c.record(session, subject_ref=to, purpose=c.MARKETING)
        await c.record(session, subject_ref=cc, purpose=c.MARKETING,
                       action=c.WITHDRAWN)

        row = await _send(session, [to], purpose=c.MARKETING, cc=[cc])
        assert row.cc_addresses == []

    async def test_lawful_cc_alone_does_not_rescue_a_dropped_to(self, session):
        """A cc-only send is not a send — there is no lawful primary recipient."""
        to = f"to-{uuid.uuid4().hex[:6]}@x.com"
        cc = f"cc-{uuid.uuid4().hex[:6]}@x.com"
        await c.record(session, subject_ref=cc, purpose=c.MARKETING)

        with pytest.raises(ConsentRefused):
            await _send(session, [to], purpose=c.MARKETING, cc=[cc])


class TestCaseProcessingIsNeverGated:
    async def test_default_purpose_sends_without_any_consent_record(self, session):
        """Case correspondence is lawful under contract — gating it breaks the platform."""
        row = await _send(session, [f"case-{uuid.uuid4().hex[:6]}@x.com"])
        assert row.status in ("sent", "queued")
        assert len(row.to_addresses) == 1

    async def test_explicit_case_processing_is_not_gated_either(self, session):
        subj = f"c-{uuid.uuid4().hex[:6]}@x.com"
        await c.record(session, subject_ref=subj, purpose=c.MARKETING,
                       action=c.WITHDRAWN)

        # Withdrawing marketing must not stop the case email.
        row = await _send(session, [subj], purpose=c.CASE_PROCESSING)
        assert row.to_addresses == [subj]

    async def test_case_processing_is_not_in_the_gated_set(self):
        assert c.CASE_PROCESSING not in c.GATED_PURPOSES
        assert c.MARKETING in c.GATED_PURPOSES


class TestFailsClosed:
    async def test_a_failing_consent_check_refuses_rather_than_sends(self, session, monkeypatch):
        """A gate that opens when the ledger is unreachable is not a gate."""
        async def _boom(*_a, **_kw):
            raise RuntimeError("ledger unreachable")

        monkeypatch.setattr(c, "has_consent", _boom)

        subj = f"err-{uuid.uuid4().hex[:6]}@x.com"
        await c.record(session, subject_ref=subj, purpose=c.MARKETING)

        with pytest.raises(ConsentRefused):
            await _send(session, [subj], purpose=c.MARKETING)


class TestDropsAreProvable:
    async def test_a_drop_is_written_to_security_events(self, session):
        """Without the record, "we stopped marketing to them" is only an assertion."""
        from sqlalchemy import select

        from velaris_shared.db.models import SecurityEventModel

        ok = f"ok-{uuid.uuid4().hex[:6]}@x.com"
        no = f"no-{uuid.uuid4().hex[:6]}@x.com"
        await c.record(session, subject_ref=ok, purpose=c.MARKETING)
        await c.record(session, subject_ref=no, purpose=c.MARKETING,
                       action=c.WITHDRAWN)

        await _send(session, [ok, no], purpose=c.MARKETING)

        events = (await session.execute(
            select(SecurityEventModel).where(
                SecurityEventModel.event_type == "consent.send_filtered"
            )
        )).scalars().all()
        assert events, "the drop left no evidence"
        assert no in events[-1].details["dropped"]
        assert events[-1].details["dropped_count"] == 1
