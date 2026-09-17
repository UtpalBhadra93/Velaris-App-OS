"""F-02 — pricing a decision, explainably.

Velaris already priced a case's HUMAN time: `rate_cards` holds an hourly rate,
`case_event_log.duration_seconds` is recorded per activity, and
`costing/service.py` multiplies them. What it could not price is the AI half.

The word that decides this module's shape is **explainable**. A single number
per case is a bill nobody can argue with, which is the opposite of what an
operator asking "why did this case cost that" needs. So every cost is a LINE
ITEM carrying `quantity × unit_rate = amount`, and a total is the sum of its
lines — a total you can take apart.

Three rules:

* **Token counts come from the chokepoint, never from a re-derivation.**
  `GuardedBackend.complete` stashes what it measured; re-hashing the text at the
  call site would produce a second, quietly different number for the same call.
* **An unpriced line is zero AND SAYS SO.** `rate_source='unpriced'` is the
  difference between "this was free" and "we have no rate for this model", and
  a report that conflates them is a lie an operator acts on.
* **Nothing is ever re-priced in place.** A rate change appends new lines; last
  month's invoice must not move when this month's contract does.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import logging
import uuid
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from velaris_shared.db.models import (
    AIDecisionRecordModel,
    DecisionCostModel,
    ModelRateCardModel,
    _utcnow,
)

log = logging.getLogger(__name__)

#: Rates are published per million tokens, which is how every provider quotes
#: and how `llmops.MODEL_RATES` already estimates.
TOKENS_PER_UNIT = Decimal(1_000_000)

#: What this table stores. LABOUR is deliberately absent: it is derived on read
#: from `case_event_log.duration_seconds` and the tenant's `rate_cards` row,
#: both of which already exist and are already the source of truth. Persisting a
#: copy would mean two answers to one question, diverging the moment a rate
#: changed. AI cost has to be stored because the token counts are ephemeral —
#: measured once, at the chokepoint, and then gone.
KINDS = frozenset({"ai"})
UNITS = frozenset({"mtok_in", "mtok_out", "hour"})


class CostingError(ValueError):
    """The caller asked for a price that cannot be produced."""


def _as_uuid(value: Any, what: str) -> uuid.UUID:
    if isinstance(value, uuid.UUID):
        return value
    try:
        return uuid.UUID(str(value))
    except (ValueError, TypeError, AttributeError) as exc:
        raise CostingError(f"{what} is not a valid id") from exc


def _tenant_key(tenant_id: uuid.UUID | None) -> str:
    """'platform' for the inherited default, the id otherwise.

    The NULL-uniqueness fix from migration 177: `UNIQUE (tenant_id, model_id)`
    cannot constrain platform rows, because NULL <> NULL.
    """
    return "platform" if tenant_id is None else str(tenant_id)


# ── rate cards ───────────────────────────────────────────────────────────────

async def resolve_rate(
    session: AsyncSession, *, tenant_id: Any | None, model_id: str,
) -> tuple[ModelRateCardModel | None, str]:
    """The rate that applies, and where it came from.

    Tenant first, platform second, nothing third. Returning the SOURCE beside
    the row is not decoration: a tenant priced from the platform default is
    paying a rate nobody negotiated for them, and an operator reconciling an
    invoice needs to see which lines those are.
    """
    tid = _as_uuid(tenant_id, "tenant id") if tenant_id is not None else None
    if tid is not None:
        row = (await session.execute(
            select(ModelRateCardModel).where(
                ModelRateCardModel.tenant_key == str(tid),
                ModelRateCardModel.model_id == str(model_id),
            )
        )).scalar_one_or_none()
        if row is not None:
            return row, "tenant"
    row = (await session.execute(
        select(ModelRateCardModel).where(
            ModelRateCardModel.tenant_key == "platform",
            ModelRateCardModel.model_id == str(model_id),
        )
    )).scalar_one_or_none()
    if row is not None:
        return row, "platform"
    return None, "unpriced"


async def upsert_rate(
    session: AsyncSession,
    *,
    tenant_id: Any | None,
    model_id: str,
    input_per_mtok: float,
    output_per_mtok: float,
    currency: str = "USD",
    created_by: Any | None = None,
) -> ModelRateCardModel:
    """Set a model's price for one tenant, or platform-wide when tenant is None.

    Updating a rate does NOT re-price anything already priced. That is the
    point of the line items being append-only: a contract that changes today
    changes today's costs, not last quarter's.
    """
    if float(input_per_mtok) < 0 or float(output_per_mtok) < 0:
        raise CostingError("a rate cannot be negative")
    tid = _as_uuid(tenant_id, "tenant id") if tenant_id is not None else None
    key = _tenant_key(tid)
    row = (await session.execute(
        select(ModelRateCardModel).where(
            ModelRateCardModel.tenant_key == key,
            ModelRateCardModel.model_id == str(model_id),
        )
    )).scalar_one_or_none()
    if row is None:
        row = ModelRateCardModel(
            id=uuid.uuid4(), tenant_id=tid, tenant_key=key,
            model_id=str(model_id)[:255],
            created_by=(_as_uuid(created_by, "actor id")
                        if created_by is not None else None))
        session.add(row)
    row.input_per_mtok = Decimal(str(input_per_mtok))
    row.output_per_mtok = Decimal(str(output_per_mtok))
    row.currency = str(currency)[:8] or "USD"
    row.updated_at = _utcnow()
    await session.flush()
    return row


async def list_rates(
    session: AsyncSession, *, tenant_id: Any | None,
) -> list[dict[str, Any]]:
    """Every rate that applies here — the tenant's own, plus inherited defaults.

    Inherited rows are marked, so an operator can see at a glance which models
    they have never priced for themselves.
    """
    tid = _as_uuid(tenant_id, "tenant id") if tenant_id is not None else None
    rows = list((await session.execute(
        select(ModelRateCardModel).where(
            ModelRateCardModel.tenant_key.in_([_tenant_key(tid), "platform"])
        ).order_by(ModelRateCardModel.model_id)
    )).scalars())
    own = {r.model_id for r in rows if r.tenant_key != "platform"}
    out = []
    for r in rows:
        inherited = r.tenant_key == "platform"
        if inherited and r.model_id in own:
            continue                      # the tenant's own row wins; show one
        out.append({
            "model_id": r.model_id,
            "input_per_mtok": float(r.input_per_mtok),
            "output_per_mtok": float(r.output_per_mtok),
            "currency": r.currency,
            "source": "platform" if inherited else "tenant",
            "inherited": inherited,
        })
    return out


# ── pricing ──────────────────────────────────────────────────────────────────

def _line(
    *, tenant_id: uuid.UUID, kind: str, unit: str, quantity: Decimal,
    unit_rate: Decimal, currency: str, rate_source: str, estimated: bool,
    record_id: uuid.UUID | None = None, case_id: uuid.UUID | None = None,
    step_id: str | None = None, model_id: str | None = None,
) -> DecisionCostModel:
    if kind not in KINDS:
        raise CostingError(f"unknown cost kind {kind!r}")
    if unit not in UNITS:
        raise CostingError(f"unknown cost unit {unit!r}")
    return DecisionCostModel(
        id=uuid.uuid4(), tenant_id=tenant_id, record_id=record_id,
        case_id=case_id, step_id=step_id, kind=kind, model_id=model_id,
        quantity=quantity, unit=unit, unit_rate=unit_rate,
        # Quantised at the column's own scale so `quantity * unit_rate` and the
        # stored `amount` agree exactly. A float here would make the arithmetic
        # a caller checks fail on the sixth decimal, which is precisely the
        # check this table exists to survive.
        amount=(quantity * unit_rate).quantize(Decimal("0.000001")),
        currency=currency, rate_source=rate_source, estimated=estimated,
        priced_at=_utcnow(),
    )


async def price_ai_decision(
    session: AsyncSession,
    record: AIDecisionRecordModel,
    *,
    input_tokens: int,
    output_tokens: int,
    estimated: bool = True,
) -> list[DecisionCostModel]:
    """Two line items for one AI decision: input tokens and output tokens.

    Two, not one, because an operator asking why a case cost what it did should
    see that the answer was mostly OUTPUT — which is the actionable half, since
    it is the one a shorter prompt does not fix.

    An unknown model still writes both lines, at rate zero, marked `unpriced`.
    Writing nothing would make an unpriced call indistinguishable from a call
    that never happened, and reporting it as free would be worse.
    """
    if input_tokens < 0 or output_tokens < 0:
        raise CostingError("token counts cannot be negative")

    rate, source = await resolve_rate(
        session, tenant_id=record.tenant_id, model_id=record.model_id)
    in_rate = Decimal(str(rate.input_per_mtok)) if rate else Decimal(0)
    out_rate = Decimal(str(rate.output_per_mtok)) if rate else Decimal(0)
    currency = rate.currency if rate else "USD"

    lines = [
        _line(tenant_id=record.tenant_id, kind="ai", unit="mtok_in",
              quantity=(Decimal(input_tokens) / TOKENS_PER_UNIT),
              unit_rate=in_rate, currency=currency, rate_source=source,
              estimated=estimated, record_id=record.id, case_id=record.case_id,
              step_id=record.step_id, model_id=record.model_id),
        _line(tenant_id=record.tenant_id, kind="ai", unit="mtok_out",
              quantity=(Decimal(output_tokens) / TOKENS_PER_UNIT),
              unit_rate=out_rate, currency=currency, rate_source=source,
              estimated=estimated, record_id=record.id, case_id=record.case_id,
              step_id=record.step_id, model_id=record.model_id),
    ]
    for line in lines:
        session.add(line)
    await session.flush()
    return lines


async def price_from_usage(
    session: AsyncSession,
    record: AIDecisionRecordModel,
    usage: dict[str, Any] | None,
) -> list[DecisionCostModel]:
    """Price a decision from what the chokepoint measured.

    `usage` is `GuardedBackend.last_usage`. `None` means the call did not go
    through the guarded chokepoint — a stubbed backend in a test, or a caller
    holding a raw backend — and produces NO cost lines at all rather than a
    zero one. A zero here would say "this decision cost nothing", when the truth
    is "nobody measured it".
    """
    if not usage:
        return []
    try:
        return await price_ai_decision(
            session, record,
            input_tokens=int(usage.get("prompt_tokens") or 0),
            output_tokens=int(usage.get("completion_tokens") or 0),
            estimated=bool(usage.get("tokens_estimated", True)),
        )
    except CostingError:
        raise
    except Exception:                                          # noqa: BLE001
        log.warning("costing: could not price decision %s", record.id,
                    exc_info=True)
        return []


# ── reporting ────────────────────────────────────────────────────────────────

async def labour_lines(
    session: AsyncSession, *, tenant_slug: Any, case_id: Any,
) -> list[dict[str, Any]]:
    """The human half, DERIVED rather than stored.

    `case_event_log.duration_seconds` and the tenant's `rate_cards` row are
    already the source of truth for this, so a stored copy would be a second
    answer to one question that diverges the moment a rate changes. It is
    computed here in the same line-item shape as the AI half so a breakdown
    reads as one document.

    Only MANUAL time is priced. `case_time_rollup` separates it from automated
    time because a workflow step that ran in three seconds is not three seconds
    of someone's salary, and charging for it would make automation look
    expensive in exactly the report used to justify it.

    `tenant_slug`, not the id: `rate_cards` keys on the slug, which is the
    convention of the module this borrows from.
    """
    from case_service.costing import service as _labour

    rollup = await _labour.case_time_rollup(session, _as_uuid(case_id, "case id"))
    manual_seconds = int(rollup.get("manual_seconds") or 0)
    if manual_seconds <= 0:
        return []

    rate_row = await _labour.get_default_rate(session, tenant_slug)
    hours = (Decimal(manual_seconds) / Decimal(3600))
    unit_rate = Decimal(str(rate_row.hourly_rate)) if rate_row else Decimal(0)
    return [{
        "id": None,                       # derived: there is no row to point at
        "record_id": None,
        "step_id": None,
        "kind": "labour",
        "model_id": None,
        "quantity": float(hours),
        "unit": "hour",
        "unit_rate": float(unit_rate),
        "amount": float((hours * unit_rate).quantize(Decimal("0.000001"))),
        "currency": (rate_row.currency if rate_row else "USD"),
        # Same honesty as an unpriced model: a tenant with no rate card gets
        # zero AND is told the rate is missing, never a zero that reads as
        # "this case took no human effort".
        "rate_source": "tenant" if rate_row else "unpriced",
        "estimated": False,               # recorded durations, not estimates
        "derived": True,
    }]


async def case_breakdown(
    session: AsyncSession, *, tenant_id: Any, case_id: Any,
    tenant_slug: Any | None = None,
) -> dict[str, Any]:
    """What one case cost, and the arithmetic behind it.

    Reconciles by construction: `total` is the sum of `lines`, and every line
    carries its own `quantity × unit_rate`. The AI and labour halves are
    reported separately AND summed, because the interesting number is usually
    the ratio rather than either one.

    Omitting `tenant_slug` returns the AI half only — and says so via
    `labour_included`, rather than reporting a total that silently excludes
    every human hour on the case.
    """
    tid = _as_uuid(tenant_id, "tenant id")
    cid = _as_uuid(case_id, "case id")
    rows = list((await session.execute(
        select(DecisionCostModel).where(
            DecisionCostModel.tenant_id == tid,
            DecisionCostModel.case_id == cid,
        ).order_by(DecisionCostModel.priced_at)
    )).scalars())

    lines = [
        {
            "id": str(r.id),
            "record_id": str(r.record_id) if r.record_id else None,
            "step_id": r.step_id,
            "kind": r.kind,
            "model_id": r.model_id,
            "quantity": float(r.quantity),
            "unit": r.unit,
            "unit_rate": float(r.unit_rate),
            "amount": float(r.amount),
            "currency": r.currency,
            "rate_source": r.rate_source,
            "estimated": bool(r.estimated),
            "derived": False,
        }
        for r in rows
    ]
    derived = ([] if tenant_slug is None
               else await labour_lines(session, tenant_slug=tenant_slug, case_id=cid))
    lines.extend(derived)

    by_kind: dict[str, float] = {}
    for l in lines:
        by_kind[l["kind"]] = by_kind.get(l["kind"], 0.0) + l["amount"]

    currencies = {l["currency"] for l in lines}
    unpriced = [l for l in lines if l["rate_source"] == "unpriced"]

    return {
        "case_id": str(cid),
        "lines": lines,
        # Stated, never assumed. A total that omits the human half is a
        # different number from a total that includes it and found none.
        "labour_included": tenant_slug is not None,
        "by_kind": by_kind,
        "total": round(sum(by_kind.values()), 6),
        # A single currency is asserted rather than assumed. Summing two
        # currencies into one number is the arithmetic error that makes a cost
        # report worthless, and it is silent unless somebody checks.
        "currency": (currencies.pop() if len(currencies) == 1 else None),
        "mixed_currency": len(currencies) > 1,
        # Named, not hidden. A total that excludes three unpriced models is not
        # the cost of the case, and the reader has to be told which they are.
        "unpriced_lines": len(unpriced),
        "unpriced_models": sorted({l["model_id"] for l in unpriced if l["model_id"]}),
        "estimated_tokens": any(l["estimated"] for l in lines if l["kind"] == "ai"),
    }


async def tenant_rollup(
    session: AsyncSession, *, tenant_id: Any, since: Any | None = None,
) -> dict[str, Any]:
    """Cost per model and per kind across a tenant.

    Deliberately computed from the line items rather than a materialised
    rollup: the volumes here are one row per AI call, and a cached total that
    can disagree with the lines it summarises is a support ticket waiting to
    happen. A materialised view is a change to make when a real install is
    slow, with its numbers checked against this.
    """
    tid = _as_uuid(tenant_id, "tenant id")
    q = select(DecisionCostModel).where(DecisionCostModel.tenant_id == tid)
    if since is not None:
        q = q.where(DecisionCostModel.priced_at >= since)
    rows = list((await session.execute(q)).scalars())

    by_model: dict[str, dict[str, Any]] = {}
    by_kind: dict[str, float] = {}
    for r in rows:
        by_kind[r.kind] = by_kind.get(r.kind, 0.0) + float(r.amount)
        if r.kind != "ai" or not r.model_id:
            continue
        bucket = by_model.setdefault(
            r.model_id, {"amount": 0.0, "mtok_in": 0.0, "mtok_out": 0.0,
                         "rate_source": r.rate_source})
        bucket["amount"] += float(r.amount)
        bucket[r.unit] = bucket.get(r.unit, 0.0) + float(r.quantity)
    for bucket in by_model.values():
        bucket["amount"] = round(bucket["amount"], 6)

    return {
        "by_kind": {k: round(v, 6) for k, v in by_kind.items()},
        "by_model": by_model,
        "total": round(sum(by_kind.values()), 6),
        "lines": len(rows),
    }
