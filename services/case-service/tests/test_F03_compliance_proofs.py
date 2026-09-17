"""F-03 — counterfactual compliance proofs.

A proof is only worth anything if it is **re-derivable** and **tamper-evident**,
so those are what the tests are about:

* the same cohort and rules must give the **same content address**, twice;
* editing a decision the proof was taken over must make `inputs_unchanged`
  false — otherwise the proof is about inputs that no longer exist;
* rewriting the policy version must make `rules_unchanged` false;
* editing the stored document must break its own address;
* and a **refusal must be stored**, because "we could not prove this" is an
  answer an auditor keeps.

The refusals are the other half. A proof that silently sampled, or that replayed
rules reading attributes the log never recorded, would produce a confident
number that is wrong in the most dangerous direction — "nothing would change".

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
    ArbiterNamespaceModel,
    ComplianceProofModel,
    TenantModel,
)

from case_service.hxarbiter import PolicyCompiler, PolicyMode, Rule, VELARIS_CATALOG
from case_service.hxarbiter import pdp, store
from case_service.proofs import engine

pytestmark = pytest.mark.asyncio

PREFIX = "/api/v1/proofs"

SEED = [("ai", "deny"), ("egress", "deny"), ("export", "deny"),
        ("vault", "deny"), ("case", "allow"), ("auth", "allow"),
        ("agent", "deny")]

#: Reads only `context.*`, so the decision log can reconstruct it.
DENY_EXTERNAL = {"attr": "context.provider_class", "op": "eq", "value": "external"}
#: Reads a PIP-resolved path the log never stored — the refusal case.
DENY_EU = {"attr": "tenant.region", "op": "eq", "value": "eu-west"}


@pytest_asyncio.fixture(autouse=True)
async def tenant(session):
    from case_service.tenancy import identity as _identity

    _identity.clear_cache()
    pdp.invalidate_cache()
    for ns, eff in SEED:
        session.add(ArbiterNamespaceModel(
            namespace=ns, default_effect=eff, max_mode="shadow", description="",
            sample_allow_rate=1, sample_default_rate=1,
            updated_at=datetime.now(timezone.utc)))
    t = TenantModel(id=uuid.uuid4(), slug="default", name="Default")
    session.add(t)
    await session.commit()
    yield t
    _identity.clear_cache()
    pdp.invalidate_cache()


async def _policy(session, tenant, rules, *, namespace="ai", name=None):
    p = await store.create_policy(
        session, tenant_id=tenant.id,
        name=name or f"p-{uuid.uuid4().hex[:8]}", namespace=namespace)
    await store.add_version(
        session, policy_id=p.id,
        rules=[Rule.model_validate(r) for r in rules],
        compiler=PolicyCompiler(VELARIS_CATALOG), mode=PolicyMode.SHADOW)
    await session.commit()
    return p


async def _decision(session, tenant, *, effect="allow", provider_class="external",
                    namespace="ai", resource_kind="case_content"):
    row = ArbiterDecisionModel(
        id=uuid.uuid4(), tenant_id=tenant.id, namespace=namespace,
        action=f"{namespace}.external_call",
        subject_kind="agent", subject_id=str(uuid.uuid4()),
        subject={"kind": "agent", "id": "a", "attrs": {}},
        resource={"kind": resource_kind, "id": str(uuid.uuid4()), "attrs": {}},
        context={"provider_class": provider_class},
        effect=effect, mode="shadow", enforced=False, default_applied=False,
        matched=[], obligations=[], simulated=False,
        evaluated_at=datetime.now(timezone.utc) - timedelta(minutes=5))
    session.add(row)
    return row


async def _generate(session, tenant, policy, **kw):
    proof = await engine.generate(
        session, policy_id=policy.id, policy_version=1,
        catalog=VELARIS_CATALOG, tenant_id=kw.pop("tenant_id", tenant.id), **kw)
    await session.commit()
    return proof


# ══ the finding ═════════════════════════════════════════════════════════════


async def test_a_proof_counts_the_whole_cohort(session, tenant):
    """'0 of 12,844' is the claim; the count has to be exact."""
    policy = await _policy(session, tenant, [
        {"id": "r1", "effect": "deny", "condition": DENY_EXTERNAL}])
    # 4 external calls recorded as ALLOW — the rule matches and disagrees.
    for _ in range(4):
        await _decision(session, tenant, effect="allow", provider_class="external")
    # 2 external calls already recorded as DENY — matches and agrees.
    for _ in range(2):
        await _decision(session, tenant, effect="deny", provider_class="external")
    # 3 local calls — the rule says nothing about them.
    for _ in range(3):
        await _decision(session, tenant, effect="allow", provider_class="local")
    await session.commit()

    proof = await _generate(session, tenant, policy)
    assert proof.status == "complete"
    assert proof.result["cohort_size"] == 9
    assert proof.result["would_change_to_deny"] == 4
    assert proof.result["unchanged"] == 2
    assert proof.result["no_opinion"] == 3
    assert proof.result["matched"] == 6
    assert "4 of 9" in proof.result["statement"]


async def test_a_policy_that_says_nothing_has_changed_nothing(session, tenant):
    """The distinction that decides whether a proof is useful.

    The candidate is evaluated alone, so an unmatched decision falls through to
    the namespace default — `deny` in every sovereignty-bearing namespace.
    Counting those as divergent would make the headline number equal the cohort
    size for any policy about one narrow thing, which is a true statement about
    a hypothetical nobody asked about.
    """
    policy = await _policy(session, tenant, [
        {"id": "r1", "effect": "deny", "condition": DENY_EXTERNAL}])
    for _ in range(5):
        await _decision(session, tenant, effect="allow", provider_class="local")
    await session.commit()

    proof = await _generate(session, tenant, policy)
    assert proof.result["would_change"] == 0
    assert proof.result["no_opinion"] == 5
    assert proof.result["matched"] == 0
    assert proof.divergent == []


async def test_the_divergent_list_names_the_decisions(session, tenant):
    policy = await _policy(session, tenant, [
        {"id": "r1", "effect": "deny", "condition": DENY_EXTERNAL}])
    row = await _decision(session, tenant, effect="allow", provider_class="external")
    await session.commit()

    proof = await _generate(session, tenant, policy)
    assert len(proof.divergent) == 1
    assert proof.divergent[0]["decision_id"] == str(row.id)
    assert proof.divergent[0]["was"] == "allow"
    assert proof.divergent[0]["would_be"] == "deny"


# ══ refusals ════════════════════════════════════════════════════════════════


async def test_rules_reading_unrecorded_attributes_are_refused(session, tenant):
    """The most dangerous possible output is a confident 'nothing would change'
    for a rule that changes a great deal."""
    policy = await _policy(session, tenant, [
        {"id": "r1", "effect": "deny", "condition": DENY_EU}])
    await _decision(session, tenant)
    await session.commit()

    proof = await _generate(session, tenant, policy)
    assert proof.status == "refused"
    assert proof.result["refused"] == "attributes_not_recorded"
    assert "tenant.region" in proof.cohort["unreplayable_attributes"]


async def test_a_refusal_is_stored_not_discarded(session, tenant):
    """'We could not prove this, and here is why' is an answer worth keeping;
    returning nothing invites the question being asked until it accidentally
    succeeds."""
    policy = await _policy(session, tenant, [
        {"id": "r1", "effect": "deny", "condition": DENY_EU}])
    await session.commit()
    proof = await _generate(session, tenant, policy)

    rows = (await session.execute(select(ComplianceProofModel))).scalars().all()
    assert len(rows) == 1
    assert rows[0].id == proof.id
    assert rows[0].refusal_reason


async def test_an_empty_cohort_is_refused_not_reported_as_clean(session, tenant):
    """'Nothing would change' over zero decisions is evidence nothing has been
    asked of the policy, not evidence it is harmless."""
    policy = await _policy(session, tenant, [
        {"id": "r1", "effect": "deny", "condition": DENY_EXTERNAL}])
    proof = await _generate(session, tenant, policy)
    assert proof.status == "refused"
    assert proof.result["refused"] == "empty_cohort"


async def test_an_oversized_cohort_is_refused_not_sampled(session, tenant, monkeypatch):
    """A sampled result cannot support a statement of the form '0 of N'."""
    monkeypatch.setattr(engine, "MAX_COHORT", 2)
    policy = await _policy(session, tenant, [
        {"id": "r1", "effect": "deny", "condition": DENY_EXTERNAL}])
    for _ in range(3):
        await _decision(session, tenant)
    await session.commit()

    proof = await _generate(session, tenant, policy)
    assert proof.status == "refused"
    assert proof.result["refused"] == "cohort_too_large"
    assert proof.cohort["cohort_size"] == 3


async def test_a_small_platform_cohort_is_refused_for_k_anonymity(
        session, tenant, monkeypatch):
    """A cross-tenant cohort this small identifies a tenant by arithmetic."""
    monkeypatch.setattr(engine, "K_ANONYMITY_FLOOR", 10)
    policy = await _policy(session, tenant, [
        {"id": "r1", "effect": "deny", "condition": DENY_EXTERNAL}])
    for _ in range(3):
        await _decision(session, tenant)
    await session.commit()

    proof = await _generate(session, tenant, policy, tenant_id=None)
    assert proof.status == "refused"
    assert proof.result["refused"] == "below_k_anonymity"


async def test_a_platform_proof_carries_no_divergent_list(
        session, tenant, monkeypatch):
    """The distribution is the whole permitted output of a cross-tenant proof."""
    monkeypatch.setattr(engine, "K_ANONYMITY_FLOOR", 1)
    policy = await _policy(session, tenant, [
        {"id": "r1", "effect": "deny", "condition": DENY_EXTERNAL}])
    for _ in range(3):
        await _decision(session, tenant)
    await session.commit()

    proof = await _generate(session, tenant, policy, tenant_id=None)
    assert proof.status == "complete"
    assert proof.aggregate_only is True
    assert proof.divergent == []
    assert proof.result["would_change_to_deny"] == 3


# ══ re-derivation ═══════════════════════════════════════════════════════════


async def test_the_same_cohort_and_rules_give_the_same_address(session, tenant):
    """Determinism. Two proofs over identical inputs must be the same document
    — the content address differs only through `prev_hash`, which is what makes
    it a chain."""
    policy = await _policy(session, tenant, [
        {"id": "r1", "effect": "deny", "condition": DENY_EXTERNAL}])
    for _ in range(3):
        await _decision(session, tenant)
    await session.commit()

    first = await _generate(session, tenant, policy)
    second = await _generate(session, tenant, policy)

    assert first.inputs_sha256 == second.inputs_sha256
    assert first.rules_sha256 == second.rules_sha256
    assert first.result == second.result
    # Different addresses, because the second links to the first.
    assert first.content_sha256 != second.content_sha256
    assert second.prev_hash == first.content_sha256


async def test_a_fresh_proof_verifies_on_every_finding(session, tenant):
    policy = await _policy(session, tenant, [
        {"id": "r1", "effect": "deny", "condition": DENY_EXTERNAL}])
    for _ in range(3):
        await _decision(session, tenant)
    await session.commit()
    proof = await _generate(session, tenant, policy)

    out = await engine.verify(session, proof, catalog=VELARIS_CATALOG)
    assert out["content_address_holds"] is True
    assert out["chain_links"] is True
    assert out["rules_unchanged"] is True
    assert out["inputs_unchanged"] is True
    assert out["sample_agreed"] is True


async def test_editing_a_replayed_decision_is_detected(session, tenant):
    """Otherwise the proof is about inputs that no longer exist."""
    policy = await _policy(session, tenant, [
        {"id": "r1", "effect": "deny", "condition": DENY_EXTERNAL}])
    row = await _decision(session, tenant)
    await session.commit()
    proof = await _generate(session, tenant, policy)
    assert (await engine.verify(session, proof,
                                catalog=VELARIS_CATALOG))["inputs_unchanged"]

    await session.execute(
        ArbiterDecisionModel.__table__.update()
        .where(ArbiterDecisionModel.id == row.id)
        .values(context={"provider_class": "local"}))
    await session.commit()

    out = await engine.verify(session, proof, catalog=VELARIS_CATALOG)
    assert out["inputs_unchanged"] is False
    # The document itself is untouched, and says so — two different findings.
    assert out["content_address_holds"] is True


async def test_rewriting_the_policy_version_is_detected(session, tenant):
    from velaris_shared.db.models import ArbiterPolicyVersionModel

    policy = await _policy(session, tenant, [
        {"id": "r1", "effect": "deny", "condition": DENY_EXTERNAL}])
    await _decision(session, tenant)
    await session.commit()
    proof = await _generate(session, tenant, policy)

    await session.execute(
        ArbiterPolicyVersionModel.__table__.update()
        .where(ArbiterPolicyVersionModel.policy_id == policy.id)
        .values(rules=[{"id": "r1", "effect": "allow",
                        "condition": DENY_EXTERNAL}]))
    await session.commit()

    out = await engine.verify(session, proof, catalog=VELARIS_CATALOG)
    assert out["rules_unchanged"] is False


async def test_editing_the_stored_proof_breaks_its_own_address(session, tenant):
    policy = await _policy(session, tenant, [
        {"id": "r1", "effect": "deny", "condition": DENY_EXTERNAL}])
    await _decision(session, tenant)
    await session.commit()
    proof = await _generate(session, tenant, policy)

    await session.execute(
        ComplianceProofModel.__table__.update()
        .where(ComplianceProofModel.id == proof.id)
        .values(result={**proof.result, "would_change_to_deny": 0}))
    await session.commit()
    await session.refresh(proof)

    out = await engine.verify(session, proof, catalog=VELARIS_CATALOG)
    assert out["content_address_holds"] is False


async def test_removing_a_proof_breaks_the_chain_after_it(session, tenant):
    """The reason proofs are chained at all: a result that turns out
    inconvenient must not be quietly deletable."""
    policy = await _policy(session, tenant, [
        {"id": "r1", "effect": "deny", "condition": DENY_EXTERNAL}])
    await _decision(session, tenant)
    await session.commit()
    first = await _generate(session, tenant, policy)
    second = await _generate(session, tenant, policy)

    await session.execute(
        ComplianceProofModel.__table__.delete()
        .where(ComplianceProofModel.id == first.id))
    await session.commit()

    out = await engine.verify(session, second, catalog=VELARIS_CATALOG)
    assert out["chain_links"] is False


async def test_the_rules_digest_ignores_rule_order(session, tenant):
    """The same policy written in a different order is the same policy; a
    digest that disagreed would make two identical proofs look different."""
    a = [Rule.model_validate({"id": "r1", "effect": "deny", "condition": DENY_EXTERNAL}),
         Rule.model_validate({"id": "r2", "effect": "allow", "condition": DENY_EXTERNAL})]
    assert engine.rules_digest(a) == engine.rules_digest(list(reversed(a)))


# ══ the API ═════════════════════════════════════════════════════════════════


def _headers(roles: list[str]) -> dict[str, str]:
    from velaris_shared.auth.jwt_handler import create_dev_token
    from velaris_shared.config import get_settings

    s = get_settings()
    token = create_dev_token(
        user_id=str(uuid.uuid4()), username="proof-user", roles=roles,
        secret=s.auth_secret, private_key=s.auth_rsa_private_key or "")
    return {"Authorization": f"Bearer {token}"}


async def test_every_route_refuses_an_anonymous_caller(anon_client, tenant):
    from case_service.api.routers.proofs import router

    for route in router.routes:
        path = route.path.replace("{proof_id}", str(uuid.uuid4()))
        for method in sorted(route.methods):
            r = await anon_client.request(method, f"/api/v1{path}", json={})
            assert r.status_code in (401, 403), f"{method} {path} -> {r.status_code}"


async def test_a_viewer_cannot_generate_a_proof(client, tenant):
    r = await client.post(PREFIX, headers=_headers(["viewer"]),
                          json={"policy_id": str(uuid.uuid4()), "policy_version": 1})
    assert r.status_code == 403


async def test_a_tenant_admin_cannot_generate_a_platform_proof(client, tenant):
    r = await client.post(PREFIX, headers=_headers(["admin"]),
                          json={"policy_id": str(uuid.uuid4()),
                                "policy_version": 1, "platform": True})
    assert r.status_code == 403
    assert "PLATFORM-SCOPE" in r.json()["detail"]


async def test_generating_and_verifying_through_the_api(client, session, tenant):
    policy = await _policy(session, tenant, [
        {"id": "r1", "effect": "deny", "condition": DENY_EXTERNAL}])
    for _ in range(3):
        await _decision(session, tenant)
    await session.commit()
    h = _headers(["admin"])

    r = await client.post(PREFIX, headers=h,
                          json={"policy_id": str(policy.id), "policy_version": 1})
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["status"] == "complete"
    assert body["result"]["would_change_to_deny"] == 3
    proof_id = body["id"]

    r = await client.post(f"{PREFIX}/{proof_id}/verify", headers=h)
    assert r.status_code == 200, r.text
    assert r.json()["verified"] is True


async def test_a_refusal_is_a_201_with_a_reason(client, session, tenant):
    """The caller asked a well-formed question and got an answer. A 4xx would
    say they got the request wrong."""
    policy = await _policy(session, tenant, [
        {"id": "r1", "effect": "deny", "condition": DENY_EU}])
    await session.commit()

    r = await client.post(PREFIX, headers=_headers(["admin"]),
                          json={"policy_id": str(policy.id), "policy_version": 1})
    assert r.status_code == 201
    assert r.json()["status"] == "refused"
    assert r.json()["refusal_reason"]


async def test_a_missing_policy_version_is_422(client, session, tenant):
    policy = await _policy(session, tenant, [
        {"id": "r1", "effect": "deny", "condition": DENY_EXTERNAL}])
    await session.commit()
    r = await client.post(PREFIX, headers=_headers(["admin"]),
                          json={"policy_id": str(policy.id), "policy_version": 9})
    assert r.status_code == 422


async def test_another_tenants_proof_is_404(client, session, tenant):
    other = TenantModel(id=uuid.uuid4(), slug="other", name="Other")
    session.add(other)
    await session.commit()
    policy = await _policy(session, tenant, [
        {"id": "r1", "effect": "deny", "condition": DENY_EXTERNAL}])
    await _decision(session, tenant)
    await session.commit()
    # generated against the OTHER tenant, so this tenant must not see it
    proof = await engine.generate(
        session, policy_id=policy.id, policy_version=1,
        catalog=VELARIS_CATALOG, tenant_id=other.id)
    await session.commit()

    r = await client.get(f"{PREFIX}/{proof.id}", headers=_headers(["admin"]))
    assert r.status_code == 404


async def test_unknown_fields_are_refused(client, tenant):
    r = await client.post(PREFIX, headers=_headers(["admin"]),
                          json={"policy_id": str(uuid.uuid4()),
                                "policy_version": 1, "dayz": 30})
    assert r.status_code == 422
