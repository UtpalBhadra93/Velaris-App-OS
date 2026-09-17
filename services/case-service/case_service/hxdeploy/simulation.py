"""F-04 (first slice) — rehearsing a promotion, and gating on the result.

**What this is, and what it is not.** The design's digital twin executes the
candidate definition on an isolated Temporal task queue against scratch schemas,
under synthetic Locust load. That is infrastructure, and none of it is built
here. What IS built is the half that can be proven today:

* the **rehearsal** — HxReplay already replays the recorded caseload through a
  candidate rule set and reports divergence and a cost delta, which is the
  "replayed historical load" half of the design's load mix;
* the **gate** — thresholds, a pass/fail verdict, and a promotion that cannot
  be approved past a failure without a signed, reasoned, audited override;
* the **twin identity** — an F-00 agent with an empty capability ceiling, so
  "the twin can reach nothing" is a property of the registry rather than a
  promise in a comment.

The synthetic-load half is absent, and `findings` says so on every run rather
than leaving a reader to assume the rehearsal was more than it was.

**The gate ships OFF.** No `simulation_gates` row means not enforced, and the
column defaults to disabled besides. An install taking this update approves
deployments exactly as it did yesterday.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from velaris_shared.db.models import (
    DeploymentRunModel,
    SimulationGateModel,
    SimulationRunModel,
    _utcnow,
)

log = logging.getLogger(__name__)

#: The F-00 agent every twin run is attributed to. One per tenant, registered on
#: first use, and with an EMPTY capability ceiling — a simulation reads history
#: and writes a report, and needs no authority at all to do it. An empty ceiling
#: also means no token can ever be minted for it, which is the correct amount of
#: power to give a thing that executes a candidate definition.
TWIN_AGENT_SLUG = "hxdeploy.twin"

#: Named on every run. The rehearsal is honest about what it covered, because a
#: reader who assumes otherwise will read a passing gate as a capacity guarantee.
#:
#: Two notes, not one, and which appears is decided by whether a twin actually
#: ran — never by whether one is configured. An install with a twin configured
#: and no worker polling it must still read the first sentence.
COVERAGE_NOTE = (
    "Replayed recorded cases through the candidate rules. This is historical "
    "load only — no synthetic load was generated, so throughput and contention "
    "are NOT covered by this result.")

COVERAGE_NOTE_TWIN = (
    "Replayed recorded cases through the candidate rules, AND executed the "
    "candidate against synthetic cases in an isolated twin database. The twin "
    "load is a bounded batch, not a ramped concurrency profile, so sustained "
    "throughput and contention are still NOT covered by this result.")


class SimulationError(ValueError):
    """The simulation cannot be run as asked."""


def _tenant_key(tenant_id: uuid.UUID | None) -> str:
    return "platform" if tenant_id is None else str(tenant_id)


def _as_uuid(value: Any, what: str) -> uuid.UUID:
    if isinstance(value, uuid.UUID):
        return value
    try:
        return uuid.UUID(str(value))
    except (ValueError, TypeError, AttributeError) as exc:
        raise SimulationError(f"{what} is not a valid id") from exc


# ── the gate's configuration ─────────────────────────────────────────────────

async def resolve_gate(
    session: AsyncSession, *, tenant_id: Any | None,
) -> SimulationGateModel | None:
    """The gate that applies here — the tenant's own, else the platform default.

    `None` means **not enforced**, and that is the shipped state. Every caller
    treats None as "approve as before"; nothing treats it as "block".
    """
    tid = _as_uuid(tenant_id, "tenant id") if tenant_id is not None else None
    if tid is not None:
        row = (await session.execute(
            select(SimulationGateModel)
            .where(SimulationGateModel.tenant_key == str(tid))
        )).scalar_one_or_none()
        if row is not None:
            return row
    return (await session.execute(
        select(SimulationGateModel)
        .where(SimulationGateModel.tenant_key == "platform")
    )).scalar_one_or_none()


async def upsert_gate(
    session: AsyncSession, *, tenant_id: Any | None, enabled: bool,
    max_divergent_pct: float | None = None,
    max_cost_increase_pct: float | None = None,
    min_cohort: int | None = None,
    require_proof: bool = False,
    min_twin_cases: int | None = None,
    max_twin_failure_pct: float | None = None,
    max_twin_p95_ms: int | None = None,
    created_by: Any | None = None,
) -> SimulationGateModel:
    tid = _as_uuid(tenant_id, "tenant id") if tenant_id is not None else None
    key = _tenant_key(tid)
    row = (await session.execute(
        select(SimulationGateModel).where(SimulationGateModel.tenant_key == key)
    )).scalar_one_or_none()
    if row is None:
        row = SimulationGateModel(id=uuid.uuid4(), tenant_id=tid, tenant_key=key,
                                  created_by=(_as_uuid(created_by, "actor id")
                                              if created_by is not None else None))
        session.add(row)
    row.enabled = bool(enabled)
    row.max_divergent_pct = max_divergent_pct
    row.max_cost_increase_pct = max_cost_increase_pct
    row.min_cohort = min_cohort
    row.require_proof = bool(require_proof)
    row.min_twin_cases = min_twin_cases
    row.max_twin_failure_pct = max_twin_failure_pct
    row.max_twin_p95_ms = max_twin_p95_ms
    row.updated_at = _utcnow()
    await session.flush()
    return row


def _thresholds(gate: SimulationGateModel | None) -> dict[str, Any]:
    if gate is None:
        return {"enforced": False}
    return {
        "enforced": bool(gate.enabled),
        "max_divergent_pct": (float(gate.max_divergent_pct)
                              if gate.max_divergent_pct is not None else None),
        "max_cost_increase_pct": (float(gate.max_cost_increase_pct)
                                  if gate.max_cost_increase_pct is not None else None),
        "min_cohort": gate.min_cohort,
        "require_proof": bool(gate.require_proof),
        # F-04 second slice. NULL is NOT CHECKED, never zero — the same rule the
        # first three thresholds follow, and the reason an install that already
        # holds a gate row approves exactly what it approved yesterday.
        "min_twin_cases": gate.min_twin_cases,
        "max_twin_failure_pct": (float(gate.max_twin_failure_pct)
                                 if gate.max_twin_failure_pct is not None else None),
        "max_twin_p95_ms": gate.max_twin_p95_ms,
    }


# ── the twin's identity ──────────────────────────────────────────────────────

async def twin_agent_id(tenant_slug: Any) -> str | None:
    """Register (once) and return the twin's agent id.

    Empty ceiling, deliberately. A simulation reads recorded history and writes
    a report; it needs no capability, and an agent with none can never be issued
    a token — which is the correct amount of authority for a thing that runs a
    candidate definition.
    """
    from case_service.agents import identity as agent_identity

    return await agent_identity.register_out_of_band(
        tenant_id=tenant_slug, slug=TWIN_AGENT_SLUG, kind="internal",
        display_name="HxDeploy — simulation twin", capabilities=[])


async def twin_may_egress(
    session: AsyncSession, *, tenant_id: Any, agent_id: str | None, target: str,
) -> bool:
    """Always False, and recorded as a decision so the attempt is visible.

    The design's headline risk is a mis-stubbed connector firing a real email
    from inside a rehearsal. The answer here is not a policy an operator might
    later relax: `legacy_allowed=False` means the caller is denied whatever the
    `egress` namespace is set to, so the twin cannot reach the network even if
    somebody promotes that namespace to permissive.

    It still goes through the PEP, because an attempt nobody recorded is an
    attempt nobody can review.
    """
    from case_service.agents import attribution as _attr

    try:
        return await _attr.check(
            session,
            agent_id=agent_id or f"unregistered:{TWIN_AGENT_SLUG}",
            agent_kind="internal",
            tenant_id=tenant_id,
            action="egress.http",
            legacy_allowed=False,        # deny by construction, not by policy
            resource_kind="simulation",
            context={"target_domain": str(target).lower(),
                     "purpose": "simulation_twin"},
            claimed_by="hxdeploy.simulation",
        )
    except Exception:                                          # noqa: BLE001
        # An observer that breaks must not turn a denial into an allow.
        log.warning("simulation: twin egress check failed; denying", exc_info=True)
        return False


# ── running one ──────────────────────────────────────────────────────────────

async def simulate(
    session: AsyncSession,
    *,
    run: DeploymentRunModel,
    tenant_uuid: Any,
    tenant_slug: Any,
    replay_summary: dict[str, Any] | None,
    replay_run_id: Any | None = None,
    proof_id: Any | None = None,
    policy_touched: bool = False,
    created_by: Any | None = None,
    twin_run: Any | None = None,
) -> SimulationRunModel:
    """Judge a rehearsal against the gate and store the verdict.

    `replay_summary` is HxReplay's own aggregate — this function does not
    re-derive it. A second aggregation would be a second opinion about the same
    replay, and the two would eventually disagree.

    `None` means no rehearsal was possible, which is **refused**, not failed:
    thresholds exceeded is something an author can act on, and "we could not
    rehearse this" is something else entirely.
    """
    gate = await resolve_gate(session, tenant_id=tenant_uuid)
    thresholds = _thresholds(gate)
    agent_id = await twin_agent_id(tenant_slug)

    # `synthetic_load` is true only when a twin actually EXECUTED something —
    # not when one is configured, and not when one was configured and refused.
    # That distinction is the whole reason this field is trustworthy.
    twin_ran = bool(twin_run is not None and twin_run.status == "complete"
                    and int(twin_run.started_cases or 0) > 0)
    findings: dict[str, Any] = {
        "coverage": COVERAGE_NOTE_TWIN if twin_ran else COVERAGE_NOTE,
        "synthetic_load": twin_ran,
        "twin_agent_id": agent_id,
        "package_checksum": run.package_checksum,
        "twin": _twin_findings(twin_run),
    }

    if replay_summary is None:
        return await _store(
            session, run=run, tenant_uuid=tenant_uuid, status="refused",
            findings={**findings, "refused": "no_replay",
                      "explanation": (
                          "No replay could be run for this package, so there is "
                          "nothing to judge. A gate that passed on an absent "
                          "rehearsal would be worse than no gate.")},
            thresholds=thresholds, replay_run_id=replay_run_id, proof_id=proof_id,
            agent_id=agent_id, created_by=created_by,
            twin_run_id=getattr(twin_run, "id", None))

    total = int(replay_summary.get("total") or replay_summary.get("cases") or 0)
    determinate = int(replay_summary.get("determinate") or 0)
    divergent = int(replay_summary.get("diverged")
                    or replay_summary.get("divergent") or 0)
    cost = replay_summary.get("cost") or {}
    cost_delta_pct = cost.get("delta_pct")

    divergent_pct = (round(100.0 * divergent / total, 3) if total else None)
    findings.update({
        "cohort_size": total,
        "determinate": determinate,
        "divergent": divergent,
        "divergent_pct": divergent_pct,
        "cost_delta_pct": cost_delta_pct,
        "proof_id": str(proof_id) if proof_id else None,
        "policy_touched": bool(policy_touched),
    })

    breaches: list[str] = []
    if thresholds["enforced"]:
        mc = thresholds["min_cohort"]
        if mc is not None and total < mc:
            # Too small to judge is a REFUSAL. Passing a change because only two
            # cases were available to rehearse it is the failure mode a minimum
            # exists to prevent.
            return await _store(
                session, run=run, tenant_uuid=tenant_uuid, status="refused",
                findings={**findings, "refused": "cohort_too_small",
                          "explanation": (
                              f"Only {total} case(s) could be replayed and the "
                              f"gate requires {mc}. A verdict from a cohort this "
                              f"small is not evidence either way.")},
                thresholds=thresholds, replay_run_id=replay_run_id,
                proof_id=proof_id, agent_id=agent_id, created_by=created_by,
                twin_run_id=getattr(twin_run, "id", None))

        md = thresholds["max_divergent_pct"]
        if md is not None and divergent_pct is not None and divergent_pct > md:
            breaches.append(
                f"{divergent_pct}% of replayed cases diverge, above the {md}% limit")

        mcp = thresholds["max_cost_increase_pct"]
        if mcp is not None and cost_delta_pct is not None and cost_delta_pct > mcp:
            breaches.append(
                f"cost rises {cost_delta_pct}%, above the {mcp}% limit")

        if thresholds["require_proof"] and policy_touched and proof_id is None:
            breaches.append(
                "this package changes policy and the gate requires a compliance "
                "proof, which was not produced")

        breaches += _twin_breaches(thresholds, twin_run, twin_ran)

    findings["breaches"] = breaches
    status = "failed" if breaches else "passed"
    return await _store(
        session, run=run, tenant_uuid=tenant_uuid, status=status,
        findings=findings, thresholds=thresholds, replay_run_id=replay_run_id,
        proof_id=proof_id, agent_id=agent_id, created_by=created_by,
        twin_run_id=getattr(twin_run, "id", None))


def _twin_findings(twin_run: Any | None) -> dict[str, Any] | None:
    """What the twin did, in the simulation's own words.

    `None` when no twin run exists at all, which is what an install with no twin
    configured produces and what every run produced before the twin was built.
    """
    if twin_run is None:
        return None
    return {
        "id": str(twin_run.id),
        "status": twin_run.status,
        "refusal_reason": twin_run.refusal_reason,
        "isolation_verified": bool(twin_run.isolation_verified),
        "load_profile": twin_run.load_profile,
        "requested": twin_run.requested_cases,
        "started": twin_run.started_cases,
        "terminal": twin_run.terminal_cases,
        "stuck": twin_run.stuck_cases,
        "failed": twin_run.failed_cases,
        "p50_ms": twin_run.p50_ms,
        "p95_ms": twin_run.p95_ms,
        "torn_down_rows": twin_run.torn_down_rows,
        "teardown_error": twin_run.teardown_error,
    }


def _twin_breaches(thresholds: dict[str, Any], twin_run: Any | None,
                   twin_ran: bool) -> list[str]:
    """Judge the twin half.

    **A twin threshold that is set and could not be measured is a BREACH**, not
    a pass. The three thresholds below are an operator saying "do not promote
    this without rehearsing it"; treating an absent rehearsal as satisfying them
    would let a broken twin quietly disable the gate the operator asked for.
    That is the opposite of every other NULL rule here, and it is deliberate:
    NULL means *not checked*, but "checked and unavailable" means refuse.
    """
    wanted = [k for k in ("min_twin_cases", "max_twin_failure_pct", "max_twin_p95_ms")
              if thresholds.get(k) is not None]
    if not wanted:
        return []
    if not twin_ran:
        why = (twin_run.error or twin_run.refusal_reason) if twin_run is not None \
            else "no twin run was attempted"
        return [f"the gate requires a twin rehearsal ({', '.join(wanted)}) "
                f"and none was produced: {why}"]

    out: list[str] = []
    started = int(twin_run.started_cases or 0)
    mc = thresholds.get("min_twin_cases")
    if mc is not None and started < mc:
        out.append(f"the twin ran {started} case(s), below the {mc} the gate requires")

    mf = thresholds.get("max_twin_failure_pct")
    if mf is not None and started:
        # Stuck counts as failure here. A candidate that strands cases has
        # failed at the only thing a rehearsal is asked to check.
        bad = int(twin_run.failed_cases or 0) + int(twin_run.stuck_cases or 0)
        pct = round(100.0 * bad / started, 3)
        if pct > mf:
            out.append(f"{pct}% of twin cases failed or stalled, above the {mf}% limit")

    mp = thresholds.get("max_twin_p95_ms")
    if mp is not None and twin_run.p95_ms is not None and twin_run.p95_ms > mp:
        out.append(f"twin p95 is {twin_run.p95_ms}ms, above the {mp}ms limit")
    return out


async def _store(
    session: AsyncSession, *, run, tenant_uuid, status, findings, thresholds,
    replay_run_id, proof_id, agent_id, created_by, twin_run_id=None,
) -> SimulationRunModel:
    row = SimulationRunModel(
        id=uuid.uuid4(),
        tenant_id=_as_uuid(tenant_uuid, "tenant id"),
        deployment_run_id=run.id,
        package_checksum=run.package_checksum,
        status=status,
        findings=findings,
        thresholds=thresholds,
        replay_run_id=(_as_uuid(replay_run_id, "replay run id")
                       if replay_run_id else None),
        proof_id=_as_uuid(proof_id, "proof id") if proof_id else None,
        twin_agent_id=(_as_uuid(agent_id, "agent id")
                       if agent_id and "unregistered" not in str(agent_id) else None),
        twin_run_id=_as_uuid(twin_run_id, "twin run id") if twin_run_id else None,
        created_by=_as_uuid(created_by, "actor id") if created_by is not None else None,
        completed_at=_utcnow(),
    )
    session.add(row)
    await session.flush()
    # The run points at the NEWEST simulation; earlier ones stay readable.
    run.simulation_id = row.id
    await session.flush()
    return row


async def latest(
    session: AsyncSession, run_id: Any,
) -> SimulationRunModel | None:
    return (await session.execute(
        select(SimulationRunModel)
        .where(SimulationRunModel.deployment_run_id == _as_uuid(run_id, "run id"))
        .order_by(SimulationRunModel.created_at.desc()).limit(1)
    )).scalars().first()


# ── the gate itself ──────────────────────────────────────────────────────────

async def assert_gate_clear(
    session: AsyncSession, run: DeploymentRunModel, *, tenant_uuid: Any,
) -> None:
    """Raise unless this run may be approved. Silent when the gate is off.

    Called from `approve_run`. Four things let a run through, and only four:

    1. **No gate configured, or it is disabled.** The shipped state. An install
       taking this update reaches this line and returns immediately.
    2. An explicit, recorded **override**.
    3. A **passing** simulation of THIS package.
    4. Nothing else.

    The package check is not ceremony: a simulation of a different build is not
    evidence about this promotion, and without the comparison the gate could be
    satisfied by rehearsing something harmless and then approving something else.
    """
    gate = await resolve_gate(session, tenant_id=tenant_uuid)
    if gate is None or not gate.enabled:
        return

    if run.gate_overridden_by:
        return

    sim = await latest(session, run.id)
    if sim is None:
        raise PermissionError(
            "This deployment has not been simulated. Run the simulation, or "
            "override the gate with a reason.")
    if sim.status != "passed":
        raise PermissionError(
            f"The simulation {sim.status}: "
            + "; ".join(sim.findings.get("breaches")
                        or [sim.findings.get("explanation", "see the findings")])
            + ". Fix the change, or override the gate with a reason.")
    if (run.package_checksum or sim.package_checksum) and \
            sim.package_checksum != run.package_checksum:
        raise PermissionError(
            "The passing simulation was run against a different package than "
            "this deployment carries. Re-simulate the package being promoted.")


async def override_gate(
    session: AsyncSession, run: DeploymentRunModel, *, actor_id: str, reason: str,
) -> DeploymentRunModel:
    """Record a deliberate bypass. The reason is mandatory and permanent.

    Overriding is separated from approving, and the two must not be the same
    person's single act: this is the one control that lets a change through
    after its rehearsal said not to.
    """
    reason = (reason or "").strip()
    if len(reason) < 10:
        # Not a formality. "ok" as a reason makes the audit record useless, and
        # this row is the only trace that a failed gate was ever bypassed.
        raise SimulationError(
            "an override needs a real reason — it is the only record that a "
            "failed gate was bypassed")
    run.gate_override_reason = reason[:4000]
    run.gate_overridden_by = str(actor_id)[:255]
    run.gate_overridden_at = _utcnow()
    await session.flush()
    log.warning("hxdeploy: simulation gate OVERRIDDEN on run %s by %s: %s",
                run.id, actor_id, reason)
    return run
