"""F-11 (first slice) — intent-based process execution.

The roadmap's exit criteria: golden intents plan correctly, a fragment whose
declared capabilities understate its behaviour is caught by the conformance
gate, and plan-hash binding is verified end to end. All three are here.

The tests that matter most:

* **the model never emits a process** — everything outside the library's own
  vocabulary is dropped before the planner sees it, and a spec that survives as
  empty produces a refusal rather than a guess;
* a fragment that **understates** its capabilities cannot be verified, and so
  cannot be composed;
* the planner is **deterministic** — same library, same goal, same plan;
* runaway plans are **structurally impossible**: no fragment repeats, and the
  step budget is enforced during the search;
* a plan that changed after a person read it **cannot be confirmed**, and one
  that changed after confirmation **cannot be executed**.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import uuid

import pytest
import pytest_asyncio

from velaris_shared.db.models import IntentPlanModel, ProcessFragmentModel, TenantModel

from case_service.intent import fragments as frag
from case_service.intent import planner, service

pytestmark = pytest.mark.asyncio


def _fragment(slug, *, requires=(), provides=(), caps=("case.advance",),
              steps=(("s1", "automated"),), classes=()):
    return {
        "slug": slug, "display_name": slug.replace("-", " ").title(),
        "description": f"does {slug}",
        "requires": list(requires), "provides": list(provides),
        "data_classes": list(classes), "capabilities": list(caps),
        "stages": [{"id": slug, "name": slug, "steps": [
            {"id": sid, "name": sid, "step_type": stype} for sid, stype in steps]}],
    }


@pytest_asyncio.fixture
async def tenant(session):
    from case_service.tenancy import identity as _identity

    _identity.clear_cache()
    t = TenantModel(id=uuid.uuid4(), slug="default", name="Default")
    session.add(t)
    await session.commit()
    yield t
    _identity.clear_cache()


async def _publish(session, tenant, spec, *, verify=True):
    row = await service.publish_fragment(session, tenant_uuid=tenant.id,
                                         fragment=spec, created_by="alice")
    if verify:
        await service.verify_fragment(session, row=row, actor="alice")
    await session.commit()
    return row


async def _library(session, tenant):
    """A small library that composes: verify → decide → refund → notify."""
    await _publish(session, tenant, _fragment(
        "verify-identity", provides=["identity_verified"],
        caps=["case.advance"], steps=(("check", "automated"),)))
    await _publish(session, tenant, _fragment(
        "assess-claim", requires=["identity_verified"], provides=["claim_assessed"],
        caps=["case.advance", "case.write_variable"],
        steps=(("assess", "form"),)))
    await _publish(session, tenant, _fragment(
        "issue-refund", requires=["claim_assessed"], provides=["refund_issued"],
        caps=["case.advance", "payment.refund"], classes=["financial"],
        steps=(("refund", "refund"),)))
    await _publish(session, tenant, _fragment(
        "notify-customer", requires=["refund_issued"], provides=["customer_notified"],
        caps=["case.advance", "notify.customer"], classes=["personal"],
        steps=(("tell", "customer_notification"),)))


# ══ the conformance gate — the exit criterion ═══════════════════════════════


def test_understating_capabilities_is_caught():
    """The failure the gate exists for: a fragment declaring LESS than it does
    gets composed into plans approved on a smaller union than what runs."""
    result = frag.conformance({
        "capabilities": ["case.advance"],
        "stages": [{"id": "s", "steps": [
            {"id": "x", "step_type": "customer_notification"}]}]})
    assert result["passed"] is False
    assert result["understated"] == ["notify.customer"]


def test_overstating_capabilities_is_allowed():
    """Merely pessimistic — plans are judged against a larger union than they
    need, which is the safe direction."""
    result = frag.conformance({
        "capabilities": ["case.advance", "payment.refund"],
        "stages": [{"id": "s", "steps": [{"id": "x", "step_type": "automated"}]}]})
    assert result["passed"] is True and result["overstated"] == ["payment.refund"]


def test_a_capability_outside_the_vocabulary_is_refused():
    errors = frag.validate(_fragment("x", provides=["y"], caps=["shell.exec"]))
    assert any("closed vocabulary" in e for e in errors)


async def test_an_understating_fragment_cannot_be_verified(session, tenant):
    row = await _publish(session, tenant, _fragment(
        "sneaky", provides=["done"], caps=["case.advance"],
        steps=(("mail", "customer_notification"),)), verify=False)
    with pytest.raises(service.IntentError, match="notify.customer"):
        await service.verify_fragment(session, row=row, actor="alice")
    assert row.status == "draft"


async def test_an_unverified_fragment_cannot_be_composed(session, tenant):
    await _publish(session, tenant, _fragment("draft-one", provides=["goal"]),
                   verify=False)
    fragments = await service.library(session, tenant.id)
    with pytest.raises(planner.PlanError, match="no verified fragments"):
        planner.compose({"goals": ["goal"]}, fragments)


# ══ the model is contained ══════════════════════════════════════════════════


def test_everything_outside_the_vocabulary_is_dropped():
    """Whatever a prompt injection persuades the model to say, what survives is
    a subset of words the fragments themselves defined."""
    vocab = {"goals": ["refund_issued"], "facts": ["refund_issued", "identity_verified"]}
    spec = service.constrain(
        {"goals": ["refund_issued", "grant_admin", "rm -rf /"],
         "given": ["identity_verified", "is_superuser"]}, vocab)
    assert spec["goals"] == ["refund_issued"]
    assert spec["given"] == ["identity_verified"]
    assert set(spec["dropped"]) == {"grant_admin", "rm -rf /", "is_superuser"}


def test_a_spec_that_survives_as_empty_is_a_refusal_not_a_guess():
    spec = service.constrain({"goals": ["anything"], "given": []},
                             {"goals": [], "facts": []})
    assert spec["goals"] == []
    with pytest.raises(planner.PlanError, match="names no outcome"):
        planner.compose(spec, [])


async def test_the_model_is_called_correctly(monkeypatch):
    """Found live. `generate_json` returns `dict | None`, not a tuple —
    unpacking it as one binds the first KEY and produces a refusal that reads
    exactly like the containment working. A bug shaped like a refusal is the
    worst shape a bug can have here."""
    async def _fake(prompt, *a, **k):
        return {"goals": ["refund_issued"], "given": ["claim_assessed"]}

    monkeypatch.setattr("velaris_shared.llm.factory.generate_json", _fake)
    spec, source = await service.goal_spec_from_intent(
        "give the money back",
        {"goals": ["refund_issued"], "facts": ["refund_issued", "claim_assessed"]})
    assert source == "model"
    assert spec["goals"] == ["refund_issued"]
    assert spec["given"] == ["claim_assessed"]


async def test_a_model_that_returns_nothing_is_a_refusal(monkeypatch):
    async def _none(prompt, *a, **k):
        return None

    monkeypatch.setattr("velaris_shared.llm.factory.generate_json", _none)
    spec, _ = await service.goal_spec_from_intent("x", {"goals": [], "facts": []})
    assert spec["goals"] == [] and spec["dropped"]


def test_a_model_returning_nonsense_produces_an_empty_spec():
    spec = service.constrain("not an object", {"goals": ["g"], "facts": ["g"]})
    assert spec["goals"] == [] and spec["dropped"]


# ══ the planner ═════════════════════════════════════════════════════════════


async def test_a_golden_intent_plans_the_shortest_path(session, tenant):
    await _library(session, tenant)
    fragments = await service.library(session, tenant.id)
    out = planner.compose({"goals": ["customer_notified"], "given": []}, fragments)
    assert out["fragment_slugs"] == [
        "verify-identity", "assess-claim", "issue-refund", "notify-customer"]
    assert out["capabilities"] == [
        "case.advance", "case.write_variable", "notify.customer", "payment.refund"]
    assert out["data_classes"] == ["financial", "personal"]


async def test_a_given_fact_shortens_the_plan(session, tenant):
    await _library(session, tenant)
    fragments = await service.library(session, tenant.id)
    out = planner.compose({"goals": ["refund_issued"],
                           "given": ["claim_assessed"]}, fragments)
    assert out["fragment_slugs"] == ["issue-refund"]


async def test_the_planner_is_deterministic(session, tenant):
    """Same library, same goal, same plan. No ranking heuristic, no model."""
    await _library(session, tenant)
    fragments = await service.library(session, tenant.id)
    first = planner.compose({"goals": ["customer_notified"]}, fragments)
    second = planner.compose({"goals": ["customer_notified"]}, list(reversed(fragments)))
    assert first["plan_sha256"] == second["plan_sha256"]


async def test_an_unreachable_goal_is_refused_not_bridged(session, tenant):
    await _library(session, tenant)
    fragments = await service.library(session, tenant.id)
    with pytest.raises(planner.PlanError, match="will not invent"):
        planner.compose({"goals": ["case_closed_with_apology"]}, fragments)


async def test_a_plan_never_repeats_a_fragment(session, tenant):
    """Acyclic by construction rather than by a cycle check that could be
    wrong."""
    await _publish(session, tenant, _fragment("loop-a", requires=["b"], provides=["a"]))
    await _publish(session, tenant, _fragment("loop-b", requires=["a"], provides=["b"]))
    fragments = await service.library(session, tenant.id)
    with pytest.raises(planner.PlanError):
        planner.compose({"goals": ["a"]}, fragments)


async def test_the_step_budget_is_enforced_during_the_search(session, tenant):
    await _library(session, tenant)
    fragments = await service.library(session, tenant.id)
    with pytest.raises(planner.PlanError):
        planner.compose({"goals": ["customer_notified"]}, fragments, max_steps=2)


async def test_stage_ids_are_namespaced_so_fragments_cannot_collide(session, tenant):
    await _publish(session, tenant, _fragment("one", provides=["x"]))
    await _publish(session, tenant, _fragment("two", requires=["x"], provides=["y"]))
    fragments = await service.library(session, tenant.id)
    out = planner.compose({"goals": ["y"]}, fragments)
    ids = [s["id"] for s in out["definition"]["stages"]]
    assert ids == ["one.one", "two.two"]


# ══ the ladder ══════════════════════════════════════════════════════════════


async def test_a_plan_records_every_rung(session, tenant):
    await _library(session, tenant)
    row = await service.plan(session, tenant_uuid=tenant.id,
                             intent="refund this customer",
                             goal_spec={"goals": ["customer_notified"]})
    await session.commit()
    assert row.status == "planned"
    assert [r["rung"] for r in row.ladder] == list(service.RUNGS)


async def test_a_refused_plan_is_still_a_record(session, tenant):
    """"The platform was asked for this and could not compose it safely" is
    what a gap in the library looks like from outside."""
    await _library(session, tenant)
    row = await service.plan(session, tenant_uuid=tenant.id, intent="do the impossible",
                             goal_spec={"goals": ["world_peace"]})
    await session.commit()
    assert row.status == "refused" and row.stopped_at == "plan"
    assert await session.get(IntentPlanModel, row.id) is not None


async def test_special_category_data_refuses_the_whole_composition(session, tenant):
    """A process assembled by a planner is not the place for Article 9 data."""
    await _publish(session, tenant, _fragment(
        "health-check", provides=["assessed"], classes=["special_category"]))
    row = await service.plan(session, tenant_uuid=tenant.id, intent="assess",
                             goal_spec={"goals": ["assessed"]})
    await session.commit()
    assert row.status == "refused" and row.stopped_at == "policy"
    assert "Article 9" in row.stopped_reason


async def test_consequential_capabilities_are_called_out(session, tenant):
    """A reader must not skim the ones that leave the platform or move money."""
    await _library(session, tenant)
    row = await service.plan(session, tenant_uuid=tenant.id, intent="refund",
                             goal_spec={"goals": ["customer_notified"]})
    caps_rung = next(r for r in row.ladder if r["rung"] == "capabilities")
    assert set(caps_rung["consequential"]) == {"notify.customer", "payment.refund"}


# ══ plan-hash binding — the exit criterion ══════════════════════════════════


async def test_confirming_binds_the_hash_that_was_shown(session, tenant):
    await _library(session, tenant)
    row = await service.plan(session, tenant_uuid=tenant.id, intent="refund",
                             goal_spec={"goals": ["customer_notified"]})
    await session.commit()
    await service.confirm(session, row=row, actor="alice",
                          plan_sha256=row.plan_sha256)
    assert row.status == "confirmed"
    assert row.confirmed_sha256 == row.plan_sha256


async def test_a_plan_that_moved_cannot_be_confirmed(session, tenant):
    """The plan-swap this binding exists to make impossible."""
    await _library(session, tenant)
    row = await service.plan(session, tenant_uuid=tenant.id, intent="refund",
                             goal_spec={"goals": ["customer_notified"]})
    await session.commit()
    with pytest.raises(service.IntentError, match="has changed"):
        await service.confirm(session, row=row, actor="alice", plan_sha256="0" * 64)


async def test_a_definition_edited_after_confirmation_will_not_run(session, tenant):
    """Checking only at confirm time leaves a window in which the stored
    definition could be edited and run under somebody else's approval."""
    await _library(session, tenant)
    row = await service.plan(session, tenant_uuid=tenant.id, intent="refund",
                             goal_spec={"goals": ["customer_notified"]})
    await session.commit()
    await service.confirm(session, row=row, actor="alice", plan_sha256=row.plan_sha256)
    await session.commit()

    definition = dict(row.definition)
    definition["stages"] = [*definition["stages"],
                            {"id": "smuggled", "order": 99, "steps": []}]
    row.definition = definition
    await session.commit()

    with pytest.raises(service.IntentError, match="no longer matches"):
        await service.execute(session, row=row, actor="bob")
    assert row.status == "abandoned"


async def test_an_unconfirmed_plan_does_not_run(session, tenant):
    await _library(session, tenant)
    row = await service.plan(session, tenant_uuid=tenant.id, intent="refund",
                             goal_spec={"goals": ["customer_notified"]})
    await session.commit()
    with pytest.raises(service.IntentError, match="only a confirmed plan runs"):
        await service.execute(session, row=row, actor="alice")


async def test_a_confirmed_plan_executes_and_returns_its_definition(session, tenant):
    await _library(session, tenant)
    row = await service.plan(session, tenant_uuid=tenant.id, intent="refund",
                             goal_spec={"goals": ["customer_notified"]})
    await session.commit()
    await service.confirm(session, row=row, actor="alice", plan_sha256=row.plan_sha256)
    out = await service.execute(session, row=row, actor="alice")
    assert row.status == "executed"
    assert out["plan_sha256"] == row.confirmed_sha256
    assert len(out["definition"]["stages"]) == 4


# ══ the library's own rules ═════════════════════════════════════════════════


async def test_a_fragment_slug_cannot_be_reused(session, tenant):
    """A fragment's content is its identity — plans may already have been
    approved against it."""
    await _publish(session, tenant, _fragment("only-once", provides=["x"]))
    with pytest.raises(service.IntentError, match="already exists"):
        await service.publish_fragment(
            session, tenant_uuid=tenant.id,
            fragment=_fragment("only-once", provides=["y"]))


def test_the_content_hash_covers_the_stages_not_just_the_declaration():
    """A fragment whose stages changed under an unchanged declaration is exactly
    the supply-chain problem content addressing exists to catch."""
    one = frag.content_hash(_fragment("f", provides=["x"]))
    two = frag.content_hash(_fragment("f", provides=["x"],
                                      steps=(("s1", "automated"), ("s2", "form"))))
    assert one != two


async def test_the_api_is_gated(anon_client):
    resp = await anon_client.get("/api/v1/intent/fragments")
    assert resp.status_code in (401, 403)
    resp = await anon_client.post("/api/v1/intent/plans", json={})
    assert resp.status_code in (401, 403, 422)
