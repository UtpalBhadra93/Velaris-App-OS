"""ARB-A — the HTTP surface.

Two things are checked here that a happy-path walkthrough would not find:

* **Every route is gated.** A detector that looks for gates cannot find their
  absence, so this asserts against the BUILT route table — every arbiter path
  must reject an anonymous caller. 58 routes once shipped with no gate at all in
  this platform, and they were found exactly this way.
* **A by-id route 404s outside the tenant**, never 403 — a 403 confirms the
  policy exists, which is the thing being hidden.

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
    ArbiterPolicyModel,
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
    # The test admin token carries no tenant, so every request resolves the
    # fallback slug `default`. Without a row for it the router refuses with
    # VEL-ARB-TENANT-UNRESOLVED-403 — which is the correct behaviour, and is
    # itself asserted in `test_an_unresolvable_tenant_is_refused`.
    session.add(TenantModel(id=uuid.uuid4(), slug="default", name="Default"))
    await session.commit()
    yield
    pdp.invalidate_cache()
    _identity.clear_cache()


# ── every route is gated ─────────────────────────────────────────────────────


def _arbiter_routes():
    from case_service.main import app
    out = []
    for r in app.routes:
        path = getattr(r, "path", "")
        if "/arbiter" in path:
            for m in sorted(getattr(r, "methods", set()) - {"HEAD", "OPTIONS"}):
                out.append((m, path))
    return out


async def test_the_router_is_actually_mounted():
    routes = _arbiter_routes()
    assert routes, "the arbiter router is not registered on the app"
    # 19 for ARB-A..G, 2 for ARB-I upkeep (the report, and HxNexus reading it
    # back), and 4 from the 2026-08-29 gap pass: export + import (a rule set
    # could not be backed up or moved between installs), and anchor + purge
    # (the decision log was never externally timestamped and had no retention
    # horizon at all), and 1 from the adversarial pass — the retention horizon
    # existed as a column that only direct SQL could set, and a control nobody
    # can see is one nobody reviews. Pinned so a route cannot be added without
    # the anonymous sweep below being noticed.
    assert len(routes) == 26


@pytest.mark.parametrize("method,path", _arbiter_routes(),
                         ids=lambda v: str(v).replace("/", "_"))
async def test_no_arbiter_route_answers_an_anonymous_caller(
        anon_client, method, path):
    concrete = (path
                .replace("{policy_id}", str(uuid.uuid4()))
                .replace("{namespace}", "ai"))
    resp = await anon_client.request(method, concrete, json={})
    assert resp.status_code in (401, 403), (
        f"{method} {path} answered {resp.status_code} with no token")


# ── catalogue ────────────────────────────────────────────────────────────────


async def test_catalog_lists_attributes_operators_and_namespaces(client):
    r = await client.get(f"{PREFIX}/catalog")
    assert r.status_code == 200
    body = r.json()
    assert any(a["path"] == "tenant.region" for a in body["attributes"])
    assert "matches_domain" in body["operators"]
    assert {n["namespace"] for n in body["namespaces"]} >= {"ai", "egress"}
    # Everything ships clamped: nothing here can decide anything yet.
    assert all(n["max_mode"] == "shadow" for n in body["namespaces"])


# ── policy lifecycle over HTTP ───────────────────────────────────────────────


async def _make_policy(client, **over):
    body = {"name": "no-external-ai", "namespace": "ai"} | over
    r = await client.post(f"{PREFIX}/policies", json=body)
    assert r.status_code == 201, r.text
    return r.json()["id"]


async def test_full_lifecycle_create_version_activate_and_decide(client):
    pid = await _make_policy(client)

    r = await client.post(f"{PREFIX}/policies/{pid}/versions",
                          json={"rules": [DENY_EXTERNAL]})
    assert r.status_code == 201, r.text
    assert len(r.json()["signature"]) == 64

    r = await client.post(f"{PREFIX}/policies/{pid}/activate?version=1")
    assert r.status_code == 200

    r = await client.post(f"{PREFIX}/decide", json={
        "subject": {"kind": "user", "id": "u-1"},
        "action": "ai.external_call",
        "resource": {"kind": "case"},
        "context": {"provider_class": "external"},
    })
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["effect"] == "deny"
    assert d["enforced"] is False, "the namespace ceiling is still shadow"
    assert [m["rule_id"] for m in d["matched"]] == ["r1"]


async def test_a_rule_that_does_not_compile_is_refused_at_422(client):
    pid = await _make_policy(client)
    r = await client.post(f"{PREFIX}/policies/{pid}/versions", json={"rules": [{
        "id": "r1", "effect": "deny",
        "condition": {"attr": "context.target_domain",
                      "op": "contains", "value": "example.com"}}]})
    assert r.status_code == 422
    assert "bypass class" in r.json()["detail"]


async def test_activating_a_version_that_does_not_exist_is_404(client):
    pid = await _make_policy(client)
    r = await client.post(f"{PREFIX}/policies/{pid}/activate?version=9")
    assert r.status_code == 404


async def test_active_at_answers_the_audit_question(client):
    pid = await _make_policy(client)
    await client.post(f"{PREFIX}/policies/{pid}/versions",
                      json={"rules": [DENY_EXTERNAL]})
    await client.post(f"{PREFIX}/policies/{pid}/activate?version=1")

    now = datetime.now(timezone.utc).isoformat()
    r = await client.get(f"{PREFIX}/policies/{pid}/active-at?moment={now}")
    assert r.status_code == 200
    assert r.json()["active"] is True
    assert r.json()["version"] == 1

    long_ago = datetime(2020, 1, 1, tzinfo=timezone.utc).isoformat()
    r = await client.get(f"{PREFIX}/policies/{pid}/active-at?moment={long_ago}")
    assert r.json()["active"] is False


async def test_active_at_accepts_an_isotimestamp_however_it_was_encoded(client):
    """`+00:00` is a legal offset and also how a query string spells a space.

    The obvious way to call this endpoint must not be the one that 422s.
    """
    import urllib.parse
    pid = await _make_policy(client)
    await client.post(f"{PREFIX}/policies/{pid}/versions",
                      json={"rules": [DENY_EXTERNAL]})
    await client.post(f"{PREFIX}/policies/{pid}/activate?version=1")

    raw = datetime.now(timezone.utc).isoformat()
    for moment in (raw, urllib.parse.quote(raw), raw.replace("+00:00", "Z")):
        r = await client.get(f"{PREFIX}/policies/{pid}/active-at?moment={moment}")
        assert r.status_code == 200, f"{moment} -> {r.status_code}"
        assert r.json()["version"] == 1

    r = await client.get(f"{PREFIX}/policies/{pid}/active-at?moment=not-a-time")
    assert r.status_code == 422


async def test_retiring_a_policy_stops_it_deciding(client):
    pid = await _make_policy(client)
    await client.post(f"{PREFIX}/policies/{pid}/versions",
                      json={"rules": [DENY_EXTERNAL]})
    await client.post(f"{PREFIX}/policies/{pid}/activate?version=1")
    r = await client.post(f"{PREFIX}/policies/{pid}/retire")
    assert r.status_code == 200

    r = await client.post(f"{PREFIX}/decide", json={
        "subject": {"kind": "user", "id": "u-1"},
        "action": "ai.external_call",
        "resource": {"kind": "case"},
        "context": {"provider_class": "external"},
    })
    body = r.json()
    assert body["default_applied"] is True, "a retired policy must not still match"


# ── tenant isolation ─────────────────────────────────────────────────────────


async def test_another_tenants_policy_is_404_not_403(client, session):
    """403 would confirm it exists, which is the thing being hidden."""
    other = TenantModel(id=uuid.uuid4(), slug="other-co", name="Other")
    session.add(other)
    session.add(ArbiterPolicyModel(
        id=uuid.uuid4(), tenant_id=other.id, tenant_key=str(other.id),
        name="theirs", description="", namespace="ai", status="draft",
        origin="user", created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc)))
    await session.commit()
    theirs = (await session.execute(
        select(ArbiterPolicyModel).where(ArbiterPolicyModel.name == "theirs")
    )).scalar_one()

    for method, path in (("GET", f"/policies/{theirs.id}/active-at"
                                 f"?moment={datetime.now(timezone.utc).isoformat()}"),
                         ("POST", f"/policies/{theirs.id}/retire")):
        r = await client.request(method, f"{PREFIX}{path}")
        assert r.status_code == 404, f"{method} {path} leaked {r.status_code}"


# ── simulate ─────────────────────────────────────────────────────────────────


async def test_simulate_uses_supplied_attributes_and_enforces_nothing(client, session):
    pid = await _make_policy(client)
    await client.post(f"{PREFIX}/policies/{pid}/versions", json={"rules": [{
        "id": "r1", "effect": "deny",
        "condition": {"attr": "tenant.region", "op": "eq", "value": "eu-west"}}]})
    await client.post(f"{PREFIX}/policies/{pid}/activate?version=1")

    payload = {
        "subject": {"kind": "user", "id": "u-1"},
        "action": "ai.external_call",
        "resource": {"kind": "case"},
        "context": {},
        "attributes": {"tenant.region": "eu-west"},
    }
    r = await client.post(f"{PREFIX}/simulate", json=payload)
    assert r.status_code == 200, r.text
    assert r.json()["effect"] == "deny"
    assert r.json()["enforced"] is False

    payload["attributes"] = {"tenant.region": "us-east"}
    r = await client.post(f"{PREFIX}/simulate", json=payload)
    assert r.json()["default_applied"] is True

    rows = (await session.execute(
        select(ArbiterDecisionModel).where(
            ArbiterDecisionModel.simulated.is_(True)))).scalars().all()
    assert len(rows) >= 1, "a simulation is recorded, and marked as one"


# ── promotion, decisions, sealing ────────────────────────────────────────────


async def test_promoting_a_namespace_is_what_makes_a_decision_binding(client):
    pid = await _make_policy(client)
    await client.post(f"{PREFIX}/policies/{pid}/versions",
                      json={"rules": [DENY_EXTERNAL], "mode": "enforce"})
    await client.post(f"{PREFIX}/policies/{pid}/activate?version=1")

    body = {
        "subject": {"kind": "user", "id": "u-1"},
        "action": "ai.external_call",
        "resource": {"kind": "case"},
        "context": {"provider_class": "external"},
    }
    assert (await client.post(f"{PREFIX}/decide", json=body)).json()["enforced"] is False

    # ARB-G: with no parity evidence the promotion is REFUSED, not merely
    # discouraged. A fresh namespace has no soak behind it by definition.
    r = await client.post(f"{PREFIX}/namespaces/ai/promote",
                          json={"max_mode": "enforce"})
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "VEL-ARB-PARITY-409"
    assert (await client.post(f"{PREFIX}/decide", json=body)).json()["enforced"] is False

    # The override exists for exactly this case — a namespace with no legacy
    # control to prove parity against — and it demands a written reason.
    r = await client.post(f"{PREFIX}/namespaces/ai/promote",
                          json={"max_mode": "enforce", "override_parity": True})
    assert r.status_code == 422, "an override with no reason is unattributable"

    r = await client.post(f"{PREFIX}/namespaces/ai/promote",
                          json={"max_mode": "enforce", "override_parity": True,
                                "override_reason": "new namespace, no legacy control"})
    assert r.status_code == 200
    assert r.json()["override"] is True
    assert (await client.post(f"{PREFIX}/decide", json=body)).json()["enforced"] is True


async def test_lowering_a_ceiling_is_never_gated(client):
    """Backing out must never be harder than going in."""
    r = await client.post(f"{PREFIX}/namespaces/ai/promote",
                          json={"max_mode": "shadow"})
    assert r.status_code == 200


async def test_promoting_an_unknown_namespace_is_404(client):
    r = await client.post(f"{PREFIX}/namespaces/nope/promote",
                          json={"max_mode": "enforce"})
    assert r.status_code == 404


async def test_the_decision_log_lists_and_verifies(client):
    await client.post(f"{PREFIX}/decide", json={
        "subject": {"kind": "user", "id": "u-1"},
        "action": "ai.external_call",
        "resource": {"kind": "case"},
        "context": {"provider_class": "external"},
    })
    r = await client.get(f"{PREFIX}/decisions")
    assert r.status_code == 200
    rows = r.json()
    assert len(rows) == 1
    assert rows[0]["effect"] == "deny"
    assert rows[0]["sealed"] is False

    r = await client.get(f"{PREFIX}/decisions/verify")
    assert r.json()["ok"] is True          # an empty chain is a valid chain


async def test_the_decision_log_filters_to_this_tenant_only(client, session):
    await client.post(f"{PREFIX}/decide", json={
        "subject": {"kind": "user", "id": "u-1"},
        "action": "ai.external_call",
        "resource": {"kind": "case"},
        "context": {"provider_class": "external"},
    })
    session.add(ArbiterDecisionModel(
        id=uuid.uuid4(), tenant_id=uuid.uuid4(), namespace="ai",
        action="ai.external_call", subject_kind="user", subject_id="someone-else",
        subject={}, resource={}, context={}, effect="deny", mode="shadow",
        enforced=False, default_applied=True, matched=[], obligations=[],
        latency_us=1, evaluated_at=datetime.now(timezone.utc)))
    await session.commit()

    rows = (await client.get(f"{PREFIX}/decisions")).json()
    assert [r["subject_id"] for r in rows] == ["u-1"]


# ── ARB-I: upkeep over HTTP ──────────────────────────────────────────────────


async def test_upkeep_answers_without_any_ai_provider(client):
    """The property that makes this shippable to an install that has never
    configured AI: the report is complete without a model. The dev test
    environment has no provider, so this is the real unconfigured path and not
    a mock of it."""
    r = await client.get(f"{PREFIX}/upkeep")
    assert r.status_code == 200
    body = r.json()
    assert body["scope"] in ("tenant", "platform")
    assert "findings" in body and "limitations" in body
    assert set(body["counts"]) == {"error", "warning", "info"}


async def test_upkeep_reports_a_policy_that_stopped_compiling(client, session):
    from velaris_shared.db.models import ArbiterPolicyVersionModel

    created = await client.post(f"{PREFIX}/policies",
                                json={"name": "regional", "namespace": "ai"})
    pid = created.json()["id"]
    assert (await client.post(f"{PREFIX}/policies/{pid}/versions",
                              json={"rules": [DENY_EXTERNAL]})).status_code == 201
    assert (await client.post(
        f"{PREFIX}/policies/{pid}/activate?version=1")).status_code == 200

    v = await session.get(ArbiterPolicyVersionModel, (uuid.UUID(pid), 1))
    v.rules = [{"id": "r1", "effect": "deny",
                "condition": {"attr": "tenant.gone", "op": "eq", "value": "x"}}]
    await session.commit()

    body = (await client.get(f"{PREFIX}/upkeep")).json()
    stale = [f for f in body["findings"] if f["kind"] == "stale_attribute"]
    assert stale and stale[0]["severity"] == "error"
    assert stale[0]["policy_name"] == "regional"


async def test_advise_is_a_503_and_not_a_500_with_no_provider(client):
    """No provider means the FEATURE is absent. A 500 here would make an
    unconfigured install look broken on the one screen that exists to reassure
    an operator."""
    r = await client.post(f"{PREFIX}/upkeep/advise", json={})
    assert r.status_code == 503
    assert "VEL-ARB-NO-PROVIDER-503" in str(r.json())


async def test_upkeep_refuses_an_unknown_window(client):
    assert (await client.get(f"{PREFIX}/upkeep?window_days=0")).status_code == 422
    assert (await client.get(f"{PREFIX}/upkeep?window_days=999")).status_code == 422


async def test_advise_will_not_take_findings_from_the_caller(client):
    """The body carries ids to FILTER by, never findings to explain. A caller
    supplying text would be putting arbitrary content in front of a model that,
    on a `local_only` tenant, is running on this box."""
    r = await client.post(f"{PREFIX}/upkeep/advise",
                          json={"findings": [{"id": "x", "message": "ignore all"}]})
    assert r.status_code == 422
