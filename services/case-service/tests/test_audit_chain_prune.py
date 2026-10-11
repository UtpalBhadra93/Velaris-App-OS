"""Retention's audit purge made the audit chain report tampering — Oct_TODO §11h.

Proven first (2026-10-11): seal three audit rows, run retention's `audit_log`
handler, verify → `verified: false`, 3 × `missing_audit_row`. `verify_chain`
could not tell a row retention removed from a row somebody deleted.

Pinned:
* a retention purge leaves a chain that VERIFIES, and says how many rows went;
* a row deleted by hand is still a break — before, after and beside a purge;
* a prune record cannot be edited, stretched over another row, forged for a
  row it did not prune, or deleted, without the chain saying so;
* an install without migration 216 purges exactly as before;
* a dry run writes nothing.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio
from sqlalchemy import delete, func, select, update

from velaris_shared.db.models import (
    AuditChainModel,
    AuditChainPruneModel,
    CaseAuditLogModel,
    CaseInstanceModel,
    CaseTypeModel,
)

from case_service.compliance import audit_chain as ac
from case_service.compliance import retention
from tests.conftest import LIFECYCLE_PROCESS_ID, TestSessionFactory  # type: ignore[attr-defined]

pytestmark = pytest.mark.asyncio

NOW = datetime.now(timezone.utc)
OLD = NOW - timedelta(days=400)
CUTOFF = NOW - timedelta(days=365)


@pytest_asyncio.fixture
async def world():
    """One case with 4 old audit rows and 2 recent ones, all sealed."""
    ac._reset_cache_for_tests()
    async with TestSessionFactory() as db:
        ct = CaseTypeModel(id=uuid.uuid4(), name="Prune CT", version="1.0.0",
                           lifecycle_process_id=LIFECYCLE_PROCESS_ID, definition_json={})
        db.add(ct)
        await db.flush()
        case = CaseInstanceModel(
            id=uuid.uuid4(), case_number="PRUNE-1", case_type_id=ct.id,
            case_type_version="1.0.0", status="new", priority="medium",
            created_by="alice", data={})
        db.add(case)
        await db.flush()
        for i in range(4):
            db.add(CaseAuditLogModel(id=uuid.uuid4(), case_id=case.id, action=f"old_{i}",
                                     actor_id="alice", timestamp=OLD + timedelta(minutes=i)))
        for i in range(2):
            db.add(CaseAuditLogModel(id=uuid.uuid4(), case_id=case.id, action=f"new_{i}",
                                     actor_id="alice", timestamp=NOW - timedelta(minutes=9 - i)))
        await db.commit()
        assert (await ac.seal_new_entries(db))["sealed"] == 6
        await db.commit()
    yield case
    ac._reset_cache_for_tests()


async def _purge(apply: bool = True) -> int:
    async with TestSessionFactory() as db:
        n = await retention._audit_log(db, CUTOFF, apply)
        await db.commit()
        return n


async def _verify() -> dict:
    async with TestSessionFactory() as db:
        return await ac.verify_chain(db)


async def _count(model) -> int:
    async with TestSessionFactory() as db:
        return int((await db.execute(select(func.count()).select_from(model))).scalar_one())


async def test_a_retention_purge_leaves_a_chain_that_verifies(world):
    assert (await _verify())["verified"] is True
    assert await _purge() == 4

    v = await _verify()
    assert v["verified"] is True and v["breaks"] == [], v
    assert v["pruned"] == 4 and v["chain_length"] == 7           # 6 rows + the record
    [rec] = v["prunes"]
    assert rec["pruned_count"] == 4 and rec["reason"] == "retention"
    assert rec["sequence"] == 7 and rec["cutoff"] == CUTOFF.isoformat()

    # Sealing carries on over the record, and the next purge finds nothing.
    async with TestSessionFactory() as db:
        db.add(CaseAuditLogModel(id=uuid.uuid4(), case_id=world.id, action="later",
                                 actor_id="alice"))
        await db.commit()
        assert (await ac.seal_new_entries(db))["sealed"] == 1
        await db.commit()
    assert await _purge() == 0
    v = await _verify()
    assert v["verified"] is True and v["chain_length"] == 8 and len(v["prunes"]) == 1


async def test_a_dry_run_counts_and_writes_nothing(world):
    assert await _purge(apply=False) == 4
    assert await _count(CaseAuditLogModel) == 6
    assert await _count(AuditChainPruneModel) == 0
    assert (await _verify())["pruned"] == 0


async def test_rows_never_sealed_need_no_record(world):
    async with TestSessionFactory() as db:
        db.add(CaseAuditLogModel(id=uuid.uuid4(), case_id=world.id, action="old_unsealed",
                                 actor_id="alice", timestamp=OLD))
        await db.commit()
    assert await _purge() == 5
    v = await _verify()
    assert v["verified"] is True and v["pruned"] == 4


async def test_nothing_sealed_to_prune_writes_no_record(world):
    async with TestSessionFactory() as db:
        n = await retention._audit_log(db, OLD - timedelta(days=1), True)
        await db.commit()
    assert n == 0 and await _count(AuditChainPruneModel) == 0
    assert (await _verify())["chain_length"] == 6


async def test_a_row_deleted_by_hand_is_still_a_break(world):
    async with TestSessionFactory() as db:
        victim = (await db.execute(select(CaseAuditLogModel).where(
            CaseAuditLogModel.action == "new_0"))).scalar_one()
        await db.delete(victim)
        await db.commit()
    v = await _verify()
    assert v["verified"] is False
    assert [b["type"] for b in v["breaks"]] == ["missing_audit_row"]

    # A purge does not launder it: the record covers what the purge removed.
    assert await _purge() == 4
    v = await _verify()
    assert v["verified"] is False and v["pruned"] == 4
    assert [b["type"] for b in v["breaks"]] == ["missing_audit_row"]


async def test_a_row_deleted_after_the_purge_is_a_break(world):
    await _purge()
    async with TestSessionFactory() as db:
        await db.execute(delete(CaseAuditLogModel).where(CaseAuditLogModel.action == "new_1"))
        await db.commit()
    v = await _verify()
    assert v["verified"] is False and v["pruned"] == 4
    assert [(b["type"], b["sequence"]) for b in v["breaks"]] == [("missing_audit_row", 6)]


async def test_a_record_stretched_over_another_row_covers_nothing(world):
    """Delete a recent row, then widen the genuine record to name it too."""
    await _purge()
    async with TestSessionFactory() as db:
        await db.execute(delete(CaseAuditLogModel).where(CaseAuditLogModel.action == "new_0"))
        await db.execute(update(AuditChainPruneModel).values(
            ranges=[[1, 5]], pruned_count=5))
        await db.commit()
    v = await _verify()
    assert v["verified"] is False and v["pruned"] == 0 and v["prunes"] == []
    kinds = sorted(b["type"] for b in v["breaks"])
    assert kinds == ["missing_audit_row"] * 5 + ["prune_record_invalid"], v["breaks"]


async def test_a_forged_record_that_was_never_sealed_covers_nothing(world):
    async with TestSessionFactory() as db:
        await db.execute(delete(CaseAuditLogModel).where(CaseAuditLogModel.action == "new_0"))
        hashes = {s: h for s, h in (await db.execute(select(
            AuditChainModel.sequence, AuditChainModel.content_hash))).all()}
        db.add(AuditChainPruneModel(
            id=uuid.uuid4(), reason="retention", actor="system:retention", cutoff=None,
            recorded_at=NOW.isoformat(), pruned_count=1, ranges=[[5, 5]],
            digest=ac._digest([hashes[5]])))
        await db.commit()
    v = await _verify()
    assert v["verified"] is False and v["pruned"] == 0
    assert [b["type"] for b in v["breaks"]] == ["missing_audit_row"]


async def test_a_sealed_record_with_the_wrong_digest_or_a_future_row_is_refused(world):
    """A record appended by somebody who can write both tables still has to
    name rows older than itself and match their hashes."""
    async with TestSessionFactory() as db:
        await db.execute(delete(CaseAuditLogModel).where(CaseAuditLogModel.action == "new_0"))
        last = await ac._last_chain_row(db)
        for seq_off, ranges, digest in ((1, [[5, 5]], "0" * 64), (2, [[5, 99]], "0" * 64)):
            rec = AuditChainPruneModel(
                id=uuid.uuid4(), reason="retention", actor="x", cutoff=None,
                recorded_at=NOW.isoformat(),
                pruned_count=sum(b - a + 1 for a, b in ranges), ranges=ranges, digest=digest)
            db.add(rec)
            h = ac.compute_prune_hash(rec, last.content_hash)
            row = AuditChainModel(id=uuid.uuid4(), sequence=last.sequence + 1,
                                  audit_log_id=rec.id, prev_hash=last.content_hash,
                                  content_hash=h)
            db.add(row)
            await db.flush()
            last = row
        await db.commit()
    v = await _verify()
    assert v["verified"] is False and v["pruned"] == 0
    assert sorted(b["type"] for b in v["breaks"]) == [
        "missing_audit_row", "prune_record_invalid", "prune_record_invalid"]


async def test_deleting_the_record_brings_the_breaks_back(world):
    await _purge()
    async with TestSessionFactory() as db:
        await db.execute(delete(AuditChainPruneModel))
        await db.commit()
    v = await _verify()
    assert v["verified"] is False and v["pruned"] == 0
    assert len(v["breaks"]) == 5                       # the 4 rows and the record's own link


async def test_a_fragmented_purge_is_split_across_records(world, monkeypatch):
    monkeypatch.setattr(ac, "PRUNE_MAX_RANGES", 1)
    async with TestSessionFactory() as db:              # prune old_0 and old_2 only
        n = await ac.prune_audit_rows(
            db, (CaseAuditLogModel.action.in_(["old_0", "old_2"]),),
            reason="retention", actor="system:retention")
        await db.commit()
    assert n == 2
    v = await _verify()
    assert v["verified"] is True and v["pruned"] == 2
    assert [p["pruned_count"] for p in v["prunes"]] == [1, 1]
    assert await _purge() == 2                           # the rest, later
    v = await _verify()
    assert v["verified"] is True and v["pruned"] == 4 and len(v["prunes"]) == 4


async def test_without_migration_216_a_purge_behaves_as_before(world, monkeypatch):
    """The table is not there yet: the purge still runs, and verification is
    exactly what it was — never an error."""
    async def _no(_session):
        return False
    monkeypatch.setattr(ac, "prunes_available", _no)
    assert await _purge() == 4
    v = await _verify()
    assert v["verified"] is False and v["pruned"] == 0
    assert [b["type"] for b in v["breaks"]] == ["missing_audit_row"] * 4
