"""ARB-A — the decision chain, which is what makes the log evidence.

The claim being tested is narrow and total: **if a sealed decision is altered,
verification fails.** Everything else here supports that one assertion, because
a chain that seals cleanly and detects nothing is worse than no chain — it
produces confidence rather than evidence.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio
from sqlalchemy import select, update

from velaris_shared.db.models import (
    ArbiterDecisionChainModel,
    ArbiterDecisionModel,
)

from case_service.hxarbiter import chain

pytestmark = pytest.mark.asyncio

T0 = datetime(2026, 3, 3, 12, 0, 0, tzinfo=timezone.utc)


def _decision(at: datetime, *, effect: str = "deny", tenant=None) -> ArbiterDecisionModel:
    return ArbiterDecisionModel(
        id=uuid.uuid4(),
        tenant_id=tenant or uuid.uuid4(),
        namespace="ai",
        action="ai.external_call",
        subject_kind="user",
        subject_id="u-1",
        subject={}, resource={}, context={},
        effect=effect, mode="shadow", enforced=False, default_applied=True,
        matched=[], obligations=[],
        latency_us=10,
        evaluated_at=at,
    )


@pytest_asyncio.fixture
async def sealed(session):
    """Three decisions inside one window, sealed."""
    for i in range(3):
        session.add(_decision(T0 + timedelta(seconds=i)))
    await session.commit()
    n = await chain.seal_once(session, now=T0 + timedelta(seconds=300))
    assert n >= 1
    return n


async def test_nothing_to_seal_is_not_an_error(session):
    assert await chain.seal_once(session) == 0


async def test_sealing_marks_the_decisions_and_links_from_genesis(session, sealed):
    batches = (await session.execute(
        select(ArbiterDecisionChainModel)
        .order_by(ArbiterDecisionChainModel.sequence))).scalars().all()
    assert batches[0].sequence == 1
    assert batches[0].prev_hash == chain.GENESIS
    assert batches[0].decision_count == 3

    rows = (await session.execute(select(ArbiterDecisionModel))).scalars().all()
    assert all(r.chain_sequence == 1 for r in rows)


async def test_a_sealed_chain_verifies(session, sealed):
    ok, problem = await chain.verify_chain(session)
    assert ok, problem


async def test_altering_a_sealed_decision_is_detected(session, sealed):
    """The entire product claim, as one test."""
    row = (await session.execute(select(ArbiterDecisionModel))).scalars().first()
    await session.execute(
        update(ArbiterDecisionModel)
        .where(ArbiterDecisionModel.id == row.id)
        .values(effect="allow"))
    await session.commit()

    ok, problem = await chain.verify_chain(session)
    assert ok is False
    assert "does not match what was sealed" in problem


async def test_deleting_a_sealed_decision_is_detected(session, sealed):
    row = (await session.execute(select(ArbiterDecisionModel))).scalars().first()
    await session.delete(row)
    await session.commit()

    ok, problem = await chain.verify_chain(session)
    assert ok is False
    assert "now holds" in problem


async def test_removing_a_whole_sealed_batch_is_detected(session):
    """A rewrite that takes the decisions AND their batch still breaks the chain."""
    session.add(_decision(T0))
    await session.commit()
    await chain.seal_once(session, now=T0 + timedelta(seconds=200))

    session.add(_decision(T0 + timedelta(seconds=200)))
    await session.commit()
    await chain.seal_once(session, now=T0 + timedelta(seconds=400))

    batches = (await session.execute(
        select(ArbiterDecisionChainModel)
        .order_by(ArbiterDecisionChainModel.sequence))).scalars().all()
    assert len(batches) >= 2

    # Delete the first batch and everything it sealed — a thorough rewrite.
    await session.execute(
        ArbiterDecisionModel.__table__.delete()
        .where(ArbiterDecisionModel.chain_sequence == batches[0].sequence))
    await session.delete(batches[0])
    await session.commit()

    ok, problem = await chain.verify_chain(session)
    assert ok is False
    assert "missing" in problem or "link" in problem


async def test_an_empty_window_is_still_sealed(session):
    """Skipping empty windows would leave gaps indistinguishable from deletions."""
    session.add(_decision(T0))
    session.add(_decision(T0 + timedelta(seconds=200)))   # 3 windows later
    await session.commit()

    await chain.seal_once(session, now=T0 + timedelta(seconds=400))
    batches = (await session.execute(
        select(ArbiterDecisionChainModel)
        .order_by(ArbiterDecisionChainModel.sequence))).scalars().all()
    assert len(batches) >= 3
    assert any(b.decision_count == 0 for b in batches), "empty windows must be sealed"
    # And the windows are contiguous, which is what makes a gap meaningful.
    for a, b in zip(batches, batches[1:]):
        assert a.window_end == b.window_start


async def test_sealing_is_idempotent(session, sealed):
    before = (await session.execute(
        select(ArbiterDecisionChainModel))).scalars().all()
    await chain.seal_once(session, now=T0 + timedelta(seconds=300))
    after = (await session.execute(
        select(ArbiterDecisionChainModel))).scalars().all()
    assert len(after) == len(before)
    ok, problem = await chain.verify_chain(session)
    assert ok, problem


async def test_a_decision_inside_the_settle_window_is_not_sealed_yet(session):
    """Sealing up to `now` would strand a row written in an open transaction."""
    now = datetime.now(timezone.utc)
    session.add(_decision(now))
    await session.commit()
    await chain.seal_once(session, now=now)
    row = (await session.execute(select(ArbiterDecisionModel))).scalars().one()
    assert row.chain_sequence is None


async def test_a_late_decision_is_picked_up_by_the_next_seal(session):
    """The sealer resumes from the tip, so a restart loses no evidence."""
    session.add(_decision(T0))
    await session.commit()
    await chain.seal_once(session, now=T0 + timedelta(seconds=200))

    session.add(_decision(T0 + timedelta(seconds=210)))
    await session.commit()
    await chain.seal_once(session, now=T0 + timedelta(seconds=400))

    rows = (await session.execute(select(ArbiterDecisionModel))).scalars().all()
    assert all(r.chain_sequence is not None for r in rows)
    ok, problem = await chain.verify_chain(session)
    assert ok, problem


async def test_the_batch_hash_ignores_row_order():
    rows = [_decision(T0), _decision(T0 + timedelta(seconds=1))]
    a = chain.compute_batch_hash(chain.GENESIS, rows, T0, T0 + timedelta(seconds=60))
    b = chain.compute_batch_hash(chain.GENESIS, list(reversed(rows)), T0,
                                 T0 + timedelta(seconds=60))
    assert a == b


async def test_the_batch_hash_depends_on_the_previous_link():
    rows = [_decision(T0)]
    a = chain.compute_batch_hash(chain.GENESIS, rows, T0, T0 + timedelta(seconds=60))
    b = chain.compute_batch_hash("f" * 64, rows, T0, T0 + timedelta(seconds=60))
    assert a != b
