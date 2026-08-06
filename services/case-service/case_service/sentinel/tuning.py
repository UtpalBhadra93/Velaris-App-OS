"""HxSentinel P3c-1 — score-driven rule tuning (deterministic; no LLM).

Aggregates the reliability-weighted P2b scores per rule and turns a persistent
signal into a PROPOSAL a human approves. Nothing here changes a rule on its own:
``generate_proposals`` only writes pending rows; ``decide_proposal`` applies a
change to ``case_risk_rules`` only on explicit approval.

Anti-gaming (inherited from P3b): scores are weighted by the reviewer's
reliability, disputed scores are excluded, and a min-reviews floor is required —
so one miscalibrated or rubber-stamping reviewer cannot move a rule.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from case_service.db.models import (
    CaseRiskFeedbackModel,
    CaseRiskFindingModel,
    CaseRiskRuleModel,
    CaseRiskRuleProposalModel,
    CaseRiskScoreDisputeModel,
)
from case_service.sentinel import oversight
from case_service.sentinel import rules as rulecat

#: A reviewer whose reliability is unknown still counts, but at a neutral weight
#: — never at full trust — so brand-new reviewers cannot dominate a proposal.
_UNKNOWN_WEIGHT = 0.5

#: Defaults; a rule fires a proposal only past the review floor.
LOW_AVG = 2.0
HIGH_AVG = 4.5
MIN_REVIEWS = 5


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


async def analyze_rule_scores(session: AsyncSession, tenant: str) -> dict[str, dict]:
    """Per-rule reliability-weighted average score (disputed excluded)."""
    dash = await oversight.reviewer_dashboard(session, tenant)
    weight = {r["reviewer"]: (r["reliability"] if r["reliability"] is not None else _UNKNOWN_WEIGHT)
              for r in dash["reviewers"]}
    disputed = set((await session.execute(
        select(CaseRiskScoreDisputeModel.feedback_id)
        .where(CaseRiskScoreDisputeModel.tenant_id == tenant)
    )).scalars().all())

    rows = (await session.execute(
        select(CaseRiskFeedbackModel, CaseRiskFindingModel.rule_key)
        .join(CaseRiskFindingModel, CaseRiskFindingModel.id == CaseRiskFeedbackModel.finding_id)
        .where(CaseRiskFeedbackModel.tenant_id == tenant)
    )).all()

    acc: dict[str, dict] = {}
    for fb, rule_key in rows:
        if fb.id in disputed:
            continue
        w = weight.get(fb.reviewer, _UNKNOWN_WEIGHT)
        a = acc.setdefault(rule_key, {"wsum": 0.0, "w": 0.0, "reviews": 0})
        a["wsum"] += fb.score * w
        a["w"] += w
        a["reviews"] += 1
    return {
        rk: {"weighted_avg": round(a["wsum"] / a["w"], 2) if a["w"] else None,
             "reviews": a["reviews"]}
        for rk, a in acc.items()
    }


def _next_severity_up(current: str) -> str | None:
    i = rulecat.severity_rank(current)
    return rulecat.SEVERITIES[i + 1] if 0 <= i < len(rulecat.SEVERITIES) - 1 else None


async def generate_proposals(
    session: AsyncSession, tenant: str, *,
    low: float = LOW_AVG, high: float = HIGH_AVG, min_reviews: int = MIN_REVIEWS,
) -> list[CaseRiskRuleProposalModel]:
    """Create pending proposals from the current score signal. Idempotent: never
    a second pending proposal for a rule that already has one open."""
    analysis = await analyze_rule_scores(session, tenant)
    open_rules = set((await session.execute(
        select(CaseRiskRuleProposalModel.rule_key).where(
            CaseRiskRuleProposalModel.tenant_id == tenant,
            CaseRiskRuleProposalModel.status == "pending")
    )).scalars().all())
    # Current per-tenant overrides, to skip proposing a change already in effect
    # (e.g. re-analysing after a deactivate was approved must NOT re-propose it).
    overrides = {
        r.rule_key: r for r in (await session.execute(
            select(CaseRiskRuleModel).where(CaseRiskRuleModel.tenant_id == tenant))
    ).scalars().all()}

    created: list[CaseRiskRuleProposalModel] = []
    for rule_key, a in analysis.items():
        if rule_key not in rulecat.RULES_BY_KEY or a["reviews"] < min_reviews or a["weighted_avg"] is None:
            continue
        if rule_key in open_rules:
            continue
        ov = overrides.get(rule_key)
        eff_enabled = ov.enabled if ov is not None else True
        eff_severity = (ov.severity if ov and ov.severity else rulecat.RULES_BY_KEY[rule_key].default_severity)

        avg = a["weighted_avg"]
        change: dict | None = None
        rationale = ""
        if avg < low:
            if not eff_enabled:
                continue                  # already deactivated — nothing to propose
            change = {"enabled": False}
            rationale = (f"Reviewers score this rule {avg}/5 (reliability-weighted, "
                         f"{a['reviews']} reviews) — persistently low. Propose deactivating it.")
        elif avg >= high:
            nxt = _next_severity_up(eff_severity)
            if nxt is not None and nxt != eff_severity:
                change = {"severity": nxt}
                rationale = (f"Reviewers score this rule {avg}/5 (reliability-weighted, "
                             f"{a['reviews']} reviews) — consistently valued. Propose raising "
                             f"its severity to {nxt}.")
        if change is None:
            continue
        row = CaseRiskRuleProposalModel(
            tenant_id=tenant, rule_key=rule_key, proposed_change=change,
            rationale=rationale,
            evidence={"source": "score_tuning", "weighted_avg": avg, "reviews": a["reviews"]})
        session.add(row)
        created.append(row)
    await session.flush()
    return created


def _next_severity_down(current: str) -> str | None:
    i = rulecat.severity_rank(current)
    return rulecat.SEVERITIES[i - 1] if i > 0 else None


async def _local_llm_json(prompt: str, system: str) -> dict | None:
    """Structured output from the LOCAL backend only — reviewer notes are
    sensitive and must never leave the platform (stricter than case-QA's
    per-tenant egress choice: notes-mining is local-only, always). Honest skip
    (returns None) when the local model is unavailable."""
    import json as _json
    try:
        from case_service.config import get_settings
        from case_service.hxnexus.factory import _local_backend, _strip_fences
        backend = _local_backend(get_settings())
        available = await backend.check_available() if hasattr(backend, "check_available") else backend.available
        if not available:
            return None
        raw = await backend.complete(
            prompt, system=system + "\n\nRespond with valid JSON only — no prose, no markdown.",
            temperature=0.2, max_tokens=2048)
        return _json.loads(_strip_fences(raw)) if raw else None
    except Exception as exc:                  # unavailable / unparseable -> skip
        import logging
        logging.getLogger(__name__).info("notes-mining LLM skipped: %s", type(exc).__name__)
        return None


_MINE_SYSTEM = (
    "You are a fraud-risk analyst reviewing reviewers' notes about a detection "
    "rule. Cluster the recurring THEME across the notes and suggest ONE change. "
    'Return JSON: {"theme": str, "suggestion": "deactivate"|"lower_severity"|"advisory", '
    '"rationale": str}. Use "advisory" when the notes do not clearly justify a '
    "mechanical change. Never invent details not present in the notes."
)


async def mine_notes(
    session: AsyncSession, tenant: str, *, min_notes: int = 3, per_rule: int = 25,
) -> list[CaseRiskRuleProposalModel]:
    """P3c-2 — cluster reviewers' feedback notes (LOCAL LLM) into rule-change
    proposals. Notes stay on-platform. Honest skip when the model is down.

    Proposals land in the same `case_risk_rule_proposals` table as P3c-1, marked
    ``evidence.source = "notes_mining"``; a human approves each one.
    """
    from collections import defaultdict

    disputed = set((await session.execute(
        select(CaseRiskScoreDisputeModel.feedback_id)
        .where(CaseRiskScoreDisputeModel.tenant_id == tenant))).scalars().all())
    rows = (await session.execute(
        select(CaseRiskFeedbackModel.id, CaseRiskFeedbackModel.note, CaseRiskFindingModel.rule_key)
        .join(CaseRiskFindingModel, CaseRiskFindingModel.id == CaseRiskFeedbackModel.finding_id)
        .where(CaseRiskFeedbackModel.tenant_id == tenant,
               CaseRiskFeedbackModel.note.is_not(None))
    )).all()

    notes_by_rule: dict[str, list[str]] = defaultdict(list)
    for fb_id, note, rule_key in rows:
        if fb_id in disputed or not (note or "").strip():
            continue
        if len(notes_by_rule[rule_key]) < per_rule:
            notes_by_rule[rule_key].append(note.strip())

    open_rules = set((await session.execute(
        select(CaseRiskRuleProposalModel.rule_key).where(
            CaseRiskRuleProposalModel.tenant_id == tenant,
            CaseRiskRuleProposalModel.status == "pending"))).scalars().all())
    overrides = {r.rule_key: r for r in (await session.execute(
        select(CaseRiskRuleModel).where(CaseRiskRuleModel.tenant_id == tenant))).scalars().all()}

    created: list[CaseRiskRuleProposalModel] = []
    for rule_key, notes in notes_by_rule.items():
        if rule_key not in rulecat.RULES_BY_KEY or len(notes) < min_notes or rule_key in open_rules:
            continue
        prompt = (f"Rule: {rule_key}\nReviewer notes ({len(notes)}):\n"
                  + "\n".join(f"- {n}" for n in notes))
        result = await _local_llm_json(prompt, _MINE_SYSTEM)
        if not result or not isinstance(result, dict):
            continue                          # model down/garbled -> skip this rule
        suggestion = str(result.get("suggestion") or "advisory")
        theme = str(result.get("theme") or "").strip()
        rationale = str(result.get("rationale") or theme).strip()
        if not rationale:
            continue

        ov = overrides.get(rule_key)
        eff_enabled = ov.enabled if ov is not None else True
        eff_severity = (ov.severity if ov and ov.severity else rulecat.RULES_BY_KEY[rule_key].default_severity)
        change: dict = {}
        if suggestion == "deactivate" and eff_enabled:
            change = {"enabled": False}
        elif suggestion == "lower_severity":
            nxt = _next_severity_down(eff_severity)
            if nxt is not None:
                change = {"severity": nxt}
        # else advisory: change stays {} (approve = acknowledge, no rule mutation)

        row = CaseRiskRuleProposalModel(
            tenant_id=tenant, rule_key=rule_key, proposed_change=change,
            rationale=f"[from {len(notes)} reviewer notes] {rationale}",
            evidence={"source": "notes_mining", "theme": theme,
                      "note_count": len(notes), "suggestion": suggestion})
        session.add(row)
        created.append(row)
    await session.flush()
    return created


async def list_proposals(
    session: AsyncSession, tenant: str, *, status: str | None = "pending"
) -> list[CaseRiskRuleProposalModel]:
    q = select(CaseRiskRuleProposalModel).where(CaseRiskRuleProposalModel.tenant_id == tenant)
    if status:
        q = q.where(CaseRiskRuleProposalModel.status == status)
    return list((await session.execute(
        q.order_by(CaseRiskRuleProposalModel.created_at.desc()))).scalars().all())


async def decide_proposal(
    session: AsyncSession, *, proposal_id: uuid.UUID, tenant: str,
    decision: str, decided_by: str, note: str | None,
) -> CaseRiskRuleProposalModel | None:
    """Approve (apply the change to case_risk_rules) or reject a proposal."""
    p = await session.get(CaseRiskRuleProposalModel, proposal_id)
    if p is None or p.tenant_id != tenant or p.status != "pending":
        return p
    p.status = "approved" if decision == "approved" else "rejected"
    p.decided_by = decided_by
    p.decided_at = _utcnow()
    p.decision_note = note

    if p.status == "approved":
        await _apply_change(session, tenant, p.rule_key, p.proposed_change, decided_by)
    # The proposal row IS the audit trail: status + decided_by/decided_at +
    # decision_note capture who decided what, when, and why. (The case-scoped
    # audit log can't record a tenant-wide rule change — it requires a case id.)
    await session.flush()
    return p


async def _apply_change(session, tenant, rule_key, change, actor) -> None:
    row = (await session.execute(
        select(CaseRiskRuleModel).where(
            CaseRiskRuleModel.tenant_id == tenant, CaseRiskRuleModel.rule_key == rule_key)
    )).scalars().first()
    if row is None:
        row = CaseRiskRuleModel(tenant_id=tenant, rule_key=rule_key, updated_by=actor)
        session.add(row)
    if "enabled" in change:
        row.enabled = bool(change["enabled"])
    if "severity" in change:
        row.severity = change["severity"]
    row.updated_by = actor
    await session.flush()
