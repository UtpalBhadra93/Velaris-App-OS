"""ARB-B…D — the five enforcement points, exercised through their real callers.

Written after a live audit on 2026-08-29 found that **three of the five PEPs
had no test at all and had never fired anywhere**: `VEL-ARB-STAGE-DENIED-403`,
`VEL-ARB-EXPORT-DENIED-403` and `VEL-ARB-VAULT-SHRED-DENIED-403` appeared in zero
tests, and the decision log on the live box had never recorded a decision in
the `export`, `vault` or `case` namespaces.

That is the worst shape a control can be in. Those three lines are what will
refuse live traffic the day an operator promotes a namespace, and not one of
them had ever been executed — so the first time they ran would have been in
production, on a refusal.

The file therefore does the one thing the existing tests did not: it **promotes
the namespace** and proves each refusal actually reaches the caller, in the
caller's own idiom (a `PermissionError` here, a 403 there), and that the
un-promoted case still changes nothing.

Also pins three defects the same audit found and fixed:

* the DSAR export PEP read `session.tenant_id`, which does not exist, so every
  tenant's export was evaluated and logged as the platform-default tenant;
* no PEP passed `claimed_by`, leaving the column that exists to answer "who
  asserted this subject?" empty on exactly the rows it is for;
* decision-log redaction matched key NAMES only, so PII inside a free-text
  value was written verbatim into an append-only, hash-chained log.

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
    ArbiterDecisionModel,
    ArbiterNamespaceModel,
    TenantModel,
)

from case_service.hxarbiter import (
    PolicyCompiler,
    PolicyMode,
    Rule,
    VELARIS_CATALOG,
)
from case_service.hxarbiter import pdp, pep, store

pytestmark = pytest.mark.asyncio

PREFIX_ARB = "/api/v1/arbiter"

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
    t = TenantModel(id=uuid.uuid4(), slug="default", name="Default")
    session.add(t)
    await session.commit()
    yield t
    pdp.invalidate_cache()
    _identity.clear_cache()


async def _deny_everything(session, tenant, namespace: str, action_attr="subject.kind"):
    """A policy that denies every request in the namespace, then promote it."""
    p = await store.create_policy(
        session, tenant_id=tenant.id, name=f"e2e-deny-{namespace}",
        namespace=namespace)
    await store.add_version(
        session, policy_id=p.id, compiler=PolicyCompiler(VELARIS_CATALOG),
        mode=PolicyMode.ENFORCE,
        rules=[Rule.model_validate({
            "id": "deny-all", "effect": "deny",
            "condition": {"attr": action_attr, "op": "is_present"}})])
    await store.activate_version(session, policy_id=p.id, version=1)
    await store.promote_namespace(session, namespace, PolicyMode.ENFORCE)
    await session.commit()
    pdp.invalidate_cache()
    return p


# ══ the DSAR export PEP (ARB-C) ═════════════════════════════════════════════


async def test_the_export_pep_is_inert_until_the_namespace_is_promoted(
        session, tenant):
    from case_service.enterprise import gdpr

    await _deny_everything(session, tenant, "export")
    await store.promote_namespace(session, "export", PolicyMode.SHADOW)
    await session.commit()
    pdp.invalidate_cache()

    # Shadow: the engine says deny, the caller is unaffected.
    data = await gdpr.export_user_data(session, "someone", tenant_id="default")
    assert data["subject_id"] == "someone"


async def test_a_promoted_export_policy_actually_refuses(session, tenant):
    """The refusal line that had never been executed anywhere."""
    from case_service.enterprise import gdpr

    await _deny_everything(session, tenant, "export")
    with pytest.raises(PermissionError, match="VEL-ARB-EXPORT-DENIED-403"):
        await gdpr.export_user_data(session, "someone", tenant_id="default")


async def test_the_export_pep_uses_the_CALLERS_tenant(session, tenant):
    """It read `session.tenant_id`, which does not exist on an AsyncSession and
    never has — so every tenant's DSAR was evaluated and logged against the
    platform-default tenant. Proven on the live box before the fix: a
    `dev`-tenant admin's export landed under `default`.

    Two consequences, and the second only bites after a promotion: the tenant's
    own `export.*` policy was never consulted, and its parity evidence stayed
    empty while another tenant's log filled with its exports.
    """
    from case_service.enterprise import gdpr

    other = TenantModel(id=uuid.uuid4(), slug="other-tenant", name="Other")
    session.add(other)
    await session.commit()

    await gdpr.export_user_data(session, "someone", tenant_id="other-tenant")
    await session.commit()

    row = (await session.execute(
        select(ArbiterDecisionModel)
        .where(ArbiterDecisionModel.namespace == "export")
        .order_by(ArbiterDecisionModel.evaluated_at.desc())
    )).scalars().first()
    assert row is not None, "the export PEP recorded nothing"
    assert row.tenant_id == other.id, (
        "the DSAR was logged against the wrong tenant — the defect this test "
        "exists for")


# ══ the HxVault crypto-shred PEP (ARB-C) ════════════════════════════════════


async def test_a_promoted_vault_policy_refuses_a_shred(session, tenant):
    """A refused shred must RAISE, never return False: False means "there was
    no key", which is a different and reassuring answer."""
    from case_service.hxvault import keyring

    await _deny_everything(session, tenant, "vault")
    with pytest.raises(PermissionError, match="VEL-ARB-VAULT-SHRED-DENIED-403"):
        await keyring.shred(session, None)


async def test_the_vault_pep_is_inert_in_shadow(session, tenant):
    from case_service.hxvault import keyring

    await _deny_everything(session, tenant, "vault")
    await store.promote_namespace(session, "vault", PolicyMode.SHADOW)
    await session.commit()
    pdp.invalidate_cache()
    # No key exists, so False — the point is that it returned rather than raised.
    assert await keyring.shred(session, None) is False


# ══ the case-stage PEP (ARB-D) ══════════════════════════════════════════════


async def test_a_promoted_case_policy_refuses_a_stage_transition(
        session, tenant, client):
    """Through the real route, which is the only way to prove the PEP is
    reached: it sits after the HxGuard gate and the HxSentinel hold, and a
    unit call on `pep.check` would prove none of that."""
    from tests.conftest import create_case, deploy_case_type

    ct = await deploy_case_type(client)
    case = await create_case(client, ct["id"])
    stages = ct.get("stages") or []
    target = stages[-1]["id"] if stages else "s2"

    await _deny_everything(session, tenant, "case")

    r = await client.post(f"/api/v1/cases/{case['id']}/stage",
                          json={"target_stage_id": target})
    assert r.status_code == 403, r.text
    assert "VEL-ARB-STAGE-DENIED-403" in r.text


async def test_the_stage_pep_changes_nothing_in_shadow(session, tenant, client):
    from tests.conftest import create_case, deploy_case_type

    ct = await deploy_case_type(client)
    case = await create_case(client, ct["id"])
    stages = ct.get("stages") or []
    target = stages[-1]["id"] if stages else "s2"

    await _deny_everything(session, tenant, "case")
    await store.promote_namespace(session, "case", PolicyMode.SHADOW)
    await session.commit()
    pdp.invalidate_cache()

    r = await client.post(f"/api/v1/cases/{case['id']}/stage",
                          json={"target_stage_id": target})
    assert r.status_code == 200, r.text


# ══ attribution and redaction ═══════════════════════════════════════════════


async def test_every_pep_stamps_who_asserted_the_subject(session, tenant):
    """`subject_id` on a PEP row is a service label, not a principal — which
    makes it an assertion, and an assertion nobody signed for is
    unattributable. Every in-process PEP left `claimed_by` None, so the one
    column that answers "who asked?" was empty on exactly the rows a reader
    would ask it about. 10 such rows were found on the live box."""
    from case_service.enterprise import gdpr

    await gdpr.export_user_data(session, "someone", tenant_id="default")
    await session.commit()
    row = (await session.execute(
        select(ArbiterDecisionModel)
        .where(ArbiterDecisionModel.namespace == "export")
    )).scalars().first()
    assert row.claimed_by == "enterprise.gdpr"


async def test_pii_inside_a_free_text_context_value_is_redacted(session, tenant):
    """Key-name redaction alone is half a control. `{"note": "call me on
    07700900123"}` was stored verbatim on the live box because `note` is not a
    listed key — into a table that is append-only and hash-chained, so it
    cannot be scrubbed later without breaking the chain that makes the log
    evidence."""
    await pep.check(
        session, tenant_id="default", action="ai.external_call",
        subject_id="redaction-test",
        context={"provider_class": "external",
                 "note": "call me on 07700900123 or at a.person@example.com"},
        legacy_allowed=True)
    await session.commit()

    row = (await session.execute(
        select(ArbiterDecisionModel)
        .where(ArbiterDecisionModel.subject_id == "redaction-test")
    )).scalars().first()
    assert "07700900123" not in str(row.context)
    assert "a.person@example.com" not in str(row.context)
    # And it did not throw the useful part away.
    assert row.context["provider_class"] == "external"


async def test_redaction_leaves_non_text_values_alone(session, tenant):
    """Coercing everything to a string to scan it would change what the log
    says a decision was made on."""
    from case_service.hxarbiter.pdp import _redact

    out = _redact({"count": 5, "ok": True, "ratio": 0.5, "nothing": None})
    assert out == {"count": 5, "ok": True, "ratio": 0.5, "nothing": None}


async def test_an_oversized_context_value_is_truncated(session, tenant):
    from case_service.hxarbiter.pdp import _redact, MAX_CONTEXT_VALUE_CHARS

    out = _redact({"blob": "z" * (MAX_CONTEXT_VALUE_CHARS + 500)})
    assert out["blob"].endswith("[truncated]")
    assert len(out["blob"]) < MAX_CONTEXT_VALUE_CHARS + 50


# ══ obligations — the "yes, if" half of an allow (§4.5) ═════════════════════


async def _allow_with_obligations(session, tenant, namespace, obligations):
    """An allow that carries obligations, in a promoted namespace."""
    p = await store.create_policy(
        session, tenant_id=tenant.id, name=f"oblig-{namespace}-{uuid.uuid4().hex[:6]}",
        namespace=namespace)
    await store.add_version(
        session, policy_id=p.id, compiler=PolicyCompiler(VELARIS_CATALOG),
        mode=PolicyMode.ENFORCE,
        rules=[Rule.model_validate({
            "id": "allow-with-obligations", "effect": "allow",
            "condition": {"attr": "subject.kind", "op": "is_present"},
            "obligations": obligations})])
    await store.activate_version(session, policy_id=p.id, version=1)
    await store.promote_namespace(session, namespace, PolicyMode.ENFORCE)
    await session.commit()
    pdp.invalidate_cache()
    return p


async def test_an_obligation_nobody_implements_denies(session, tenant):
    """`ObligationKind`'s own docstring: a PEP that receives one it does not
    implement MUST deny. Before this, the evaluator produced obligations, the
    store recorded them, the API returned them — and `check()` handed back a
    bare bool, so every call site silently discarded them. "Allow, but
    pseudonymize" became a plain allow: a rule that reads as a control and is
    not one."""
    await _allow_with_obligations(
        session, tenant, "case", [{"kind": "watermark_export"}])

    allowed = await pep.check(
        session, tenant_id="default", action="case.stage.transition",
        subject_id="u", legacy_allowed=True)
    assert allowed is False, (
        "an allow qualified by an obligation nothing can carry out is not an "
        "allow")


async def test_a_centrally_dischargeable_obligation_allows_and_has_an_effect(
        session, tenant):
    from velaris_shared.db.models import SecurityEventModel

    await _allow_with_obligations(
        session, tenant, "case",
        [{"kind": "record_purpose", "params": {"purpose": "test-purpose"}}])

    allowed = await pep.check(
        session, tenant_id="default", action="case.stage.transition",
        subject_id="u", legacy_allowed=True)
    assert allowed is True
    await session.commit()

    row = (await session.execute(
        select(SecurityEventModel)
        .where(SecurityEventModel.event_type == "arbiter_obligation")
    )).scalars().first()
    assert row is not None, "the obligation allowed the request and did nothing"
    assert row.details["purpose"] == "test-purpose"


async def test_shadow_never_discharges_an_obligation(session, tenant):
    """The one thing shadow must not do is have effects. A caller handed
    instructions from a decision that is not in force would act on a policy
    nobody promoted."""
    from velaris_shared.db.models import SecurityEventModel

    await _allow_with_obligations(
        session, tenant, "case", [{"kind": "record_purpose"}])
    await store.promote_namespace(session, "case", PolicyMode.SHADOW)
    await session.commit()
    pdp.invalidate_cache()

    allowed, obligations = await pep.check_with_obligations(
        session, tenant_id="default", action="case.stage.transition",
        subject_id="u", legacy_allowed=True)
    assert allowed is True
    assert obligations == []
    await session.commit()
    assert (await session.execute(
        select(SecurityEventModel)
        .where(SecurityEventModel.event_type == "arbiter_obligation")
    )).scalars().first() is None


async def test_a_caller_declared_obligation_is_handed_back_not_discharged(
        session, tenant):
    """`pseudonymize` and `watermark_export` act on the payload the caller is
    about to build; no central handler can reach it. They are returned only to
    a call site that named them."""
    await _allow_with_obligations(
        session, tenant, "export",
        [{"kind": "watermark_export", "params": {"label": "L"}},
         {"kind": "record_purpose"}])

    allowed, mine = await pep.check_with_obligations(
        session, tenant_id="default", action=pep.ACTION_EXPORT_CASE,
        subject_id="u", legacy_allowed=True,
        caller_handles=("watermark_export",))
    assert allowed is True
    # record_purpose was discharged centrally; only the declared kind comes back.
    assert [str(o.kind) for o in mine] == ["watermark_export"]


async def test_the_dsar_export_actually_applies_its_obligations(session, tenant):
    """End to end: the policy says "allow, if you watermark and pseudonymise",
    and the returned document carries both."""
    from case_service.enterprise import gdpr

    await _allow_with_obligations(
        session, tenant, "export",
        [{"kind": "watermark_export", "params": {"label": "DSAR"}},
         {"kind": "pseudonymize"}])

    doc = await gdpr.export_user_data(session, "someone", tenant_id="default")
    assert doc["watermark"]["applied"] is True
    assert doc["watermark"]["label"] == "DSAR"
    assert doc["pseudonymized"] is True
    assert sorted(doc["policy_obligations"]) == ["pseudonymize", "watermark_export"]


async def test_pseudonymize_replaces_identifiers_rather_than_removing_them(
        session, tenant):
    """Removing a key changes the SHAPE of the response, and a caller that
    branches on a missing key behaves differently for a pseudonymised subject —
    which is how a privacy control becomes an oracle."""
    from case_service.enterprise.gdpr import _pseudonymize_export

    out = _pseudonymize_export(
        {"subject_id": "s", "user_profile": [{"user_id": "u",
                                              "email": "a@b.com"}]}, {})
    prof = out["user_profile"][0]
    assert "email" in prof
    assert prof["email"].startswith("pseudo:")
    assert prof["user_id"] == "u"          # the subject id survives — this is
                                           # pseudonymisation, not anonymisation


async def test_pseudonymisation_is_stable_within_an_export(session, tenant):
    from case_service.enterprise.gdpr import _pseudonymize_export

    out = _pseudonymize_export(
        {"subject_id": "s",
         "a": [{"email": "x@y.com"}], "b": [{"email": "x@y.com"}]}, {})
    assert out["a"][0]["email"] == out["b"][0]["email"], (
        "an export where the same person reads differently on every line is "
        "unusable for the purpose a DSAR exists to serve")


async def test_a_handler_that_raises_denies(session, tenant):
    """An obligation that was supposed to run and did not is the same
    situation as one nothing implements."""
    from hxarbiter_core import ObligationKind

    async def broken(session, ob, ctx):
        raise RuntimeError("nope")

    original = pep._HANDLERS.get(str(ObligationKind.NOTIFY))
    pep.register_obligation(str(ObligationKind.NOTIFY), broken)
    try:
        await _allow_with_obligations(
            session, tenant, "case", [{"kind": "notify"}])
        assert await pep.check(
            session, tenant_id="default", action="case.stage.transition",
            subject_id="u", legacy_allowed=True) is False
    finally:
        if original is not None:
            pep.register_obligation(str(ObligationKind.NOTIFY), original)


# ══ the auth step-up PEP (the sixth, wired 2026-08-29) ══════════════════════


async def test_the_auth_namespace_now_has_an_enforcement_point(session, tenant):
    """`auth` was registered in migration 177 and had no PEP, so
    `pep.ACTION_SESSION_ELEVATE` was a constant with no caller and §11's "auth
    step-up" was a promise the code did not keep.

    The platform's real elevation is HxDB Manager's `reauth`: fifteen minutes
    of read access to account numbers and IBANs, unlocked by a password. A
    tenant can now say "not from outside our region" — something a role cannot
    express.
    """
    await _deny_everything(session, tenant, "auth")

    allowed = await pep.check(
        session, tenant_id="default", action=pep.ACTION_SESSION_ELEVATE,
        subject_id="u", subject_kind=pep.SubjectKind.USER,
        resource_kind="session", legacy_allowed=True)
    assert allowed is False


async def test_the_auth_namespace_defaults_to_allow_so_no_policy_means_no_change(
        session, tenant):
    """`auth` defaults to ALLOW, unlike the sovereignty namespaces. An install
    with no policy must see elevation behave exactly as it did before the PEP
    existed — including once somebody promotes the namespace."""
    await store.promote_namespace(session, "auth", PolicyMode.ENFORCE)
    await session.commit()
    pdp.invalidate_cache()

    assert await pep.check(
        session, tenant_id="default", action=pep.ACTION_SESSION_ELEVATE,
        subject_id="u", subject_kind=pep.SubjectKind.USER,
        resource_kind="session", legacy_allowed=True) is True


async def test_a_binding_engine_is_authoritative_and_NG1_comes_from_ordering(
        session, tenant):
    """Where "it can only restrict, never grant" actually comes from.

    Not from `pep.check`: once a namespace is promoted the engine's answer IS
    the answer, including an allow. That is deliberate and is the whole point
    of §8 step 4 — after promotion HxArbiter *replaces* the control it shadowed,
    which is why the bridge mirrors `local_only` as a deny so parity can prove
    the swap is safe first.

    NG1 holds at the enforcement points because of **call-site ordering**: the
    reauth PEP sits after the privilege check and after `bcrypt.checkpw`, both
    of which raise, so a caller whose password was wrong never reaches it. The
    same is true of the case-stage PEP, which sits after the HxGuard gate.

    Pinned here because the two are easy to conflate, and someone adding a PEP
    who assumes the helper enforces NG1 would put it before the gate.
    """
    p = await store.create_policy(
        session, tenant_id=tenant.id, name="allow-all-elevation",
        namespace="auth")
    await store.add_version(
        session, policy_id=p.id, compiler=PolicyCompiler(VELARIS_CATALOG),
        mode=PolicyMode.ENFORCE,
        rules=[Rule.model_validate({
            "id": "yes", "effect": "allow",
            "condition": {"attr": "subject.kind", "op": "is_present"}})])
    await store.activate_version(session, policy_id=p.id, version=1)
    await store.promote_namespace(session, "auth", PolicyMode.ENFORCE)
    await session.commit()
    pdp.invalidate_cache()

    # A binding allow overrides a legacy refusal — by design.
    assert await pep.check(
        session, tenant_id="default", action=pep.ACTION_SESSION_ELEVATE,
        subject_id="u", subject_kind=pep.SubjectKind.USER,
        resource_kind="session", legacy_allowed=False) is True

    # And in shadow it does not, which is what makes promotion the moment of
    # change rather than the wiring.
    await store.promote_namespace(session, "auth", PolicyMode.SHADOW)
    await session.commit()
    pdp.invalidate_cache()
    assert await pep.check(
        session, tenant_id="default", action=pep.ACTION_SESSION_ELEVATE,
        subject_id="u", subject_kind=pep.SubjectKind.USER,
        resource_kind="session", legacy_allowed=False) is False


# ══ obligations through every enforcement point ═════════════════════════════


async def test_an_undischargeable_obligation_refuses_at_the_STAGE_route(
        session, tenant, client):
    """A second real call site, end to end through HTTP.

    Obligations were first proven on the DSAR export. This pins the property at
    a call site with a completely different refusal idiom — the stage route
    raises `HTTPException(403)` where the export path raises `PermissionError`
    — so the guarantee is shown to live in `pep.check`, not in one caller's
    error handling.
    """
    from tests.conftest import create_case, deploy_case_type

    ct = await deploy_case_type(client)
    case = await create_case(client, ct["id"])
    target = (ct.get("stages") or [{}])[-1].get("id", "s2")

    await _allow_with_obligations(
        session, tenant, "case", [{"kind": "watermark_export"}])

    r = await client.post(f"/api/v1/cases/{case['id']}/stage",
                          json={"target_stage_id": target})
    assert r.status_code == 403, r.text
    assert "VEL-ARB-STAGE-DENIED-403" in r.text


async def test_pep_check_refuses_an_undischargeable_obligation_in_every_namespace(
        session, tenant):
    """The guarantee is in the shared helper, so it holds for all six PEPs.

    Asserted per namespace because each carries its own default effect, and a
    namespace defaulting to ALLOW is the one where a bug would be invisible:
    the request would succeed for the wrong reason.
    """
    for namespace, action in (
        ("ai", pep.ACTION_AI_EXTERNAL),
        ("egress", pep.ACTION_EGRESS_HTTP),
        ("export", pep.ACTION_EXPORT_CASE),
        ("vault", pep.ACTION_VAULT_SHRED),
        ("case", pep.ACTION_STAGE_TRANSITION),
        ("auth", pep.ACTION_SESSION_ELEVATE),
    ):
        await _allow_with_obligations(
            session, tenant, namespace, [{"kind": "require_step_up"}])
        allowed = await pep.check(
            session, tenant_id="default", action=action, subject_id="u",
            legacy_allowed=True)
        assert allowed is False, (
            f"{namespace}: an allow carrying an obligation nothing implements "
            f"was honoured")


async def test_the_pdp_HTTP_surface_still_reports_the_obligation(
        session, tenant, client):
    """`/decide` is the PDP, not a PEP: it reports what the policy said, and an
    out-of-process caller is contractually required to honour or refuse.

    Pinned because the two surfaces differ deliberately, and a future change
    that made `/decide` hide obligations would silently break every
    out-of-process integration built against it.
    """
    await _allow_with_obligations(
        session, tenant, "case", [{"kind": "watermark_export"}])

    body = (await client.post(f"{PREFIX_ARB}/decide", json={
        "subject": {"kind": "user", "id": "u"},
        "action": "case.stage.transition",
        "resource": {"kind": "case"}, "context": {}})).json()
    assert body["effect"] == "allow"
    assert body["enforced"] is True
    assert [o["kind"] for o in body["obligations"]] == ["watermark_export"]
