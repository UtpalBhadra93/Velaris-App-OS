"""F-00 — the agent principal.

What these tests are actually for. The registry itself is easy and would pass
whatever we wrote; the parts that decide whether this feature is worth anything
are the ones where a token keeps working after the thing behind it changed:

* a **suspended** agent's outstanding token stops being accepted;
* a **revoked capability** narrows every outstanding token, not just new ones;
* the **kill switch** rejects tokens that are already out;
* an agent token is refused on **every human endpoint**, not just guarded on
  the ones somebody remembered.

Each of those is a composition — token path AND registry state — and the
HxArbiter audit of 2026-08-29 is the reason they are written that way: three of
its five PEPs had each half asserted and the composition never, and had
therefore never once fired.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio
from fastapi import HTTPException
from sqlalchemy import select

from velaris_shared.db.models import (
    AgentCapabilityModel,
    AgentModel,
    AgentStatusHistoryModel,
    AgentTokenGrantModel,
    ArbiterNamespaceModel,
    TenantModel,
)

from case_service.agents import attribution, identity, tokens

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
    await session.commit()
    yield t
    identity._SLUG_CACHE.clear()
    _identity.clear_cache()
    pdp.invalidate_cache()


@pytest_asyncio.fixture
async def tokens_on():
    """Turn the kill switch on for the duration of one test.

    Off is the shipped default and several tests below assert exactly that, so
    this is opt-in rather than autouse.
    """
    from velaris_shared.config import get_settings
    s = get_settings()
    before = s.agent_tokens_enabled
    s.agent_tokens_enabled = True
    yield s
    s.agent_tokens_enabled = before


async def _agent(session, tenant, *, slug="worker", caps=("agent.propose",),
                 kind="internal") -> AgentModel:
    a = await identity.create_agent(
        session, tenant_id=tenant.id, slug=slug, kind=kind, capabilities=caps)
    await session.commit()
    return a


# ══ the registry ════════════════════════════════════════════════════════════


async def test_capability_vocabulary_is_closed(session, tenant):
    """A typo must be refused, never silently dropped.

    Dropping it would let `agent.act_on_cases` read as a successful grant and
    the agent would fail later, somewhere else, with nothing saying why.
    """
    with pytest.raises(identity.AgentError) as exc:
        await identity.create_agent(
            session, tenant_id=tenant.id, slug="typo", kind="internal",
            capabilities=["agent.act_on_cases"])
    assert "unknown capability" in str(exc.value)


async def test_external_agents_cannot_be_created_in_this_build(session, tenant):
    """The schema allows three kinds; this build issues two.

    `external` is in the CHECK constraint so F-09 is a code change rather than
    a migration on a live table — which means the refusal has to live in code,
    and has to be tested, or the constraint quietly admits it.
    """
    with pytest.raises(identity.AgentError):
        await identity.create_agent(
            session, tenant_id=tenant.id, slug="third-party", kind="external")


async def test_creation_writes_its_own_history(session, tenant):
    a = await _agent(session, tenant)
    rows = await identity.status_history(session, a.id)
    assert [r.to_status for r in rows] == ["active"]
    assert rows[0].from_status is None


async def test_retired_is_terminal(session, tenant):
    a = await _agent(session, tenant)
    await identity.set_status(session, a, "retired", reason="done")
    await session.commit()
    with pytest.raises(identity.AgentError) as exc:
        await identity.set_status(session, a, "active", reason="oops")
    assert "retired" in str(exc.value)


async def test_revoking_a_capability_keeps_the_row(session, tenant):
    """Append-only, because 'what could this agent do in March' is asked later
    and a DELETE cannot answer it."""
    a = await _agent(session, tenant, caps=["agent.propose", "agent.read_case"])
    await identity.revoke_capability(session, a, "agent.read_case")
    await session.commit()

    assert await identity.live_capabilities(session, a.id) == {"agent.propose"}
    history = await identity.capability_history(session, a.id)
    assert len(history) == 2
    revoked = [h for h in history if h.capability == "agent.read_case"][0]
    assert revoked.revoked_at is not None


async def test_regranting_after_revocation_inserts_a_new_row(session, tenant):
    """Resurrecting the old row would make the history lie about the gap."""
    a = await _agent(session, tenant, caps=["agent.propose"])
    await identity.revoke_capability(session, a, "agent.propose")
    await identity.grant_capability(session, a, "agent.propose")
    await session.commit()

    history = await identity.capability_history(session, a.id)
    assert len(history) == 2
    assert history[0].revoked_at is not None
    assert history[1].revoked_at is None
    assert await identity.live_capabilities(session, a.id) == {"agent.propose"}


async def test_revoking_closes_every_live_row(session, tenant):
    """Duplicates are permitted by design, so closing one is not enough."""
    a = await _agent(session, tenant, caps=["agent.propose"])
    # Force a duplicate the way a lost race would.
    session.add(AgentCapabilityModel(
        id=uuid.uuid4(), agent_id=a.id, capability="agent.propose"))
    await session.commit()

    closed = await identity.revoke_capability(session, a, "agent.propose")
    await session.commit()
    assert closed == 2
    assert await identity.live_capabilities(session, a.id) == set()


async def test_ensure_agent_never_widens_an_existing_one(session, tenant):
    """An operator who narrowed a ceiling must not have it restored by a
    restart. `ensure_agent` runs on every boot of a worker."""
    a = await _agent(session, tenant, slug="w", caps=["agent.propose"])
    await identity.revoke_capability(session, a, "agent.propose")
    await session.commit()

    again = await identity.ensure_agent(
        session, tenant_id=tenant.id, slug="w", kind="internal",
        capabilities=["agent.propose", "agent.act_on_case"])
    await session.commit()

    assert again.id == a.id
    assert await identity.live_capabilities(session, a.id) == set()


# ══ tokens ══════════════════════════════════════════════════════════════════


async def test_minting_is_refused_while_the_switch_is_off(session, tenant):
    """The shipped default. An install taking this update mints nothing."""
    a = await _agent(session, tenant)
    with pytest.raises(identity.AgentError) as exc:
        await tokens.mint_agent_token(session, a)
    assert "disabled" in str(exc.value)


async def test_a_token_cannot_exceed_the_ceiling(session, tenant, tokens_on):
    a = await _agent(session, tenant, caps=["agent.propose"])
    with pytest.raises(identity.AgentError) as exc:
        await tokens.mint_agent_token(session, a, capabilities=["agent.act_on_case"])
    assert "ceiling" in str(exc.value)


async def test_ttl_is_capped(session, tenant, tokens_on):
    a = await _agent(session, tenant)
    with pytest.raises(identity.AgentError):
        await tokens.mint_agent_token(
            session, a, ttl_minutes=tokens_on.agent_token_max_ttl_minutes + 1)


async def test_a_minted_token_resolves(session, tenant, tokens_on):
    a = await _agent(session, tenant, caps=["agent.propose"])
    token, _ = await tokens.mint_agent_token(session, a)
    await session.commit()

    caller = await tokens.resolve_agent_token(session, token)
    assert caller.agent_id == str(a.id)
    assert caller.kind == "internal"
    assert caller.capabilities == frozenset({"agent.propose"})
    assert caller.can("agent.propose")
    assert not caller.can("agent.act_on_case")


async def test_suspending_the_agent_kills_its_live_token(
        session, tenant, tokens_on):
    """The incident-response control, and the reason the grant row is re-read
    on every call. Time-validity is not revocation."""
    a = await _agent(session, tenant)
    token, _ = await tokens.mint_agent_token(session, a)
    await session.commit()
    await tokens.resolve_agent_token(session, token)      # works before

    await identity.set_status(session, a, "suspended", reason="misbehaving")
    await session.commit()

    with pytest.raises(HTTPException) as exc:
        await tokens.resolve_agent_token(session, token)
    assert exc.value.status_code == 401


async def test_revoking_a_capability_narrows_a_live_token(
        session, tenant, tokens_on):
    """The token froze two capabilities; the ceiling now holds one.

    This is the composition that matters: asserting the mint and asserting the
    revocation separately would both pass while the token kept the authority.
    """
    a = await _agent(session, tenant, caps=["agent.propose", "agent.read_case"])
    token, _ = await tokens.mint_agent_token(session, a)
    await session.commit()
    assert (await tokens.resolve_agent_token(session, token)).can("agent.read_case")

    await identity.revoke_capability(session, a, "agent.read_case")
    await session.commit()

    caller = await tokens.resolve_agent_token(session, token)
    assert caller.capabilities == frozenset({"agent.propose"})
    assert not caller.can("agent.read_case")


async def test_revoking_every_capability_refuses_the_token(
        session, tenant, tokens_on):
    """A bearer of nothing is refused, not admitted with an empty set — so the
    agent stops instead of quietly no-opping."""
    a = await _agent(session, tenant, caps=["agent.propose"])
    token, _ = await tokens.mint_agent_token(session, a)
    await session.commit()
    await identity.revoke_capability(session, a, "agent.propose")
    await session.commit()

    with pytest.raises(HTTPException) as exc:
        await tokens.resolve_agent_token(session, token)
    assert exc.value.status_code == 401


async def test_revoking_the_grant_is_immediate(session, tenant, tokens_on):
    a = await _agent(session, tenant)
    token, grant = await tokens.mint_agent_token(session, a)
    await session.commit()
    await tokens.revoke_grant(session, grant)
    await session.commit()

    with pytest.raises(HTTPException):
        await tokens.resolve_agent_token(session, token)


async def test_an_expired_grant_is_refused(session, tenant, tokens_on):
    a = await _agent(session, tenant)
    token, grant = await tokens.mint_agent_token(session, a)
    grant.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    await session.commit()

    with pytest.raises(HTTPException):
        await tokens.resolve_agent_token(session, token)


async def test_the_kill_switch_rejects_tokens_already_out(
        session, tenant, tokens_on):
    """Off means off for every outstanding token, not merely for new mints.

    A flag that only stops minting leaves the ones already issued running for
    the rest of their TTL, which is not a kill switch.
    """
    a = await _agent(session, tenant)
    token, _ = await tokens.mint_agent_token(session, a)
    await session.commit()
    await tokens.resolve_agent_token(session, token)

    tokens_on.agent_tokens_enabled = False
    with pytest.raises(HTTPException) as exc:
        await tokens.resolve_agent_token(session, token)
    assert exc.value.status_code == 401


async def test_a_case_scoped_token_is_valid_for_that_case_only(
        session, tenant, tokens_on):
    a = await _agent(session, tenant)
    case_id = uuid.uuid4()
    token, _ = await tokens.mint_agent_token(session, a, case_id=case_id)
    await session.commit()

    caller = await tokens.resolve_agent_token(session, token)
    tokens.require_case(caller, case_id)                 # its own case: fine
    with pytest.raises(HTTPException) as exc:
        tokens.require_case(caller, uuid.uuid4())
    assert exc.value.status_code == 403


async def test_an_agent_scoped_token_is_not_pinned_to_a_case(
        session, tenant, tokens_on):
    a = await _agent(session, tenant)
    token, _ = await tokens.mint_agent_token(session, a)
    await session.commit()
    caller = await tokens.resolve_agent_token(session, token)
    tokens.require_case(caller, uuid.uuid4())            # no audience: passes


async def test_an_agent_token_carries_no_roles(session, tenant, tokens_on):
    """If one ever reached a role check it must fail closed, not inherit."""
    from velaris_shared.auth.jwt_handler import decode_jwt_token
    from velaris_shared.config import get_settings

    a = await _agent(session, tenant)
    token, _ = await tokens.mint_agent_token(session, a)
    await session.commit()

    s = get_settings()
    claims = decode_jwt_token(
        token, secret=s.auth_secret, issuer=s.auth_issuer,
        audience=s.auth_audience, public_key=s.auth_rsa_public_key or "")
    assert claims["realm_access"]["roles"] == []
    assert claims["token_use"] == "agent"


async def test_an_agent_token_is_refused_by_the_human_dependency(
        session, tenant, tokens_on):
    """The single-purpose rule. A leaked agent token must not reach REST.

    Asserted against `get_current_user` itself rather than one endpoint,
    because that is the dependency every human route shares — testing one route
    would prove nothing about the other several hundred.
    """
    from velaris_shared.auth.dependencies import get_current_user
    from fastapi.security import HTTPAuthorizationCredentials

    a = await _agent(session, tenant)
    token, _ = await tokens.mint_agent_token(session, a)
    await session.commit()

    class _Req:
        state = type("S", (), {"db_session": None})()

    with pytest.raises(HTTPException) as exc:
        await get_current_user(
            _Req(),
            HTTPAuthorizationCredentials(scheme="Bearer", credentials=token))
    assert exc.value.status_code == 401
    assert "agent token is not valid" in exc.value.detail


async def test_refusals_do_not_say_which_reason(session, tenant, tokens_on):
    """Revoked, expired, suspended and unknown all read identically.

    Distinguishing them turns the endpoint into an oracle for probing live
    agent ids — the portal OTP lesson of 2026-08-20.
    """
    a = await _agent(session, tenant)
    good, grant = await tokens.mint_agent_token(session, a)
    await session.commit()

    seen = set()
    await tokens.revoke_grant(session, grant)
    await session.commit()
    for candidate in (good, "not-even-a-jwt"):
        with pytest.raises(HTTPException) as exc:
            await tokens.resolve_agent_token(session, candidate)
        seen.add(exc.value.detail)
    assert len(seen) == 1, seen


# ══ attribution ═════════════════════════════════════════════════════════════


async def test_the_agent_namespace_ships_shadow(session, tenant):
    ns = (await session.execute(
        select(ArbiterNamespaceModel)
        .where(ArbiterNamespaceModel.namespace == "agent")
    )).scalar_one()
    assert ns.max_mode == "shadow"
    assert ns.default_effect == "deny"


async def test_a_shadow_decision_returns_the_callers_own_answer(session, tenant):
    """`agent` defaults to DENY, so if shadow leaked the engine's answer this
    would come back False and the caller would be refused on upgrade."""
    a = await _agent(session, tenant)
    allowed = await attribution.check(
        session, agent_id=str(a.id), agent_kind="internal",
        tenant_id="default", action=attribution.ACTION_PROPOSE,
        legacy_allowed=True, model="ollama/llama3.2")
    await session.commit()
    assert allowed is True


async def test_the_decision_records_the_agent_and_the_model(session, tenant):
    """The whole reason F-00 exists: the log has to name the actor."""
    from velaris_shared.db.models import ArbiterDecisionModel

    a = await _agent(session, tenant)
    await attribution.check(
        session, agent_id=str(a.id), agent_kind="internal",
        tenant_id="default", action=attribution.ACTION_PROPOSE,
        legacy_allowed=True, model="ollama/llama3.2",
        resource_kind="case", resource_id=str(uuid.uuid4()))
    await session.commit()

    row = (await session.execute(
        select(ArbiterDecisionModel)
        .where(ArbiterDecisionModel.action == attribution.ACTION_PROPOSE)
    )).scalars().first()
    assert row is not None, "the decision was never recorded"
    assert row.subject["id"] == str(a.id)
    assert row.subject["kind"] == "agent"
    assert row.subject["attrs"]["model_id"] == "ollama/llama3.2"
    assert row.subject["attrs"]["agent_kind"] == "internal"
    assert row.claimed_by == "agents.attribution"


async def test_internal_attribution_resolves_a_real_id(session, tenant):
    """`check_internal` registers the worker so the log names a principal
    rather than a display string."""
    agent_id = await attribution.internal_agent_id("default", "hxnexus.case_qa")
    uuid.UUID(agent_id)          # raises if it fell back to the slug form

    row = (await session.execute(
        select(AgentModel).where(AgentModel.slug == "hxnexus.case_qa")
    )).scalar_one()
    assert row.kind == "internal"
    assert str(row.id) == agent_id


async def test_attribution_never_raises_when_the_registry_is_unreachable(
        session, tenant, monkeypatch):
    """Attribution failing must never fail the call being attributed."""
    async def _boom(*_a, **_k):
        raise RuntimeError("registry down")

    identity._SLUG_CACHE.clear()
    monkeypatch.setattr(identity, "ensure_agent", _boom)
    monkeypatch.setattr(identity, "resolve_agent", _boom)
    got = await attribution.internal_agent_id("default", "hxnexus.case_qa")
    assert got == "unregistered:hxnexus.case_qa"


async def test_model_id_never_raises(monkeypatch):
    import velaris_shared.llm.factory as factory

    def _boom(*_a, **_k):
        raise RuntimeError("no backend")

    monkeypatch.setattr(factory, "active_model", _boom)
    assert attribution.model_id() == "unknown/unknown"


# ══ the management API ══════════════════════════════════════════════════════


def _headers(roles: list[str]) -> dict[str, str]:
    from velaris_shared.auth.jwt_handler import create_dev_token
    from velaris_shared.config import get_settings

    s = get_settings()
    token = create_dev_token(
        user_id=str(uuid.uuid4()), username="agent-admin", roles=roles,
        secret=s.auth_secret, private_key=s.auth_rsa_private_key or "")
    return {"Authorization": f"Bearer {token}"}


async def test_every_route_refuses_an_anonymous_caller(anon_client, tenant):
    """A route sweep, because a gate that exists on four of five routes is the
    population no audit of the four can find.

    `anon_client`, not `client`: the ordinary fixture carries a default admin
    bearer token, so sweeping with it proves the routes EXIST and nothing about
    whether they are gated. That is the same shape as the 58 ungated routes of
    2026-08-15 — a check that cannot observe an absent gate.
    """
    from case_service.api.routers.agents import router

    aid = str(uuid.uuid4())
    for route in router.routes:
        path = route.path.replace("{agent_id}", aid) \
                         .replace("{grant_id}", str(uuid.uuid4())) \
                         .replace("{capability}", "agent.propose")
        for method in sorted(route.methods):
            r = await anon_client.request(method, f"/api/v1{path}", json={})
            assert r.status_code in (401, 403), f"{method} {path} -> {r.status_code}"


async def test_a_viewer_cannot_create_an_agent(client, tenant):
    r = await client.post(PREFIX, headers=_headers(["viewer"]),
                          json={"slug": "x", "kind": "internal"})
    assert r.status_code == 403


async def test_admin_creates_lists_and_suspends(client, session, tenant):
    h = _headers(["admin"])
    r = await client.post(PREFIX, headers=h, json={
        "slug": "hxnexus.summariser", "kind": "internal",
        "display_name": "Summariser", "capabilities": ["agent.propose"]})
    assert r.status_code == 201, r.text
    agent_id = r.json()["id"]
    assert r.json()["capabilities"] == ["agent.propose"]

    r = await client.get(PREFIX, headers=h)
    assert r.status_code == 200
    assert [a["slug"] for a in r.json()["agents"]] == ["hxnexus.summariser"]
    # An operator looking at agents whose tokens nothing will accept is told so
    # here rather than discovering it from a 401 on the agent's first call.
    assert r.json()["tokens_enabled"] is False

    r = await client.post(f"{PREFIX}/{agent_id}/status", headers=h,
                          json={"status": "suspended", "reason": "noisy"})
    assert r.status_code == 200
    assert r.json()["status"] == "suspended"


async def test_taking_an_agent_out_of_service_needs_a_reason(
        client, session, tenant):
    h = _headers(["admin"])
    a = await _agent(session, tenant, slug="w")
    r = await client.post(f"{PREFIX}/{a.id}/status", headers=h,
                          json={"status": "suspended", "reason": "   "})
    assert r.status_code == 422
    assert "REASON" in r.json()["detail"]


async def test_unknown_fields_are_refused(client, tenant):
    r = await client.post(PREFIX, headers=_headers(["admin"]), json={
        "slug": "x", "kind": "internal", "capabilties": ["agent.propose"]})
    assert r.status_code == 422


async def test_another_tenants_agent_is_404_not_403(client, session, tenant):
    """403 confirms the id exists, which is itself the leak."""
    other = TenantModel(id=uuid.uuid4(), slug="other", name="Other")
    session.add(other)
    await session.commit()
    theirs = await identity.create_agent(
        session, tenant_id=other.id, slug="theirs", kind="internal")
    await session.commit()

    r = await client.get(f"{PREFIX}/{theirs.id}", headers=_headers(["admin"]))
    assert r.status_code == 404


async def test_minting_through_the_api_returns_the_token_once(
        client, session, tenant, tokens_on):
    h = _headers(["admin"])
    a = await _agent(session, tenant, slug="w", caps=["agent.propose"])

    r = await client.post(f"{PREFIX}/{a.id}/tokens", headers=h, json={})
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["token"]
    assert body["capabilities"] == ["agent.propose"]

    r = await client.get(f"{PREFIX}/{a.id}/tokens", headers=h)
    assert r.status_code == 200
    listed = r.json()[0]
    assert "token" not in listed         # nothing stores the value; nothing shows it
    assert listed["grant_id"] == body["grant_id"]


async def test_a_security_officer_can_suspend_but_not_create(
        client, session, tenant):
    """The separation that lets an incident responder act at 3am without also
    being able to widen a ceiling."""
    h = _headers(["security"])
    a = await _agent(session, tenant, slug="w")

    r = await client.post(f"{PREFIX}/{a.id}/status", headers=h,
                          json={"status": "suspended", "reason": "incident"})
    assert r.status_code == 200

    r = await client.post(PREFIX, headers=h, json={"slug": "y", "kind": "internal"})
    assert r.status_code == 403


async def test_a_grant_from_another_agent_is_404(client, session, tenant, tokens_on):
    a = await _agent(session, tenant, slug="a1")
    b = await _agent(session, tenant, slug="b1")
    _, grant = await tokens.mint_agent_token(session, b)
    await session.commit()

    r = await client.delete(f"{PREFIX}/{a.id}/tokens/{grant.id}",
                            headers=_headers(["admin"]))
    assert r.status_code == 404


async def test_the_status_history_survives_in_the_detail_view(
        client, session, tenant):
    h = _headers(["admin"])
    a = await _agent(session, tenant, slug="w", caps=["agent.propose"])
    await identity.revoke_capability(session, a, "agent.propose")
    await identity.set_status(session, a, "suspended", reason="test")
    await session.commit()

    r = await client.get(f"{PREFIX}/{a.id}", headers=h)
    assert r.status_code == 200
    body = r.json()
    assert body["capabilities"] == []
    assert len(body["capability_history"]) == 1
    assert body["capability_history"][0]["revoked_at"] is not None
    assert [h_["to"] for h_ in body["status_history"]] == ["active", "suspended"]


# ══ who created it ══════════════════════════════════════════════════════════
#
# Sep item 39. Every helper above mints `user_id=str(uuid.uuid4())`, which is
# why the suite was green while the route refused half the platform's callers:
# `sub` is a UUID for a password login and a **username** for an SSO or service
# principal, and `agents.created_by` is a `GUID`. The username went straight
# into `_as_uuid` and came back as *"creator id is not a valid id"* — a 422 on a
# request that was correct in every respect. Proven live on 2026-09-06.


def _headers_named(username: str, roles: list[str]) -> dict[str, str]:
    """A caller whose token subject is a NAME, as an SSO login's is."""
    from velaris_shared.auth.jwt_handler import create_dev_token
    from velaris_shared.config import get_settings

    s = get_settings()
    token = create_dev_token(
        user_id=username, username=username, roles=roles,
        secret=s.auth_secret, private_key=s.auth_rsa_private_key or "")
    return {"Authorization": f"Bearer {token}"}


async def test_a_named_caller_can_create_an_agent_and_is_attributed(
        client, session, tenant):
    from velaris_shared.db.models import HelixUserModel
    from case_service.tenancy import identity as _identity

    me = HelixUserModel(id=uuid.uuid4(), username="jo.smith",
                        email="jo.smith@example.test")
    session.add(me)
    await session.commit()
    _identity.clear_actor_cache()

    r = await client.post(PREFIX, headers=_headers_named("jo.smith", ["admin"]),
                          json={"slug": "sso.made.this", "kind": "internal"})
    assert r.status_code == 201, r.text

    row = (await session.execute(select(AgentModel).where(
        AgentModel.slug == "sso.made.this"))).scalar_one()
    # Not merely "it did not 422": the point is the audit row names the person.
    # Degrading to NULL here would keep the route working and still lose the
    # only record of who registered an AI principal.
    assert row.created_by == me.id

    hist = (await session.execute(select(AgentStatusHistoryModel).where(
        AgentStatusHistoryModel.agent_id == row.id))).scalars().all()
    assert [h.changed_by for h in hist] == [me.id]


async def test_a_caller_with_no_user_row_is_recorded_as_absent_not_refused(
        client, session, tenant):
    """The degrade. A service principal has no user row and never will; the
    column is nullable and already means "not recorded". What must not happen
    is the 422 — refusing the write would make the whole route unusable for a
    caller the platform authenticated perfectly well."""
    from case_service.tenancy import identity as _identity

    _identity.clear_actor_cache()
    r = await client.post(PREFIX, headers=_headers_named("intake_webhook", ["admin"]),
                          json={"slug": "webhook.made.this", "kind": "internal"})
    assert r.status_code == 201, r.text

    row = (await session.execute(select(AgentModel).where(
        AgentModel.slug == "webhook.made.this"))).scalar_one()
    assert row.created_by is None


async def test_a_uuid_caller_is_still_stored_verbatim(client, session, tenant):
    """The compatibility claim, at the route. An install working today must keep
    storing precisely what it stores today — no lookup, no substitution, even
    when the id names no user row."""
    from case_service.tenancy import identity as _identity

    _identity.clear_actor_cache()
    who = uuid.uuid4()
    from velaris_shared.auth.jwt_handler import create_dev_token
    from velaris_shared.config import get_settings
    s = get_settings()
    token = create_dev_token(
        user_id=str(who), username="whoever", roles=["admin"],
        secret=s.auth_secret, private_key=s.auth_rsa_private_key or "")

    r = await client.post(PREFIX, headers={"Authorization": f"Bearer {token}"},
                          json={"slug": "uuid.made.this", "kind": "internal"})
    assert r.status_code == 201, r.text

    row = (await session.execute(select(AgentModel).where(
        AgentModel.slug == "uuid.made.this"))).scalar_one()
    assert row.created_by == who
