"""F-04 (first slice) — the simulation gate on promotion.

The single most important test in this file is the first one: **an install that
has not configured a gate must approve deployments exactly as it did before.**
Everything else is a feature; that one is the upgrade.

After that, the tests are about the gate refusing to be satisfied cheaply:

* a rehearsal that could not be run is **refused**, never passed;
* a cohort too small to judge is refused rather than waved through;
* a passing simulation of a **different package** does not clear this one;
* an override needs a real reason, and is recorded permanently.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest
import pytest_asyncio
from sqlalchemy import select

from velaris_shared.db.models import (
    ArbiterNamespaceModel,
    DeploymentRunModel,
    SimulationGateModel,
    SimulationRunModel,
    TenantModel,
)

from case_service.hxdeploy import simulation as sim

pytestmark = pytest.mark.asyncio

SEED = [("ai", "deny"), ("egress", "deny"), ("export", "deny"),
        ("vault", "deny"), ("case", "allow"), ("auth", "allow"),
        ("agent", "deny")]


@pytest_asyncio.fixture(autouse=True)
async def tenant(session):
    from case_service.agents import identity as agent_identity
    from case_service.tenancy import identity as _identity

    _identity.clear_cache()
    agent_identity._SLUG_CACHE.clear()
    for ns, eff in SEED:
        session.add(ArbiterNamespaceModel(
            namespace=ns, default_effect=eff, max_mode="shadow", description="",
            sample_allow_rate=1, sample_default_rate=1,
            updated_at=datetime.now(timezone.utc)))
    t = TenantModel(id=uuid.uuid4(), slug="default", name="Default")
    session.add(t)
    await session.commit()
    yield t
    agent_identity._SLUG_CACHE.clear()
    _identity.clear_cache()


async def _run(session, *, checksum="abc123", initiated_by="alice"):
    run = DeploymentRunModel(
        id=uuid.uuid4(), tenant_id="default", to_env_id=None,
        package_checksum=checksum, risk_level="low", risk_summary={},
        status="awaiting_approval", initiated_by=initiated_by)
    session.add(run)
    await session.commit()
    return run


def _summary(total=100, divergent=2, cost_delta=None):
    out = {"total": total, "determinate": total, "diverged": divergent}
    if cost_delta is not None:
        out["cost"] = {"delta_pct": cost_delta}
    return out


# ══ the upgrade ═════════════════════════════════════════════════════════════


async def test_with_no_gate_configured_approval_is_untouched(session, tenant):
    """THE upgrade-safety test. No `simulation_gates` row means not enforced,
    and that is what every install taking this update has."""
    run = await _run(session)
    await sim.assert_gate_clear(session, run, tenant_uuid=tenant.id)   # no raise


async def test_a_disabled_gate_blocks_nothing(session, tenant):
    """A row that exists but is off behaves exactly like no row."""
    await sim.upsert_gate(session, tenant_id=tenant.id, enabled=False,
                          max_divergent_pct=0)
    await session.commit()
    run = await _run(session)
    await sim.assert_gate_clear(session, run, tenant_uuid=tenant.id)


async def test_an_enabled_gate_blocks_an_unsimulated_run(session, tenant):
    await sim.upsert_gate(session, tenant_id=tenant.id, enabled=True)
    await session.commit()
    run = await _run(session)
    with pytest.raises(PermissionError) as exc:
        await sim.assert_gate_clear(session, run, tenant_uuid=tenant.id)
    assert "not been simulated" in str(exc.value)


# ══ judging a rehearsal ═════════════════════════════════════════════════════


async def test_a_clean_rehearsal_passes(session, tenant):
    await sim.upsert_gate(session, tenant_id=tenant.id, enabled=True,
                          max_divergent_pct=5)
    await session.commit()
    run = await _run(session)

    result = await sim.simulate(
        session, run=run, tenant_uuid=tenant.id, tenant_slug="default",
        replay_summary=_summary(total=100, divergent=2))
    await session.commit()
    assert result.status == "passed"
    assert result.findings["divergent_pct"] == 2.0
    await sim.assert_gate_clear(session, run, tenant_uuid=tenant.id)


async def test_a_bad_change_is_blocked(session, tenant):
    """The design's own exit criterion: a change that breaches a threshold does
    not get approved."""
    await sim.upsert_gate(session, tenant_id=tenant.id, enabled=True,
                          max_divergent_pct=5)
    await session.commit()
    run = await _run(session)

    result = await sim.simulate(
        session, run=run, tenant_uuid=tenant.id, tenant_slug="default",
        replay_summary=_summary(total=100, divergent=40))
    await session.commit()
    assert result.status == "failed"
    assert "40.0% of replayed cases diverge" in result.findings["breaches"][0]

    with pytest.raises(PermissionError) as exc:
        await sim.assert_gate_clear(session, run, tenant_uuid=tenant.id)
    assert "simulation failed" in str(exc.value)


async def test_a_cost_rise_is_a_breach(session, tenant):
    await sim.upsert_gate(session, tenant_id=tenant.id, enabled=True,
                          max_cost_increase_pct=10)
    await session.commit()
    run = await _run(session)
    result = await sim.simulate(
        session, run=run, tenant_uuid=tenant.id, tenant_slug="default",
        replay_summary=_summary(cost_delta=25.0))
    await session.commit()
    assert result.status == "failed"
    assert "cost rises 25.0%" in result.findings["breaches"][0]


async def test_no_rehearsal_is_refused_not_passed(session, tenant):
    """A gate that passed on an absent rehearsal would be worse than no gate."""
    await sim.upsert_gate(session, tenant_id=tenant.id, enabled=True)
    await session.commit()
    run = await _run(session)
    result = await sim.simulate(
        session, run=run, tenant_uuid=tenant.id, tenant_slug="default",
        replay_summary=None)
    await session.commit()
    assert result.status == "refused"
    assert result.findings["refused"] == "no_replay"

    with pytest.raises(PermissionError):
        await sim.assert_gate_clear(session, run, tenant_uuid=tenant.id)


async def test_a_cohort_too_small_to_judge_is_refused(session, tenant):
    """Passing a change because only two cases were available to rehearse it is
    the failure a minimum exists to prevent."""
    await sim.upsert_gate(session, tenant_id=tenant.id, enabled=True,
                          min_cohort=50, max_divergent_pct=5)
    await session.commit()
    run = await _run(session)
    result = await sim.simulate(
        session, run=run, tenant_uuid=tenant.id, tenant_slug="default",
        replay_summary=_summary(total=2, divergent=0))
    await session.commit()
    assert result.status == "refused"
    assert result.findings["refused"] == "cohort_too_small"


async def test_a_missing_proof_breaches_when_required(session, tenant):
    await sim.upsert_gate(session, tenant_id=tenant.id, enabled=True,
                          require_proof=True)
    await session.commit()
    run = await _run(session)
    result = await sim.simulate(
        session, run=run, tenant_uuid=tenant.id, tenant_slug="default",
        replay_summary=_summary(), policy_touched=True, proof_id=None)
    await session.commit()
    assert result.status == "failed"
    assert "compliance proof" in result.findings["breaches"][0]


async def test_thresholds_are_frozen_onto_the_simulation(session, tenant):
    """Reading them live at review time would let a later edit change what a
    past simulation appears to have been judged against."""
    await sim.upsert_gate(session, tenant_id=tenant.id, enabled=True,
                          max_divergent_pct=5)
    await session.commit()
    run = await _run(session)
    result = await sim.simulate(
        session, run=run, tenant_uuid=tenant.id, tenant_slug="default",
        replay_summary=_summary(divergent=2))
    await session.commit()
    assert result.thresholds["max_divergent_pct"] == 5.0

    await sim.upsert_gate(session, tenant_id=tenant.id, enabled=True,
                          max_divergent_pct=99)
    await session.commit()
    await session.refresh(result)
    assert result.thresholds["max_divergent_pct"] == 5.0


async def test_the_rehearsal_states_its_own_coverage(session, tenant):
    """A reader who assumes a passing gate is a capacity guarantee has been
    misled, so the run says what it did and did not cover."""
    run = await _run(session)
    result = await sim.simulate(
        session, run=run, tenant_uuid=tenant.id, tenant_slug="default",
        replay_summary=_summary())
    await session.commit()
    assert result.findings["synthetic_load"] is False
    assert "no synthetic load" in result.findings["coverage"]


async def test_a_simulation_of_another_package_does_not_clear_this_one(
        session, tenant):
    """Otherwise the gate is satisfied by rehearsing something harmless and
    then approving something else."""
    await sim.upsert_gate(session, tenant_id=tenant.id, enabled=True,
                          max_divergent_pct=50)
    await session.commit()
    run = await _run(session, checksum="package-A")
    await sim.simulate(session, run=run, tenant_uuid=tenant.id,
                       tenant_slug="default", replay_summary=_summary())
    await session.commit()
    await sim.assert_gate_clear(session, run, tenant_uuid=tenant.id)   # fine

    run.package_checksum = "package-B"
    await session.commit()
    with pytest.raises(PermissionError) as exc:
        await sim.assert_gate_clear(session, run, tenant_uuid=tenant.id)
    assert "different package" in str(exc.value)


async def test_resimulating_appends(session, tenant):
    """'It failed twice and then passed' is what a later reviewer needs."""
    await sim.upsert_gate(session, tenant_id=tenant.id, enabled=True,
                          max_divergent_pct=5)
    await session.commit()
    run = await _run(session)
    await sim.simulate(session, run=run, tenant_uuid=tenant.id,
                       tenant_slug="default", replay_summary=_summary(divergent=40))
    second = await sim.simulate(session, run=run, tenant_uuid=tenant.id,
                                tenant_slug="default", replay_summary=_summary(divergent=1))
    await session.commit()

    rows = (await session.execute(select(SimulationRunModel))).scalars().all()
    assert len(rows) == 2
    assert {r.status for r in rows} == {"failed", "passed"}
    assert run.simulation_id == second.id
    assert (await sim.latest(session, run.id)).id == second.id


# ══ the override ════════════════════════════════════════════════════════════


async def test_an_override_needs_a_real_reason(session, tenant):
    """This row is the only trace that a failed gate was ever bypassed."""
    run = await _run(session)
    with pytest.raises(sim.SimulationError):
        await sim.override_gate(session, run, actor_id="bob", reason="ok")


async def test_an_override_clears_the_gate_and_is_recorded(session, tenant):
    await sim.upsert_gate(session, tenant_id=tenant.id, enabled=True)
    await session.commit()
    run = await _run(session)
    with pytest.raises(PermissionError):
        await sim.assert_gate_clear(session, run, tenant_uuid=tenant.id)

    await sim.override_gate(session, run, actor_id="bob",
                            reason="hotfix for the incident at 03:00, signed off by ops")
    await session.commit()
    await sim.assert_gate_clear(session, run, tenant_uuid=tenant.id)
    assert run.gate_overridden_by == "bob"
    assert run.gate_overridden_at is not None


# ══ the twin's identity ═════════════════════════════════════════════════════


async def test_the_twin_agent_has_an_empty_ceiling(session, tenant):
    """'The twin can reach nothing' has to be a property of the registry, not a
    promise in a comment. An agent with no capabilities can never be issued a
    token either."""
    from case_service.agents import identity as agent_identity

    agent_id = await sim.twin_agent_id("default")
    assert agent_id is not None
    assert await agent_identity.live_capabilities(session, agent_id) == set()


async def test_the_twin_is_denied_egress_and_the_attempt_is_recorded(
        session, tenant):
    """Denied by construction rather than by policy: the caller passes
    `legacy_allowed=False`, so the twin stays blocked even if somebody promotes
    the `egress` namespace to permissive."""
    from velaris_shared.db.models import ArbiterDecisionModel

    agent_id = await sim.twin_agent_id("default")
    allowed = await sim.twin_may_egress(
        session, tenant_id="default", agent_id=agent_id, target="example.test")
    await session.commit()
    assert allowed is False

    row = (await session.execute(
        select(ArbiterDecisionModel)
        .where(ArbiterDecisionModel.action == "egress.http")
    )).scalars().first()
    assert row is not None, "the attempt was denied and never recorded"
    assert row.claimed_by == "hxdeploy.simulation"


# ══ the API ═════════════════════════════════════════════════════════════════


PREFIX = "/api/v1/deploy"


def _headers(roles: list[str]) -> dict[str, str]:
    from velaris_shared.auth.jwt_handler import create_dev_token
    from velaris_shared.config import get_settings

    s = get_settings()
    token = create_dev_token(
        user_id=str(uuid.uuid4()), username="deploy-user", roles=roles,
        secret=s.auth_secret, private_key=s.auth_rsa_private_key or "")
    return {"Authorization": f"Bearer {token}"}


async def test_the_new_routes_refuse_an_anonymous_caller(anon_client, tenant):
    rid = str(uuid.uuid4())
    for method, path in (
        ("GET", "/simulation-gate"), ("PUT", "/simulation-gate"),
        ("POST", f"/runs/{rid}/simulate"), ("GET", f"/runs/{rid}/simulation"),
        ("POST", f"/runs/{rid}/gate-override"),
    ):
        r = await anon_client.request(method, f"{PREFIX}{path}", json={})
        assert r.status_code in (401, 403), f"{method} {path} -> {r.status_code}"


async def test_the_gate_reads_unconfigured_by_default(client, tenant):
    r = await client.get(f"{PREFIX}/simulation-gate", headers=_headers(["admin"]))
    assert r.status_code == 200
    assert r.json() == {"configured": False, "enabled": False}


async def test_a_developer_cannot_configure_the_gate(client, tenant):
    r = await client.put(f"{PREFIX}/simulation-gate", headers=_headers(["developer"]),
                         json={"enabled": True})
    assert r.status_code == 403


async def test_a_tenant_admin_cannot_set_a_platform_gate(client, tenant):
    r = await client.put(f"{PREFIX}/simulation-gate", headers=_headers(["admin"]),
                         json={"enabled": True, "platform": True})
    assert r.status_code == 403
    assert "PLATFORM-SCOPE" in r.json()["detail"]


async def test_configuring_and_reading_the_gate(client, tenant):
    h = _headers(["admin"])
    r = await client.put(f"{PREFIX}/simulation-gate", headers=h,
                         json={"enabled": True, "max_divergent_pct": 5})
    assert r.status_code == 200, r.text
    r = await client.get(f"{PREFIX}/simulation-gate", headers=h)
    assert r.json()["enabled"] is True
    assert r.json()["max_divergent_pct"] == 5.0


async def test_unknown_fields_on_the_gate_are_refused(client, tenant):
    r = await client.put(f"{PREFIX}/simulation-gate", headers=_headers(["admin"]),
                         json={"enabled": True, "max_divergnet_pct": 5})
    assert r.status_code == 422
