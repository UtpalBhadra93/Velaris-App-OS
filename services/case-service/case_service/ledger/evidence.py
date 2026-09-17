"""F-01 — the `ai_governance` evidence pack.

An Article 22 request is not "show me your logs". It is: *this decision was
made about me — what made it, on what basis, was a human involved, and can you
prove the record has not been edited since?* Four questions, and a pack that
answers three of them is not a defence.

So the pack is built from the ledger plus the chain, and it **states its own
verification result rather than assuming it**. A dossier that says "verified"
without having recomputed the links is worse than no dossier: it is a claim an
opposing expert can falsify in one command.

It plugs into the existing SOC2 / ISO27001 generator in
`compliance/reports.py` rather than starting a second one — same JSON shape,
same period arguments, same download route.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from velaris_shared.db.models import AIDecisionRecordModel

from . import chain, records

log = logging.getLogger(__name__)

#: The controls this pack evidences. Named the way an assessor names them, so
#: the pack can be handed over without a translation layer.
CONTROLS = [
    "EU AI Act Art. 12 — record-keeping over the system's lifetime",
    "EU AI Act Art. 14 — human oversight, and what the human actually did",
    "GDPR Art. 22 — no solely automated decision without a route to a person",
    "GDPR Art. 5(2) — accountability: the record is provably unedited",
    "GDPR Art. 30 — processing activities, including which model and where it ran",
]


async def build_pack(
    session: AsyncSession,
    *,
    tenant_id: Any,
    period_start: datetime,
    period_end: datetime,
    case_id: Any | None = None,
    generated_by: str | None = None,
) -> dict[str, Any]:
    """The `ai_governance` pack, for a period or for one case.

    Per-case is the Article 22 shape — a data subject asks about *their*
    decision — and per-period is the conformity-assessment shape. They are one
    function because the evidence is the same evidence; only the filter differs,
    and two implementations would eventually disagree about what counts.
    """
    q = select(AIDecisionRecordModel).where(
        AIDecisionRecordModel.tenant_id == records._as_uuid(tenant_id, "tenant id"),
        AIDecisionRecordModel.recorded_at >= period_start,
        AIDecisionRecordModel.recorded_at < period_end,
    )
    if case_id is not None:
        q = q.where(AIDecisionRecordModel.case_id
                    == records._as_uuid(case_id, "case id"))
    rows = list((await session.execute(
        q.order_by(AIDecisionRecordModel.recorded_at))).scalars())

    verdicts = await records.latest_verdicts(session, [r.id for r in rows])
    ok, note = await chain.verify_chain(session)

    decisions = []
    for r in rows:
        v = verdicts.get(str(r.id))
        decisions.append({
            "record_id": str(r.id),
            "recorded_at": r.recorded_at,
            "purpose": r.purpose,
            "case_id": str(r.case_id) if r.case_id else None,
            "step_id": r.step_id,
            # Who decided, and what answered. The two halves of "which agent,
            # which model" that this ledger exists to make sayable.
            "agent": {
                "id": str(r.agent_id) if r.agent_id else None,
                "slug": r.agent_slug,
                "kind": r.agent_kind,
            },
            "model": {"id": r.model_id, "provider_class": r.provider_class},
            "input": {
                "sha256": r.input_sha256,
                "data_classes": r.input_data_classes,
                # Stated explicitly rather than inferred from a null: "we chose
                # not to keep the prompt" and "the prompt is missing" are
                # different answers to an auditor.
                "raw_retained": bool(r.prompt_sealed),
            },
            "output": {"sha256": r.output_sha256,
                       "raw_retained": bool(r.output_sealed)},
            "confidence": (float(r.confidence) if r.confidence is not None else None),
            "latency_ms": (float(r.latency_ms) if r.latency_ms is not None else None),
            "policy_decision_id": (str(r.arbiter_decision_id)
                                   if r.arbiter_decision_id else None),
            "human_review": (
                None if v is None else {
                    "verdict": v.verdict,
                    "by": v.decided_by,
                    "at": v.decided_at,
                    "reason": v.reason,
                    "diff_sha256": v.diff_sha256,
                }
            ),
            "sealed_in_batch": r.chain_sequence,
        })

    reviewed = sum(1 for d in decisions if d["human_review"] is not None)
    overridden = sum(1 for d in decisions
                     if d["human_review"] and d["human_review"]["verdict"] in
                     ("edited", "rejected"))
    unsealed = sum(1 for d in decisions if d["sealed_in_batch"] is None)
    external = sum(1 for d in decisions if d["model"]["provider_class"] == "external")

    pack = {
        "framework": "ai_governance",
        "framework_name": "AI governance (EU AI Act + GDPR Art. 22)",
        "controls_covered": CONTROLS,
        "scope": {"tenant_id": str(tenant_id),
                  "case_id": str(case_id) if case_id else None},
        "period": {"start": period_start.isoformat(), "end": period_end.isoformat()},
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "generated_by": generated_by,
        "evidence": {
            "integrity": {
                "chain_verified": ok,
                # Carried through verbatim. When retention has pruned windows
                # this names them, and an auditor is entitled to that sentence
                # rather than a bare "true".
                "note": note,
                "decisions_not_yet_sealed": unsealed,
            },
            "model_inventory": await records.model_inventory(
                session, tenant_id=tenant_id),
            "human_oversight": {
                "decisions": len(decisions),
                "reviewed": reviewed,
                "overridden": overridden,
                # None rather than 0.0 on an empty denominator: a 0% override
                # rate over zero reviews reads as "the model is never wrong"
                # and means "nobody looked".
                "override_rate": (round(overridden / reviewed, 4) if reviewed else None),
                "review_coverage": (round(reviewed / len(decisions), 4)
                                    if decisions else None),
            },
            "sovereignty": {
                "decisions_run_externally": external,
                "decisions_run_locally": len(decisions) - external,
            },
            "decisions": decisions,
        },
    }
    summary = {
        "decisions": len(decisions),
        "reviewed": reviewed,
        "overridden": overridden,
        "external_calls": external,
        "chain_verified": ok,
        "unsealed": unsealed,
    }
    return {"pack": pack, "summary": summary, "chain_verified": ok}
