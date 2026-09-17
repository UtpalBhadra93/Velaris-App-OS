"""F-01 — writing and reading the AI decision ledger.

The one rule that shapes this file: **the ledger stores commitments, not
content.** An input hash proves "this input produced this output" without the
ledger becoming a second copy of every prompt — and prompts carry case content,
which is the personal data the rest of the platform works to minimise. A
governance record that quietly becomes the largest PII store on the box is a
worse outcome than no record at all.

Raw text is written only when an HxArbiter policy attaches the `record_prompt`
obligation, and then it is sealed under the tenant DEK so a crypto-shred takes
it with everything else.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import hashlib
import logging
import uuid
from typing import Any, Iterable

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from velaris_shared.db.models import (
    AIDecisionRecordModel,
    AIDecisionVerdictModel,
    _utcnow,
)

log = logging.getLogger(__name__)

#: The closed verdict vocabulary. `unreviewed` is deliberately absent — an
#: absent row IS unreviewed, and a value for it would make "nobody has looked
#: at this" indistinguishable from "somebody looked and recorded nothing".
VERDICTS: frozenset[str] = frozenset({"accepted", "edited", "rejected"})

#: The obligation kind that turns raw storage on. Named here so the PEP call
#: site and this writer cannot drift; `hxarbiter.pep` deliberately leaves it
#: unregistered, so a policy attaching it DENIES unless a call site opts in.
OBLIGATION_RECORD_PROMPT = "record_prompt"


class LedgerError(ValueError):
    """The caller asked for something the ledger cannot record."""


def digest(text: str | bytes | None) -> str | None:
    """SHA-256 of the text, or None for nothing at all.

    An empty string hashes to the empty-string digest rather than None: "the
    model was given nothing" and "we did not record what it was given" are
    different facts and must not collapse into one.
    """
    if text is None:
        return None
    raw = text.encode("utf-8") if isinstance(text, str) else text
    return hashlib.sha256(raw).hexdigest()


def _seal(tenant_id: Any, text: str | None, context: str) -> str | None:
    """Encrypt under the tenant DEK, or return None.

    Never raises. A ledger row that could not seal its raw text is written
    WITHOUT it — the hashes and the attribution still stand, which is the part
    that is evidence. Failing the AI call because its optional transcript could
    not be encrypted would trade a real capability for a nice-to-have.
    """
    if not text:
        return None
    try:
        from case_service import hxvault
        return hxvault.encrypt(tenant_id, text.encode("utf-8"), context)
    except Exception:                                          # noqa: BLE001
        log.warning("ledger: raw text not sealed for %s; the record is written "
                    "with hashes only", context, exc_info=True)
        return None


async def record(
    session: AsyncSession,
    *,
    tenant_id: Any,
    purpose: str,
    model_id: str,
    provider_class: str = "local",
    agent_id: Any | None = None,
    agent_slug: str | None = None,
    agent_kind: str | None = None,
    case_id: Any | None = None,
    step_id: str | None = None,
    input_text: str | None = None,
    output_text: str | None = None,
    input_sha256: str | None = None,
    output_sha256: str | None = None,
    data_classes: Iterable[str] = (),
    confidence: float | None = None,
    latency_ms: float | None = None,
    arbiter_decision_id: Any | None = None,
    store_raw: bool = False,
) -> AIDecisionRecordModel:
    """Write one decision. The caller commits.

    `input_text` is hashed and then **dropped** unless `store_raw` is true, and
    `store_raw` should only ever come from an honoured `record_prompt`
    obligation — never from a caller's own opinion, and never from a request
    body.

    A caller that already holds a hash (the `ai_action` path stores
    `prompt_sha256` today) may pass `input_sha256` instead of the text, so
    wiring an existing call site does not mean re-reading a prompt it has
    already discarded.
    """
    purpose = str(purpose).strip()
    if not purpose:
        raise LedgerError("a ledger record needs a purpose")
    in_hash = input_sha256 or digest(input_text)
    if not in_hash:
        # Refused rather than defaulted. A record with no input commitment
        # cannot answer the question the ledger exists for, and a row of zeros
        # would look like an answer.
        raise LedgerError(
            "a ledger record needs an input commitment: pass input_text or "
            "input_sha256")

    tid = _as_uuid(tenant_id, "tenant id")
    row = AIDecisionRecordModel(
        id=uuid.uuid4(),
        tenant_id=tid,
        agent_id=_as_uuid(agent_id, "agent id") if _is_uuidish(agent_id) else None,
        # An unresolvable agent still records its NAME. `unregistered:<slug>`
        # says which worker acted, which is worth more than a null.
        agent_slug=(str(agent_slug or agent_id)[:128] if (agent_slug or agent_id) else None),
        agent_kind=str(agent_kind)[:20] if agent_kind else None,
        model_id=str(model_id or "unknown/unknown")[:255],
        provider_class="external" if str(provider_class) == "external" else "local",
        purpose=purpose[:64],
        case_id=_as_uuid(case_id, "case id") if _is_uuidish(case_id) else None,
        step_id=str(step_id)[:255] if step_id else None,
        input_sha256=in_hash,
        input_data_classes=sorted({str(c) for c in (data_classes or ())}),
        output_sha256=output_sha256 or digest(output_text),
        prompt_sealed=_seal(tid, input_text, "ai_ledger.prompt") if store_raw else None,
        output_sealed=_seal(tid, output_text, "ai_ledger.output") if store_raw else None,
        confidence=confidence,
        latency_ms=latency_ms,
        arbiter_decision_id=(_as_uuid(arbiter_decision_id, "decision id")
                             if _is_uuidish(arbiter_decision_id) else None),
        recorded_at=_utcnow(),
    )
    session.add(row)
    await session.flush()
    return row


async def record_verdict(
    session: AsyncSession,
    *,
    record_id: Any,
    verdict: str,
    decided_by: str | None = None,
    reason: str = "",
    diff_text: str | None = None,
    store_raw: bool = False,
) -> AIDecisionVerdictModel:
    """Append what a human decided about one record.

    Appends — never updates. A second review inserts a second row, and the
    first stays visible, because "accepted, then reversed three days later" is
    the sequence an override-rate report exists to show.
    """
    verdict = str(verdict).strip()
    if verdict not in VERDICTS:
        raise LedgerError(
            f"unknown verdict {verdict!r}; the vocabulary is "
            + ", ".join(sorted(VERDICTS)))
    rec = await session.get(AIDecisionRecordModel, _as_uuid(record_id, "record id"))
    if rec is None:
        raise LedgerError("no such ledger record")

    row = AIDecisionVerdictModel(
        id=uuid.uuid4(),
        record_id=rec.id,
        tenant_id=rec.tenant_id,
        verdict=verdict,
        diff_sha256=digest(diff_text),
        diff_sealed=_seal(rec.tenant_id, diff_text, "ai_ledger.diff") if store_raw else None,
        reason=str(reason or "")[:4000],
        decided_by=str(decided_by)[:255] if decided_by else None,
        decided_at=_utcnow(),
    )
    session.add(row)
    await session.flush()
    return row


def _is_uuidish(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, uuid.UUID):
        return True
    try:
        uuid.UUID(str(value))
        return True
    except (ValueError, TypeError, AttributeError):
        return False


def _as_uuid(value: Any, what: str) -> uuid.UUID:
    if isinstance(value, uuid.UUID):
        return value
    try:
        return uuid.UUID(str(value))
    except (ValueError, TypeError, AttributeError) as exc:
        raise LedgerError(f"{what} is not a valid id") from exc


# ── reads ────────────────────────────────────────────────────────────────────

async def latest_verdicts(
    session: AsyncSession, record_ids: Iterable[Any],
) -> dict[str, AIDecisionVerdictModel]:
    """The newest verdict per record. Absent means unreviewed."""
    ids = [_as_uuid(r, "record id") for r in record_ids]
    if not ids:
        return {}
    rows = (await session.execute(
        select(AIDecisionVerdictModel)
        .where(AIDecisionVerdictModel.record_id.in_(ids))
        .order_by(AIDecisionVerdictModel.decided_at)
    )).scalars().all()
    out: dict[str, AIDecisionVerdictModel] = {}
    for r in rows:                       # ordered ascending, so the last wins
        out[str(r.record_id)] = r
    return out


async def list_records(
    session: AsyncSession,
    *,
    tenant_id: Any,
    case_id: Any | None = None,
    agent_id: Any | None = None,
    purpose: str | None = None,
    limit: int = 100,
) -> list[AIDecisionRecordModel]:
    q = select(AIDecisionRecordModel).where(
        AIDecisionRecordModel.tenant_id == _as_uuid(tenant_id, "tenant id"))
    if case_id is not None:
        q = q.where(AIDecisionRecordModel.case_id == _as_uuid(case_id, "case id"))
    if agent_id is not None:
        q = q.where(AIDecisionRecordModel.agent_id == _as_uuid(agent_id, "agent id"))
    if purpose:
        q = q.where(AIDecisionRecordModel.purpose == str(purpose))
    q = q.order_by(AIDecisionRecordModel.recorded_at.desc()).limit(max(1, min(limit, 500)))
    return list((await session.execute(q)).scalars())


async def model_inventory(
    session: AsyncSession, *, tenant_id: Any,
) -> list[dict[str, Any]]:
    """Which models decided anything here, how often, and how externally.

    The first question an AI Act conformity assessment asks is *what models are
    in use* — and the honest answer comes from what actually ran, not from the
    provider configuration, which says what would run next.
    """
    tid = _as_uuid(tenant_id, "tenant id")
    rows = (await session.execute(
        select(
            AIDecisionRecordModel.model_id,
            AIDecisionRecordModel.provider_class,
            func.count().label("calls"),
            func.min(AIDecisionRecordModel.recorded_at).label("first_seen"),
            func.max(AIDecisionRecordModel.recorded_at).label("last_seen"),
        )
        .where(AIDecisionRecordModel.tenant_id == tid)
        .group_by(AIDecisionRecordModel.model_id,
                  AIDecisionRecordModel.provider_class)
        .order_by(func.count().desc())
    )).all()
    return [
        {
            "model_id": r.model_id,
            "provider_class": r.provider_class,
            "calls": int(r.calls),
            "first_seen": r.first_seen,
            "last_seen": r.last_seen,
        }
        for r in rows
    ]


async def override_rates(
    session: AsyncSession, *, tenant_id: Any,
    since: Any | None = None, until: Any | None = None,
) -> dict[str, Any]:
    """How often a human disagreed, per model.

    Counted from the **newest** verdict per record: a decision reviewed twice
    is one decision, and counting both rows would report an override rate above
    100% for a diligent reviewer.
    """
    tid = _as_uuid(tenant_id, "tenant id")
    q = select(AIDecisionRecordModel).where(AIDecisionRecordModel.tenant_id == tid)
    if since is not None:
        q = q.where(AIDecisionRecordModel.recorded_at >= since)
    if until is not None:
        q = q.where(AIDecisionRecordModel.recorded_at < until)
    recs = list((await session.execute(q)).scalars())
    verdicts = await latest_verdicts(session, [r.id for r in recs])

    per_model: dict[str, dict[str, int]] = {}
    for rec in recs:
        bucket = per_model.setdefault(
            rec.model_id,
            {"decisions": 0, "reviewed": 0, "accepted": 0, "edited": 0, "rejected": 0})
        bucket["decisions"] += 1
        v = verdicts.get(str(rec.id))
        if v is not None:
            bucket["reviewed"] += 1
            bucket[v.verdict] = bucket.get(v.verdict, 0) + 1

    for bucket in per_model.values():
        reviewed = bucket["reviewed"]
        overridden = bucket["edited"] + bucket["rejected"]
        # None, not 0.0, when nothing was reviewed. A 0% override rate on zero
        # reviews reads as "the model is never wrong" and means "nobody looked".
        bucket["override_rate"] = (round(overridden / reviewed, 4)
                                   if reviewed else None)
    return {
        "total_decisions": len(recs),
        "total_reviewed": sum(b["reviewed"] for b in per_model.values()),
        "by_model": per_model,
    }
