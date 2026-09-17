"""ARB-A — store and PDP against a real session.

The core suite proves the evaluator is right. This proves the parts that can
fail for reasons the evaluator never sees: an unresolvable tenant, a policy
store that will not answer, a PIP that hangs, a version that stopped compiling.

Every one of those must produce a DECISION, not an exception. A PEP handed an
exception has to invent a policy at the call site, and forty call sites will
invent forty different ones — most of them "log it and carry on", which is the
failure this engine exists to prevent.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio
from sqlalchemy import select

from velaris_shared.db.models import (
    ArbiterDecisionModel,
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
from case_service.hxarbiter import pdp, store
from hxarbiter_core import StaticResolver
from hxarbiter_core import (
    DecisionRequest,
    ResourceRef,
    SubjectKind,
    SubjectRef,
)

pytestmark = pytest.mark.asyncio

#: Mirrors the seed in migration 177. The tests build it themselves because
#: `Base.metadata.create_all` makes the tables and not the rows.
SEED = [
    ("ai", "deny"), ("egress", "deny"), ("export", "deny"),
    ("vault", "deny"), ("case", "allow"), ("auth", "allow"),
]


def _rules(*specs) -> list[Rule]:
    return [Rule.model_validate(s) for s in specs]


DENY_EXTERNAL = {
    "id": "r1",
    "description": "external AI is refused",
    "effect": "deny",
    "condition": {"attr": "context.provider_class", "op": "eq", "value": "external"},
}
ALLOW_EXTERNAL = {
    "id": "a1",
    "effect": "allow",
    "condition": {"attr": "context.provider_class", "op": "eq", "value": "external"},
}


@pytest_asyncio.fixture
async def tenant(session):
    # Two caches survive between tests and both must be cleared, or a test gets
    # the PREVIOUS test's tenant UUID for the slug "arb-co" — tenant-scoped
    # policies then match nothing and decisions log the wrong tenant. Only the
    # PDP cache is obvious; `tenancy.identity` has its own slug->UUID cache with
    # a TTL, and that is the one that actually bit.
    from case_service.tenancy import identity as _identity
    _identity.clear_cache()
    pdp.invalidate_cache()
    for ns, eff in SEED:
        session.add(ArbiterNamespaceModel(
            namespace=ns, default_effect=eff, max_mode="shadow",
            description="", sample_allow_rate=1, sample_default_rate=1,
            updated_at=datetime.now(timezone.utc)))
    t = TenantModel(id=uuid.uuid4(), slug="arb-co", name="Arb Co")
    session.add(t)
    await session.commit()
    yield t
    pdp.invalidate_cache()
    _identity.clear_cache()


def _req(tenant_id, *, provider_class="external", action="ai.external_call"):
    return DecisionRequest(
        tenant_id=str(tenant_id),
        subject=SubjectRef(kind=SubjectKind.USER, id="u-1"),
        action=action,
        resource=ResourceRef(kind="case", id="c-1"),
        context={"provider_class": provider_class},
    )


# ── store: the activation interval, which is the audit claim ─────────────────


async def test_activation_records_an_interval(session, tenant):
    c = PolicyCompiler(VELARIS_CATALOG)
    p = await store.create_policy(session, tenant_id=tenant.id,
                                  name="no-external", namespace="ai")
    await store.add_version(session, policy_id=p.id,
                            rules=_rules(DENY_EXTERNAL), compiler=c)
    await store.activate_version(session, policy_id=p.id, version=1)
    await session.commit()

    v = (await session.execute(
        select(ArbiterPolicyVersionModel)
        .where(ArbiterPolicyVersionModel.policy_id == p.id))).scalar_one()
    assert v.activated_at is not None
    assert v.deactivated_at is None


async def test_a_new_version_closes_the_previous_interval(session, tenant):
    c = PolicyCompiler(VELARIS_CATALOG)
    p = await store.create_policy(session, tenant_id=tenant.id,
                                  name="no-external", namespace="ai")
    await store.add_version(session, policy_id=p.id,
                            rules=_rules(DENY_EXTERNAL), compiler=c)
    await store.activate_version(session, policy_id=p.id, version=1)
    await store.add_version(session, policy_id=p.id,
                            rules=_rules(ALLOW_EXTERNAL), compiler=c)
    await store.activate_version(session, policy_id=p.id, version=2)
    await session.commit()

    rows = {v.version: v for v in (await session.execute(
        select(ArbiterPolicyVersionModel)
        .where(ArbiterPolicyVersionModel.policy_id == p.id))).scalars()}
    assert rows[1].deactivated_at is not None, "two versions must never look live at once"
    assert rows[2].deactivated_at is None


async def test_version_active_at_answers_the_3_march_question(session, tenant):
    c = PolicyCompiler(VELARIS_CATALOG)
    p = await store.create_policy(session, tenant_id=tenant.id,
                                  name="no-external", namespace="ai")
    await store.add_version(session, policy_id=p.id,
                            rules=_rules(DENY_EXTERNAL), compiler=c)
    await store.activate_version(session, policy_id=p.id, version=1)
    await session.commit()
    t1 = datetime.now(timezone.utc)

    await store.add_version(session, policy_id=p.id,
                            rules=_rules(ALLOW_EXTERNAL), compiler=c)
    await store.activate_version(session, policy_id=p.id, version=2)
    await session.commit()

    was = await store.version_active_at(session, policy_id=p.id, moment=t1)
    assert was is not None and was.version == 1

    now = await store.version_active_at(
        session, policy_id=p.id, moment=datetime.now(timezone.utc))
    assert now.version == 2

    before = await store.version_active_at(
        session, policy_id=p.id,
        moment=datetime.now(timezone.utc) - timedelta(days=365))
    assert before is None, "a version cannot have been active before it existed"


async def test_retiring_closes_the_interval_and_starts_the_soak(session, tenant):
    c = PolicyCompiler(VELARIS_CATALOG)
    p = await store.create_policy(session, tenant_id=tenant.id,
                                  name="no-external", namespace="ai")
    await store.add_version(session, policy_id=p.id,
                            rules=_rules(DENY_EXTERNAL), compiler=c)
    await store.activate_version(session, policy_id=p.id, version=1)
    await store.retire_policy(session, policy_id=p.id)
    await session.commit()

    pol = (await session.execute(
        select(ArbiterPolicyModel).where(ArbiterPolicyModel.id == p.id))).scalar_one()
    assert pol.status == "retired"
    assert pol.retired_at is not None
    v = (await session.execute(
        select(ArbiterPolicyVersionModel)
        .where(ArbiterPolicyVersionModel.policy_id == p.id))).scalar_one()
    assert v.deactivated_at is not None


async def test_a_version_that_does_not_compile_is_never_stored(session, tenant):
    from case_service.hxarbiter import CompileError
    c = PolicyCompiler(VELARIS_CATALOG)
    p = await store.create_policy(session, tenant_id=tenant.id,
                                  name="typo", namespace="ai")
    with pytest.raises(CompileError):
        await store.add_version(session, policy_id=p.id, compiler=c, rules=_rules({
            "id": "r1", "effect": "deny",
            "condition": {"attr": "tenant.regoin", "op": "eq", "value": "eu"}}))


async def test_two_platform_policies_cannot_share_a_name(session, tenant):
    """`tenant_key` is what makes this refusable at all.

    A UNIQUE over the nullable `tenant_id` would not constrain platform names,
    because NULL <> NULL — proven against a live PostgreSQL, which accepted
    three identical ones.

    The refusal arrives at the FLUSH inside `create_policy`, not at commit, and
    a session left holding a failed flush poisons every test after it. Rolling
    back here is part of the test, not tidying.
    """
    from sqlalchemy.exc import IntegrityError

    await store.create_policy(session, tenant_id=None,
                              name="baseline", namespace="ai")
    await session.commit()
    try:
        with pytest.raises(IntegrityError):
            await store.create_policy(session, tenant_id=None,
                                      name="baseline", namespace="ai")
    finally:
        await session.rollback()


async def test_a_tenant_may_reuse_a_platform_policy_name(session, tenant):
    await store.create_policy(session, tenant_id=None,
                              name="baseline", namespace="ai")
    await store.create_policy(session, tenant_id=tenant.id,
                              name="baseline", namespace="ai")
    await session.commit()   # must not raise


async def test_generation_moves_when_policy_changes(session, tenant):
    before = await store.policy_generation(session)
    await store.create_policy(session, tenant_id=tenant.id,
                              name="p", namespace="ai")
    await session.commit()
    assert await store.policy_generation(session) != before


# ── pdp: every failure is an answer, never an exception ──────────────────────


async def test_an_unresolvable_tenant_is_denied_not_raised(session, tenant):
    d = await pdp.decide(session, _req("no-such-tenant"), record=False)
    assert d.effect is Effect.DENY
    assert d.error == "tenant_unresolved"
    assert d.enforced is False


async def test_namespace_default_applies_with_no_policies(session, tenant):
    d = await pdp.decide(session, _req(tenant.slug))
    assert d.effect is Effect.DENY          # ai.* defaults deny
    assert d.default_applied is True
    d2 = await pdp.decide(session, _req(tenant.slug, action="case.stage_transition"))
    assert d2.effect is Effect.ALLOW        # case.* defaults allow


async def test_an_enforce_policy_is_still_not_binding_under_the_shadow_clamp(
        session, tenant):
    """The whole reason ARB-A is safe on a running install."""
    c = PolicyCompiler(VELARIS_CATALOG)
    p = await store.create_policy(session, tenant_id=tenant.id,
                                  name="no-external", namespace="ai")
    await store.add_version(session, policy_id=p.id, rules=_rules(DENY_EXTERNAL),
                            compiler=c, mode=PolicyMode.ENFORCE)
    await store.activate_version(session, policy_id=p.id, version=1)
    await session.commit()
    pdp.invalidate_cache()

    d = await pdp.decide(session, _req(tenant.slug))
    assert d.effect is Effect.DENY
    assert d.enforced is False, "every namespace ships clamped to shadow"
    assert d.default_applied is False


async def test_a_platform_deny_beats_a_tenant_allow_through_the_pdp(session, tenant):
    c = PolicyCompiler(VELARIS_CATALOG)
    plat = await store.create_policy(session, tenant_id=None,
                                     name="baseline", namespace="ai")
    await store.add_version(session, policy_id=plat.id,
                            rules=_rules(DENY_EXTERNAL), compiler=c)
    await store.activate_version(session, policy_id=plat.id, version=1)

    ten = await store.create_policy(session, tenant_id=tenant.id,
                                    name="local-exception", namespace="ai")
    await store.add_version(session, policy_id=ten.id,
                            rules=_rules(ALLOW_EXTERNAL), compiler=c)
    await store.activate_version(session, policy_id=ten.id, version=1)
    await session.commit()
    pdp.invalidate_cache()

    d = await pdp.decide(session, _req(tenant.slug))
    assert d.effect is Effect.DENY
    assert [m.rule_id for m in d.matched] == ["r1"]


async def test_a_decision_is_recorded_with_its_tenant_resolved(session, tenant):
    await pdp.decide(session, _req(tenant.slug))       # called with the SLUG
    await session.commit()
    row = (await session.execute(select(ArbiterDecisionModel))).scalar_one()
    assert row.tenant_id == tenant.id                  # stored as the UUID
    assert row.mode == "shadow"
    assert row.enforced is False
    assert row.policy_set_hash is not None
    assert row.subject_id == "u-1"


async def test_the_context_is_redacted_before_it_is_logged(session, tenant):
    req = _req(tenant.slug)
    req.context["prompt"] = "the claimant's full medical history"
    await pdp.decide(session, req)
    await session.commit()
    row = (await session.execute(select(ArbiterDecisionModel))).scalar_one()
    assert row.context["prompt"] == "[redacted]"
    assert row.context["provider_class"] == "external"


async def test_a_hanging_pip_times_out_and_denies(session, tenant):
    c = PolicyCompiler(VELARIS_CATALOG)
    p = await store.create_policy(session, tenant_id=tenant.id,
                                  name="eu-only", namespace="ai")
    await store.add_version(session, policy_id=p.id, compiler=c, rules=_rules({
        "id": "r1", "effect": "allow",
        "condition": {"attr": "tenant.region", "op": "eq", "value": "eu-west"}}))
    await store.activate_version(session, policy_id=p.id, version=1)
    await session.commit()
    pdp.invalidate_cache()

    class Hanging:
        prefix = "tenant"
        async def resolve(self, req, paths):
            await asyncio.sleep(5)
            return {"tenant.region": "eu-west"}

    d = await pdp.decide(session, _req(tenant.slug), resolvers=[Hanging()])
    assert d.error == "pip_timeout"
    assert d.effect is Effect.DENY, "an unresolved attribute must not allow"
    assert d.default_applied is True


async def test_a_raising_pip_does_not_take_down_the_decision(session, tenant):
    c = PolicyCompiler(VELARIS_CATALOG)
    p = await store.create_policy(session, tenant_id=tenant.id,
                                  name="eu-only", namespace="ai")
    await store.add_version(session, policy_id=p.id, compiler=c, rules=_rules({
        "id": "r1", "effect": "allow",
        "condition": {"attr": "tenant.region", "op": "eq", "value": "eu-west"}}))
    await store.activate_version(session, policy_id=p.id, version=1)
    await session.commit()
    pdp.invalidate_cache()

    class Broken:
        prefix = "tenant"
        async def resolve(self, req, paths):
            raise RuntimeError("HxGlobal is down")

    d = await pdp.decide(session, _req(tenant.slug), resolvers=[Broken()])
    assert d.error == "pip_error"
    assert d.effect is Effect.DENY


async def test_a_resolved_attribute_reaches_the_evaluator(session, tenant):
    c = PolicyCompiler(VELARIS_CATALOG)
    p = await store.create_policy(session, tenant_id=tenant.id,
                                  name="eu-only", namespace="ai")
    await store.add_version(session, policy_id=p.id, compiler=c, rules=_rules({
        "id": "r1", "effect": "allow",
        "condition": {"attr": "tenant.region", "op": "eq", "value": "eu-west"}}))
    await store.activate_version(session, policy_id=p.id, version=1)
    await session.commit()
    pdp.invalidate_cache()

    d = await pdp.decide(
        session, _req(tenant.slug),
        resolvers=[StaticResolver("tenant", {"tenant.region": "eu-west"})])
    assert d.effect is Effect.ALLOW
    assert d.error is None


async def test_an_unavailable_policy_store_denies_and_never_raises(
        session, tenant, monkeypatch):
    async def dead(_session):
        return ""            # the sentinel for "freshness unknown"
    monkeypatch.setattr(store, "policy_generation", dead)
    pdp.invalidate_cache()

    d = await pdp.decide(session, _req(tenant.slug), record=False)
    assert d.effect is Effect.DENY
    assert d.error == "policy_store_unavailable"


async def test_a_failed_decision_write_does_not_deny_an_allowed_request(
        session, tenant, monkeypatch):
    """Losing evidence is bad. Denying a request policy allowed is worse."""
    async def boom(*a, **k):
        raise RuntimeError("disk full")
    monkeypatch.setattr(store, "write_decision", boom)

    d = await pdp.decide(session, _req(tenant.slug, action="case.stage_transition"))
    assert d.effect is Effect.ALLOW
    assert d.decision_id is None


async def test_sampling_never_discards_a_deny(session, tenant):
    await session.execute(
        ArbiterNamespaceModel.__table__.update()
        .where(ArbiterNamespaceModel.namespace == "ai")
        .values(sample_allow_rate=0, sample_default_rate=0))
    await session.commit()
    pdp.invalidate_cache()

    await pdp.decide(session, _req(tenant.slug))          # deny by default
    await session.commit()
    rows = (await session.execute(select(ArbiterDecisionModel))).scalars().all()
    assert len(rows) == 1, "a deny is never sampled away"


async def test_sampling_can_discard_an_uninteresting_allow(session, tenant):
    await session.execute(
        ArbiterNamespaceModel.__table__.update()
        .where(ArbiterNamespaceModel.namespace == "case")
        .values(sample_allow_rate=0, sample_default_rate=0))
    await session.commit()
    pdp.invalidate_cache()

    await pdp.decide(session, _req(tenant.slug, action="case.stage_transition"))
    await session.commit()
    rows = (await session.execute(select(ArbiterDecisionModel))).scalars().all()
    assert rows == []


async def test_a_disagreement_with_the_legacy_control_is_always_kept(session, tenant):
    await session.execute(
        ArbiterNamespaceModel.__table__.update()
        .where(ArbiterNamespaceModel.namespace == "case")
        .values(sample_allow_rate=0, sample_default_rate=0))
    await session.commit()
    pdp.invalidate_cache()

    await pdp.decide(session, _req(tenant.slug, action="case.stage_transition"),
                     legacy_effect=Effect.DENY)
    await session.commit()
    row = (await session.execute(select(ArbiterDecisionModel))).scalar_one()
    assert row.legacy_effect == "deny"
    assert row.effect == "allow"


async def test_the_cache_notices_a_policy_added_elsewhere(session, tenant):
    """No writer bumps anything; the generation is derived from the rows."""
    first = await pdp.decide(session, _req(tenant.slug))
    assert first.default_applied is True

    c = PolicyCompiler(VELARIS_CATALOG)
    p = await store.create_policy(session, tenant_id=tenant.id,
                                  name="no-external", namespace="ai")
    await store.add_version(session, policy_id=p.id,
                            rules=_rules(DENY_EXTERNAL), compiler=c)
    await store.activate_version(session, policy_id=p.id, version=1)
    await session.commit()
    # deliberately NOT calling invalidate_cache()

    second = await pdp.decide(session, _req(tenant.slug))
    assert second.default_applied is False
    assert [m.rule_id for m in second.matched] == ["r1"]
