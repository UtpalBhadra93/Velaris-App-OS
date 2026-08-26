"""HxSentinel P1 — automatic signal extraction.

`extract_signals()` had exactly one caller: the manual
`POST /risk/cases/{id}/signals/extract` endpoint. Nothing invoked it during
normal case work, so in production `case_risk_signals` stayed empty, every rule
had nothing to match on, and the engine produced no findings however many cases
flowed through. The rules compare a case against OTHER cases' signals, so it is
not enough to extract lazily for the case being evaluated — the corpus has to be
populated as cases move.

The design calls for "nightly / on-close extraction (HxEvolve-style job)", and
that is what this is: an on-close hook for the moment a case's data and document
verifications are final, plus a periodic sweep that catches everything the hook
missed (cases edited after closure, a worker that was down, historical rows).

Both paths share the same posture as the verify gate:

  * ZERO DB work when HxSentinel is off platform-wide;
  * extraction is idempotent (unique index on case_id+signal_type+signal_key),
    so overlapping sweeps and a re-fired hook are harmless;
  * FAIL OPEN on any error — signal extraction must never wedge case work.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)

#: Statuses at which a case's data and document verifications are final. Same
#: set the retro-labelling queue uses, so "extractable" and "labellable" cannot
#: drift apart.
TERMINAL_STATUSES = ("closed", "resolved", "cancelled")

#: Sweep cadence and lookback. The lookback deliberately exceeds the interval so
#: consecutive sweeps overlap — a case updated during a sweep is picked up by the
#: next one instead of falling through the gap. Idempotency makes the overlap free.
SWEEP_INTERVAL_SECONDS = 3600
SWEEP_LOOKBACK_HOURS = 3

#: Upper bound on cases touched per sweep, so a large backfill degrades into
#: several quiet cycles rather than one long transaction holding the DB.
SWEEP_BATCH_LIMIT = 500


def _enabled() -> bool:
    from case_service.api.routers.releases import is_feature_enabled
    return is_feature_enabled("hxsentinel")


async def extract_on_close(session: AsyncSession, case_id: uuid.UUID, new_status: str) -> None:
    """Extract a case's signals when it reaches a terminal status.

    Called from the status-change machinery. Best-effort and non-fatal: a
    HxSentinel problem must never prevent a case from closing.
    """
    if new_status not in TERMINAL_STATUSES or not _enabled():
        return
    try:
        from case_service.sentinel import service as sentinel
        added = await sentinel.extract_signals(session, case_id)
        if getattr(added, "skipped", None):
            # Not an error — extraction fails closed by design — but silence here
            # is what let an empty signal store look like a case with no signals.
            logger.info("HxSentinel: case %s closed, signals skipped: %s",
                        case_id, "; ".join(added.skipped))
        elif added:
            logger.info("HxSentinel: extracted %d signal(s) on close of %s", len(added), case_id)
    except Exception as exc:                  # never block a close
        logger.warning("HxSentinel: on-close extraction failed for %s "
                       "(non-fatal): %s", case_id, exc)


async def sweep(session: AsyncSession, *, lookback_hours: int = SWEEP_LOOKBACK_HOURS,
                limit: int = SWEEP_BATCH_LIMIT) -> dict:
    """Extract signals for recently-updated terminal cases. Returns a summary.

    Flushes but deliberately does NOT commit: committing a session it does not
    own would also commit whatever else the caller had pending, which is a trap
    for any future caller that is not the cron. The cron owns its session and
    commits there.
    """
    from velaris_shared.db.models import CaseInstanceModel
    from case_service.sentinel import service as sentinel

    cutoff = datetime.now(timezone.utc) - timedelta(hours=lookback_hours)
    cases = (await session.execute(
        select(CaseInstanceModel.id)
        .where(CaseInstanceModel.status.in_(TERMINAL_STATUSES),
               CaseInstanceModel.updated_at >= cutoff)
        .order_by(CaseInstanceModel.updated_at.desc())
        .limit(limit)
    )).scalars().all()

    scanned = added = skipped = 0
    for cid in cases:
        try:
            rows = await sentinel.extract_signals(session, cid)
            scanned += 1
            added += len(rows)
            skipped += len(getattr(rows, "skipped", ()))
        except Exception as exc:              # one bad case must not stop the sweep
            logger.warning("HxSentinel sweep: extraction failed for %s: %s", cid, exc)
    return {"scanned": scanned, "signals_added": added, "cases_with_skips": skipped}


async def signal_extraction_cron() -> None:
    """Periodic sweep. Registered at startup alongside the other crons."""
    import asyncio
    from velaris_shared.db.session import get_session_factory

    await asyncio.sleep(25)     # let the DB come up and the flag cache warm
    while True:
        try:
            if _enabled():
                # The cron owns this session, so committing here is safe — see sweep().
                async with get_session_factory()() as session:
                    result = await sweep(session)
                    if result["signals_added"]:
                        await session.commit()
                        logger.info("HxSentinel sweep: %s", result)
        except Exception as exc:
            logger.warning("HxSentinel signal sweep failed: %s", exc)
        await asyncio.sleep(SWEEP_INTERVAL_SECONDS)
