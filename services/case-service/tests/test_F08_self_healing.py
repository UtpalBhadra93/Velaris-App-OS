"""F-08 (first slice) — self-healing processes.

The most important test here is a unit-level one: **a healing change cannot be
constructed that touches a forbidden surface.** The roadmap's claim is
"structurally impossible, not policy-checked", and a claim like that is only
worth having if a test recomputes it — so `test_the_catalog_cannot_reach_a_
forbidden_surface` intersects what the operator catalog can write with what it
must never write, and asserts the result is empty.

After that the tests are about the loop refusing to close itself:

* healing is **off** until an operator switches it on AND a tenant opts in, and
  the refusal says WHICH switch is off;
* the ladder **stops at the first refusal** rather than running the expensive
  rungs anyway;
* an insight HxEvolve never surfaced has no replay evidence and cannot become
  an episode a person is asked about;
* **nothing applies without an approval**, and nothing applies without a
  pre-signed rollback;
* a change is not **healed** because it deployed — the watch window decides, and
  it knows that a falling conformance rate is a regression while a falling cycle
  time is not;
* a rolled-back change opens an **incident**, because a process that healed
  itself and got worse is what somebody needs to be told about.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio

from velaris_shared.db.models import (
    CaseTypeModel,
    HealingEpisodeModel,
    HxEvolveInsightModel,
    RuleDefinitionModel,
    TenantModel,
)

from case_service.healing import catalog, ladder, service

pytestmark = pytest.mark.asyncio

DEFINITION = {
    "stages": [
        {"id": "intake", "order": 1, "steps": [
            {"id": "s1", "required": True, "step_type": "manual"}]},
        {"id": "review", "order": 2, "steps": [
            {"id": "s2", "required": True, "step_type": "manual"}]},
    ]
}


@pytest_asyncio.fixture
async def tenant(session, monkeypatch):
    from case_service.tenancy import identity as _identity

    _identity.clear_cache()
    monkeypatch.setenv("VELARIS_CASE_HEALING_ENABLED", "true")
    t = TenantModel(id=uuid.uuid4(), slug="default", name="Default",
                    settings={"healing": {"enabled": True, "require_twin": False}})
    session.add(t)
    await session.commit()
    yield t
    _identity.clear_cache()


async def _case_type(session, tenant):
    ct = CaseTypeModel(id=uuid.uuid4(), name="Claims", version="1.0.0",
                       lifecycle_process_id="p", definition_json=DEFINITION,
                       tenant_id=str(tenant.id))
    session.add(ct)
    await session.commit()
    return ct


#: A REAL reorder proposal, in the shape `hxevolve.proposer._build_reorder`
#: produces: the whole proposed definition under `definition_json`, with only
#: the stage ORDER changed. Anything else and the reorder gate rejects it —
#: which is the gate working, and is why this fixture mirrors the builder rather
#: than inventing a convenient shape.
REORDERED = {
    **DEFINITION,
    "stages": [
        {**DEFINITION["stages"][1], "order": 1},
        {**DEFINITION["stages"][0], "order": 2},
    ],
}


async def _insight(session, tenant, ct, *, kind="reorder", status="surfaced",
                   proposal=None):
    i = HxEvolveInsightModel(
        id=uuid.uuid4(), tenant_id=str(tenant.id), case_type_id=ct.id,
        signal={"kind": "slow_stage", "stage_id": "review"},
        proposal=proposal if proposal is not None else {
            "definition_json": REORDERED},
        proposal_kind=kind, rationale="review is the bottleneck",
        status=status, evidence_kind="replay", evidence={"improvement": 0.2})
    session.add(i)
    await session.commit()
    return i


# ══ the boundary — the claim the roadmap makes ══════════════════════════════


def test_the_catalog_cannot_reach_a_forbidden_surface():
    """"Structurally impossible, not policy-checked" — recomputed, not asserted
    in prose. A failing run names which surface leaked."""
    assert catalog.proves_forbidden_surfaces_unreachable() == set()


def test_everything_the_catalog_reaches_is_process_shape():
    """Two artifact types, and neither is auth, policy, egress or a credential."""
    assert catalog.artifact_types() == {"case_type", "rule"}


def test_the_operator_vocabulary_is_hxevolves_own():
    """Restating the kinds here would let the two drift, and a sixth kind added
    to the miner would silently widen this boundary."""
    from case_service.hxevolve.proposer import PROPOSAL_KINDS

    assert catalog.OPERATORS == tuple(PROPOSAL_KINDS)


def test_an_operator_outside_the_catalog_is_refused():
    with pytest.raises(catalog.CatalogError, match="not one of"):
        catalog.check("change_sso_provider", {"anything": True})


def test_widening_the_catalog_without_widening_the_tests_stops_everything(monkeypatch):
    """Belt and braces: if somebody adds a forbidden reach, every episode
    refuses rather than the boundary quietly moving."""
    monkeypatch.setitem(catalog.WRITES, "rule_add", {"connector": ("config",)})
    with pytest.raises(catalog.CatalogError, match="forbidden surface"):
        catalog.check("rule_add", {"name": "x"})


# ══ the two switches ════════════════════════════════════════════════════════


async def test_healing_is_off_until_the_operator_switches_it_on(
        session, tenant, monkeypatch):
    monkeypatch.delenv("VELARIS_CASE_HEALING_ENABLED", raising=False)
    allowed, why = await service.enabled_for(session, tenant.id)
    assert allowed is False and "install" in why


async def test_healing_is_off_until_the_tenant_opts_in(session, tenant):
    """And the refusal says WHICH switch — the two send you to different
    screens."""
    tenant.settings = {"healing": {"enabled": False}}
    await session.commit()
    allowed, why = await service.enabled_for(session, tenant.id)
    assert allowed is False and "tenant" in why


async def test_defaults_are_conservative(session):
    cfg = service.tenant_config(None)
    assert cfg["enabled"] is False
    assert cfg["require_twin"] is True


# ══ the ladder ══════════════════════════════════════════════════════════════


async def test_an_unsurfaced_insight_has_no_replay_evidence(session, tenant):
    ct = await _case_type(session, tenant)
    i = await _insight(session, tenant, ct, status="discarded_gate")
    e = await service.open_episode(session, tenant_uuid=tenant.id, insight_id=i.id)
    assert e.status == "refused" and e.stopped_at == "replay"


async def test_the_ladder_stops_at_the_first_refusal(session, tenant):
    """A change that cannot be expressed should not then be replayed across the
    whole caseload to learn whether it would also have been slow."""
    ct = await _case_type(session, tenant)
    i = await _insight(session, tenant, ct, kind="not_an_operator")
    e = await service.open_episode(session, tenant_uuid=tenant.id, insight_id=i.id)
    assert e.stopped_at == "catalog"
    assert [r["rung"] for r in e.ladder] == ["catalog"]


async def test_a_passing_ladder_records_every_rung_it_ran(session, tenant):
    """A reviewer approving a change to their own live process is entitled to
    see what was checked, not a single green tick."""
    ct = await _case_type(session, tenant)
    i = await _insight(session, tenant, ct)
    e = await service.open_episode(session, tenant_uuid=tenant.id, insight_id=i.id)
    assert e.status == "ready", e.stopped_reason
    assert [r["rung"] for r in e.ladder] == list(ladder.RUNGS)


async def test_the_f03_rung_is_recorded_as_not_applicable_not_skipped(session, tenant):
    """An F-03 counterfactual proves a POLICY change, and the catalog cannot
    name policy. An auditor still sees five rungs."""
    ct = await _case_type(session, tenant)
    i = await _insight(session, tenant, ct)
    e = await service.open_episode(session, tenant_uuid=tenant.id, insight_id=i.id)
    proof = next(r for r in e.ladder if r["rung"] == "proof")
    assert proof["ok"] is True and proof["applicable"] is False


async def test_requiring_a_twin_refuses_when_there_is_none(session, tenant):
    tenant.settings = {"healing": {"enabled": True, "require_twin": True}}
    await session.commit()
    ct = await _case_type(session, tenant)
    i = await _insight(session, tenant, ct)
    e = await service.open_episode(session, tenant_uuid=tenant.id, insight_id=i.id)
    assert e.status == "refused" and e.stopped_at == "twin"
    assert "Nothing is wrong with the proposal" in e.stopped_reason


async def test_a_refused_episode_is_still_written(session, tenant):
    """"The platform wanted to change this and was not allowed to" is exactly
    what somebody reviewing an autonomous system needs to read."""
    ct = await _case_type(session, tenant)
    i = await _insight(session, tenant, ct, kind="not_an_operator")
    e = await service.open_episode(session, tenant_uuid=tenant.id, insight_id=i.id)
    await session.commit()
    assert await session.get(HealingEpisodeModel, e.id) is not None


async def test_two_open_episodes_for_one_insight_are_refused(session, tenant):
    """They would race to change the same artifact, and the second would
    overwrite the first's pre-signed rollback."""
    ct = await _case_type(session, tenant)
    i = await _insight(session, tenant, ct)
    await service.open_episode(session, tenant_uuid=tenant.id, insight_id=i.id)
    await session.commit()
    with pytest.raises(service.HealingError, match="already has an open episode"):
        await service.open_episode(session, tenant_uuid=tenant.id, insight_id=i.id)


# ══ approving, applying, undoing ════════════════════════════════════════════


async def test_nothing_applies_without_an_approval(session, tenant):
    ct = await _case_type(session, tenant)
    i = await _insight(session, tenant, ct)
    e = await service.open_episode(session, tenant_uuid=tenant.id, insight_id=i.id)
    with pytest.raises(service.HealingError, match="without an approval"):
        await service.apply(session, episode=e)


async def test_nothing_applies_without_a_pre_signed_rollback(session, tenant):
    ct = await _case_type(session, tenant)
    i = await _insight(session, tenant, ct)
    e = await service.open_episode(session, tenant_uuid=tenant.id, insight_id=i.id)
    e.approved_by = "alice"
    e.rollback_patch = None
    with pytest.raises(service.HealingError, match="no pre-signed rollback"):
        await service.apply(session, episode=e)


async def test_approving_signs_the_rollback_from_current_state(session, tenant):
    """What gets written back later is exactly what the approver was looking
    at — not the proposal, and not a snapshot taken afterwards."""
    ct = await _case_type(session, tenant)
    i = await _insight(session, tenant, ct)
    e = await service.open_episode(session, tenant_uuid=tenant.id, insight_id=i.id)
    await service.approve(session, episode=e, actor="alice", reason="looks right")
    assert e.rollback_artifact_type == "case_type"
    assert e.rollback_patch["definition_json"] == DEFINITION


async def test_apply_changes_the_case_type_and_starts_a_watch(session, tenant):
    ct = await _case_type(session, tenant)
    i = await _insight(session, tenant, ct)
    e = await service.open_episode(session, tenant_uuid=tenant.id, insight_id=i.id)
    await service.approve(session, episode=e, actor="alice")
    await service.apply(session, episode=e, watch_hours=6)
    await session.commit()
    await session.refresh(ct)
    assert ct.definition_json["stages"] == REORDERED["stages"]
    assert e.status == "watching" and e.watch_until is not None


async def test_a_rejection_needs_a_reason_somebody_can_read(session, tenant):
    ct = await _case_type(session, tenant)
    i = await _insight(session, tenant, ct)
    e = await service.open_episode(session, tenant_uuid=tenant.id, insight_id=i.id)
    with pytest.raises(service.HealingError, match="reason"):
        await service.reject(session, episode=e, actor="alice", reason="no")


async def test_rollback_restores_exactly_what_was_signed(session, tenant):
    ct = await _case_type(session, tenant)
    i = await _insight(session, tenant, ct)
    e = await service.open_episode(session, tenant_uuid=tenant.id, insight_id=i.id)
    await service.approve(session, episode=e, actor="alice")
    await service.apply(session, episode=e)
    await session.commit()
    await service.rollback(session, episode=e, reason="changed my mind")
    await session.commit()
    await session.refresh(ct)
    assert ct.definition_json == DEFINITION
    assert e.status == "rolled_back"


# ══ the watch window ════════════════════════════════════════════════════════


def test_a_rising_cycle_time_is_a_regression():
    bad, why = service._regressed({"cases": 50, "avg_duration_hours": 10},
                                  {"cases": 50, "avg_duration_hours": 13}, 10)
    assert bad and "rose" in why


def test_a_FALLING_conformance_rate_is_also_a_regression():
    """The one that a one-directional check gets wrong. Higher is better for
    conformance, so a collapse shows up as a negative delta and would otherwise
    be read as an improvement."""
    bad, why = service._regressed({"cases": 50, "conformance_rate": 90},
                                  {"cases": 50, "conformance_rate": 70}, 10)
    assert bad and "fell" in why


def test_an_improving_conformance_rate_is_not_a_regression():
    bad, _ = service._regressed({"cases": 50, "conformance_rate": 70},
                                {"cases": 50, "conformance_rate": 90}, 10)
    assert bad is False


def test_a_thin_window_concludes_nothing():
    """A change is not undone on the strength of three cases."""
    bad, why = service._regressed({"cases": 2, "avg_duration_hours": 10},
                                  {"cases": 2, "avg_duration_hours": 99}, 10)
    assert bad is False and "too few cases" in why


def test_a_metric_missing_on_one_side_is_not_a_regression():
    bad, _ = service._regressed({"cases": 50, "avg_duration_hours": 10},
                                {"cases": 50}, 10)
    assert bad is False


async def test_a_clean_window_makes_it_healed(session, tenant, monkeypatch):
    ct = await _case_type(session, tenant)
    i = await _insight(session, tenant, ct)
    e = await service.open_episode(session, tenant_uuid=tenant.id, insight_id=i.id)
    await service.approve(session, episode=e, actor="alice")

    async def _metrics(*_a, **_k):
        return {"cases": 50, "avg_duration_hours": 10, "conformance_rate": 90}

    monkeypatch.setattr(service, "_metric_snapshot", _metrics)
    await service.apply(session, episode=e)
    await service.close_watch(session, episode=e)
    assert e.status == "healed"


async def test_a_regression_rolls_back_and_opens_an_incident(session, tenant, monkeypatch):
    """A process that healed itself and got worse is what somebody needs to be
    told about, and a status change on a row nobody opens is not telling
    anybody."""
    ct = await _case_type(session, tenant)
    i = await _insight(session, tenant, ct)
    e = await service.open_episode(session, tenant_uuid=tenant.id, insight_id=i.id)
    await service.approve(session, episode=e, actor="alice")

    snapshots = iter([
        {"cases": 50, "avg_duration_hours": 10, "conformance_rate": 90},
        {"cases": 50, "avg_duration_hours": 30, "conformance_rate": 90},
    ])

    async def _metrics(*_a, **_k):
        return next(snapshots)

    monkeypatch.setattr(service, "_metric_snapshot", _metrics)
    await service.apply(session, episode=e)
    await service.close_watch(session, episode=e)
    await session.commit()
    await session.refresh(ct)

    assert e.status == "rolled_back"
    assert e.incident_case_id is not None
    assert ct.definition_json == DEFINITION


async def test_only_due_windows_are_swept(session, tenant, monkeypatch):
    ct = await _case_type(session, tenant)
    i = await _insight(session, tenant, ct)
    e = await service.open_episode(session, tenant_uuid=tenant.id, insight_id=i.id)
    await service.approve(session, episode=e, actor="alice")

    async def _metrics(*_a, **_k):
        return {"cases": 50, "avg_duration_hours": 10}

    monkeypatch.setattr(service, "_metric_snapshot", _metrics)
    await service.apply(session, episode=e, watch_hours=48)
    await session.commit()
    assert await service.due_watches(session) == []
    later = datetime.now(timezone.utc) + timedelta(hours=49)
    assert [d.id for d in await service.due_watches(session, now=later)] == [e.id]


# ══ the chain ═══════════════════════════════════════════════════════════════


async def test_episodes_chain_and_verify(session, tenant):
    ct = await _case_type(session, tenant)
    for _ in range(3):
        i = await _insight(session, tenant, ct, kind="not_an_operator")
        await service.open_episode(session, tenant_uuid=tenant.id, insight_id=i.id)
    await session.commit()
    out = await service.verify_chain(session)
    assert out["verified"] is True and out["episodes"] == 3, out


async def test_an_edited_episode_breaks_the_chain(session, tenant):
    """The record of the platform changing itself must not be quietly
    rewritable."""
    ct = await _case_type(session, tenant)
    for _ in range(3):
        i = await _insight(session, tenant, ct, kind="not_an_operator")
        await service.open_episode(session, tenant_uuid=tenant.id, insight_id=i.id)
    await session.commit()

    rows = (await session.execute(
        __import__("sqlalchemy").select(HealingEpisodeModel)
        .order_by(HealingEpisodeModel.sequence))).scalars().all()
    rows[1].proposal = {"stages": [{"id": "tampered"}]}
    await session.commit()

    out = await service.verify_chain(session)
    assert out["verified"] is False and out["broken_at"] == 2


async def test_the_outcome_is_outside_the_hash(session, tenant, monkeypatch):
    """An address that moved every time the watch outcome arrived would chain
    nothing."""
    ct = await _case_type(session, tenant)
    i = await _insight(session, tenant, ct)
    e = await service.open_episode(session, tenant_uuid=tenant.id, insight_id=i.id)
    before = e.content_hash
    await service.approve(session, episode=e, actor="alice")

    async def _metrics(*_a, **_k):
        return {"cases": 50, "avg_duration_hours": 10}

    monkeypatch.setattr(service, "_metric_snapshot", _metrics)
    await service.apply(session, episode=e)
    await service.close_watch(session, episode=e)
    await session.commit()
    assert e.status == "healed"
    assert (await service.verify_chain(session))["verified"] is True
    assert e.content_hash == before
