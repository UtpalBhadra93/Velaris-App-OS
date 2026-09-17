"""F-05 (first slice) — agents as workers.

The tests that matter are the ones where the roster refuses to do something
convenient:

* an agent in the registry is **not** automatically a worker;
* an agent with no reachable escalation queue is **benched**, not skipped;
* **autonomy is a policy** — the step flag is necessary and not sufficient;
* a **budget breach benches** the agent rather than merely declining one call;
* a routing-weight change **does nothing until a supervisor approves it**;
* and the new assignment strategy is **opt-in**, so no existing case type starts
  routing work to an agent because it took an update.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio
from sqlalchemy import select

from velaris_shared.db.models import (
    AgentScorecardModel,
    AgentWorkforceModel,
    ArbiterNamespaceModel,
    CaseAssignmentModel,
    TenantModel,
    UserDirectoryModel,
)

from case_service.agents import identity, workforce
from case_service.ledger import records as ledger

pytestmark = pytest.mark.asyncio

PREFIX = "/api/v1/agents"

SEED = [("ai", "deny"), ("egress", "deny"), ("export", "deny"),
        ("vault", "deny"), ("case", "allow"), ("auth", "allow"),
        ("agent", "deny")]


@pytest_asyncio.fixture(autouse=True)
async def tenant(session):
    from case_service.hxarbiter import pdp
    from case_service.tenancy import identity as _identity

    _identity.clear_cache()
    identity._SLUG_CACHE.clear()
    pdp.invalidate_cache()
    for ns, eff in SEED:
        session.add(ArbiterNamespaceModel(
            namespace=ns, default_effect=eff, max_mode="shadow", description="",
            sample_allow_rate=1, sample_default_rate=1,
            updated_at=datetime.now(timezone.utc)))
    t = TenantModel(id=uuid.uuid4(), slug="default", name="Default")
    session.add(t)
    # Somebody has to be behind the escalation queue, or every agent benches.
    session.add(UserDirectoryModel(
        user_id=str(uuid.uuid4()), email="ops@example.test",
        display_name="Ops", is_active=True))
    await session.commit()
    yield t
    identity._SLUG_CACHE.clear()
    _identity.clear_cache()
    pdp.invalidate_cache()


async def _agent(session, tenant, slug="worker-1", caps=("agent.act_on_case",)):
    a = await identity.create_agent(
        session, tenant_id=tenant.id, slug=slug, kind="internal",
        capabilities=caps)
    await session.commit()
    return a


async def _enrol(session, agent, **kw):
    row = await workforce.enrol(
        session, agent=agent,
        escalation_queue=kw.pop("escalation_queue", "ops-queue"), **kw)
    await session.commit()
    return row


# ══ the roster ══════════════════════════════════════════════════════════════


async def test_an_agent_is_not_a_worker_until_enrolled(session, tenant):
    """HxNexus's case-Q&A agent must never be handed a case step."""
    agent = await _agent(session, tenant)
    assert await workforce.profile(session, agent.id) is None
    assert await workforce.eligible(session, tenant_id=tenant.id) == []


async def test_enrolling_needs_an_escalation_queue(session, tenant):
    """The one field with no sensible default: an agent that cannot hand work
    to a person is one whose failures have nowhere to go."""
    agent = await _agent(session, tenant)
    with pytest.raises(workforce.WorkforceError) as exc:
        await workforce.enrol(session, agent=agent, escalation_queue="   ")
    assert "escalation queue" in str(exc.value)


async def test_a_suspended_agent_cannot_be_enrolled(session, tenant):
    agent = await _agent(session, tenant)
    await identity.set_status(session, agent, "suspended", reason="test")
    await session.commit()
    with pytest.raises(workforce.WorkforceError):
        await workforce.enrol(session, agent=agent, escalation_queue="ops")


async def test_skills_are_normalised_to_the_human_taxonomy(session, tenant):
    """`_resolve_skill_based` lower-cases the human directory at comparison
    time; an agent whose skills were stored as typed would never match."""
    agent = await _agent(session, tenant)
    row = await _enrol(session, agent, skills=["Claims", " FRAUD ", "claims"])
    assert row.skills == ["claims", "fraud"]


async def test_all_required_skills_must_be_held(session, tenant):
    """A partial match is not a match — 'claims + fraud' asks for both."""
    agent = await _agent(session, tenant)
    await _enrol(session, agent, skills=["claims"])
    assert await workforce.eligible(
        session, tenant_id=tenant.id, required_skills=["claims"]) != []
    assert await workforce.eligible(
        session, tenant_id=tenant.id, required_skills=["claims", "fraud"]) == []


async def test_an_unreachable_escalation_queue_benches_the_agent(session, tenant):
    """Fail-closed staffing: the alternative is work stuck on something that
    cannot ask for help. Benched, not silently skipped, so the roster says why."""
    agent = await _agent(session, tenant)
    row = await _enrol(session, agent)
    # Nobody active in the directory at all.
    await session.execute(
        UserDirectoryModel.__table__.update().values(is_active=False))
    await session.commit()

    assert await workforce.eligible(session, tenant_id=tenant.id) == []
    await session.refresh(row)
    assert row.benched is True
    assert row.bench_reason == workforce.BENCH_NO_ESCALATION


async def test_capacity_is_respected(session, tenant):
    agent = await _agent(session, tenant)
    row = await _enrol(session, agent, max_concurrent=1)
    session.add(CaseAssignmentModel(
        case_id=uuid.uuid4(), step_id="s1", assignee_type="agent",
        assignee_id=str(agent.id), status="active"))
    await session.commit()

    state = await workforce.availability(session, row)
    assert state["available"] is False
    assert "at capacity" in state["reasons"][0]
    assert await workforce.eligible(session, tenant_id=tenant.id) == []


async def test_availability_gives_reasons_not_just_a_flag(session, tenant):
    """'At capacity' and 'over budget' are different problems with different
    fixes; a router that logged only 'unavailable' would hide both."""
    agent = await _agent(session, tenant)
    row = await _enrol(session, agent, max_concurrent=1, token_budget_per_hour=100)
    state = await workforce.availability(session, row)
    assert state["available"] is True
    assert state["reasons"] == []
    assert state["tokens_last_hour"] == 0


async def test_a_benched_agent_takes_no_work(session, tenant):
    agent = await _agent(session, tenant)
    row = await _enrol(session, agent)
    await workforce.bench(session, row, reason="under review")
    await session.commit()
    assert await workforce.eligible(session, tenant_id=tenant.id) == []
    await workforce.unbench(session, row)
    await session.commit()
    assert await workforce.eligible(session, tenant_id=tenant.id) != []


async def test_the_least_loaded_agent_wins_scaled_by_weight(session, tenant):
    a1 = await _agent(session, tenant, slug="a1")
    a2 = await _agent(session, tenant, slug="a2")
    await _enrol(session, a1)
    r2 = await _enrol(session, a2)
    session.add(CaseAssignmentModel(
        case_id=uuid.uuid4(), step_id="s1", assignee_type="agent",
        assignee_id=str(a2.id), status="active"))
    await session.commit()

    ranked = await workforce.eligible(session, tenant_id=tenant.id)
    assert str(ranked[0][0].agent_id) == str(a1.id)

    # A supervisor-approved weight can outrank load.
    r2.routing_weight = 10
    await session.commit()
    ranked = await workforce.eligible(session, tenant_id=tenant.id)
    assert str(ranked[0][0].agent_id) == str(a2.id)


# ══ budget ══════════════════════════════════════════════════════════════════


async def test_a_budget_breach_benches_the_agent(session, tenant):
    """Benching, not merely declining one call: a runaway loop spends the budget
    between assignments, so waiting for the next routing decision lets it run."""
    from case_service.costing import decisions as dcost

    agent = await _agent(session, tenant)
    row = await _enrol(session, agent, token_budget_per_hour=1_000)

    rec = await ledger.record(
        session, tenant_id=tenant.id, purpose="test", model_id="m",
        agent_id=str(agent.id), input_text="q", output_text="a")
    await dcost.price_ai_decision(session, rec, input_tokens=5_000, output_tokens=0)
    await session.commit()

    assert await workforce.tokens_used_last_hour(session, agent.id) == 5_000
    benched = await workforce.enforce_budget(session, agent.id)
    await session.commit()
    assert benched is not None
    await session.refresh(row)
    assert row.benched is True
    assert workforce.BENCH_BUDGET in row.bench_reason


async def test_an_agent_within_budget_is_not_benched(session, tenant):
    agent = await _agent(session, tenant)
    await _enrol(session, agent, token_budget_per_hour=1_000_000)
    assert await workforce.enforce_budget(session, agent.id) is None


async def test_budget_spend_is_read_from_the_ledger_not_memory(session, tenant):
    """A per-process counter would reset on restart, and a budget a restart
    clears is not a budget."""
    from case_service.costing import decisions as dcost

    agent = await _agent(session, tenant)
    await _enrol(session, agent, token_budget_per_hour=10)
    rec = await ledger.record(
        session, tenant_id=tenant.id, purpose="test", model_id="m",
        agent_id=str(agent.id), input_text="q")
    await dcost.price_ai_decision(session, rec, input_tokens=1_000, output_tokens=2_000)
    await session.commit()
    assert await workforce.tokens_used_last_hour(session, agent.id) == 3_000


# ══ autonomy ════════════════════════════════════════════════════════════════


async def test_a_non_autonomous_step_always_needs_a_human(session, tenant):
    agent = await _agent(session, tenant)
    assert await workforce.may_act_autonomously(
        session, agent=agent, step_autonomous=False) is False


async def test_autonomy_is_a_policy_not_just_the_step_flag(session, tenant):
    """The step setting is necessary and NOT sufficient — HxArbiter must also
    allow, so a tenant can clamp agent autonomy regardless of process design.

    In `shadow` the engine is not binding, so the caller's own answer stands and
    wiring this in changes nothing until the namespace is promoted.
    """
    agent = await _agent(session, tenant)
    allowed = await workforce.may_act_autonomously(
        session, agent=agent, step_autonomous=True,
        case_id=uuid.uuid4(), data_classes=["special_category"])
    await session.commit()
    assert allowed is True

    from velaris_shared.db.models import ArbiterDecisionModel
    row = (await session.execute(
        select(ArbiterDecisionModel)
        .where(ArbiterDecisionModel.action == "agent.act_on_case")
    )).scalars().first()
    assert row is not None, "the autonomy decision was never recorded"
    assert row.claimed_by == "agents.workforce"


async def test_autonomy_fails_closed(session, tenant, monkeypatch):
    """An error resolving the policy means a human confirms."""
    from case_service.agents import attribution

    async def _boom(*_a, **_k):
        raise RuntimeError("engine down")

    monkeypatch.setattr(attribution, "check", _boom)
    agent = await _agent(session, tenant)
    assert await workforce.may_act_autonomously(
        session, agent=agent, step_autonomous=True) is False


# ══ scorecards ══════════════════════════════════════════════════════════════


async def test_the_scorecard_reconciles_with_the_ledger(session, tenant):
    """The design's exit criterion. It holds by construction: the scorecard
    counts the same rows the F-01 report counts."""
    from case_service.costing import decisions as dcost

    agent = await _agent(session, tenant)
    await dcost.upsert_rate(session, tenant_id=tenant.id, model_id="m",
                            input_per_mtok=10, output_per_mtok=10)
    await session.commit()

    for verdict in ("accepted", "accepted", "edited"):
        rec = await ledger.record(
            session, tenant_id=tenant.id, purpose="test", model_id="m",
            agent_id=str(agent.id), input_text="q", output_text="a")
        await ledger.record_verdict(session, record_id=rec.id, verdict=verdict)
        await dcost.price_ai_decision(session, rec, input_tokens=100_000,
                                      output_tokens=0)
    await session.commit()

    card = await workforce.scorecard(session, agent=agent)
    await session.commit()
    assert card.decisions == 3
    assert card.reviewed == 3
    assert card.accepted == 2
    assert card.overridden == 1
    assert float(card.override_rate) == pytest.approx(1 / 3, abs=1e-4)
    assert float(card.cost_total) == pytest.approx(3.0)     # 3 × 0.1 Mtok × 10
    assert float(card.cost_per_decision) == pytest.approx(1.0)


async def test_the_override_rate_is_null_when_nothing_was_reviewed(session, tenant):
    """0% over zero reviews reads as 'never wrong' and means 'nobody looked'."""
    agent = await _agent(session, tenant)
    await ledger.record(session, tenant_id=tenant.id, purpose="test",
                        model_id="m", agent_id=str(agent.id), input_text="q")
    await session.commit()
    card = await workforce.scorecard(session, agent=agent)
    assert card.decisions == 1
    assert card.override_rate is None


async def test_a_weight_proposal_changes_nothing_until_approved(session, tenant):
    """A system that adjusted its own routing from its own performance data is
    a feedback loop nobody is steering."""
    agent = await _agent(session, tenant)
    row = await _enrol(session, agent)
    card = await workforce.scorecard(session, agent=agent)
    await workforce.propose_weight(session, card, weight=2.5,
                                   reason="consistently accepted")
    await session.commit()

    await session.refresh(row)
    assert float(row.routing_weight) == 1.0        # untouched
    assert card.proposal_status == "proposed"

    await workforce.decide_weight(session, card, approve=True, decided_by="sup")
    await session.commit()
    await session.refresh(row)
    assert float(row.routing_weight) == 2.5


async def test_a_rejected_proposal_leaves_the_weight_alone(session, tenant):
    agent = await _agent(session, tenant)
    row = await _enrol(session, agent)
    card = await workforce.scorecard(session, agent=agent)
    await workforce.propose_weight(session, card, weight=9, reason="x")
    await workforce.decide_weight(session, card, approve=False, decided_by="sup")
    await session.commit()
    await session.refresh(row)
    assert float(row.routing_weight) == 1.0
    assert card.proposal_status == "rejected"


async def test_a_proposal_needs_a_reason(session, tenant):
    agent = await _agent(session, tenant)
    await _enrol(session, agent)
    card = await workforce.scorecard(session, agent=agent)
    with pytest.raises(workforce.WorkforceError):
        await workforce.propose_weight(session, card, weight=2, reason="  ")


# ══ assignment ══════════════════════════════════════════════════════════════


async def test_the_workforce_strategy_is_opt_in(session, tenant):
    """No existing case type starts routing work to an agent because it took an
    update — every other strategy is untouched."""
    from case_service.core.assignment_router import _RESOLVERS, resolve_assignment

    assert "workforce" in _RESOLVERS
    agent = await _agent(session, tenant)
    await _enrol(session, agent, skills=["claims"])

    # skill_based, the pre-existing strategy, still routes to humans only.
    kind, _ = await resolve_assignment(
        session, {"strategy": "skill_based", "skill_requirements": ["claims"],
                  "target": "q"}, {})
    assert kind != "agent"


async def test_the_workforce_strategy_picks_an_agent(session, tenant):
    from case_service.core.assignment_router import resolve_assignment

    agent = await _agent(session, tenant)
    await _enrol(session, agent, skills=["claims"])
    kind, who = await resolve_assignment(
        session, {"strategy": "workforce", "skill_requirements": ["claims"],
                  "target": "ops-queue"}, {})
    assert kind == "agent"
    assert who == str(agent.id)


async def test_human_only_steps_never_reach_an_agent(session, tenant):
    from case_service.core.assignment_router import resolve_assignment

    agent = await _agent(session, tenant)
    await _enrol(session, agent, skills=["claims"])
    kind, _ = await resolve_assignment(
        session, {"strategy": "workforce", "assignable_to": "human",
                  "skill_requirements": ["claims"], "target": "ops-queue"}, {})
    assert kind != "agent"


async def test_agent_only_work_falls_to_a_queue_never_to_nobody(session, tenant):
    from case_service.core.assignment_router import resolve_assignment

    kind, who = await resolve_assignment(
        session, {"strategy": "workforce", "assignable_to": "agent",
                  "skill_requirements": ["nobody-has-this"],
                  "target": "ops-queue"}, {})
    assert (kind, who) == ("queue", "ops-queue")


async def test_an_unknown_assignable_to_routes_to_humans(session, tenant):
    """Fail towards the party that can ask questions."""
    from case_service.core.assignment_router import resolve_assignment

    agent = await _agent(session, tenant)
    await _enrol(session, agent, skills=["claims"])
    kind, _ = await resolve_assignment(
        session, {"strategy": "workforce", "assignable_to": "robots",
                  "skill_requirements": ["claims"], "target": "ops-queue"}, {})
    assert kind != "agent"


# ══ the API ═════════════════════════════════════════════════════════════════


def _headers(roles: list[str]) -> dict[str, str]:
    from velaris_shared.auth.jwt_handler import create_dev_token
    from velaris_shared.config import get_settings

    s = get_settings()
    token = create_dev_token(
        user_id=str(uuid.uuid4()), username="wf-user", roles=roles,
        secret=s.auth_secret, private_key=s.auth_rsa_private_key or "")
    return {"Authorization": f"Bearer {token}"}


async def test_the_roster_route_is_not_shadowed_by_the_agent_id_route(
        client, tenant):
    """`/agents/{agent_id}` is declared with a UUID parameter and FastAPI
    matches in order — without its own router this would 422 on the parse."""
    r = await client.get(f"{PREFIX}/workforce", headers=_headers(["admin"]))
    assert r.status_code == 200, r.text
    assert r.json()["roster"] == []


async def test_the_workforce_routes_refuse_an_anonymous_caller(anon_client, tenant):
    aid = str(uuid.uuid4())
    for method, path in (
        ("GET", "/workforce"), ("PUT", f"/{aid}/workforce"),
        ("POST", f"/{aid}/workforce/bench"),
        ("POST", f"/{aid}/workforce/scorecard"),
        ("GET", "/workforce/scorecards"),
    ):
        r = await anon_client.request(method, f"{PREFIX}{path}", json={})
        assert r.status_code in (401, 403), f"{method} {path} -> {r.status_code}"


async def test_a_manager_can_read_the_roster_but_not_enrol(client, session, tenant):
    agent = await _agent(session, tenant)
    h = _headers(["manager"])
    assert (await client.get(f"{PREFIX}/workforce", headers=h)).status_code == 200
    r = await client.put(f"{PREFIX}/{agent.id}/workforce", headers=h,
                         json={"escalation_queue": "ops"})
    assert r.status_code == 403


async def test_enrolling_through_the_api(client, session, tenant):
    agent = await _agent(session, tenant)
    r = await client.put(f"{PREFIX}/{agent.id}/workforce", headers=_headers(["admin"]),
                         json={"escalation_queue": "ops-queue",
                               "skills": ["Claims"], "max_concurrent": 3})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["skills"] == ["claims"]
    assert body["availability"]["available"] is True


async def test_benching_through_the_api_needs_a_reason(client, session, tenant):
    agent = await _agent(session, tenant)
    await _enrol(session, agent)
    r = await client.post(f"{PREFIX}/{agent.id}/workforce/bench",
                          headers=_headers(["admin"]),
                          json={"benched": True, "reason": "  "})
    assert r.status_code == 422


async def test_unknown_fields_are_refused(client, session, tenant):
    agent = await _agent(session, tenant)
    r = await client.put(f"{PREFIX}/{agent.id}/workforce", headers=_headers(["admin"]),
                         json={"escalation_queue": "ops", "skils": []})
    assert r.status_code == 422


# ══ the Studio view's reads ═════════════════════════════════════════════════


async def test_the_roster_names_the_agent_not_just_its_uuid(client, session, tenant):
    """A page of UUIDs cannot be read by the person deciding who to bench."""
    agent = await _agent(session, tenant, slug="claims-bot")
    await _enrol(session, agent)
    r = await client.get(f"{PREFIX}/workforce", headers=_headers(["admin"]))
    assert r.status_code == 200, r.text
    row = r.json()["roster"][0]
    assert row["agent"]["slug"] == "claims-bot"
    assert row["agent"]["status"] == "active"


async def test_the_roster_says_whether_nightly_reviews_are_running(client, tenant):
    """An operator whose scorecards stopped updating should learn it here,
    rather than deducing it from dates."""
    r = await client.get(f"{PREFIX}/workforce", headers=_headers(["admin"]))
    assert r.json()["scorecard_cron"]["enabled"] is False


async def test_pending_proposals_are_findable(client, session, tenant):
    """A proposal changes nothing until approved, so an unfindable one is
    invisible — the failure mode of every approval queue nobody built."""
    agent = await _agent(session, tenant)
    await _enrol(session, agent)
    card = await workforce.scorecard(session, agent=agent)
    await workforce.propose_weight(session, card, weight=1.5, reason="steady")
    other = await workforce.scorecard(session, agent=agent)
    await session.commit()

    h = _headers(["manager"])
    r = await client.get(f"{PREFIX}/workforce/scorecards?status=pending", headers=h)
    assert r.status_code == 200, r.text
    ids = [c["id"] for c in r.json()["scorecards"]]
    assert ids == [str(card.id)]
    assert str(other.id) not in ids

    allc = await client.get(f"{PREFIX}/workforce/scorecards", headers=h)
    assert len(allc.json()["scorecards"]) == 2


async def test_a_scorecard_carries_the_evidence_its_proposal_rests_on(
        client, session, tenant):
    agent = await _agent(session, tenant)
    card = await workforce.scorecard(session, agent=agent)
    await workforce.propose_weight(session, card, weight=0.5, reason="too costly")
    await session.commit()
    r = await client.get(f"{PREFIX}/workforce/scorecards", headers=_headers(["admin"]))
    body = r.json()["scorecards"][0]
    assert body["proposed_weight"] == 0.5
    assert body["proposal_reason"] == "too costly"
    assert body["decisions"] == 0
    assert body["override_rate"] is None


async def test_an_unknown_scorecard_status_is_refused(client, tenant):
    r = await client.get(f"{PREFIX}/workforce/scorecards?status=maybe",
                         headers=_headers(["admin"]))
    assert r.status_code == 422


# ══ the scheduled sweep ═════════════════════════════════════════════════════


async def test_the_sweep_writes_a_card_for_each_rostered_agent(session, tenant):
    a = await _agent(session, tenant, slug="a")
    b = await _agent(session, tenant, slug="b")
    await _enrol(session, a)
    await _enrol(session, b)

    result = await workforce.scorecard_sweep(
        session, window_days=7, min_interval_hours=24)
    assert result == {"written": 2, "skipped": 0, "failed": 0}
    cards = (await session.execute(select(AgentScorecardModel))).scalars().all()
    assert {c.agent_id for c in cards} == {a.id, b.id}


async def test_the_sweep_does_not_recompute_a_fresh_card(session, tenant):
    """A service restarted six times in an afternoon must not leave six
    identical reviews of the same week."""
    agent = await _agent(session, tenant)
    await _enrol(session, agent)

    first = await workforce.scorecard_sweep(
        session, window_days=7, min_interval_hours=24)
    second = await workforce.scorecard_sweep(
        session, window_days=7, min_interval_hours=24)
    assert first["written"] == 1
    assert second == {"written": 0, "skipped": 1, "failed": 0}
    assert len((await session.execute(
        select(AgentScorecardModel))).scalars().all()) == 1


async def test_the_sweep_recomputes_once_the_interval_has_passed(session, tenant):
    agent = await _agent(session, tenant)
    await _enrol(session, agent)
    await workforce.scorecard_sweep(session, window_days=7, min_interval_hours=24)

    card = (await session.execute(select(AgentScorecardModel))).scalars().one()
    card.created_at = datetime.now(timezone.utc) - timedelta(hours=30)
    await session.commit()

    again = await workforce.scorecard_sweep(
        session, window_days=7, min_interval_hours=24)
    assert again["written"] == 1


async def test_the_sweep_sweeps_a_benched_agent_too(session, tenant):
    """Its scorecard is exactly what the supervisor deciding whether to unbench
    it wants to read."""
    agent = await _agent(session, tenant)
    row = await _enrol(session, agent)
    await workforce.bench(session, row, reason="paused by ops")
    await session.commit()

    assert (await workforce.scorecard_sweep(
        session, window_days=7, min_interval_hours=24))["written"] == 1


async def test_the_sweep_survives_a_roster_row_with_no_agent(session, tenant):
    """One bad row must not throw away the cards computed for the others.

    NOTE: this row can only exist here because the SQLite harness enforces no
    foreign keys — on a real dialect the cascade removes it with the agent. The
    guard is kept for the same reason it is tested: a sweep that raises on a
    surprise stops reviewing every agent behind it.
    """
    from velaris_shared.db.models import AgentModel

    good = await _agent(session, tenant, slug="good")
    orphan = await _agent(session, tenant, slug="orphan")
    await _enrol(session, orphan)
    await _enrol(session, good)
    await session.delete(await session.get(AgentModel, orphan.id))
    await session.commit()

    result = await workforce.scorecard_sweep(
        session, window_days=7, min_interval_hours=24)
    assert result["written"] == 1
    assert result["skipped"] == 1
    assert result["failed"] == 0


async def test_the_sweep_is_off_until_an_operator_turns_it_on(tenant):
    """Default off, and it returns rather than looping — an update does not get
    to start a background writer on somebody's box."""
    from velaris_shared.config import get_settings

    assert get_settings().agent_scorecard_cron_enabled is False
    await workforce.scorecard_cron()      # returns immediately, never sleeps
