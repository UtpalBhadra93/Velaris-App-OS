"""HxArbiter — sealing the decision log so it is evidence, not just rows.

A decision log nobody can prove is unedited answers "what did the engine decide"
and not "what did the engine decide, provably". The second is the question an
auditor asks, and it is one of the two things §18.7 claims no competing engine
ships — so it has to actually hold.

The shape mirrors `case_audit_log_chain` (migration 027) rather than inventing a
second one: `content_hash = sha256(prev_hash || canonical(batch))`, and the tip
is what `compliance.audit_anchor` already obtains RFC-3161 receipts over. A DB
administrator who rewrites both the decisions and this chain still cannot forge
anything older than the newest receipt.

**Batched, not per-decision.** Chaining each decision individually would put
every evaluation behind one sequence, which is a throughput ceiling on the hot
path — and the hot path is ordinary request handling. One row per interval
(default 60 s) costs nothing and proves the same thing.

Two properties worth stating because they are what make the seal meaningful:

* **A gap in the sealed windows is itself evidence.** The windows are contiguous
  by construction, so a missing interval says rows were removed.
* **Sealing is idempotent and resumable.** It seals whatever is unsealed and
  older than the settle delay, so a process that dies mid-run loses nothing and
  a restarted one does not double-seal.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from velaris_shared.db.models import (
    ArbiterDecisionChainModel, ArbiterDecisionModel, ArbiterNamespaceModel)

log = logging.getLogger(__name__)

#: How much of the log one batch covers.
BATCH_SECONDS = 60

#: How long a decision is left alone before it can be sealed. A decision written
#: inside an open transaction is not visible yet; sealing right up to `now`
#: would leave it unsealed forever, since the sealer only ever looks forward.
SETTLE_SECONDS = 5

#: The genesis link. Same convention as the audit chain.
GENESIS = "0" * 64

#: How often the background loop runs.
LOOP_INTERVAL_SECONDS = 60

#: Most batches one pass will seal. The loop walks forward in fixed windows from
#: the oldest unsealed decision, so a single row bearing a far-past timestamp —
#: a restored backup, a clock that jumped, a badly seeded fixture — would
#: otherwise spin out millions of empty batches in one call. Capping means such
#: a row is caught up over several passes instead of taking the process down;
#: 1440 is a day of one-minute windows.
MAX_BATCHES_PER_RUN = 1440


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def compute_batch_hash(
    prev_hash: str,
    rows: list[ArbiterDecisionModel],
    window_start: datetime,
    window_end: datetime,
) -> str:
    """`sha256(prev_hash || canonical(batch))`.

    Only the fields that make a decision what it is are hashed, sorted by id so
    the digest cannot depend on the order rows came back in. Hashing a mutable
    field — `chain_sequence`, which this very process is about to write — would
    make the seal unverifiable a moment after it was made.

    **Callers must pass rows READ BACK FROM THE DATABASE, never rows still held
    in a session's identity map.** `evaluated_at` is hashed via `isoformat()`,
    and an aware value renders as `…+00:00` where the same instant read back
    from a naive column renders without the offset. Seal one way and verify the
    other and the digests differ for a reason that has nothing to do with
    tampering. `seal_once` and `verify_chain` both re-read, so this holds — it
    is written down because the next caller might not, and the failure would
    look exactly like a compromised log.

    The normalisation is deliberately NOT applied here: every seal already
    written was computed this way, and changing the function would invalidate
    every one of them at once.
    """
    payload = {
        "window_start": window_start.isoformat(),
        "window_end": window_end.isoformat(),
        "decisions": [
            {
                "id": str(r.id),
                "tenant_id": str(r.tenant_id),
                "namespace": r.namespace,
                "action": r.action,
                "subject_kind": r.subject_kind,
                "subject_id": r.subject_id,
                "effect": r.effect,
                "mode": r.mode,
                "enforced": bool(r.enforced),
                "default_applied": bool(r.default_applied),
                "matched": r.matched,
                "obligations": r.obligations,
                "legacy_effect": r.legacy_effect,
                "policy_set_hash": r.policy_set_hash,
                "error": r.error,
                "evaluated_at": r.evaluated_at.isoformat() if r.evaluated_at else None,
            }
            for r in sorted(rows, key=lambda x: str(x.id))
        ],
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256((prev_hash + canonical).encode("utf-8")).hexdigest()


async def _tip(session: AsyncSession) -> tuple[int, str, datetime | None]:
    """(sequence, hash, window_end) of the newest sealed batch."""
    row = (await session.execute(
        select(ArbiterDecisionChainModel)
        .order_by(ArbiterDecisionChainModel.sequence.desc()).limit(1)
    )).scalars().first()
    if row is None:
        return 0, GENESIS, None
    return row.sequence, row.content_hash, row.window_end


async def seal_once(session: AsyncSession, *, now: datetime | None = None) -> int:
    """Seal every complete batch that is ready. Returns how many were sealed.

    An **empty window is still sealed**. Skipping empty ones would leave gaps
    that are indistinguishable from deleted rows, and the whole point of
    contiguous windows is that a gap means something.
    """
    now = now or _utcnow()
    horizon = now - timedelta(seconds=SETTLE_SECONDS)

    sequence, prev_hash, last_end = await _tip(session)

    if last_end is None:
        oldest = (await session.execute(
            select(func.min(ArbiterDecisionModel.evaluated_at))
            .where(ArbiterDecisionModel.chain_sequence.is_(None))
        )).scalar_one_or_none()
        if oldest is None:
            return 0                       # nothing has ever been decided
        if oldest.tzinfo is None:
            oldest = oldest.replace(tzinfo=timezone.utc)
        window_start = oldest
    else:
        window_start = last_end if last_end.tzinfo else last_end.replace(
            tzinfo=timezone.utc)

    sealed = 0
    while sealed < MAX_BATCHES_PER_RUN:
        window_end = window_start + timedelta(seconds=BATCH_SECONDS)
        if window_end > horizon:
            break                          # the window is not closed yet

        # `populate_existing` for the same reason `verify_chain` uses it, and it
        # has to be on BOTH sides or they disagree: sealing from a session's
        # cached rows and verifying from the database renders `evaluated_at`
        # two different ways when the column is naive (MySQL, SQLite), so the
        # digests differ for a reason that has nothing to do with tampering.
        # In production the sealer already runs in its own fresh session, so
        # this changes no seal that has ever been written.
        rows = (await session.execute(
            select(ArbiterDecisionModel).where(
                ArbiterDecisionModel.chain_sequence.is_(None),
                ArbiterDecisionModel.evaluated_at >= window_start,
                ArbiterDecisionModel.evaluated_at < window_end,
            ).execution_options(populate_existing=True)
        )).scalars().all()

        sequence += 1
        content_hash = compute_batch_hash(prev_hash, list(rows), window_start, window_end)
        session.add(ArbiterDecisionChainModel(
            id=uuid.uuid4(),
            sequence=sequence,
            window_start=window_start,
            window_end=window_end,
            decision_count=len(rows),
            prev_hash=prev_hash,
            content_hash=content_hash,
            sealed_at=_utcnow(),
        ))
        if rows:
            await session.execute(
                update(ArbiterDecisionModel)
                .where(ArbiterDecisionModel.id.in_([r.id for r in rows]))
                .values(chain_sequence=sequence)
            )
        await session.flush()

        prev_hash = content_hash
        window_start = window_end
        sealed += 1

    if sealed:
        await session.commit()
    if sealed >= MAX_BATCHES_PER_RUN:
        log.warning(
            "hxarbiter: hit the %d-batch ceiling in one pass; the backlog "
            "continues on the next run", MAX_BATCHES_PER_RUN)
    return sealed


async def verify_chain(session: AsyncSession) -> tuple[bool, str | None]:
    """Recompute every link. Returns (ok, first problem).

    This is the whole product claim executed as code: if it returns False, the
    decision log has been altered since it was sealed.
    """
    batches = (await session.execute(
        select(ArbiterDecisionChainModel)
        .order_by(ArbiterDecisionChainModel.sequence)
        .execution_options(populate_existing=True)
    )).scalars().all()

    prev_hash = GENESIS
    expected_seq = 1
    pruned: list[dict] = []
    for batch in batches:
        if batch.sequence != expected_seq:
            return False, (f"sequence jumps from {expected_seq - 1} to "
                           f"{batch.sequence}: a sealed batch is missing")
        if batch.prev_hash != prev_hash:
            return False, f"batch {batch.sequence} does not link to the one before it"

        if batch.pruned_at is not None:
            # A batch whose decisions retention deleted. Its content cannot be
            # re-derived — that is what deleting them means — but the LINK is
            # still checked above and below, so the chain continues to prove
            # that nothing before or after it was edited.
            #
            # Skipping the content check here is the only honest option. The
            # alternatives are to report a deliberate, recorded, operator-
            # authorised deletion as tampering (which makes retention unusable,
            # and was the behaviour that shipped in mig 180), or to re-seal the
            # batch over its new contents — which would let anyone erase
            # evidence and leave a chain that says everything is fine.
            pruned.append({"sequence": batch.sequence,
                           "pruned_at": batch.pruned_at,
                           "decisions_removed": batch.pruned_count})
            prev_hash = batch.content_hash
            expected_seq += 1
            continue

        # `populate_existing` refreshes from the database instead of handing
        # back whatever this session already holds. Without it, a verifier run
        # on a session with the rows cached — any session created with
        # `expire_on_commit=False`, which is an ordinary choice — checks the
        # cache rather than the record, and reports "verified" over tampered
        # data. Found while building the F-01 ledger's chain, whose test edited
        # a sealed row and watched verification succeed.
        rows = (await session.execute(
            select(ArbiterDecisionModel)
            .where(ArbiterDecisionModel.chain_sequence == batch.sequence)
            .execution_options(populate_existing=True)
        )).scalars().all()
        if len(rows) != batch.decision_count:
            return False, (f"batch {batch.sequence} sealed {batch.decision_count} "
                           f"decisions and now holds {len(rows)}. If this was a "
                           f"retention purge, the batch was not tombstoned — "
                           f"which is a bug, not a purge.")

        ws = batch.window_start if batch.window_start.tzinfo else \
            batch.window_start.replace(tzinfo=timezone.utc)
        we = batch.window_end if batch.window_end.tzinfo else \
            batch.window_end.replace(tzinfo=timezone.utc)
        if compute_batch_hash(prev_hash, list(rows), ws, we) != batch.content_hash:
            return False, f"batch {batch.sequence} does not match what was sealed"

        prev_hash = batch.content_hash
        expected_seq += 1

    if pruned:
        # Reported, never hidden. "It verifies" and "it verifies, and these
        # eleven windows were emptied on 3 March" are different answers, and an
        # auditor is entitled to the second one.
        total = sum(p["decisions_removed"] or 0 for p in pruned)
        return True, (
            f"verified; {len(pruned)} batch(es) were pruned by retention, "
            f"removing {total} decision(s). Their content can no longer be "
            f"re-derived — the chain still proves nothing was edited.")
    return True, None


async def sealer_loop() -> None:                              # pragma: no cover
    """Background task. Failure is logged and retried, never fatal.

    A sealer that dies takes the *evidence* with it, not the platform: decisions
    keep being written and are sealed late when it comes back, because
    `seal_once` resumes from the tip rather than from now.
    """
    from velaris_shared.db.session import get_session_factory

    while True:
        try:
            async with get_session_factory()() as session:
                n = await seal_once(session)
                # Refreshed here rather than on the hot path: it is a COUNT
                # over the log, and the sealer is the only thing that already
                # runs on a schedule and cares about the answer.
                try:
                    from velaris_shared.observability import metrics as _m

                    unsealed = (await session.execute(
                        select(func.count()).select_from(ArbiterDecisionModel)
                        .where(ArbiterDecisionModel.chain_sequence.is_(None))
                    )).scalar_one()
                    _m.arbiter_unsealed_decisions.set(float(unsealed))
                except Exception:                              # noqa: BLE001
                    pass
                if n:
                    log.info("hxarbiter: sealed %d decision batch(es)", n)
        except asyncio.CancelledError:
            raise
        except Exception as exc:                              # noqa: BLE001
            log.error("hxarbiter: decision sealing failed: %s", exc)
        await asyncio.sleep(LOOP_INTERVAL_SECONDS)


# ── retention ────────────────────────────────────────────────────────────────


async def purge_expired_decisions(
    session: AsyncSession, *, now: datetime | None = None, limit: int = 5_000,
) -> dict[str, int]:
    """Delete decisions past their namespace's retention horizon.

    The decision log had no retention of any kind: nothing in
    `compliance/retention.py` touched `arbiter_decisions`, so it grew without
    bound and no erasure request could reach it.

    Four rules, and the first two are what make this safe to run unattended:

    * **NULL retention means keep forever**, and that is the default on every
      namespace. An operator opts a namespace in; an upgrade never does it for
      them. Purging evidence has legal weight and is not an update's decision.
    * **An UNSEALED row is never deleted.** A gap in the sealed windows is
      itself evidence that rows were removed (§10), so deleting a row the chain
      has not yet covered would look identical to tampering. They are simply
      skipped until the sealer catches up.
    * Bounded per run, so a first purge on a large log does not hold a
      transaction open across millions of rows.
    * Counted per namespace and returned, because "we deleted some evidence"
      is a sentence that needs a number next to it in a log.

    Note what this deliberately does NOT do: it does not delete the CHAIN rows.
    The chain keeps proving that the log was not edited; a purge that also
    rewrote the chain would destroy the only thing that could show a purge from
    a tampering.
    """
    now = now or _utcnow()
    specs = (await session.execute(
        select(ArbiterNamespaceModel.namespace,
               ArbiterNamespaceModel.decision_retention_days)
    )).all()

    purged: dict[str, int] = {}
    for namespace, days in specs:
        if not days or int(days) <= 0:
            continue                       # keep forever — the default
        cutoff = now - timedelta(days=int(days))
        ids = (await session.execute(
            select(ArbiterDecisionModel.id).where(
                ArbiterDecisionModel.namespace == namespace,
                ArbiterDecisionModel.evaluated_at < cutoff,
                ArbiterDecisionModel.chain_sequence.isnot(None),
            ).limit(limit)
        )).scalars().all()
        if not ids:
            continue
        # Which sealed batches these rows belong to — needed BEFORE the delete,
        # because afterwards there is nothing left to ask.
        affected = {
            r[0] for r in (await session.execute(
                select(ArbiterDecisionModel.chain_sequence)
                .where(ArbiterDecisionModel.id.in_(ids))
                .distinct()
            )).all() if r[0] is not None
        }
        await session.execute(
            delete(ArbiterDecisionModel).where(ArbiterDecisionModel.id.in_(ids)))
        purged[namespace] = len(ids)

        # Tombstone every batch that lost rows. Without this the chain stops
        # verifying the moment retention runs, which is how mig 180 shipped —
        # a retention horizon that destroyed the tamper-evidence the product is
        # sold on. A batch may be pruned more than once (two namespaces, two
        # runs), so the count accumulates.
        for sequence in sorted(affected):
            remaining = (await session.execute(
                select(func.count()).select_from(ArbiterDecisionModel)
                .where(ArbiterDecisionModel.chain_sequence == sequence)
            )).scalar_one()
            batch = (await session.execute(
                select(ArbiterDecisionChainModel)
                .where(ArbiterDecisionChainModel.sequence == sequence)
            )).scalars().first()
            if batch is None:
                continue
            removed = (batch.decision_count or 0) - int(remaining)
            batch.pruned_count = (batch.pruned_count or 0) + max(0, removed)
            batch.pruned_at = now
        log.warning(
            "hxarbiter retention: deleted %d decision(s) in namespace %s older "
            "than %d days", len(ids), namespace, int(days))
    if purged:
        await session.flush()
    return purged


async def erase_subject(session: AsyncSession, subject_id: str) -> dict[str, int]:
    """Erase what the decision log holds ABOUT a data subject, keeping the seal.

    A GDPR erasure could not reach `arbiter_decisions` at all: retention purges
    by age, and `anonymize_user_data` had never heard of the table. Every
    decision about a person kept their subject payload, their resource payload
    and the request context indefinitely — in a log designed never to be
    editable.

    **What is scrubbed and what is not is decided by the hash, not by taste.**
    `compute_batch_hash` covers `subject_id` and does NOT cover `subject`,
    `resource` or `context`:

    * the three JSON blobs are where the personal detail actually lives, and
      they can be emptied without making a single sealed batch unverifiable;
    * `subject_id` is hashed, so changing it would break the chain from that
      batch onward — the log would stop being able to prove it had not been
      edited, which is a worse outcome for everyone including the data subject.

    So the identifier is retained as integrity-protecting audit data and
    everything else about the person goes. That is the same position the case
    audit log already takes, and it is a defensible one: the record that a
    decision happened is not erased, the person behind it is.

    Sealed and unsealed rows alike — an erasure that stopped at the seal would
    be arbitrary, and the seal is unaffected either way.
    """
    result = await session.execute(
        update(ArbiterDecisionModel)
        .where(ArbiterDecisionModel.subject_id == str(subject_id))
        .values(subject={}, resource={}, context={"erased": True})
    )
    count = int(result.rowcount or 0)
    if count:
        log.warning(
            "hxarbiter: erased the subject/resource/context payloads of %d "
            "decision(s) for subject %s; subject_id retained because it is "
            "covered by the chain hash", count, subject_id)
        await session.flush()
    return {"arbiter_decisions": count}
