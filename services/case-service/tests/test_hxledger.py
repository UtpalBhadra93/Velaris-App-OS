"""HxLedger — double-entry ledger & reconciliation (§8.4).

The invariant is the product. A ledger that stores an unbalanced transaction
is worse than no ledger, because it looks authoritative and is not — so the
tests concentrate on what must be *refused*, not on what works.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import uuid

import pytest

from case_service.compliance import ledger as L

pytestmark = pytest.mark.asyncio


async def _accounts(session):
    cash = await L.ensure_account(
        session, code=f"1000-{uuid.uuid4().hex[:6]}", name="Cash",
        account_type=L.ASSET,
    )
    revenue = await L.ensure_account(
        session, code=f"4000-{uuid.uuid4().hex[:6]}", name="Revenue",
        account_type=L.INCOME,
    )
    return cash, revenue


def _lines(cash, revenue, amount=10_000):
    return [
        {"account_id": cash.id, "direction": L.DEBIT, "amount_cents": amount},
        {"account_id": revenue.id, "direction": L.CREDIT, "amount_cents": amount},
    ]


class TestBalanceIsEnforcedAtWrite:
    async def test_balanced_transaction_posts(self, session):
        cash, revenue = await _accounts(session)
        txn = await L.post(session, lines=_lines(cash, revenue), description="sale")
        assert txn.id is not None

    async def test_unbalanced_transaction_is_refused(self, session):
        """Never stored and corrected later — it would be relied on first."""
        cash, revenue = await _accounts(session)
        bad = [
            {"account_id": cash.id, "direction": L.DEBIT, "amount_cents": 10_000},
            {"account_id": revenue.id, "direction": L.CREDIT, "amount_cents": 9_999},
        ]
        with pytest.raises(L.LedgerError, match="does not balance"):
            await L.post(session, lines=bad)

    async def test_single_line_is_refused(self, session):
        cash, _ = await _accounts(session)
        with pytest.raises(L.LedgerError, match="at least two lines"):
            await L.post(session, lines=[
                {"account_id": cash.id, "direction": L.DEBIT, "amount_cents": 100},
            ])

    async def test_float_amount_is_refused(self, session):
        """Floats accumulate error that reconciliation reports as a real gap."""
        cash, revenue = await _accounts(session)
        bad = [
            {"account_id": cash.id, "direction": L.DEBIT, "amount_cents": 100.5},
            {"account_id": revenue.id, "direction": L.CREDIT, "amount_cents": 100.5},
        ]
        with pytest.raises(L.LedgerError, match="integer number of cents"):
            await L.post(session, lines=bad)

    async def test_negative_amount_is_refused(self, session):
        """Direction is expressed by debit/credit, not by sign."""
        cash, revenue = await _accounts(session)
        bad = [
            {"account_id": cash.id, "direction": L.DEBIT, "amount_cents": -100},
            {"account_id": revenue.id, "direction": L.CREDIT, "amount_cents": -100},
        ]
        with pytest.raises(L.LedgerError, match="must be positive"):
            await L.post(session, lines=bad)

    async def test_bad_direction_is_refused(self, session):
        cash, revenue = await _accounts(session)
        bad = [
            {"account_id": cash.id, "direction": "sideways", "amount_cents": 100},
            {"account_id": revenue.id, "direction": L.CREDIT, "amount_cents": 100},
        ]
        with pytest.raises(L.LedgerError, match="direction must be"):
            await L.post(session, lines=bad)

    async def test_nothing_is_stored_when_a_posting_is_refused(self, session):
        from sqlalchemy import func, select

        from velaris_shared.db.models import LedgerEntryModel

        cash, revenue = await _accounts(session)
        before = (await session.execute(
            select(func.count()).select_from(LedgerEntryModel)
        )).scalar_one()

        with pytest.raises(L.LedgerError):
            await L.post(session, lines=[
                {"account_id": cash.id, "direction": L.DEBIT, "amount_cents": 5},
                {"account_id": revenue.id, "direction": L.CREDIT, "amount_cents": 6},
            ])

        after = (await session.execute(
            select(func.count()).select_from(LedgerEntryModel)
        )).scalar_one()
        assert after == before


class TestIdempotency:
    async def test_same_source_posts_once(self, session):
        """A payment webhook firing twice must not double-post."""
        cash, revenue = await _accounts(session)
        src = uuid.uuid4()

        a = await L.post(session, lines=_lines(cash, revenue),
                         source_type="payment_request", source_id=src, purpose="capture")
        b = await L.post(session, lines=_lines(cash, revenue),
                         source_type="payment_request", source_id=src, purpose="capture")
        assert a.id == b.id

    async def test_different_purpose_posts_separately(self, session):
        cash, revenue = await _accounts(session)
        src = uuid.uuid4()
        a = await L.post(session, lines=_lines(cash, revenue),
                         source_type="payment_request", source_id=src, purpose="capture")
        b = await L.post(session, lines=_lines(cash, revenue),
                         source_type="payment_request", source_id=src, purpose="refund")
        assert a.id != b.id


class TestAppendOnlyCorrections:
    async def test_reversal_mirrors_the_original(self, session):
        from sqlalchemy import select

        from velaris_shared.db.models import LedgerEntryModel

        cash, revenue = await _accounts(session)
        txn = await L.post(session, lines=_lines(cash, revenue), description="sale")
        rev = await L.reverse(session, txn.id, posted_by="admin")

        lines = (await session.execute(
            select(LedgerEntryModel).where(LedgerEntryModel.transaction_id == rev.id)
        )).scalars().all()
        directions = {(l.account_id, l.direction) for l in lines}
        assert (cash.id, L.CREDIT) in directions       # was a debit
        assert (revenue.id, L.DEBIT) in directions     # was a credit

    async def test_original_survives_the_reversal(self, session):
        """The books must still be reproducible as at any past date."""
        cash, revenue = await _accounts(session)
        txn = await L.post(session, lines=_lines(cash, revenue))
        await L.reverse(session, txn.id, posted_by="admin")

        still_there = await session.get(type(txn), txn.id)
        assert still_there is not None

    async def test_double_reversal_is_refused(self, session):
        cash, revenue = await _accounts(session)
        txn = await L.post(session, lines=_lines(cash, revenue))
        await L.reverse(session, txn.id, posted_by="admin")
        with pytest.raises(L.LedgerError, match="already been reversed"):
            await L.reverse(session, txn.id, posted_by="admin")

    async def test_reversal_nets_the_account_to_zero(self, session):
        cash, revenue = await _accounts(session)
        txn = await L.post(session, lines=_lines(cash, revenue, amount=7_500))
        await L.reverse(session, txn.id, posted_by="admin")

        assert (await L.balance(session, cash.id))["balance_cents"] == 0


class TestBalancesAndSigns:
    async def test_asset_balance_is_debit_positive(self, session):
        cash, revenue = await _accounts(session)
        await L.post(session, lines=_lines(cash, revenue, amount=2_500))
        assert (await L.balance(session, cash.id))["balance_cents"] == 2_500

    async def test_income_balance_is_credit_positive(self, session):
        """Getting this backwards silently inverts every report."""
        cash, revenue = await _accounts(session)
        await L.post(session, lines=_lines(cash, revenue, amount=2_500))
        assert (await L.balance(session, revenue.id))["balance_cents"] == 2_500

    async def test_trial_balance_is_balanced(self, session):
        cash, revenue = await _accounts(session)
        await L.post(session, lines=_lines(cash, revenue))
        tb = await L.trial_balance(session)
        assert tb["balanced"] is True
        assert tb["total_debits_cents"] == tb["total_credits_cents"]


class TestAccounts:
    async def test_ensure_account_is_idempotent(self, session):
        code = f"2000-{uuid.uuid4().hex[:6]}"
        a = await L.ensure_account(session, code=code, name="AP", account_type=L.LIABILITY)
        b = await L.ensure_account(session, code=code, name="AP", account_type=L.LIABILITY)
        assert a.id == b.id

    async def test_unknown_account_type_is_refused(self, session):
        with pytest.raises(L.LedgerError, match="not an account type"):
            await L.ensure_account(session, code="x", name="X", account_type="vibes")


class TestReconciliation:
    async def test_empty_ledger_and_no_payments_reconciles(self, session):
        out = await L.reconcile_payments(session, tenant_id=f"t-{uuid.uuid4().hex[:6]}")
        assert out["reconciled"] is True
        assert out["difference_cents"] == 0

    async def test_unposted_payment_shows_as_a_difference(self, session):
        """Money moved without a book entry — what a ledger exists to catch."""
        from velaris_shared.db.models import (
            CaseInstanceModel, CaseTypeModel, PaymentRequestModel,
        )

        tenant = f"t-{uuid.uuid4().hex[:6]}"
        ct = CaseTypeModel(name=f"L {uuid.uuid4().hex[:6]}", version="1.0.0",
                           definition_json={"stages": []})
        session.add(ct)
        await session.flush()
        case = CaseInstanceModel(case_type_id=ct.id, case_type_version="1.0.0",
                                 status="open")
        session.add(case)
        await session.flush()
        session.add(PaymentRequestModel(
            tenant_id=tenant, case_id=case.id, step_id="s1", provider="stripe",
            amount_cents=5_000, currency="usd", status="captured",
        ))
        await session.flush()

        out = await L.reconcile_payments(session, tenant_id=tenant)
        assert out["reconciled"] is False
        assert out["difference_cents"] == 5_000


class TestApiSurface:
    async def test_endpoints_require_admin(self, anon_client):
        r = await anon_client.get("/api/v1/compliance/ledger/trial-balance")
        assert r.status_code in (401, 403)

    async def test_api_refuses_an_unbalanced_posting(self, client):
        acc = await client.post("/api/v1/compliance/ledger/accounts", json={
            "code": f"1000-{uuid.uuid4().hex[:6]}", "name": "Cash",
            "account_type": "asset",
        })
        assert acc.status_code == 200, acc.text
        aid = acc.json()["id"]

        r = await client.post("/api/v1/compliance/ledger/transactions", json={
            "lines": [
                {"account_id": aid, "direction": "debit", "amount_cents": 100},
                {"account_id": aid, "direction": "credit", "amount_cents": 99},
            ],
        })
        assert r.status_code == 400
        assert "does not balance" in r.text
