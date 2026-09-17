"""ARB-A — the adversarial pass.

Every test here is an attack that **worked** against the first version of this
module. They are kept as tests rather than fixed and forgotten, because each one
is a mistake that is easy to make again:

* A tenant admin creating a policy every other tenant on the box is bound by.
* A tenant admin switching a namespace from shadow to enforce for everyone.
* Writing any principal you like into the decision log.
* One policy putting a million condition nodes in front of every request.

The happy-path walkthrough passed and found none of them.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio
from sqlalchemy import select

from velaris_shared.auth.jwt_handler import create_dev_token
from velaris_shared.config import get_settings
from velaris_shared.db.models import (
    ArbiterDecisionModel,
    ArbiterNamespaceModel,
    ArbiterPolicyModel,
    ArbiterPolicyVersionModel,
    TenantModel,
)
from case_service.hxarbiter import pdp

pytestmark = pytest.mark.asyncio

PREFIX = "/api/v1/arbiter"

SEED = [("ai", "deny"), ("egress", "deny"), ("export", "deny"),
        ("vault", "deny"), ("case", "allow"), ("auth", "allow")]

DENY_EXTERNAL = {
    "id": "r1", "effect": "deny",
    "condition": {"attr": "context.provider_class", "op": "eq", "value": "external"},
}


def _token(roles: list[str], *, username: str = "tenant-admin") -> str:
    s = get_settings()
    return create_dev_token(
        user_id=str(uuid.uuid4()), username=username, roles=roles,
        secret=s.auth_secret, private_key=s.auth_rsa_private_key or "")


#: A TENANT administrator. On a shared install this is what "admin" means, and
#: it is emphatically not a platform operator.
def _tenant_admin_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {_token(['admin'])}"}


@pytest_asyncio.fixture(autouse=True)
async def seeded(session):
    from case_service.tenancy import identity as _identity
    _identity.clear_cache()
    pdp.invalidate_cache()
    for ns, eff in SEED:
        session.add(ArbiterNamespaceModel(
            namespace=ns, default_effect=eff, max_mode="shadow", description="",
            sample_allow_rate=1, sample_default_rate=1,
            updated_at=datetime.now(timezone.utc)))
    session.add(TenantModel(id=uuid.uuid4(), slug="default", name="Default"))
    await session.commit()
    yield
    pdp.invalidate_cache()
    _identity.clear_cache()


# ── platform scope is not a tenant admin's to take ───────────────────────────


async def test_a_tenant_admin_cannot_create_a_platform_policy(client):
    """A platform policy binds every tenant and none of them can override it."""
    r = await client.post(f"{PREFIX}/policies",
                          json={"name": "bind-everyone", "namespace": "ai",
                                "platform": True},
                          headers=_tenant_admin_headers())
    assert r.status_code == 403
    assert "PLATFORM-SCOPE" in r.json()["detail"]


async def test_a_tenant_admin_can_still_create_their_own_policy(client):
    r = await client.post(f"{PREFIX}/policies",
                          json={"name": "mine", "namespace": "ai"},
                          headers=_tenant_admin_headers())
    assert r.status_code == 201


async def test_a_platform_operator_can_create_a_platform_policy(client):
    r = await client.post(f"{PREFIX}/policies",
                          json={"name": "baseline", "namespace": "ai",
                                "platform": True})
    assert r.status_code == 201, r.text


async def test_a_tenant_admin_cannot_promote_a_namespace(client):
    """Promotion switches enforcement on for every tenant sharing the box."""
    r = await client.post(f"{PREFIX}/namespaces/ai/promote",
                          json={"max_mode": "enforce"},
                          headers=_tenant_admin_headers())
    assert r.status_code == 403
    assert "PLATFORM-SCOPE" in r.json()["detail"]


async def test_a_tenant_admin_cannot_seal_the_chain(client):
    r = await client.post(f"{PREFIX}/decisions/seal",
                          headers=_tenant_admin_headers())
    assert r.status_code == 403


async def test_a_failed_promotion_leaves_the_ceiling_alone(client, session):
    await client.post(f"{PREFIX}/namespaces/ai/promote",
                      json={"max_mode": "enforce"},
                      headers=_tenant_admin_headers())
    row = (await session.execute(
        select(ArbiterNamespaceModel)
        .where(ArbiterNamespaceModel.namespace == "ai"))).scalar_one()
    await session.refresh(row)
    assert row.max_mode == "shadow"
    assert row.promoted_at is None


# ── a subject is an assertion, and it must carry a signature ─────────────────


async def test_a_claimed_subject_is_recorded_against_the_real_caller(
        client, session):
    """`subject` says who the action is FOR; `claimed_by` says who said so.

    Without the second, anyone able to call /decide could write any principal
    into the evidence log and the row would read exactly like a genuine one.
    """
    r = await client.post(f"{PREFIX}/decide", json={
        "subject": {"kind": "user", "id": "the-ceo"},
        "action": "ai.external_call",
        "resource": {"kind": "case"},
        "context": {"provider_class": "external"},
    })
    assert r.status_code == 200
    row = (await session.execute(select(ArbiterDecisionModel))).scalar_one()
    assert row.subject_id == "the-ceo"
    assert row.claimed_by == "test-admin", "the caller must be stamped"


async def test_a_simulation_also_records_who_ran_it(client, session):
    await client.post(f"{PREFIX}/simulate", json={
        "subject": {"kind": "user", "id": "somebody"},
        "action": "ai.external_call",
        "resource": {"kind": "case"},
        "context": {},
        "attributes": {},
    })
    row = (await session.execute(select(ArbiterDecisionModel))).scalar_one()
    assert row.simulated is True
    assert row.claimed_by == "test-admin"


async def test_claimed_by_cannot_be_supplied_by_the_caller(client, session):
    """`extra="forbid"` — a body that tries to set it is refused outright."""
    r = await client.post(f"{PREFIX}/decide", json={
        "subject": {"kind": "user", "id": "u-1"},
        "action": "ai.external_call",
        "resource": {"kind": "case"},
        "context": {},
        "claimed_by": "somebody-else",
    })
    assert r.status_code == 422


# ── ARB-F: a draft must not reach another tenant's policy ────────────────────


async def test_a_draft_cannot_name_another_tenants_policy(client, session):
    """A cross-tenant WRITE on the security engine itself.

    `policy_id` says "this draft proposes a new version of that policy" and is
    caller-supplied. Unchecked: name another tenant's policy, have the draft
    approved inside your own tenant, activate it — and a version is appended to
    theirs. Checked at creation AND at activation, because the draft row
    outlives the request that made it and activation is the half that writes.
    """
    other = TenantModel(id=uuid.uuid4(), slug="victim", name="Victim")
    session.add(other)
    theirs = ArbiterPolicyModel(
        id=uuid.uuid4(), tenant_id=other.id, tenant_key=str(other.id),
        name="their-baseline", description="", namespace="ai", status="active",
        origin="user", created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc))
    session.add(theirs)
    await session.commit()

    r = await client.post(f"{PREFIX}/drafts", json={
        "intent": "quietly weaken their policy please",
        "namespace": "ai",
        "policy_id": str(theirs.id),
    })
    assert r.status_code == 404, "another tenant's policy must not be nameable"


async def test_activation_rechecks_the_policy_it_writes_to(client, session):
    """Defence in depth: even a draft row that already holds a foreign
    `policy_id` cannot be activated onto it."""
    from velaris_shared.db.models import ArbiterDraftModel

    other = TenantModel(id=uuid.uuid4(), slug="victim2", name="Victim2")
    session.add(other)
    theirs = ArbiterPolicyModel(
        id=uuid.uuid4(), tenant_id=other.id, tenant_key=str(other.id),
        name="their-other", description="", namespace="ai", status="active",
        origin="user", created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc))
    session.add(theirs)
    # A draft planted directly, as though it predated the creation-time check.
    draft = ArbiterDraftModel(
        id=uuid.uuid4(), policy_id=theirs.id, tenant_id=None, namespace="ai",
        intent="x", state="approved", readback="ok", analysis_report=[],
        generated_rules=[DENY_EXTERNAL], created_by=uuid.uuid4(),
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc))
    session.add(draft)
    await session.commit()

    r = await client.post(f"{PREFIX}/drafts/{draft.id}/activate")
    assert r.status_code == 404

    versions = (await session.execute(
        select(ArbiterPolicyVersionModel).where(
            ArbiterPolicyVersionModel.policy_id == theirs.id))).scalars().all()
    assert versions == [], "nothing may be written to their policy"


# ── bounds: what one request may cost everyone else ──────────────────────────


async def test_a_version_cannot_carry_unbounded_rules(client):
    """The per-rule node limit is not a limit if the rule COUNT is unbounded."""
    r = await client.post(f"{PREFIX}/policies",
                          json={"name": "huge", "namespace": "ai"})
    pid = r.json()["id"]
    rules = [dict(DENY_EXTERNAL, id=f"r{i}") for i in range(500)]
    r = await client.post(f"{PREFIX}/policies/{pid}/versions", json={"rules": rules})
    assert r.status_code == 422


async def test_an_empty_rule_list_is_refused(client):
    r = await client.post(f"{PREFIX}/policies",
                          json={"name": "empty", "namespace": "ai"})
    pid = r.json()["id"]
    r = await client.post(f"{PREFIX}/policies/{pid}/versions", json={"rules": []})
    assert r.status_code == 422


async def test_an_over_long_identifier_is_a_422_not_a_500(client):
    """It must fail at validation, not at the INSERT — a 500 loses the decision."""
    r = await client.post(f"{PREFIX}/decide", json={
        "subject": {"kind": "user", "id": "x" * 300},
        "action": "ai.external_call",
        "resource": {"kind": "case"},
        "context": {},
    })
    assert r.status_code == 422


async def test_an_over_long_action_is_a_422_not_a_500(client):
    r = await client.post(f"{PREFIX}/decide", json={
        "subject": {"kind": "user", "id": "u-1"},
        "action": "ai." + "x" * 200,
        "resource": {"kind": "case"},
        "context": {},
    })
    assert r.status_code == 422


async def test_the_sealer_cannot_be_made_to_spin_on_an_ancient_timestamp(session):
    """A restored backup or a jumped clock must not take the process down."""
    from case_service.hxarbiter import chain

    session.add(ArbiterDecisionModel(
        id=uuid.uuid4(), tenant_id=uuid.uuid4(), namespace="ai",
        action="ai.external_call", subject_kind="user", subject_id="u",
        subject={}, resource={}, context={}, effect="deny", mode="shadow",
        enforced=False, default_applied=True, matched=[], obligations=[],
        latency_us=1,
        evaluated_at=datetime(1990, 1, 1, tzinfo=timezone.utc)))
    await session.commit()

    n = await chain.seal_once(session)
    assert n == chain.MAX_BATCHES_PER_RUN, "the pass must stop at the ceiling"


# ── isolation ────────────────────────────────────────────────────────────────


async def test_the_context_of_another_tenants_decision_is_never_returned(
        client, session):
    session.add(ArbiterDecisionModel(
        id=uuid.uuid4(), tenant_id=uuid.uuid4(), namespace="ai",
        action="ai.external_call", subject_kind="user", subject_id="theirs",
        subject={}, resource={}, context={"secret": "theirs"},
        effect="deny", mode="shadow", enforced=False, default_applied=True,
        matched=[], obligations=[], latency_us=1,
        evaluated_at=datetime.now(timezone.utc)))
    await session.commit()

    rows = (await client.get(f"{PREFIX}/decisions")).json()
    assert all(r["subject_id"] != "theirs" for r in rows)


async def test_a_tenant_policy_is_invisible_to_another_tenant(client, session):
    other = TenantModel(id=uuid.uuid4(), slug="rival", name="Rival")
    session.add(other)
    session.add(ArbiterPolicyModel(
        id=uuid.uuid4(), tenant_id=other.id, tenant_key=str(other.id),
        name="rival-secret", description="", namespace="ai", status="active",
        origin="user", created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc)))
    await session.commit()

    names = [p["name"] for p in (await client.get(f"{PREFIX}/policies")).json()]
    assert "rival-secret" not in names
