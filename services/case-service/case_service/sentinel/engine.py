"""HxSentinel P2 — the deterministic rule engine.

``evaluate_case`` runs the enabled rules for a case, cross-referencing its P1
signals against the tenant's own labelled history, and upserts a finding per
rule that fires. Everything is deterministic and explainable; every finding
cites the exact evidence (matched case numbers, shared signal, the arithmetic).

Correctness invariants (see the rules' evidence for why each matters):
  * **Latest outcome per case.** ``case_outcomes`` is append-only, latest-wins.
    A match uses a case's CURRENT label, so a fraud->clean re-label stops
    tripping the fraud rules — never "any historical row said fraud".
  * **Tenant scope in SQL.** Matching keys on ``tenant_id`` in the WHERE (P1's
    ``norm_tenant`` semantics), never a Python filter after a limit, and never
    across tenants even if two digests collided.
  * **Exclude self and the unlabelled.** A case never matches itself, and a
    case with no confirmed outcome is not evidence of anything.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import logging
import statistics
import uuid
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from case_service.db.models import (
    CaseAuditLogModel,
    CaseInstanceModel,
    CaseInstanceVariableModel,
    CaseOutcomeLabelModel,
    CaseOutcomeModel,
    CaseRiskConfigModel,
    CaseRiskFeedbackModel,
    CaseRiskFindingModel,
    CaseRiskHoldModel,
    CaseRiskRuleModel,
    CaseRiskSignalModel,
)
from case_service.sentinel import rules as rulecat
from case_service.sentinel import taxonomy
from case_service.sentinel.service import norm_tenant

logger = logging.getLogger(__name__)

#: Outcomes the document-reuse rule treats as "confirmed bad" — the serious,
#: unambiguous core negatives, a subset of all negative labels.
_SERIOUS_NEGATIVES = frozenset({"fraud", "identity_theft", "criminal_transaction"})


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


# ── Effective per-tenant configuration ───────────────────────────────────────


async def _effective_config(session: AsyncSession, tenant: str) -> dict[str, dict]:
    """Merge the code defaults with this tenant's overrides.

    Returns ``{rule_key: {"enabled", "severity", "params"}}`` for every rule in
    the catalogue. An absent override row means the code default applies.
    """
    overrides = {
        r.rule_key: r
        for r in (await session.execute(
            select(CaseRiskRuleModel).where(CaseRiskRuleModel.tenant_id == tenant)
        )).scalars().all()
    }
    cfg: dict[str, dict] = {}
    for rule in rulecat.RULES:
        ov = overrides.get(rule.key)
        cfg[rule.key] = {
            "enabled": ov.enabled if ov is not None else True,
            "severity": (ov.severity if ov and ov.severity else rule.default_severity),
            "params": rule.params_with(ov.params if ov else None),
        }
    return cfg


# ── Outcome helpers (latest-per-case, tenant-scoped) ─────────────────────────


async def _negative_keys(session: AsyncSession, tenant: str) -> set[str]:
    """The set of outcome keys that count as negative for this tenant."""
    negatives = {lbl.key for lbl in taxonomy.CORE_LABELS if lbl.is_negative}
    rows = (await session.execute(
        select(CaseOutcomeLabelModel.key).where(
            CaseOutcomeLabelModel.tenant_id == tenant,
            CaseOutcomeLabelModel.is_negative.is_(True),
            CaseOutcomeLabelModel.active.is_(True),
        )
    )).scalars().all()
    negatives.update(rows)
    return negatives


async def _current_outcomes(
    session: AsyncSession, case_ids: set[uuid.UUID]
) -> dict[uuid.UUID, str]:
    """Latest outcome per case for the given cases (skips never-labelled ones).

    Small candidate sets (cases sharing one signal) — fetch ordered and take the
    newest row per case in Python, which is portable across SQLite and Postgres
    and avoids a window function.
    """
    if not case_ids:
        return {}
    rows = (await session.execute(
        select(CaseOutcomeModel)
        .where(CaseOutcomeModel.case_id.in_(case_ids))
        .order_by(CaseOutcomeModel.created_at.desc(), CaseOutcomeModel.id.desc())
    )).scalars().all()
    latest: dict[uuid.UUID, str] = {}
    for r in rows:                       # first row seen per case is the newest
        latest.setdefault(r.case_id, r.outcome)
    return latest


async def _matching_cases(
    session: AsyncSession, tenant: str, signal_type: str, signal_key: str,
    exclude: uuid.UUID,
) -> list[uuid.UUID]:
    """Other cases in this tenant carrying the same signal (never self)."""
    return list((await session.execute(
        select(CaseRiskSignalModel.case_id).where(
            CaseRiskSignalModel.tenant_id == tenant,
            CaseRiskSignalModel.signal_type == signal_type,
            CaseRiskSignalModel.signal_key == signal_key,
            CaseRiskSignalModel.case_id != exclude,
        )
    )).scalars().all())


async def _case_numbers(
    session: AsyncSession, case_ids: list[uuid.UUID]
) -> dict[uuid.UUID, str]:
    if not case_ids:
        return {}
    rows = (await session.execute(
        select(CaseInstanceModel.id, CaseInstanceModel.case_number)
        .where(CaseInstanceModel.id.in_(case_ids))
    )).all()
    return {cid: num for cid, num in rows}


# ── The rules ────────────────────────────────────────────────────────────────
# Each returns a finding payload dict (title, message, evidence) or None. The
# caller supplies severity from the effective config, so a rule never hardcodes
# it. A rule that cannot judge (too little history) returns None — an honest
# non-finding, never a fabricated one.


async def _signals_of(
    session: AsyncSession, case_id: uuid.UUID, types: tuple[str, ...] | None = None
) -> list[CaseRiskSignalModel]:
    q = select(CaseRiskSignalModel).where(CaseRiskSignalModel.case_id == case_id)
    if types:
        q = q.where(CaseRiskSignalModel.signal_type.in_(types))
    return list((await session.execute(q)).scalars().all())


async def _rule_shared_doc_fraud(session, case, tenant, params) -> dict | None:
    signals = await _signals_of(session, case.id, ("doc_hash",))
    matched: list[dict] = []
    cited_signals: list[str] = []
    for sig in signals:
        others = await _matching_cases(session, tenant, "doc_hash", sig.signal_key, case.id)
        outcomes = await _current_outcomes(session, set(others))
        bad = {cid: oc for cid, oc in outcomes.items() if oc in _SERIOUS_NEGATIVES}
        if bad:
            numbers = await _case_numbers(session, list(bad))
            matched.extend({"case_number": numbers.get(cid, str(cid)), "outcome": oc}
                           for cid, oc in bad.items())
            cited_signals.append(str(sig.id))
    if not matched:
        return None
    return {
        "title": "Document reused from a confirmed-bad case",
        "message": (f"A document on this case shares its content hash with "
                    f"{len(matched)} case(s) confirmed fraud / identity theft / "
                    f"criminal. Re-verify the document before proceeding."),
        "evidence": {"signal_type": "doc_hash", "signal_ids": cited_signals,
                     "matched_cases": matched},
    }


async def _rule_identity_velocity(session, case, tenant, params) -> dict | None:
    window_days = int(params.get("window_days") or 30)
    min_cases = int(params.get("min_cases") or 3)
    # A ceiling as well as a floor. A digest on hundreds of cases is not a
    # person — it is a shared support address or a template constant living in a
    # variable whose name matched an identity hint, and without this every case
    # carrying it fires. 0/None disables the ceiling.
    max_cases = int(params.get("max_cases") or 0)
    signals = await _signals_of(
        session, case.id, ("identity_email", "identity_phone", "identity_address"))
    hits: list[dict] = []
    cited_signals: list[str] = []
    ignored: list[dict] = []
    for sig in signals:
        others = await _matching_cases(session, tenant, sig.signal_type, sig.signal_key, case.id)
        # distinct-case count including this one
        total = len(set(others)) + 1
        if max_cases and total > max_cases:
            # Say so rather than drop it silently: "too common to be an identity"
            # and "no match" are different facts, and only one of them means the
            # tenant should look at what is populating that variable.
            ignored.append({"signal_type": sig.signal_type, "case_count": total})
            continue
        if total >= min_cases:
            numbers = await _case_numbers(session, others)
            hits.append({"signal_type": sig.signal_type, "case_count": total,
                         "other_cases": [numbers.get(c, str(c)) for c in set(others)]})
            cited_signals.append(str(sig.id))
    if not hits:
        if ignored:
            logger.info(
                "HxSentinel identity_velocity: case %s had %d value(s) over the "
                "max_cases=%d ceiling — too common to be an identity, check what "
                "populates them: %s", case.id, len(ignored), max_cases, ignored)
        return None
    top = max(h["case_count"] for h in hits)
    evidence = {"signal_ids": cited_signals, "matches": hits,
                "window_days": window_days, "min_cases": min_cases}
    if ignored:
        # Carried on the finding so a reviewer can see the rule declined to count
        # something, and why — a silently narrowed finding is a misleading one.
        evidence["ignored_too_common"] = ignored
        evidence["max_cases"] = max_cases
    return {
        "title": "Identity shared across many recent cases",
        "message": (f"An identity value on this case recurs across {top} cases "
                    f"(threshold {min_cases}, window {window_days}d). Recommend "
                    f"enhanced verification."),
        "evidence": evidence,
    }


async def _rule_amount_anomaly(session, case, tenant, params) -> dict | None:
    z_threshold = float(params.get("z") or 3.0)
    min_samples = int(params.get("min_samples") or 8)
    hints = [h.lower() for h in (params.get("hints") or ["amount"])]

    mine = (await session.execute(
        select(CaseInstanceVariableModel.full_key, CaseInstanceVariableModel.value_num)
        .where(CaseInstanceVariableModel.case_id == case.id,
               CaseInstanceVariableModel.value_num.is_not(None))
    )).all()
    candidates = [(fk, val) for fk, val in mine
                  if any(h in (fk or "").lower() for h in hints)]
    if not candidates or case.case_type_id is None:
        return None

    worst: dict | None = None
    for full_key, value in candidates:
        others = (await session.execute(
            select(CaseInstanceVariableModel.value_num)
            .join(CaseInstanceModel, CaseInstanceModel.id == CaseInstanceVariableModel.case_id)
            .where(CaseInstanceModel.case_type_id == case.case_type_id,
                   CaseInstanceVariableModel.full_key == full_key,
                   CaseInstanceVariableModel.value_num.is_not(None),
                   CaseInstanceVariableModel.case_id != case.id)
        )).scalars().all()
        if len(others) < min_samples:
            continue                      # too little history — skip honestly
        mean = statistics.mean(others)
        std = statistics.pstdev(others)
        if std == 0:
            continue                      # z undefined for a constant history
        z = (value - mean) / std
        if abs(z) > z_threshold and (worst is None or abs(z) > abs(worst["z"])):
            worst = {"full_key": full_key, "value": value, "z": round(z, 2),
                     "mean": round(mean, 2), "std": round(std, 2), "n": len(others)}
    if worst is None:
        return None
    return {
        "title": "Transaction amount is an outlier for this case type",
        "message": (f"{worst['full_key']} = {worst['value']} is {worst['z']} std from "
                    f"the case type's mean ({worst['mean']}, n={worst['n']}). Recommend review."),
        "evidence": {"metric": worst},
    }


async def _rule_shared_signal_negative_outcome(session, case, tenant, params) -> dict | None:
    negatives = await _negative_keys(session, tenant)
    signals = await _signals_of(session, case.id)
    matched: list[dict] = []
    cited_signals: list[str] = []
    for sig in signals:
        others = await _matching_cases(session, tenant, sig.signal_type, sig.signal_key, case.id)
        outcomes = await _current_outcomes(session, set(others))
        bad = {cid: oc for cid, oc in outcomes.items() if oc in negatives}
        if bad:
            numbers = await _case_numbers(session, list(bad))
            matched.extend({"case_number": numbers.get(cid, str(cid)), "outcome": oc,
                            "signal_type": sig.signal_type} for cid, oc in bad.items())
            cited_signals.append(str(sig.id))
    if not matched:
        return None
    return {
        "title": "Signal shared with a confirmed-bad case",
        "message": (f"A signal on this case matches {len(matched)} case(s) with a "
                    f"confirmed negative outcome. Review the cited cases."),
        "evidence": {"signal_ids": cited_signals, "matched_cases": matched},
    }


async def _rule_resembles_confirmed_cases(session, case, tenant, params) -> dict | None:
    """P3a similarity layer — nearest labelled-bad cases by signal-vector overlap.

    Deterministic and explainable: count how many signals this case shares with
    each other tenant case, keep neighbours confirmed bad that overlap on at
    least ``min_shared`` signals, and cite the strongest. Stronger than a single
    shared signal (that is `shared_signal_negative_outcome`) — this is a genuine
    resemblance, so it defaults to a higher severity.
    """
    min_shared = int(params.get("min_shared") or 2)
    top_k = int(params.get("top_k") or 5)
    signals = await _signals_of(session, case.id)
    if len(signals) < min_shared:
        return None                       # too few signals to resemble anything

    # neighbour case -> set of overlapping signal types (the overlap "vector").
    overlap: dict[uuid.UUID, set[str]] = {}
    for sig in signals:
        for other in await _matching_cases(session, tenant, sig.signal_type, sig.signal_key, case.id):
            overlap.setdefault(other, set()).add(sig.signal_type)

    strong = {cid: types for cid, types in overlap.items() if len(types) >= min_shared}
    if not strong:
        return None
    negatives = await _negative_keys(session, tenant)
    outcomes = await _current_outcomes(session, set(strong))
    bad = {cid: types for cid, types in strong.items()
           if outcomes.get(cid) in negatives}
    if not bad:
        return None

    numbers = await _case_numbers(session, list(bad))
    neighbours = sorted(
        ({"case_number": numbers.get(cid, str(cid)), "outcome": outcomes[cid],
          "shared_signals": sorted(types), "shared_count": len(types)}
         for cid, types in bad.items()),
        key=lambda n: n["shared_count"], reverse=True)[:top_k]
    return {
        "title": "Resembles confirmed-bad cases",
        "message": (f"This case shares {neighbours[0]['shared_count']} signals with "
                    f"{len(bad)} confirmed-bad case(s) — a strong resemblance. Review "
                    f"the cited cases before proceeding."),
        "evidence": {"neighbours": neighbours, "min_shared": min_shared},
    }


_EVALUATORS = {
    "shared_doc_fraud": _rule_shared_doc_fraud,
    "identity_velocity": _rule_identity_velocity,
    "amount_anomaly": _rule_amount_anomaly,
    "shared_signal_negative_outcome": _rule_shared_signal_negative_outcome,
    "resembles_confirmed_cases": _rule_resembles_confirmed_cases,
}


# ── Evaluation ───────────────────────────────────────────────────────────────


async def evaluate_case(
    session: AsyncSession, case_id: uuid.UUID, *, actor_id: str = "system",
) -> list[CaseRiskFindingModel]:
    """Run every enabled rule and upsert findings. Returns the ACTIVE findings.

    Idempotent per (case_id, rule_key): a rule that fires upserts its finding to
    ``active``; a rule that previously fired but no longer does has its finding
    marked ``stale`` (kept, so P2b scores never orphan). The caller commits.
    """
    case = await session.get(CaseInstanceModel, case_id)
    if case is None:
        return []
    tenant = norm_tenant(case.tenant_id)

    # Make sure THIS case's signals exist before any rule reads them.
    #
    # Four of the five rules (shared_doc_fraud, identity_velocity,
    # shared_signal_negative_outcome, resembles_confirmed_cases) start from
    # _signals_of(case.id) and return None on an empty list. Extraction ran only
    # at a terminal status, so an OPEN case carried no signals and those rules
    # could not fire on it — which is every case the verify gate exists to hold.
    #
    # This is NOT the lazy extraction that extraction.py argues against. That
    # argument is about the CORPUS — other cases' signals, which must accumulate
    # as cases move and still do (on-close hook + sweep). The SUBJECT case is a
    # separate job, and this is its only correct moment: the data is whatever it
    # is right now, and re-extraction reconciles rather than accumulates.
    #
    # Fails open: a vault or extraction fault degrades the evaluation, never
    # breaks it — the gate's posture everywhere else.
    try:
        from case_service.sentinel import service as _sentinel_service
        await _sentinel_service.extract_signals(session, case_id)
    except Exception as exc:                       # pragma: no cover - defensive
        logger.warning("HxSentinel: pre-evaluation extraction failed for %s "
                       "(non-fatal, evaluating on stored signals): %s", case_id, exc)

    cfg = await _effective_config(session, tenant)

    existing = {
        f.rule_key: f
        for f in (await session.execute(
            select(CaseRiskFindingModel).where(CaseRiskFindingModel.case_id == case_id)
        )).scalars().all()
    }

    # Collect everything that fires this pass — code rules AND installed signal
    # packs (P4) — as {rule_key: payload+severity}, then reconcile once against
    # the existing findings. Uniform handling means a pack rule gets the same
    # upsert / stale / gate treatment as a built-in rule.
    firing: dict[str, dict] = {}
    for rule in rulecat.RULES:
        conf = cfg[rule.key]
        if not conf["enabled"]:
            continue
        try:
            payload = await _EVALUATORS[rule.key](session, case, tenant, conf["params"])
        except Exception as exc:          # a rule fault must not fail the whole eval
            logger.warning("HxSentinel rule %s failed on case %s: %s", rule.key, case_id, exc)
            payload = None
        if payload is not None:
            firing[rule.key] = {**payload, "severity": conf["severity"]}

    # Feature context (P4 packs + P5 model) — computed ONCE and only when a pack
    # or an active model actually needs it (zero extra queries for tenants using
    # neither). A pack or model fault must never fail the whole evaluation.
    try:
        from case_service.sentinel import model as risk_model
        from case_service.sentinel import packs
        installed = await packs.enabled_packs(session, tenant)
        active_model = await risk_model.get_active_model(session, tenant)
        if installed or active_model is not None:
            ctx = await packs.build_feature_context(session, case, tenant)
            for pl in packs.evaluate_pack_rules(installed, ctx):
                firing[pl["rule_key"]] = pl
            if active_model is not None:
                pl = risk_model.finding_payload(active_model, ctx)
                if pl is not None:
                    firing[pl["rule_key"]] = pl
    except Exception as exc:              # packs/model faults never fail the eval
        logger.warning("HxSentinel pack/model eval failed on case %s: %s", case_id, exc)

    active: list[CaseRiskFindingModel] = []
    for rule_key, pl in firing.items():
        prior = existing.get(rule_key)
        if prior is None:
            finding = CaseRiskFindingModel(
                tenant_id=tenant, case_id=case_id, rule_key=rule_key, severity=pl["severity"],
                title=pl["title"], message=pl["message"], evidence=pl["evidence"], status="active")
            session.add(finding)
            _audit(session, case_id, actor_id, rule_key, pl["severity"], "raised")
        else:
            finding = prior
            finding.severity = pl["severity"]
            finding.title = pl["title"]
            finding.message = pl["message"]
            finding.evidence = pl["evidence"]
            if finding.status != "active":
                finding.status = "active"
                _audit(session, case_id, actor_id, rule_key, pl["severity"], "raised")
        active.append(finding)

    # Anything that previously fired but didn't this pass is retired.
    for rule_key, prior in existing.items():
        if rule_key not in firing and prior.status == "active":
            prior.status = "stale"

    # Flush BEFORE the gate so newly-added findings have their ids populated —
    # the hold records finding_id (provenance) and must not depend on autoflush.
    await session.flush()
    await _maybe_open_hold(session, case_id, tenant, active, actor_id)
    await session.flush()
    return active


def _audit(session, case_id, actor_id, rule_key, severity, verb) -> None:
    session.add(CaseAuditLogModel(
        case_id=case_id,
        action=f"risk.finding.{verb}",
        actor_id=actor_id,
        details={"rule_key": rule_key, "severity": severity},
    ))


# ── Verify gate (optional, opt-in per tenant) ────────────────────────────────
# A hold is DECOUPLED from the finding: opened once when a gating-severity
# finding first appears, cleared ONLY by a human. A re-evaluation never clears
# or re-opens a hold — that is what keeps the gate honest. The advance check
# fails OPEN: a case is held only when an open hold affirmatively exists.


async def get_gate_severity(session: AsyncSession, tenant: str) -> str | None:
    """The tenant's minimum finding severity that opens a hold, or None (off)."""
    row = await session.get(CaseRiskConfigModel, tenant)
    return row.verify_gate_severity if row else None


async def _maybe_open_hold(session, case_id, tenant, active, actor_id) -> None:
    threshold = await get_gate_severity(session, tenant)
    if not threshold:
        return                            # gate off for this tenant — nothing to do
    gating = [f for f in active if rulecat.meets(f.severity, threshold)]
    if not gating:
        return
    # At most ONE gate event per case, ever. The check spans ALL statuses, not
    # just 'open': once a human has cleared (or dismissed) a hold, a later
    # re-evaluation must NOT silently re-freeze the case — that is the whole
    # point of decoupling the hold from the recomputable finding.
    already = (await session.execute(
        select(CaseRiskHoldModel.id).where(CaseRiskHoldModel.case_id == case_id)
    )).first()
    if already:
        return
    top = max(gating, key=lambda f: rulecat.severity_rank(f.severity))
    session.add(CaseRiskHoldModel(
        tenant_id=tenant, case_id=case_id, finding_id=top.id,
        rule_key=top.rule_key, severity=top.severity, status="open"))
    session.add(CaseAuditLogModel(
        case_id=case_id, action="risk.gate.hold_opened", actor_id=actor_id,
        details={"rule_key": top.rule_key, "severity": top.severity, "threshold": threshold}))


async def _has_open_hold(session: AsyncSession, case_id: uuid.UUID) -> bool:
    hit = (await session.execute(
        select(CaseRiskHoldModel.id).where(
            CaseRiskHoldModel.case_id == case_id,
            CaseRiskHoldModel.status == "open",
        ).limit(1)
    )).first()
    return hit is not None


async def advance_blocked(session: AsyncSession, case_id: uuid.UUID) -> bool:
    """True when the verify gate holds this case's advance.

    Called from the case stage-advance chokepoints, which run for EVERY case in
    EVERY tenant, so it must be cheap when unused and never wedge the workflow:

      * short-circuits with ZERO DB work when HxSentinel is off platform-wide;
      * an already-open hold blocks immediately;
      * otherwise, when (and only when) this tenant has opted the gate ON, it
        ARMS the gate itself — evaluating the rules so a hold can open. Without
        this the gate would only fire if a human happened to open the risk panel
        first; the advance path is exactly where it must be enforced.
      * FAILS OPEN on any error — a HxSentinel fault can never freeze case work.
    """
    try:
        from case_service.api.routers.releases import is_feature_enabled
        if not is_feature_enabled("hxsentinel"):
            return False
        if await _has_open_hold(session, case_id):
            return True
        case = await session.get(CaseInstanceModel, case_id)
        if case is None:
            return False
        tenant = norm_tenant(case.tenant_id)
        if not await get_gate_severity(session, tenant):
            return False                  # gate off for this tenant — nothing to arm
        # Arm the gate: evaluate now so a gating finding can open a hold.
        await evaluate_case(session, case_id, actor_id="system")
        return await _has_open_hold(session, case_id)
    except Exception as exc:              # never let the gate wedge the workflow
        logger.warning("HxSentinel advance_blocked check failed for %s (failing open): %s",
                       case_id, exc)
        return False


async def list_holds(
    session: AsyncSession, case_id: uuid.UUID, *, status: str | None = "open"
) -> list[CaseRiskHoldModel]:
    q = select(CaseRiskHoldModel).where(CaseRiskHoldModel.case_id == case_id)
    if status:
        q = q.where(CaseRiskHoldModel.status == status)
    return list((await session.execute(
        q.order_by(CaseRiskHoldModel.opened_at.desc())
    )).scalars().all())


async def resolve_hold(
    session: AsyncSession, hold_id: uuid.UUID, *, resolved_by: str,
    status: str, note: str | None,
) -> CaseRiskHoldModel | None:
    """Human clears (or dismisses) a hold. The only thing that releases a gate."""
    hold = await session.get(CaseRiskHoldModel, hold_id)
    if hold is None or hold.status != "open":
        return hold
    hold.status = status                  # cleared | dismissed
    hold.resolved_by = resolved_by
    hold.resolved_at = _utcnow()
    hold.resolution_note = note
    session.add(CaseAuditLogModel(
        case_id=hold.case_id, action="risk.gate.hold_resolved", actor_id=resolved_by,
        details={"hold_id": str(hold_id), "status": status, "note": note}))
    await session.flush()
    return hold


# ── Human feedback (P2b) ─────────────────────────────────────────────────────
# A 1–5 score + note per finding, append-only. A read-only outcome cross-check
# flags a score that disagrees with the case's CURRENT confirmed outcome: a
# finding is a risk warning, so on a case later confirmed bad (negative outcome)
# a dismissive low score is suspect, and on a case confirmed clean a glowing
# high score praised a false positive. Undetermined until the case is labelled.


async def add_feedback(
    session: AsyncSession, *, finding: CaseRiskFindingModel, reviewer: str,
    score: int, note: str | None,
) -> CaseRiskFeedbackModel:
    row = CaseRiskFeedbackModel(
        tenant_id=finding.tenant_id, case_id=finding.case_id, finding_id=finding.id,
        reviewer=reviewer, score=score, note=note)
    session.add(row)
    session.add(CaseAuditLogModel(
        case_id=finding.case_id, action="risk.finding.scored", actor_id=reviewer,
        details={"finding_id": str(finding.id), "rule_key": finding.rule_key, "score": score}))
    await session.flush()
    return row


def _classify_calibration(outcome: str | None, negatives: set[str], score: int) -> str:
    """Pure cross-check: does a score agree with a case's confirmed outcome?

    Returns 'ok' | 'miscalibrated' | 'undetermined'. A finding is a risk warning,
    so on a case confirmed bad a dismissive low score is suspect, and on a case
    confirmed clean a glowing high score praised a false positive.
    """
    if outcome is None:
        return "undetermined"
    finding_was_right = outcome in negatives
    if finding_was_right and score <= 2:
        return "miscalibrated"
    if not finding_was_right and score >= 4:
        return "miscalibrated"
    return "ok"


async def _calibration(
    session: AsyncSession, case_id: uuid.UUID, tenant: str, score: int
) -> str:
    """Read-only cross-check of a single score (fetches the outcome + negatives)."""
    outcome = (await _current_outcomes(session, {case_id})).get(case_id)
    negatives = await _negative_keys(session, tenant) if outcome is not None else set()
    return _classify_calibration(outcome, negatives, score)


async def get_feedback(
    session: AsyncSession, finding: CaseRiskFindingModel
) -> dict:
    rows = list((await session.execute(
        select(CaseRiskFeedbackModel)
        .where(CaseRiskFeedbackModel.finding_id == finding.id)
        .order_by(CaseRiskFeedbackModel.created_at.desc(), CaseRiskFeedbackModel.id.desc())
    )).scalars().all())
    # The outcome + negatives are identical for every row of one finding (same
    # case, same tenant) — fetch ONCE and classify each score in memory.
    tenant = norm_tenant(finding.tenant_id)
    outcome = (await _current_outcomes(session, {finding.case_id})).get(finding.case_id)
    negatives = await _negative_keys(session, tenant) if outcome is not None else set()
    history = [{
        "id": str(r.id), "reviewer": r.reviewer, "score": r.score, "note": r.note,
        "created_at": r.created_at.isoformat() if r.created_at else None,
        "calibration": _classify_calibration(outcome, negatives, r.score),
    } for r in rows]
    return {"latest": history[0] if history else None, "history": history}


async def get_findings(
    session: AsyncSession, case_id: uuid.UUID, *, include_stale: bool = False
) -> list[CaseRiskFindingModel]:
    q = select(CaseRiskFindingModel).where(CaseRiskFindingModel.case_id == case_id)
    if not include_stale:
        q = q.where(CaseRiskFindingModel.status == "active")
    rows = list((await session.execute(
        q.order_by(CaseRiskFindingModel.created_at.desc())
    )).scalars().all())
    # Sort strongest-first by the ordered severity vocabulary (lexical order on
    # low/medium/high would be wrong — "high" < "low" as strings).
    rows.sort(key=lambda f: rulecat.severity_rank(f.severity), reverse=True)
    return rows
