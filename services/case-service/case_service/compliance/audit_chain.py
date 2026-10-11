"""Audit log hash chain — out-of-band tamper detection.

Design:
  - Audit writes are NOT modified. Existing append_audit_entry stays fast.
  - A separate sealer (sync or scheduled) walks new audit rows and writes
    one hash-chain row per audit row.
  - Each chain row stores prev_hash + content_hash where:
       content_hash = sha256(prev_hash || canonical(audit_row))
  - Tampering with audit OR chain is detectable: rebuild content_hash from
    audit data, compare to stored content_hash. Any divergence flags a break.

Performance: O(n) one-pass scan for both seal and verify. Sealer state
(sequence + last_hash) read once per call from the most recent chain row.

Pruning (mig 216, 2026-10-11): retention may delete old audit rows. It does so
through `prune_audit_rows`, which seals a PRUNE RECORD into this same chain
naming the sequences it removed and a digest of their hashes. `verify_chain`
then counts those rows as `pruned` and still passes; a sealed row that is gone
with no valid record covering it is `missing_audit_row`, as before. A prune
record is a link in the chain, so it is as hard to forge as the chain: it can
be appended, never slipped in or stretched, and appending one is itself a
sealed, dated entry.
"""
from __future__ import annotations
import hashlib
import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import delete, select, func, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

# Postgres advisory lock key for seal serialization — b"HELIXCHA" as big-endian int64.
# Held for the duration of the sealing transaction; auto-released on commit/rollback.
_SEAL_LOCK_KEY = int.from_bytes(b"HELIXCHA", "big")

from velaris_shared.db.models import (
    AuditChainModel, AuditChainPruneModel, CaseAuditLogModel,
)

log = logging.getLogger(__name__)
GENESIS_HASH = "0" * 64
#: Inclusive sequence ranges one prune record may carry; a larger prune is
#: written as several records.
PRUNE_MAX_RANGES = 500


def compute_row_hash(audit_row: CaseAuditLogModel, prev_hash: str) -> str:
    """Canonical-JSON hash of an audit row chained off prev_hash."""
    canonical = json.dumps({
        "audit_log_id": str(audit_row.id),
        "case_id": str(audit_row.case_id) if audit_row.case_id else None,
        "action": audit_row.action,
        "actor_id": audit_row.actor_id,
        "actor_type": getattr(audit_row, "actor_type", None),
        "details": audit_row.details or {},
        "timestamp": audit_row.timestamp.isoformat() if audit_row.timestamp else None,
        "prev_hash": prev_hash,
    }, sort_keys=True, default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def compute_prune_hash(rec: AuditChainPruneModel, prev_hash: str) -> str:
    """Canonical-JSON hash of a prune record chained off prev_hash."""
    canonical = json.dumps({
        "kind": "audit_rows_pruned",
        "prune_id": str(rec.id),
        "reason": rec.reason,
        "actor": rec.actor,
        "cutoff": rec.cutoff,
        "recorded_at": rec.recorded_at,
        "pruned_count": int(rec.pruned_count),
        "ranges": [[int(a), int(b)] for a, b in (rec.ranges or [])],
        "digest": rec.digest,
        "prev_hash": prev_hash,
    }, sort_keys=True)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _digest(hashes: list[str]) -> str:
    return hashlib.sha256("".join(hashes).encode("ascii")).hexdigest()


def _to_ranges(sequences: list[int]) -> list[list[int]]:
    """Sorted sequences → inclusive [first, last] runs."""
    out: list[list[int]] = []
    for s in sequences:
        if out and s == out[-1][1] + 1:
            out[-1][1] = s
        else:
            out.append([s, s])
    return out


_prunes_seen = False
_prunes_missing_logged = False


async def prunes_available(session: AsyncSession) -> bool:
    """Whether migration 216 has been applied. A missing table is "no prune
    records", exactly as before it existed; any other failure propagates."""
    global _prunes_seen, _prunes_missing_logged
    if _prunes_seen:
        return True
    try:
        async with session.begin_nested():
            await session.execute(select(AuditChainPruneModel.id).limit(1))
    except DBAPIError as exc:
        msg = str(getattr(exc, "orig", exc)).lower()
        if not any(s in msg for s in ("does not exist", "doesn't exist", "no such table",
                                      "undefinedtable")):
            raise
        if not _prunes_missing_logged:
            _prunes_missing_logged = True
            log.warning("case_audit_log_chain_prunes is missing (migration 216 not "
                        "applied) — pruned audit rows will verify as missing")
        return False
    _prunes_seen = True
    return True


def _reset_cache_for_tests() -> None:
    global _prunes_seen, _prunes_missing_logged
    _prunes_seen = False
    _prunes_missing_logged = False


async def _seal_lock(session: AsyncSession) -> None:
    try:
        async with session.begin_nested():
            await session.execute(text(f"SELECT pg_advisory_xact_lock({_SEAL_LOCK_KEY})"))
    except Exception:
        pass  # Non-Postgres — the unique sequence is the fallback guard


async def prune_audit_rows(session: AsyncSession, where: tuple, *, reason: str,
                           actor: str, cutoff: datetime | None = None) -> int:
    """Delete the `case_audit_log` rows matching `where`, on the record.

    The ONE way a legitimate job may remove audit rows. Those that were sealed
    get a prune record sealed into the chain in the same transaction, so the
    chain still verifies afterwards. Rows never sealed leave no chain row, and
    need no record. Returns the number of audit rows deleted.
    """
    await _seal_lock(session)
    sealed = (await session.execute(
        select(AuditChainModel.sequence, AuditChainModel.content_hash)
        .join(CaseAuditLogModel, CaseAuditLogModel.id == AuditChainModel.audit_log_id)
        .where(*where).order_by(AuditChainModel.sequence.asc()))).all()
    deleted = int((await session.execute(
        delete(CaseAuditLogModel).where(*where))).rowcount or 0)
    if not sealed or not deleted:
        return deleted
    if not await prunes_available(session):
        return deleted

    hash_of = {int(s): h for s, h in sealed}
    ranges = _to_ranges(sorted(hash_of))
    last = await _last_chain_row(session)
    last_seq = last.sequence if last else 0
    last_hash = last.content_hash if last else GENESIS_HASH
    now = datetime.now(timezone.utc).isoformat()
    for i in range(0, len(ranges), PRUNE_MAX_RANGES):
        part = ranges[i:i + PRUNE_MAX_RANGES]
        seqs = [s for a, b in part for s in range(a, b + 1)]
        rec = AuditChainPruneModel(
            id=uuid.uuid4(), reason=str(reason)[:40], actor=str(actor)[:255],
            cutoff=cutoff.isoformat() if cutoff else None, recorded_at=now,
            pruned_count=len(seqs), ranges=part,
            digest=_digest([hash_of[s] for s in seqs]))
        session.add(rec)
        h = compute_prune_hash(rec, last_hash)
        session.add(AuditChainModel(
            id=uuid.uuid4(), sequence=last_seq + 1, audit_log_id=rec.id,
            prev_hash=last_hash, content_hash=h))
        last_hash, last_seq = h, last_seq + 1
    await session.flush()
    log.info("pruned %d audit rows (%d sealed) for %s; chain tip=%d",
             deleted, len(hash_of), reason, last_seq)
    return deleted


async def _valid_prunes(session: AsyncSession) -> tuple[dict, dict, set[int]]:
    """Every prune record, judged: `(by_id, problem_by_id, covered_sequences)`.

    A record covers its sequences only when it is sealed, its own hash holds,
    every sequence it names is older than its own, and its digest matches the
    chain rows it names — so a record cannot be edited, or stretched over a
    row it did not prune.
    """
    if not await prunes_available(session):
        return {}, {}, set()
    recs = list((await session.execute(select(AuditChainPruneModel))).scalars().all())
    if not recs:
        return {}, {}, set()
    rows = (await session.execute(select(
        AuditChainModel.sequence, AuditChainModel.audit_log_id,
        AuditChainModel.prev_hash, AuditChainModel.content_hash))).all()
    hash_at = {int(s): h for s, _, _, h in rows}
    link_of = {aid: (int(s), prev, h) for s, aid, prev, h in rows}

    by_id, problems, covered = {}, {}, set()
    for rec in recs:
        by_id[rec.id] = rec
        link = link_of.get(rec.id)
        if link is None:
            continue                              # never sealed: covers nothing
        seq, prev, stored = link
        if compute_prune_hash(rec, prev) != stored:
            problems[rec.id] = "the record was changed after it was sealed"
            continue
        try:
            seqs = [s for a, b in rec.ranges for s in range(int(a), int(b) + 1)]
        except (TypeError, ValueError):
            problems[rec.id] = "the record's ranges are unreadable"
            continue
        if not seqs or len(seqs) != int(rec.pruned_count) or max(seqs) >= seq:
            problems[rec.id] = "the record names rows it could not have pruned"
            continue
        if any(s not in hash_at for s in seqs) or _digest([hash_at[s] for s in seqs]) != rec.digest:
            problems[rec.id] = "the record does not match the rows it names"
            continue
        covered.update(seqs)
    return by_id, problems, covered


async def _last_chain_row(session: AsyncSession) -> AuditChainModel | None:
    q = select(AuditChainModel).order_by(AuditChainModel.sequence.desc()).limit(1)
    res = await session.execute(q)
    return res.scalar_one_or_none()


async def _unsealed_audit_rows(
    session: AsyncSession, after_id: uuid.UUID | None,
) -> list[CaseAuditLogModel]:
    """Audit rows not yet sealed.

    Strategy: rely on monotonic timestamp + id ordering. We seal in
    timestamp order; rows with timestamp > last_sealed_timestamp OR
    same timestamp + id > last_sealed_id are unsealed.

    Simpler: NOT IN (SELECT audit_log_id FROM chain). We use a LEFT JOIN
    via subquery for portability across SQLite + Postgres.
    """
    sealed_ids_subq = select(AuditChainModel.audit_log_id).subquery()
    q = (
        select(CaseAuditLogModel)
        .where(CaseAuditLogModel.id.notin_(select(sealed_ids_subq.c.audit_log_id)))
        .order_by(CaseAuditLogModel.timestamp.asc(), CaseAuditLogModel.id.asc())
    )
    res = await session.execute(q)
    return list(res.scalars().all())


async def seal_new_entries(session: AsyncSession, max_rows: int = 10000) -> dict:
    """Seal up to max_rows new audit entries. Returns count + new tip hash.

    Acquires a Postgres transaction-level advisory lock before reading the
    chain tip, so concurrent seal calls queue up rather than racing to insert
    duplicate sequence numbers.
    """
    await _seal_lock(session)
    last = await _last_chain_row(session)
    last_seq = last.sequence if last else 0
    last_hash = last.content_hash if last else GENESIS_HASH

    pending = await _unsealed_audit_rows(session, last.audit_log_id if last else None)
    pending = pending[:max_rows]

    sealed = 0
    for audit_row in pending:
        h = compute_row_hash(audit_row, last_hash)
        chain = AuditChainModel(
            id=uuid.uuid4(),
            sequence=last_seq + 1,
            audit_log_id=audit_row.id,
            prev_hash=last_hash,
            content_hash=h,
        )
        session.add(chain)
        last_hash = h
        last_seq += 1
        sealed += 1

    if sealed:
        await session.flush()
        log.info("sealed %d audit entries (tip=%d)", sealed, last_seq)

    return {
        "sealed": sealed,
        "tip_sequence": last_seq,
        "tip_hash": last_hash,
        "sealed_at": datetime.now(timezone.utc).isoformat(),
    }


async def verify_chain(session: AsyncSession, limit: int | None = None) -> dict:
    """Walk the chain in order. Recompute hashes. Report any breaks.

    Returns: {verified, chain_length, breaks: [...], tip_sequence, tip_hash,
    pruned, prunes: [...]}. Rows removed under a valid prune record are counted
    in `pruned` and are not breaks; `verified` is about breaks alone.
    """
    prune_by_id, prune_problem, covered = await _valid_prunes(session)
    pruned = 0
    prunes: list[dict] = []
    q = select(AuditChainModel).order_by(AuditChainModel.sequence.asc())
    if limit:
        q = q.limit(limit)
    res = await session.execute(q)
    chain = list(res.scalars().all())

    breaks: list[dict] = []
    expected_prev = GENESIS_HASH

    for row in chain:
        # 1. Linkage check
        if row.prev_hash != expected_prev:
            breaks.append({
                "sequence": row.sequence,
                "type": "broken_link",
                "expected_prev": expected_prev,
                "actual_prev": row.prev_hash,
            })
            # Continue with row.prev_hash as new baseline so downstream
            # rows aren't all reported as broken too:
            expected_prev = row.content_hash
            continue

        # 2. Content check
        audit_row = await session.get(CaseAuditLogModel, row.audit_log_id)
        rec = prune_by_id.get(row.audit_log_id) if audit_row is None else None
        if rec is not None:
            # This link seals a prune record, not an audit row.
            if rec.id in prune_problem:
                breaks.append({
                    "sequence": row.sequence,
                    "type": "prune_record_invalid",
                    "prune_id": str(rec.id),
                    "problem": prune_problem[rec.id],
                })
            else:
                prunes.append({
                    "sequence": row.sequence, "prune_id": str(rec.id),
                    "reason": rec.reason, "actor": rec.actor, "cutoff": rec.cutoff,
                    "recorded_at": rec.recorded_at, "pruned_count": int(rec.pruned_count),
                })
            expected_prev = row.content_hash
            continue
        if audit_row is None and row.sequence in covered:
            pruned += 1
            expected_prev = row.content_hash
            continue
        if audit_row is None:
            breaks.append({
                "sequence": row.sequence,
                "type": "missing_audit_row",
                "audit_log_id": str(row.audit_log_id),
            })
            expected_prev = row.content_hash
            continue

        recomputed = compute_row_hash(audit_row, row.prev_hash)
        if recomputed != row.content_hash:
            breaks.append({
                "sequence": row.sequence,
                "type": "content_tampered",
                "audit_log_id": str(row.audit_log_id),
                "stored_hash": row.content_hash,
                "recomputed_hash": recomputed,
            })

        expected_prev = row.content_hash

    return {
        "verified": len(breaks) == 0,
        "chain_length": len(chain),
        "breaks": breaks,
        "tip_sequence": chain[-1].sequence if chain else 0,
        "tip_hash": chain[-1].content_hash if chain else GENESIS_HASH,
        "pruned": pruned,
        "prunes": prunes,
    }


async def chain_status(session: AsyncSession) -> dict:
    """Lightweight summary — no verification scan."""
    q_audit = select(func.count()).select_from(CaseAuditLogModel)
    q_chain = select(func.count()).select_from(AuditChainModel)
    audit_count = (await session.execute(q_audit)).scalar_one()
    chain_count = (await session.execute(q_chain)).scalar_one()
    last = await _last_chain_row(session)
    return {
        "audit_rows": int(audit_count),
        "sealed_rows": int(chain_count),
        "unsealed_rows": int(audit_count - chain_count),
        "tip_sequence": last.sequence if last else 0,
        "tip_hash": last.content_hash if last else GENESIS_HASH,
        "last_sealed_at": last.sealed_at.isoformat() if last and last.sealed_at else None,
    }
