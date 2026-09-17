"""ARB-B…G — the bridge, the PEPs, parity, the promotion gate, and authoring.

The single property this file exists to hold down:

    **Wiring a PEP into a running system changes nothing.**

Every namespace ships at `shadow`, `pep.check()` hands the legacy answer straight
back, and the only thing that accumulates is evidence. A test that stopped
proving that would let this engine start refusing real work on somebody's
install the day they upgraded.

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
    ArbiterDecisionModel,
    ArbiterDraftModel,
    ArbiterNamespaceModel,
    ArbiterPolicyModel,
    ArbiterPolicyVersionModel,
    TenantModel,
)

from case_service.hxarbiter import (
    Effect,
    PolicyCompiler,
    PolicyMode,
    Rule,
    VELARIS_CATALOG,
)
from case_service.hxarbiter import (
    legacy_bridge,
    lifecycle,
    parity as parity_mod,
    pdp,
    pep,
    store,
)
import hxarbiter_core as analyzer
from case_service.hxarbiter.nlp import author, readback

pytestmark = pytest.mark.asyncio

SEED = [("ai", "deny"), ("egress", "deny"), ("export", "deny"),
        ("vault", "deny"), ("case", "allow"), ("auth", "allow")]


@pytest_asyncio.fixture
async def tenant(session):
    from case_service.tenancy import identity as _identity
    _identity.clear_cache()
    pdp.invalidate_cache()
    for ns, eff in SEED:
        session.add(ArbiterNamespaceModel(
            namespace=ns, default_effect=eff, max_mode="shadow", description="",
            sample_allow_rate=1, sample_default_rate=1,
            updated_at=datetime.now(timezone.utc)))
    t = TenantModel(id=uuid.uuid4(), slug="arb-bcd", name="Arb BCD")
    session.add(t)
    await session.commit()
    yield t
    pdp.invalidate_cache()
    _identity.clear_cache()


# ══ ARB-B — the legacy bridge ═══════════════════════════════════════════════


async def test_local_only_denies_external_and_still_permits_local(session, tenant):
    """The refusal must be a DENY — an allow is defeated by any other deny, and
    a mirror that can be overridden is not a mirror.

    But it must ALSO permit local, which the legacy control does. `ai.*` defaults
    to deny, so a lone deny-external rule leaves a local call falling through to
    that default — making the mirror STRICTER than the thing it shadows. Parity
    would then report `engine_stricter` forever and the namespace could never be
    promoted. Found on the live box after the suite was green.
    """
    rules = legacy_bridge.ai_local_only_rules(local_only=True)
    assert sorted(r.effect for r in rules) == [Effect.ALLOW, Effect.DENY]


async def test_the_ai_mirror_reproduces_the_legacy_answer_exactly(session, tenant):
    """The whole point of a mirror: same question, same answer, both classes."""
    # Promote FIRST, then sync — which is the order §8 step 4 describes, and
    # the order the bridge/sync endpoint is called in after a promotion.
    await store.promote_namespace(session, "ai", PolicyMode.ENFORCE)
    await legacy_bridge.mirror_ai_egress(
        session, tenant_id=tenant.id, local_only=True)
    await session.commit()
    pdp.invalidate_cache()

    async def allowed(cls):
        return await pep.check(
            session, tenant_id=tenant.slug, action="ai.external_call",
            subject_id="case_qa", context={"provider_class": cls},
            legacy_allowed=True)

    assert await allowed("external") is False   # legacy: refused
    assert await allowed("local") is True       # legacy: permitted

    # And with the tenant opted in, both are permitted, as the control says.
    await legacy_bridge.mirror_ai_egress(
        session, tenant_id=tenant.id, local_only=False)
    await session.commit()
    pdp.invalidate_cache()
    assert await allowed("external") is True
    assert await allowed("local") is True


async def test_external_allowed_still_emits_a_policy(session, tenant):
    """A permitted mirror is still a policy. Emitting nothing would let the
    namespace default (deny) make the engine look stricter than the control."""
    rules = legacy_bridge.ai_local_only_rules(local_only=False)
    assert rules and rules[0].effect is Effect.ALLOW


async def test_empty_outbound_domains_emits_no_rule(session, tenant):
    """The marketplace's own default is egress-DROP, and the mirror says so by
    saying nothing — the namespace default then denies."""
    assert legacy_bridge.outbound_domain_rules([]) == []


async def test_domain_rules_use_matches_domain(session, tenant):
    rules = legacy_bridge.outbound_domain_rules(["api.example.com"])
    cond = rules[0].condition.model_dump(by_alias=True)
    assert cond["op"] == "matches_domain"


async def test_a_domain_mirror_compiles_and_actually_decides(session, tenant):
    """The bridge's output must survive the compiler and reach the evaluator.

    A LIVE defect: `matches_domain` with a LIST of hosts — which is what an
    allowlist is, and exactly what this bridge generates — was implemented in the
    evaluator and refused by the compiler. The earlier test checked only that the
    generated rule *said* `matches_domain`, never that it compiled, so the mirror
    for any tenant with packages had never once worked.
    """
    await store.promote_namespace(session, "egress", PolicyMode.ENFORCE)
    await legacy_bridge.mirror_connector_egress(
        session, tenant_id=tenant.id,
        domains=["api.example.com", "cdn.partner.test"])
    await session.commit()
    pdp.invalidate_cache()

    async def allowed(host):
        return await pep.check(
            session, tenant_id=tenant.slug, action="egress.http",
            subject_id="app-1", context={"target_domain": host},
            legacy_allowed=True)

    assert await allowed("api.example.com") is True
    assert await allowed("deeper.api.example.com") is True
    assert await allowed("cdn.partner.test") is True
    assert await allowed("evil.test") is False
    # The bypass the operator set exists to make unreachable.
    assert await allowed("api.example.com.attacker.net") is False
    assert await allowed("notapi.example.com") is False


async def test_mirroring_is_idempotent(session, tenant):
    _, changed = await legacy_bridge.mirror_ai_egress(
        session, tenant_id=tenant.id, local_only=True)
    await session.commit()
    assert changed is True

    _, changed = await legacy_bridge.mirror_ai_egress(
        session, tenant_id=tenant.id, local_only=True)
    await session.commit()
    assert changed is False, "a boot-time bridge must not append a version per boot"

    versions = (await session.execute(
        select(ArbiterPolicyVersionModel))).scalars().all()
    assert len(versions) == 1


async def test_changing_the_legacy_setting_appends_a_version(session, tenant):
    await legacy_bridge.mirror_ai_egress(session, tenant_id=tenant.id, local_only=True)
    await session.commit()
    _, changed = await legacy_bridge.mirror_ai_egress(
        session, tenant_id=tenant.id, local_only=False)
    await session.commit()
    assert changed is True
    versions = (await session.execute(
        select(ArbiterPolicyVersionModel))).scalars().all()
    assert len(versions) == 2
    active = [v for v in versions if v.deactivated_at is None]
    assert len(active) == 1, "only one version may be live at a time"


async def test_a_tenant_with_no_packages_can_still_sync(session, tenant):
    """The commonest tenant on any box has no installed packages.

    A LIVE 500. `outbound_domain_rules([])` correctly returned no rules and
    `add_version` correctly refused a version with no rules — each tested on its
    own, and neither test ever composed them. Together they made the DEFAULT
    state of every new install unable to regenerate its mirrors.
    """
    pid, changed = await legacy_bridge.mirror_connector_egress(
        session, tenant_id=tenant.id, domains=[])
    await session.commit()
    assert pid is None and changed is False
    assert (await session.execute(select(ArbiterPolicyModel))).scalars().all() == []


async def test_narrowing_to_no_domains_retires_the_mirror(session, tenant):
    """A mirror left behind would keep claiming a permission that is gone."""
    await legacy_bridge.mirror_connector_egress(
        session, tenant_id=tenant.id, domains=["api.example.com"])
    await session.commit()
    row = (await session.execute(select(ArbiterPolicyModel))).scalar_one()
    assert row.status == "active"

    _, changed = await legacy_bridge.mirror_connector_egress(
        session, tenant_id=tenant.id, domains=[])
    await session.commit()
    assert changed is True
    await session.refresh(row)
    assert row.status == "retired"

    # ...and doing it again is a no-op, not a second retirement.
    _, changed = await legacy_bridge.mirror_connector_egress(
        session, tenant_id=tenant.id, domains=[])
    assert changed is False


async def test_the_bridge_refuses_to_overwrite_an_authored_policy(session, tenant):
    await store.create_policy(
        session, tenant_id=tenant.id, name=legacy_bridge.AI_LOCAL_ONLY,
        namespace="ai", origin="user")
    await session.commit()
    with pytest.raises(ValueError):
        await legacy_bridge.mirror_ai_egress(
            session, tenant_id=tenant.id, local_only=True)


# ══ the PEP contract — inert until promoted ═════════════════════════════════


async def test_a_pep_returns_the_legacy_answer_while_in_shadow(session, tenant):
    c = PolicyCompiler(VELARIS_CATALOG)
    p = await store.create_policy(session, tenant_id=tenant.id,
                                  name="deny-all-external", namespace="ai")
    await store.add_version(session, policy_id=p.id, compiler=c,
                            mode=PolicyMode.ENFORCE, rules=[Rule.model_validate({
                                "id": "r1", "effect": "deny",
                                "condition": {"attr": "context.provider_class",
                                              "op": "eq", "value": "external"}})])
    await store.activate_version(session, policy_id=p.id, version=1)
    await session.commit()
    pdp.invalidate_cache()

    allowed = await pep.check(
        session, tenant_id=tenant.slug, action="ai.external_call",
        subject_id="case_qa", context={"provider_class": "external"},
        legacy_allowed=True)
    assert allowed is True, "shadow must not change the caller's answer"


async def test_the_pep_records_both_answers(session, tenant):
    await pep.check(
        session, tenant_id=tenant.slug, action="ai.external_call",
        subject_id="case_qa", context={"provider_class": "external"},
        legacy_allowed=True)
    await session.commit()
    row = (await session.execute(select(ArbiterDecisionModel))).scalar_one()
    assert row.legacy_effect == "allow"
    assert row.effect == "deny"           # ai.* default


async def test_a_promoted_namespace_reissues_the_mirror_at_the_new_mode(
        session, tenant):
    """A mirror pinned at `shadow` would stay inert whatever was promoted.

    The ceiling only clamps DOWN, so tracking the namespace is what makes
    "promote the namespace" — the single action §8 step 4 describes — actually
    do something.
    """
    await legacy_bridge.mirror_ai_egress(
        session, tenant_id=tenant.id, local_only=True)
    await session.commit()
    v = (await session.execute(select(ArbiterPolicyVersionModel))).scalars().first()
    assert v.mode == "shadow"

    await store.promote_namespace(session, "ai", PolicyMode.ENFORCE)
    _, changed = await legacy_bridge.mirror_ai_egress(
        session, tenant_id=tenant.id, local_only=True)
    await session.commit()
    assert changed is True, "identical rules, but the mode moved"
    modes = {v.mode for v in (await session.execute(
        select(ArbiterPolicyVersionModel))).scalars().all()}
    assert "enforce" in modes


async def test_a_promoted_namespace_makes_the_pep_bind(session, tenant):
    c = PolicyCompiler(VELARIS_CATALOG)
    p = await store.create_policy(session, tenant_id=tenant.id,
                                  name="deny-external", namespace="ai")
    await store.add_version(session, policy_id=p.id, compiler=c,
                            mode=PolicyMode.ENFORCE, rules=[Rule.model_validate({
                                "id": "r1", "effect": "deny",
                                "condition": {"attr": "context.provider_class",
                                              "op": "eq", "value": "external"}})])
    await store.activate_version(session, policy_id=p.id, version=1)
    await store.promote_namespace(session, "ai", PolicyMode.ENFORCE)
    await session.commit()
    pdp.invalidate_cache()

    allowed = await pep.check(
        session, tenant_id=tenant.slug, action="ai.external_call",
        subject_id="case_qa", context={"provider_class": "external"},
        legacy_allowed=True)
    assert allowed is False


async def test_a_broken_engine_never_denies_live_traffic(session, tenant, monkeypatch):
    """A bug in the thing being measured must not be able to refuse work."""
    async def boom(*a, **k):
        raise RuntimeError("engine exploded")
    monkeypatch.setattr(pdp, "decide", boom)

    assert await pep.check(
        session, tenant_id=tenant.slug, action="ai.external_call",
        subject_id="x", legacy_allowed=True) is True
    assert await pep.check(
        session, tenant_id=tenant.slug, action="ai.external_call",
        subject_id="x", legacy_allowed=False) is False


async def test_the_ai_pep_asks_about_the_action_it_names(session, tenant):
    """A local_only tenant must AGREE with the engine, not disagree with it.

    Found by reading the live decision log, which showed every authoring call
    as "engine allow / legacy deny — disagreed". The PEP derived
    `provider_class` from `external_consented`, conflating *what we are about to
    do* with *what is permitted*: a local_only tenant asked "may I make a LOCAL
    call" and was scored against a legacy answer about EXTERNAL ones. The parity
    report then reported a HOLE (`engine_weaker`) for a tenant where both sides
    agreed — and a bad oracle is worse than none, because it blocks promotion
    forever while pointing at the wrong thing.
    """
    await legacy_bridge.mirror_ai_egress(
        session, tenant_id=tenant.id, local_only=True)
    await session.commit()
    pdp.invalidate_cache()

    # What the chokepoint now asks: "may this tenant call an EXTERNAL provider?"
    await pep.check(
        session, tenant_id=tenant.slug, action="ai.external_call",
        subject_id="Case Q&A", resource_kind="case_content",
        context={"provider_class": "external", "purpose": "Case Q&A"},
        legacy_allowed=False)          # local_only ⇒ the legacy answer is "no"
    await session.commit()

    row = (await session.execute(select(ArbiterDecisionModel))).scalars().all()[-1]
    assert row.effect == "deny" and row.legacy_effect == "deny", (
        "engine and legacy must AGREE for a local_only tenant")

    r = await parity_mod.parity_report(
        session, tenant_id=tenant.id, namespace="ai")
    assert r.engine_weaker == 0, "a false disagreement would read as a hole"


# ══ ARB-B/G — parity, and the promotion gate ════════════════════════════════


async def _decisions(session, tenant, *, n, engine, legacy, namespace="ai"):
    for _ in range(n):
        session.add(ArbiterDecisionModel(
            id=uuid.uuid4(), tenant_id=tenant.id, namespace=namespace,
            action=f"{namespace}.probe", subject_kind="service", subject_id="s",
            subject={}, resource={}, context={}, effect=engine, mode="shadow",
            enforced=False, default_applied=False, matched=[], obligations=[],
            legacy_effect=legacy, latency_us=1,
            evaluated_at=datetime.now(timezone.utc)))
    await session.commit()


async def test_a_namespace_nobody_exercised_is_not_eligible(session, tenant):
    """The most dangerous report this module can produce: a green light earned
    by silence."""
    r = await parity_mod.parity_report(session, tenant_id=tenant.id, namespace="ai")
    assert r.compared == 0
    assert r.agreement_rate == 0.0
    assert r.eligible is False
    assert any("nobody exercised" in x for x in r.reasons)


async def test_full_agreement_over_enough_traffic_is_eligible(session, tenant):
    await _decisions(session, tenant, n=120, engine="deny", legacy="deny")
    r = await parity_mod.parity_report(session, tenant_id=tenant.id, namespace="ai")
    assert r.compared == 120
    assert r.agreement_rate == 1.0
    assert r.eligible is True
    assert r.reasons == []


async def test_a_single_disagreement_blocks_promotion(session, tenant):
    await _decisions(session, tenant, n=120, engine="deny", legacy="deny")
    await _decisions(session, tenant, n=1, engine="allow", legacy="deny")
    r = await parity_mod.parity_report(session, tenant_id=tenant.id, namespace="ai")
    assert r.engine_weaker == 1
    assert r.eligible is False
    assert any("hole" in x for x in r.reasons)


async def test_stricter_and_weaker_are_reported_separately(session, tenant):
    await _decisions(session, tenant, n=100, engine="deny", legacy="deny")
    await _decisions(session, tenant, n=3, engine="deny", legacy="allow")
    await _decisions(session, tenant, n=2, engine="allow", legacy="deny")
    r = await parity_mod.parity_report(session, tenant_id=tenant.id, namespace="ai")
    assert r.engine_stricter == 3
    assert r.engine_weaker == 2


async def test_unshadowed_decisions_are_not_counted_as_agreement(session, tenant):
    """A bridge that stopped running would otherwise report a clean sheet."""
    await _decisions(session, tenant, n=200, engine="deny", legacy=None)
    r = await parity_mod.parity_report(session, tenant_id=tenant.id, namespace="ai")
    assert r.compared == 0
    assert r.eligible is False


async def test_a_simulated_decision_is_not_evidence(session, tenant):
    session.add(ArbiterDecisionModel(
        id=uuid.uuid4(), tenant_id=tenant.id, namespace="ai", action="ai.probe",
        subject_kind="user", subject_id="s", subject={}, resource={}, context={},
        effect="deny", mode="shadow", enforced=False, default_applied=False,
        matched=[], obligations=[], legacy_effect="deny", simulated=True,
        latency_us=1, evaluated_at=datetime.now(timezone.utc)))
    await session.commit()
    r = await parity_mod.parity_report(session, tenant_id=tenant.id, namespace="ai")
    assert r.compared == 0


async def test_decisions_outside_the_window_are_not_counted(session, tenant):
    session.add(ArbiterDecisionModel(
        id=uuid.uuid4(), tenant_id=tenant.id, namespace="ai", action="ai.probe",
        subject_kind="user", subject_id="s", subject={}, resource={}, context={},
        effect="deny", mode="shadow", enforced=False, default_applied=False,
        matched=[], obligations=[], legacy_effect="deny", latency_us=1,
        evaluated_at=datetime.now(timezone.utc) - timedelta(days=90)))
    await session.commit()
    r = await parity_mod.parity_report(
        session, tenant_id=tenant.id, namespace="ai", window_days=14)
    assert r.compared == 0


async def test_unevaluable_decisions_block_promotion(session, tenant):
    await _decisions(session, tenant, n=120, engine="deny", legacy="deny")
    session.add(ArbiterDecisionModel(
        id=uuid.uuid4(), tenant_id=tenant.id, namespace="ai", action="ai.probe",
        subject_kind="user", subject_id="s", subject={}, resource={}, context={},
        effect="deny", mode="shadow", enforced=False, default_applied=True,
        matched=[], obligations=[], error="pip_timeout", latency_us=1,
        evaluated_at=datetime.now(timezone.utc)))
    await session.commit()
    r = await parity_mod.parity_report(session, tenant_id=tenant.id, namespace="ai")
    assert r.errors == 1
    assert r.eligible is False


# ══ ARB-F — deterministic analysis ══════════════════════════════════════════


def _rule(rid, effect, condition, **kw):
    return Rule.model_validate({"id": rid, "effect": effect,
                                "condition": condition, **kw})


async def test_a_contradiction_is_an_error():
    r = _rule("r1", "deny", {"all": [
        {"attr": "tenant.region", "op": "eq", "value": "eu-west"},
        {"attr": "tenant.region", "op": "eq", "value": "us-east"}]})
    findings = analyzer.analyze([r])
    assert any(f.kind == "contradiction" and f.severity == "error" for f in findings)


async def test_opposite_effects_that_overlap_are_a_conflict():
    a = _rule("a", "deny", {"attr": "context.provider_class",
                            "op": "eq", "value": "external"})
    b = _rule("b", "allow", {"attr": "context.provider_class",
                             "op": "eq", "value": "external"})
    findings = analyzer.analyze([a, b])
    assert any(f.kind == "conflict" for f in findings)


async def test_a_narrower_rule_of_the_same_effect_is_shadowed():
    broad = _rule("broad", "deny", {"attr": "tenant.region",
                                    "op": "eq", "value": "eu-west"})
    narrow = _rule("narrow", "deny", {"all": [
        {"attr": "tenant.region", "op": "eq", "value": "eu-west"},
        {"attr": "context.provider_class", "op": "eq", "value": "external"}]})
    findings = analyzer.analyze([broad, narrow])
    assert any(f.kind == "shadowed" for f in findings)


async def test_disjoint_rules_raise_nothing():
    a = _rule("a", "deny", {"attr": "tenant.region", "op": "eq", "value": "eu-west"})
    b = _rule("b", "allow", {"attr": "tenant.region", "op": "eq", "value": "us-east"})
    assert analyzer.analyze([a, b]) == []


async def test_a_not_makes_a_rule_undecidable_rather_than_guessed():
    """A static analyser that bluffs is worse than one that admits its limits."""
    r = _rule("r1", "deny", {"not": {"attr": "tenant.region",
                                     "op": "eq", "value": "eu-west"}})
    findings = analyzer.analyze([r])
    assert [f.kind for f in findings] == ["undecidable"]
    assert findings[0].severity == "info"


async def test_seed_requests_actually_match_the_rule():
    """A seed that does not match the rule it came from is a broken fixture."""
    from hxarbiter_core import Evaluator, PolicyVersion

    r = _rule("r1", "deny", {"all": [
        {"attr": "tenant.region", "op": "in", "value": ["eu-west", "eu-central"]},
        {"attr": "context.provider_class", "op": "eq", "value": "external"}]})
    compiled = PolicyCompiler(VELARIS_CATALOG).compile_version(
        policy_id=uuid.uuid4(), tenant_id=None, namespace="ai",
        version=PolicyVersion(policy_id=uuid.uuid4(), version=1, rules=[r]),
        mode=PolicyMode.ENFORCE)
    seeds = analyzer.seed_requests(r)
    assert seeds
    for attrs in seeds:
        d = Evaluator().evaluate(
            policies=[compiled], attrs=attrs,
            namespace_default=Effect.ALLOW, namespace_max_mode=PolicyMode.ENFORCE)
        assert d.effect is Effect.DENY, attrs


# ══ ARB-F — the authoring pipeline ══════════════════════════════════════════


class _Model:
    """A backend that returns whatever it was told to, in order."""

    is_external = False
    name = "probe-model"

    def __init__(self, *replies: str) -> None:
        self._replies = list(replies)
        self.prompts: list[str] = []

    async def generate(self, prompt, temperature=0):
        self.prompts.append(prompt)
        return self._replies.pop(0) if self._replies else ""


GOOD = ('{"rules":[{"id":"r1","description":"no external AI",'
        '"effect":"deny","condition":{"attr":"context.provider_class",'
        '"op":"eq","value":"external"}}],"notes":""}')
BAD_OP = ('{"rules":[{"id":"r1","effect":"deny","condition":'
          '{"attr":"context.target_domain","op":"contains","value":"x.com"}}]}')


async def test_a_valid_draft_compiles():
    result = await author.draft_rules(
        _Model(GOOD), intent="no external AI", catalog=VELARIS_CATALOG,
        namespace="ai", default_effect="deny")
    assert result.ok and len(result.rules) == 1 and result.attempts == 1


async def test_a_bad_operator_gets_exactly_one_repair_round():
    model = _Model(BAD_OP, GOOD)
    result = await author.draft_rules(
        model, intent="block a domain", catalog=VELARIS_CATALOG,
        namespace="egress", default_effect="deny")
    assert result.ok is True
    assert result.attempts == 2
    assert "bypass class" in model.prompts[1], "the compiler's reason must be fed back"


async def test_a_model_that_will_not_comply_fails_honestly():
    """No silent fixing: the operator sees the errors, not an invented rule."""
    result = await author.draft_rules(
        _Model(BAD_OP, BAD_OP), intent="block a domain",
        catalog=VELARIS_CATALOG, namespace="egress", default_effect="deny")
    assert result.ok is False
    assert result.rules == []
    assert any("bypass class" in e for e in result.errors)


async def test_the_prompt_carries_the_catalog_and_never_tenant_data():
    model = _Model(GOOD)
    await author.draft_rules(
        model, intent="no external AI", catalog=VELARIS_CATALOG,
        namespace="ai", default_effect="deny")
    prompt = model.prompts[0]
    assert "tenant.region" in prompt and "matches_domain" in prompt
    for leak in ("portal_submitter", "case_id", "decision_id", "subject_id"):
        assert leak not in prompt


async def test_a_markdown_fence_is_stripped_but_content_is_not_repaired():
    rules, _notes, errors = author.parse_model_output(f"```json\n{GOOD}\n```")
    assert not errors and len(rules) == 1
    _, _, errors = author.parse_model_output("here you go: not json")
    assert errors


async def test_readback_falls_back_to_a_deterministic_description():
    """An install with no model still needs plain English beside a policy."""
    r = _rule("r1", "deny", {"attr": "context.provider_class",
                             "op": "eq", "value": "external"})
    text, source = await readback.readback(None, [r], default_effect="deny")
    assert source == "deterministic"
    assert "REFUSED" in text and "context.provider_class" in text


async def test_the_readback_prompt_never_sees_the_intent():
    """§7.1 P4 — a readback shown the intent agrees with the intent, including
    when the rules do not."""
    model = _Model("The request is refused for external providers.")
    r = _rule("r1", "deny", {"attr": "context.provider_class",
                             "op": "eq", "value": "external"})
    await readback.readback(model, [r], default_effect="deny")
    assert "INTENT" not in model.prompts[0]


async def test_a_backend_that_cannot_be_called_is_no_provider(session, tenant):
    """A LIVE defect. `hxnexus.resolve_backend` returns
    `(backend, external_consented)` — a TUPLE — and this module assumed a bare
    backend. The tuple resolved "successfully", failed at generation time, and
    left a junk draft row behind instead of the honest 503 that says the feature
    is simply unavailable.

    Anything without a generation method is now "no provider", refused here
    rather than discovered later.
    """
    from case_service.hxarbiter.nlp import provider as _provider

    async def returns_a_tuple(*a, **k):
        return (object(), False)

    import case_service.hxnexus.case_qa as _qa
    original = _qa.resolve_backend
    _qa.resolve_backend = returns_a_tuple
    try:
        with pytest.raises(_provider.ProviderUnavailable) as exc:
            await _provider.resolve(session, tenant_slug=tenant.slug)
        assert "no generation method" in str(exc.value)
    finally:
        _qa.resolve_backend = original


# ══ ARB-F — four-eyes ═══════════════════════════════════════════════════════


def _now():
    return datetime.now(timezone.utc)


async def test_a_different_approver_is_accepted_immediately():
    assert lifecycle.check_four_eyes(
        author=uuid.UUID(int=1), approver=uuid.UUID(int=2),
        authored_at=_now()) == "two_person"


async def test_self_approval_is_refused_before_the_cooling_off():
    with pytest.raises(lifecycle.LifecycleError) as exc:
        lifecycle.check_four_eyes(
            author=uuid.UUID(int=1), approver=uuid.UUID(int=1),
            authored_at=_now())
    assert "cooling-off" in str(exc.value)


async def test_self_approval_is_allowed_after_the_cooling_off():
    assert lifecycle.check_four_eyes(
        author=uuid.UUID(int=1), approver=uuid.UUID(int=1),
        authored_at=_now() - timedelta(minutes=11)) == "self_cooling_off"


async def test_an_unidentified_approver_is_always_refused():
    """Four-eyes is an attribution control before it is a counting one."""
    with pytest.raises(lifecycle.LifecycleError):
        lifecycle.check_four_eyes(
            author=uuid.UUID(int=1), approver=None,
            authored_at=_now() - timedelta(days=1))


async def test_a_tenant_may_require_a_second_administrator():
    with pytest.raises(lifecycle.LifecycleError):
        lifecycle.check_four_eyes(
            author=uuid.UUID(int=1), approver=uuid.UUID(int=1),
            authored_at=_now() - timedelta(days=1), allow_self_approval=False)


async def test_the_draft_machine_refuses_a_skipped_step():
    assert lifecycle.can_transition(
        lifecycle.DraftState.DRAFTED, lifecycle.DraftState.APPROVED) is False
    assert lifecycle.can_transition(
        lifecycle.DraftState.ACTIVATED, lifecycle.DraftState.APPROVED) is False


async def test_a_draft_with_an_error_finding_cannot_be_approved(session, tenant):
    draft = ArbiterDraftModel(
        id=uuid.uuid4(), tenant_id=tenant.id, namespace="ai",
        intent="x", state=str(lifecycle.DraftState.READBACK_PENDING),
        readback="something", created_by=uuid.UUID(int=1),
        analysis_report=[{"severity": "error", "kind": "contradiction",
                          "message": "can never fire"}],
        created_at=_now(), updated_at=_now())
    session.add(draft)
    await session.commit()
    with pytest.raises(lifecycle.LifecycleError) as exc:
        await lifecycle.transition(
            session, draft, lifecycle.DraftState.APPROVED,
            approver=uuid.UUID(int=2))
    assert "error-level" in str(exc.value)


async def test_a_draft_without_a_readback_cannot_be_approved(session, tenant):
    draft = ArbiterDraftModel(
        id=uuid.uuid4(), tenant_id=tenant.id, namespace="ai", intent="x",
        state=str(lifecycle.DraftState.READBACK_PENDING), readback=None,
        created_by=uuid.UUID(int=1), analysis_report=[],
        created_at=_now(), updated_at=_now())
    session.add(draft)
    await session.commit()
    with pytest.raises(lifecycle.LifecycleError) as exc:
        await lifecycle.transition(
            session, draft, lifecycle.DraftState.APPROVED,
            approver=uuid.UUID(int=2))
    assert "readback" in str(exc.value)


async def test_warnings_do_not_block_approval(session, tenant):
    """Blocking on warnings trains people to disable the analyser."""
    draft = ArbiterDraftModel(
        id=uuid.uuid4(), tenant_id=tenant.id, namespace="ai", intent="x",
        state=str(lifecycle.DraftState.READBACK_PENDING), readback="ok",
        created_by=uuid.UUID(int=1),
        analysis_report=[{"severity": "warning", "kind": "conflict",
                          "message": "overlaps"}],
        created_at=_now(), updated_at=_now())
    session.add(draft)
    await session.commit()
    mode = await lifecycle.transition(
        session, draft, lifecycle.DraftState.APPROVED, approver=uuid.UUID(int=2))
    assert mode == "two_person"
