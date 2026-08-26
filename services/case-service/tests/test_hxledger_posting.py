"""HxLedger — the payment producer and what it does to reconciliation (§8.4).

`ledger.py` shipped with no caller. These tests pin the two things that make
the producer worth having rather than merely present:

* a Stripe retry must not double the books;
* a refund must reconcile to zero, not to a permanent discrepancy.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

from case_service.compliance import ledger, ledger_posting
from velaris_shared.db.models import (
    CaseInstanceModel,
    LedgerAccountModel,
    LedgerEntryModel,
    LedgerTransactionModel,
    PaymentRequestModel,
)

pytestmark = pytest.mark.asyncio


#: `payment_requests.tenant_id` is NOT NULL, so a payment always carries one —
#: which is why `ledger_posting` can rely on it for account scoping.
TENANT = "t-ledger-tests"


async def _payment(session, *, cents=10_000, status="succeeded", tenant=TENANT):
    case = CaseInstanceModel(
        case_type_id=uuid.uuid4(), case_type_version=1, status="new",
        priority="medium", data={}, created_by="test", tenant_id=tenant,
    )
    session.add(case)
    await session.flush()

    pr = PaymentRequestModel(
        case_id=case.id, step_id="pay-step", provider="stripe",
        amount_cents=cents, currency="usd", status=status, tenant_id=tenant,
    )
    session.add(pr)
    await session.flush()
    return pr


async def _entries(session, account_code):
    rows = (await session.execute(
        select(LedgerEntryModel)
        .join(LedgerAccountModel, LedgerAccountModel.id == LedgerEntryModel.account_id)
        .where(LedgerAccountModel.code == account_code)
    )).scalars().all()
    return rows


class TestCapture:
    async def test_posts_a_balanced_transaction(self, session):
        pr = await _payment(session, cents=10_000)
        txn = await ledger_posting.post_capture(session, pr)
        await session.flush()

        assert txn is not None
        lines = (await session.execute(
            select(LedgerEntryModel).where(LedgerEntryModel.transaction_id == txn.id)
        )).scalars().all()
        assert len(lines) == 2
        debits = sum(l.amount_cents for l in lines if l.direction == ledger.DEBIT)
        credits = sum(l.amount_cents for l in lines if l.direction == ledger.CREDIT)
        assert debits == credits == 10_000

    async def test_debits_cash_and_credits_income(self, session):
        pr = await _payment(session, cents=2_500)
        await ledger_posting.post_capture(session, pr)
        await session.flush()

        cash = await _entries(session, ledger_posting.CASH_CODE)
        income = await _entries(session, ledger_posting.INCOME_CODE)
        assert [e.direction for e in cash] == [ledger.DEBIT]
        assert [e.direction for e in income] == [ledger.CREDIT]

    async def test_a_stripe_retry_does_not_double_the_books(self, session):
        """Stripe retries webhooks; the books must not grow each time."""
        pr = await _payment(session, cents=7_777)
        first = await ledger_posting.post_capture(session, pr)
        second = await ledger_posting.post_capture(session, pr)
        await session.flush()

        assert first.id == second.id
        cash = await _entries(session, ledger_posting.CASH_CODE)
        assert len(cash) == 1

    async def test_accounts_are_created_once(self, session):
        a = await _payment(session, cents=100)
        b = await _payment(session, cents=200)
        await ledger_posting.post_capture(session, a)
        await ledger_posting.post_capture(session, b)
        await session.flush()

        accounts = (await session.execute(
            select(LedgerAccountModel).where(
                LedgerAccountModel.code == ledger_posting.CASH_CODE
            )
        )).scalars().all()
        assert len(accounts) == 1

    async def test_zero_amount_posts_nothing(self, session):
        """A fully-discounted step is real; a zero transaction is not."""
        pr = await _payment(session, cents=0)
        assert await ledger_posting.post_capture(session, pr) is None

    async def test_disabled_flag_posts_nothing(self, session, monkeypatch):
        from velaris_shared.config import get_settings

        get_settings.cache_clear()
        monkeypatch.setenv("VELARIS_CASE_LEDGER_POSTING_ENABLED", "false")
        get_settings.cache_clear()
        try:
            pr = await _payment(session, cents=500)
            assert await ledger_posting.post_capture(session, pr) is None
        finally:
            monkeypatch.delenv("VELARIS_CASE_LEDGER_POSTING_ENABLED", raising=False)
            get_settings.cache_clear()


class TestRefund:
    async def test_mirrors_the_capture(self, session):
        pr = await _payment(session, cents=4_000)
        await ledger_posting.post_capture(session, pr)
        await ledger_posting.post_refund(session, pr)
        await session.flush()

        cash = await _entries(session, ledger_posting.CASH_CODE)
        assert sorted(e.direction for e in cash) == [ledger.CREDIT, ledger.DEBIT]

    async def test_refund_is_its_own_event_not_a_reversal(self, session):
        """Both really happened; `reverses_id` would claim the capture was wrong."""
        pr = await _payment(session, cents=4_000)
        await ledger_posting.post_capture(session, pr)
        refund = await ledger_posting.post_refund(session, pr)
        await session.flush()

        assert refund.reverses_id is None
        assert refund.purpose == ledger_posting.REFUND

    async def test_refund_does_not_collide_with_the_capture_key(self, session):
        pr = await _payment(session, cents=4_000)
        capture = await ledger_posting.post_capture(session, pr)
        refund = await ledger_posting.post_refund(session, pr)
        assert capture.id != refund.id


class TestReconciliation:
    async def test_a_captured_payment_reconciles(self, session):
        pr = await _payment(session, cents=12_345, status="succeeded")
        await ledger_posting.post_capture(session, pr)
        await session.flush()

        out = await ledger.reconcile_payments(session)
        assert out["payments_captured_cents"] == 12_345
        assert out["ledger_net_cents"] == 12_345
        assert out["reconciled"] is True

    async def test_an_unposted_payment_is_reported(self, session):
        """The gap this whole module closes — money in, nothing on the books."""
        await _payment(session, cents=9_000, status="succeeded")
        out = await ledger.reconcile_payments(session)
        assert out["reconciled"] is False
        assert out["difference_cents"] == 9_000

    async def test_a_refund_reconciles_to_zero(self, session):
        """The trap: gross debits would report this as broken forever."""
        pr = await _payment(session, cents=6_000, status="succeeded")
        await ledger_posting.post_capture(session, pr)
        await session.flush()

        pr.status = "refunded"
        await ledger_posting.post_refund(session, pr)
        await session.flush()

        out = await ledger.reconcile_payments(session)
        assert out["payments_captured_cents"] == 0
        assert out["ledger_debits_cents"] == 6_000
        assert out["ledger_refunds_cents"] == 6_000
        assert out["ledger_net_cents"] == 0
        assert out["reconciled"] is True

    async def test_income_credits_do_not_cancel_the_cash_debit(self, session):
        """The other trap: both entries share source_type, so direction alone nets to 0."""
        pr = await _payment(session, cents=5_000, status="succeeded")
        await ledger_posting.post_capture(session, pr)
        await session.flush()

        out = await ledger.reconcile_payments(session)
        assert out["ledger_net_cents"] == 5_000

    async def test_trial_balance_stays_balanced(self, session):
        for cents in (1_000, 2_500, 999):
            pr = await _payment(session, cents=cents)
            await ledger_posting.post_capture(session, pr)
        await session.flush()

        tb = await ledger.trial_balance(session)
        assert tb["balanced"] is True
