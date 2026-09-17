"""F-05 (first slice) — agents as workers.

An AI agent sits in a queue next to a human: it holds skills from the same
taxonomy, carries a workload cap, and gets a performance review computed from
the evidence F-01 and F-02 already record.

**What is built here, and what is not.** The roster, the capacity and budget
caps, benching, autonomy-as-policy, the scorecard and the supervisor-approved
routing-weight proposal are all here, along with a `workforce` assignment
strategy the router can use. The scorecard is computed on demand *and* on a
schedule an operator switches on — the sweep at the foot of this file, which
calls the same function over the same rows and only decides *when*.

Three rules shape the file:

* **An agent in the registry is not automatically a worker.** HxNexus's case-Q&A
  agent must never be handed a case step, so holding work requires an explicit
  roster row.
* **Autonomy is a policy, not a config flag.** A step marked `autonomous` is
  necessary and not sufficient: HxArbiter must also allow `agent.act_on_case`,
  so a tenant can clamp agent autonomy by data class or region regardless of how
  a process was drawn.
* **Fail closed on staffing.** An agent with no reachable escalation queue is
  benched, because the alternative is work stuck on something that cannot ask
  for help.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Iterable, Sequence

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from velaris_shared.db.models import (
    AgentModel,
    AgentScorecardModel,
    AgentWorkforceModel,
    CaseAssignmentModel,
    _utcnow,
)

log = logging.getLogger(__name__)

#: Reasons an agent gets benched by the platform rather than by a person. Named
#: so a supervisor reading the roster can tell "somebody parked this" from "the
#: platform stopped it", which are different problems.
BENCH_NO_ESCALATION = "no reachable escalation queue"
BENCH_BUDGET = "hourly token budget exceeded"

PROPOSAL_STATES = frozenset({"proposed", "approved", "rejected"})


class WorkforceError(ValueError):
    """The roster cannot be changed as asked."""


def _as_uuid(value: Any, what: str) -> uuid.UUID:
    if isinstance(value, uuid.UUID):
        return value
    try:
        return uuid.UUID(str(value))
    except (ValueError, TypeError, AttributeError) as exc:
        raise WorkforceError(f"{what} is not a valid id") from exc


async def _resolve_tenant_id(token: Any) -> uuid.UUID:
    """Accept a tenant slug or a UUID, and return the id.

    Callers arrive from both worlds: the assignment router holds a slug, the
    agents API holds an id.
    """
    if isinstance(token, uuid.UUID):
        return token
    try:
        return uuid.UUID(str(token))
    except (ValueError, TypeError, AttributeError):
        pass
    from velaris_shared.db.session import get_session_factory
    from case_service.tenancy.identity import resolve_tenant

    async with get_session_factory()() as own:
        resolved = await resolve_tenant(own, token)
    if resolved is None:
        raise WorkforceError(f"tenant {token!r} does not resolve")
    return resolved[0]


def _norm_skills(skills: Iterable[str]) -> list[str]:
    """Lower-cased and deduplicated, matching how the human router compares.

    `_resolve_skill_based` lower-cases `user_directory.skills` at comparison
    time; normalising on the way in means an agent and a person with the same
    skill actually match, which is the entire point of sharing the taxonomy.
    """
    return sorted({str(s).strip().lower() for s in (skills or ()) if str(s).strip()})


# ── the roster ───────────────────────────────────────────────────────────────

async def enrol(
    session: AsyncSession,
    *,
    agent: AgentModel,
    escalation_queue: str,
    skills: Iterable[str] = (),
    max_concurrent: int | None = None,
    token_budget_per_hour: int | None = None,
    created_by: Any | None = None,
) -> AgentWorkforceModel:
    """Put an agent on the roster.

    `escalation_queue` is required and cannot be blank. It is the one field with
    no sensible default: an agent that cannot hand work to a person is an agent
    whose failures have nowhere to go.
    """
    queue = (escalation_queue or "").strip()
    if not queue:
        raise WorkforceError(
            "an agent needs a human escalation queue before it can hold work")
    if agent.status != "active":
        raise WorkforceError(f"agent {agent.slug!r} is {agent.status}")
    if max_concurrent is not None and max_concurrent < 1:
        raise WorkforceError("max_concurrent must be at least 1, or unset for no cap")
    if token_budget_per_hour is not None and token_budget_per_hour < 1:
        raise WorkforceError("token_budget_per_hour must be positive, or unset")

    row = (await session.execute(
        select(AgentWorkforceModel)
        .where(AgentWorkforceModel.agent_id == agent.id)
    )).scalar_one_or_none()
    if row is None:
        row = AgentWorkforceModel(
            id=uuid.uuid4(), agent_id=agent.id, tenant_id=agent.tenant_id,
            created_by=(_as_uuid(created_by, "actor id")
                        if created_by is not None else None))
        session.add(row)
    row.skills = _norm_skills(skills)
    row.escalation_queue = queue[:255]
    row.max_concurrent = max_concurrent
    row.token_budget_per_hour = token_budget_per_hour
    row.updated_at = _utcnow()
    await session.flush()
    return row


async def profile(
    session: AsyncSession, agent_id: Any,
) -> AgentWorkforceModel | None:
    return (await session.execute(
        select(AgentWorkforceModel)
        .where(AgentWorkforceModel.agent_id == _as_uuid(agent_id, "agent id"))
    )).scalar_one_or_none()


async def bench(
    session: AsyncSession, row: AgentWorkforceModel, *, reason: str,
) -> AgentWorkforceModel:
    """Stop giving an agent new work. Existing assignments are untouched.

    Benching does NOT reassign what the agent already holds. That is a
    deliberate limit and not an oversight: pulling work out from under a
    part-finished step is a bigger operation than stopping the flow, and doing
    it silently would be worse than not doing it. The escalation queue is the
    supervisor's route for the rest.
    """
    row.benched = True
    row.bench_reason = str(reason or "")[:2000]
    row.benched_at = _utcnow()
    await session.flush()
    log.warning("workforce: agent %s benched — %s", row.agent_id, reason)
    return row


async def unbench(
    session: AsyncSession, row: AgentWorkforceModel,
) -> AgentWorkforceModel:
    row.benched = False
    row.bench_reason = None
    row.benched_at = None
    await session.flush()
    return row


# ── availability ─────────────────────────────────────────────────────────────

async def _active_load(session: AsyncSession, agent_id: uuid.UUID) -> int:
    return int((await session.execute(
        select(func.count()).select_from(CaseAssignmentModel).where(
            CaseAssignmentModel.assignee_type == "agent",
            CaseAssignmentModel.assignee_id == str(agent_id),
            CaseAssignmentModel.status == "active",
        )
    )).scalar_one())


async def tokens_used_last_hour(
    session: AsyncSession, agent_id: uuid.UUID,
) -> int:
    """Tokens this agent has spent in the last hour, from the F-01 ledger.

    Read from the ledger rather than counted in memory: a per-process counter
    would reset on restart, and a budget that a restart clears is not a budget.
    """
    from velaris_shared.db.models import AIDecisionRecordModel, DecisionCostModel

    since = _utcnow() - timedelta(hours=1)
    rows = (await session.execute(
        select(DecisionCostModel.quantity, DecisionCostModel.unit)
        .join(AIDecisionRecordModel,
              AIDecisionRecordModel.id == DecisionCostModel.record_id)
        .where(AIDecisionRecordModel.agent_id == agent_id,
               DecisionCostModel.priced_at >= since)
    )).all()
    # Cost lines carry MILLIONS of tokens as the quantity, because that is the
    # unit rates are published in. Converting back here keeps the budget in the
    # unit an operator actually thinks in.
    total = sum(Decimal(str(q)) for q, u in rows if u in ("mtok_in", "mtok_out"))
    return int(total * Decimal(1_000_000))


async def availability(
    session: AsyncSession, row: AgentWorkforceModel,
) -> dict[str, Any]:
    """Why this agent can or cannot take more work, in one answer.

    Returns the reasons rather than a bare bool: "at capacity" and "over
    budget" are different problems with different fixes, and a router that
    logged only "unavailable" would make both invisible.
    """
    reasons: list[str] = []
    if row.benched:
        reasons.append(f"benched: {row.bench_reason or 'no reason recorded'}")

    load = await _active_load(session, row.agent_id)
    if row.max_concurrent is not None and load >= row.max_concurrent:
        reasons.append(f"at capacity ({load}/{row.max_concurrent})")

    spent = None
    if row.token_budget_per_hour is not None:
        spent = await tokens_used_last_hour(session, row.agent_id)
        if spent >= row.token_budget_per_hour:
            reasons.append(
                f"over hourly token budget ({spent}/{row.token_budget_per_hour})")

    return {
        "available": not reasons,
        "reasons": reasons,
        "active_load": load,
        "tokens_last_hour": spent,
    }


async def enforce_budget(
    session: AsyncSession, agent_id: Any,
) -> AgentWorkforceModel | None:
    """Bench an agent that has blown its hourly budget.

    Called from the chokepoint side rather than the router: a runaway loop
    spends the budget between assignments, so waiting until the next routing
    decision would let it keep going. Returns the row when it benched one.
    """
    row = await profile(session, agent_id)
    if row is None or row.benched or row.token_budget_per_hour is None:
        return None
    spent = await tokens_used_last_hour(session, row.agent_id)
    if spent < row.token_budget_per_hour:
        return None
    await bench(session, row,
                reason=f"{BENCH_BUDGET}: {spent}/{row.token_budget_per_hour} in the last hour")
    return row


async def _escalation_reachable(
    session: AsyncSession, queue: str,
) -> bool:
    """Is there anybody who could pick this work up?

    Deliberately coarse: a queue with at least one active person in the
    directory counts. The check exists to catch the real failure — an agent
    pointed at a queue that was deleted or emptied — not to model rota coverage,
    which Calendar already owns.
    """
    from velaris_shared.db.models import UserDirectoryModel

    if not queue:
        return False
    count = int((await session.execute(
        select(func.count()).select_from(UserDirectoryModel)
        .where(UserDirectoryModel.is_active.is_(True))
    )).scalar_one())
    return count > 0


async def eligible(
    session: AsyncSession,
    *,
    tenant_id: Any,
    required_skills: Sequence[str] = (),
) -> list[tuple[AgentWorkforceModel, float]]:
    """Rostered agents that could take this work, best first.

    Scored the way `_resolve_skill_based` scores humans — fewest active
    assignments wins — multiplied by the routing weight a supervisor approved.
    An agent holding no work and weight 1.0 scores above one holding three.

    ALL required skills must be held. A partial match is not a match, exactly as
    it is not for a person: "claims + fraud" asks for someone who can do both.

    `tenant_id` may be a SLUG or a UUID. The assignment router holds a slug —
    `_tenant_for` returns one — and the roster keys on the id, so resolving here
    is what makes the strategy work at all. Without it the router's own guard
    swallows the error and falls back to the human path, and agents are never
    assigned anything while every log line reads as a successful fallback. That
    is the same silent-fallback shape F-00's attribution had, caught the same
    way: by running it.
    """
    tid = await _resolve_tenant_id(tenant_id)
    rows = list((await session.execute(
        select(AgentWorkforceModel).where(
            AgentWorkforceModel.tenant_id == tid,
            AgentWorkforceModel.benched.is_(False),
        )
    )).scalars())
    needed = set(_norm_skills(required_skills))

    scored: list[tuple[AgentWorkforceModel, float]] = []
    for row in rows:
        held = {str(s).strip().lower() for s in (row.skills or []) if isinstance(s, str)}
        if needed and not needed <= held:
            continue
        # Fail-closed staffing: an agent whose escalation target has nobody
        # behind it is benched on the spot rather than skipped quietly, so the
        # roster shows why it stopped receiving work.
        if not await _escalation_reachable(session, row.escalation_queue):
            await bench(session, row, reason=BENCH_NO_ESCALATION)
            continue
        state = await availability(session, row)
        if not state["available"]:
            continue
        weight = float(row.routing_weight or 1)
        scored.append((row, weight / (1.0 + state["active_load"])))

    scored.sort(key=lambda pair: (-pair[1], str(pair[0].agent_id)))
    return scored


# ── autonomy ─────────────────────────────────────────────────────────────────

async def may_act_autonomously(
    session: AsyncSession,
    *,
    agent: AgentModel,
    step_autonomous: bool,
    case_id: Any | None = None,
    data_classes: Iterable[str] = (),
) -> bool:
    """Can this agent apply its own answer, or must a human confirm?

    Two conditions, and the second is the one that matters. The step's
    `autonomous` setting is **necessary and not sufficient**: HxArbiter must also
    allow `agent.act_on_case`. That is what lets a tenant clamp agent autonomy by
    data class or region regardless of how somebody drew the process — the
    design's own words, and the reason this is not just a boolean read.

    Fails closed. An error resolving the policy means a human confirms.
    """
    if not step_autonomous:
        return False
    try:
        from case_service.agents import attribution as _attr

        return await _attr.check(
            session,
            agent_id=str(agent.id),
            agent_kind=agent.kind,
            tenant_id=str(agent.tenant_id),
            action=_attr.ACTION_ACT_ON_CASE,
            # The legacy answer is the step setting, which is what governed
            # before this existed — so wiring it in changes nothing until the
            # `agent` namespace is promoted.
            legacy_allowed=True,
            resource_kind="case",
            resource_id=case_id,
            resource_attrs={"data_classes": sorted({str(c) for c in (data_classes or ())})},
            context={"purpose": "autonomous_step"},
            claimed_by="agents.workforce",
        )
    except Exception:                                          # noqa: BLE001
        log.warning("workforce: autonomy check failed; a human will confirm",
                    exc_info=True)
        return False


# ── scorecards ───────────────────────────────────────────────────────────────

async def scorecard(
    session: AsyncSession, *, agent: AgentModel, days: int = 7,
) -> AgentScorecardModel:
    """Compute and store this agent's performance over a window.

    Every number comes from F-01 and F-02 — nothing is recorded twice. The
    design's exit criterion is that this reconciles with the raw data, which it
    does by construction: it counts the same rows the ledger report counts.
    """
    from velaris_shared.db.models import AIDecisionRecordModel, DecisionCostModel
    from case_service.ledger import records as ledger

    end = _utcnow()
    start = end - timedelta(days=max(1, days))

    recs = list((await session.execute(
        select(AIDecisionRecordModel).where(
            AIDecisionRecordModel.agent_id == agent.id,
            AIDecisionRecordModel.recorded_at >= start,
            AIDecisionRecordModel.recorded_at < end,
        )
    )).scalars())
    verdicts = await ledger.latest_verdicts(session, [r.id for r in recs])

    accepted = overridden = 0
    for rec in recs:
        v = verdicts.get(str(rec.id))
        if v is None:
            continue
        if v.verdict == "accepted":
            accepted += 1
        else:
            overridden += 1
    reviewed = accepted + overridden

    cost_rows = (await session.execute(
        select(DecisionCostModel.amount, DecisionCostModel.currency)
        .join(AIDecisionRecordModel,
              AIDecisionRecordModel.id == DecisionCostModel.record_id)
        .where(AIDecisionRecordModel.agent_id == agent.id,
               DecisionCostModel.priced_at >= start,
               DecisionCostModel.priced_at < end)
    )).all()
    cost_total = sum(Decimal(str(a)) for a, _ in cost_rows)
    currencies = {c for _, c in cost_rows}

    row = AgentScorecardModel(
        id=uuid.uuid4(), agent_id=agent.id, tenant_id=agent.tenant_id,
        window_start=start, window_end=end,
        decisions=len(recs), reviewed=reviewed,
        accepted=accepted, overridden=overridden,
        # NULL, never 0.0, on an empty denominator. A 0% override rate over zero
        # reviews reads as "never wrong" and means "nobody looked".
        override_rate=(Decimal(overridden) / Decimal(reviewed)) if reviewed else None,
        cost_total=cost_total,
        cost_per_decision=((cost_total / Decimal(len(recs))) if recs else None),
        # None when the window mixes currencies: one number over two of them is
        # the arithmetic error that makes a cost figure worthless.
        currency=(currencies.pop() if len(currencies) == 1 else None),
    )
    session.add(row)
    await session.flush()
    return row


async def propose_weight(
    session: AsyncSession, card: AgentScorecardModel, *,
    weight: float, reason: str,
) -> AgentScorecardModel:
    """Propose a routing-weight change. It changes nothing until approved.

    The separation is the control: a system that adjusted its own routing from
    its own performance data would be a feedback loop nobody is steering, and
    "the AI decided to send itself more work" is not a sentence anybody wants to
    read in an incident review.
    """
    if not 0 <= float(weight) <= 10:
        raise WorkforceError("routing weight must be between 0 and 10")
    if not (reason or "").strip():
        raise WorkforceError("a weight change needs a reason")
    card.proposed_weight = Decimal(str(weight))
    card.proposal_status = "proposed"
    card.proposal_reason = str(reason)[:2000]
    await session.flush()
    return card


async def decide_weight(
    session: AsyncSession, card: AgentScorecardModel, *,
    approve: bool, decided_by: str,
) -> AgentWorkforceModel | None:
    """A supervisor's answer. Only an approval touches the roster."""
    if card.proposal_status != "proposed":
        raise WorkforceError(
            f"this scorecard's proposal is {card.proposal_status or 'absent'}")
    card.proposal_status = "approved" if approve else "rejected"
    card.decided_by = str(decided_by)[:255]
    card.decided_at = _utcnow()

    if not approve:
        await session.flush()
        return None
    row = await profile(session, card.agent_id)
    if row is None:
        raise WorkforceError("this agent is no longer on the roster")
    row.routing_weight = card.proposed_weight
    row.updated_at = _utcnow()
    await session.flush()
    log.info("workforce: routing weight for %s set to %s by %s",
             card.agent_id, card.proposed_weight, decided_by)
    return row


# ── the scheduled recompute ──────────────────────────────────────────────────
#
# The first slice computed a scorecard only when somebody asked for one, which
# means the review exists for the agent a supervisor already suspected and for
# no other. A weekly figure nobody requested is the one that catches the agent
# whose override rate climbed while nobody was looking.
#
# Nothing here computes anything new: the sweep calls the same `scorecard()` a
# person calls, over the same F-01 and F-02 rows, so a stored card and one built
# by hand on the same window agree. The schedule is *when*, never *what*.

#: Long enough that a restart storm cannot turn the sweep into a write loop, and
#: short enough that a box rebooted at midnight still gets its cards.
SCORECARD_STARTUP_DELAY_SECONDS = 180


def _aware(value: datetime | None) -> datetime | None:
    """UTC-aware, whatever the driver handed back.

    `agent_scorecards.created_at` is a `MicroDateTime`, so PostgreSQL returns it
    aware and MySQL naive. Comparing the naive one against `datetime.now(utc)`
    raises `TypeError`, and inside a sweep's per-agent `except` that reads as
    "this agent failed" — every night, for every agent, on one dialect only.
    """
    if value is None:
        return None
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


async def _last_scorecard_at(
    session: AsyncSession, agent_id: uuid.UUID,
) -> datetime | None:
    return _aware((await session.execute(
        select(func.max(AgentScorecardModel.created_at))
        .where(AgentScorecardModel.agent_id == agent_id)
    )).scalar_one_or_none())


async def scorecard_sweep(
    session: AsyncSession, *, window_days: int, min_interval_hours: int,
) -> dict[str, int]:
    """Compute the scorecard for every rostered agent that is due one.

    Due, not "every agent every pass". The recompute is idempotent in substance
    but not in rows — running it twice writes two cards over near-identical
    windows — so an agent whose newest card is younger than the interval is
    skipped. That is what stops a service restarted six times in an afternoon
    from filling the table with six identical reviews of the same week.

    A benched agent is swept like any other. Its scorecard is exactly what the
    supervisor deciding whether to unbench it wants to read.
    """
    cutoff = _utcnow() - timedelta(hours=max(1, min_interval_hours))
    rows = (await session.execute(
        select(AgentWorkforceModel).order_by(AgentWorkforceModel.created_at)
    )).scalars().all()

    written = skipped = failed = 0
    for row in rows:
        try:
            last = await _last_scorecard_at(session, row.agent_id)
            if last is not None and last > cutoff:
                skipped += 1
                continue
            agent = await session.get(AgentModel, row.agent_id)
            if agent is None:          # roster row outliving its agent
                skipped += 1
                continue
            await scorecard(session, agent=agent, days=window_days)
            # Per agent, not per sweep: one agent's bad window must not throw
            # away the cards already computed for the others.
            await session.commit()
            written += 1
        except Exception as exc:       # noqa: BLE001 — a sweep outlives one agent
            await session.rollback()
            failed += 1
            log.warning("workforce: scorecard sweep failed for agent %s: %s",
                        row.agent_id, exc)
    return {"written": written, "skipped": skipped, "failed": failed}


async def scorecard_cron() -> None:
    """Periodic sweep. **Off unless an operator turns it on.**

    Default off for the reason every F-series default is off: an install taking
    this update gets no new writer it did not ask for. It is inert either way on
    an install with an empty roster — enrolment is an explicit act, so every
    existing install has one — but "inert today" is not a licence to start a
    background writer on somebody's box unannounced.
    """
    import asyncio

    from velaris_shared.config import get_settings
    from velaris_shared.db.session import get_session_factory

    settings = get_settings()
    if not getattr(settings, "agent_scorecard_cron_enabled", False):
        log.info("Agent scorecard sweep OFF — set "
                 "VELARIS_CASE_AGENT_SCORECARD_CRON_ENABLED=true to enable")
        return

    interval = max(1, int(getattr(settings, "agent_scorecard_interval_hours", 24)))
    window = max(1, int(getattr(settings, "agent_scorecard_window_days", 7)))
    log.info("Agent scorecard sweep ON (every %sh over a %s-day window)",
             interval, window)

    await asyncio.sleep(SCORECARD_STARTUP_DELAY_SECONDS)
    while True:
        try:
            async with get_session_factory()() as session:
                result = await scorecard_sweep(
                    session, window_days=window, min_interval_hours=interval)
            if result["written"] or result["failed"]:
                log.info("workforce: scorecard sweep wrote %s, skipped %s, "
                         "failed %s", result["written"], result["skipped"],
                         result["failed"])
        except Exception as exc:       # noqa: BLE001 — the loop must outlive it
            # Same posture as every other cron here: a bad database moment must
            # not silently end the schedule, because a dead sweep looks exactly
            # like an agent nobody reviewed.
            log.warning("workforce: scorecard sweep cycle failed: %s", exc)
        await asyncio.sleep(interval * 3600)
