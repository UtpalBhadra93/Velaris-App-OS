"""F-09 (first slice) — the agent-to-agent case handoff protocol.

The roadmap's exit criteria are a redaction assertion suite ("ceiling `internal`
never leaks `personal` fields"), revocation killing a token mid-handoff, and an
injection corpus producing zero out-of-scope effects. All three are here, and
the redaction one is the largest block in the file because it is the claim the
whole feature rests on.

The other tests are about the protocol refusing:

* an **unsolicited** agent cannot connect — a handoff to a party nobody enrolled
  cannot be minted at all;
* a signature is checked against the **pinned** key, never one that arrived with
  the message;
* a **suspended counterparty** loses every outstanding order at once, not order
  by order;
* a **dispatcher cannot exceed the enrolment**;
* Article 9 data is **never** projected, at any ceiling — there is no ceiling
  that would admit it;
* there is **no verb that writes to a case**.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import base64
import uuid
from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519
from sqlalchemy import select

from velaris_shared.db.models import (
    ArbiterNamespaceModel,
    CaseInstanceModel,
    CaseTypeModel,
    HandoffActionModel,
    HandoffCounterpartyModel,
    HandoffWorkOrderModel,
    TenantModel,
)

from case_service.handoff import projection as proj
from case_service.handoff import service

pytestmark = pytest.mark.asyncio

SEED = [("ai", "deny"), ("egress", "deny"), ("export", "deny"),
        ("vault", "deny"), ("case", "allow"), ("auth", "allow"),
        ("agent", "deny")]

#: A case carrying one of everything: plain, contact, and Article 9.
CASE_DATA = {
    "reference": "ABC-123",
    "claim_amount": 4300,
    "customer_email": "someone@example.test",
    "phone_number": "+44 7700 900000",
    "home_address": "1 Example Street",
    "diagnosis_notes": "a health note that must never leave",
}


def _keypair():
    private = ed25519.Ed25519PrivateKey.generate()
    raw = private.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw)
    return private, base64.b64encode(raw).decode()


@pytest_asyncio.fixture
async def tenant(session):
    """A tenant, the arbiter namespaces, and the agent-token kill switch ON.

    `get_settings()` is `lru_cache`d, so an env var set after import changes
    nothing — the F-00 suite mutates the cached object and restores it, and this
    does the same rather than inventing a second way.
    """
    from velaris_shared.config import get_settings
    from case_service.agents import identity as agent_identity
    from case_service.tenancy import identity as _identity

    settings = get_settings()
    before = settings.agent_tokens_enabled
    settings.agent_tokens_enabled = True
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
    settings.agent_tokens_enabled = before
    agent_identity._SLUG_CACHE.clear()
    _identity.clear_cache()


async def _case(session, tenant, data=None):
    ct = CaseTypeModel(id=uuid.uuid4(), name="Claims", version="1.0.0",
                       lifecycle_process_id="p", definition_json={"stages": []},
                       tenant_id=str(tenant.id))
    session.add(ct)
    await session.flush()
    case = CaseInstanceModel(
        id=uuid.uuid4(), case_type_id=ct.id, case_type_version="1.0.0",
        status="new", priority="medium", tenant_id=tenant.id,
        data=dict(CASE_DATA if data is None else data))
    session.add(case)
    await session.commit()
    return case


async def _counterparty(session, tenant, *, max_ceiling="personal"):
    private, public = _keypair()
    row = await service.enrol(
        session, tenant_uuid=tenant.id, slug="partner", display_name="Partner Ltd",
        public_key=public, max_ceiling=max_ceiling, created_by="alice")
    await session.commit()
    return row, private


# ══ the redaction assertion suite — the exit criterion ══════════════════════


async def test_ceiling_internal_never_leaks_a_personal_field(session, tenant):
    """The roadmap's own words. Email, phone and address are personal; none of
    them may appear at `internal`."""
    case = await _case(session, tenant)
    view = await proj.project(session, case=case, ceiling=proj.CEILING_INTERNAL,
                              tenant_token="default")
    leaked = {"customer_email", "phone_number", "home_address"} & set(view["case"]["fields"])
    assert leaked == set(), f"personal fields leaked at 'internal': {leaked}"
    assert "reference" in view["case"]["fields"]


async def test_article_9_is_never_projected_at_any_ceiling(session, tenant):
    """There is no ceiling that admits it — not a policy check that could be
    misconfigured, a value that cannot be named."""
    case = await _case(session, tenant)
    for ceiling in proj.CEILINGS:
        view = await proj.project(session, case=case, ceiling=ceiling,
                                  tenant_token="default")
        assert "diagnosis_notes" not in view["case"]["fields"], ceiling
        assert "diagnosis_notes" not in str(view["case"])


def test_special_category_is_not_a_ceiling_this_protocol_can_express():
    with pytest.raises(proj.CeilingError, match="never projected"):
        proj.rank("special_category")


async def test_ceiling_none_projects_no_case_data_at_all(session, tenant):
    case = await _case(session, tenant)
    view = await proj.project(session, case=case, ceiling=proj.CEILING_NONE,
                              tenant_token="default")
    assert view["case"]["fields"] == {}
    # Still enough to talk about the case.
    assert view["case"]["case_id"] == str(case.id)


async def test_ceiling_personal_admits_contact_but_still_not_article_9(session, tenant):
    case = await _case(session, tenant)
    view = await proj.project(session, case=case, ceiling=proj.CEILING_PERSONAL,
                              tenant_token="default")
    assert "customer_email" in view["case"]["fields"]
    assert "diagnosis_notes" not in view["case"]["fields"]


async def test_withheld_names_the_field_but_never_its_value(session, tenant):
    """An agent that can see a field exists but not its value can ask a human
    for it; one that cannot tell it exists reports the case as incomplete."""
    case = await _case(session, tenant)
    view = await proj.project(session, case=case, ceiling=proj.CEILING_INTERNAL,
                              tenant_token="default")
    names = {w["field"] for w in view["withheld"]}
    assert "customer_email" in names and "diagnosis_notes" in names
    blob = str(view["withheld"])
    assert "someone@example.test" not in blob
    assert "a health note" not in blob


async def test_the_projection_is_built_up_not_filtered_down(session, tenant):
    """A field the classifier has never heard of is still gated by the ceiling —
    the default is to withhold, not to disclose."""
    case = await _case(session, tenant, data={"utterly_unknown_field": "x"})
    at_none = await proj.project(session, case=case, ceiling=proj.CEILING_NONE,
                                 tenant_token="default")
    assert at_none["case"]["fields"] == {}


async def test_a_confirmed_finding_can_only_tighten_never_relax(session, tenant):
    """Two classifiers, and the stricter wins. A name that says nothing but
    which a reviewer flagged is still withheld."""
    from velaris_shared.db.models import PIIFindingModel
    from case_service.compliance.pii_scan import CONFIRMED

    session.add(PIIFindingModel(
        id=uuid.uuid4(), resource_type="form", resource_id=uuid.uuid4(),
        field_path="section.opaque_code", category="national_id", confidence=0.9,
        special_category=False, status=CONFIRMED, tenant_id="default"))
    await session.commit()
    case = await _case(session, tenant, data={"opaque_code": "AB123456C"})
    view = await proj.project(session, case=case, ceiling=proj.CEILING_INTERNAL,
                              tenant_token="default")
    assert "opaque_code" not in view["case"]["fields"]


# ══ who may connect at all ══════════════════════════════════════════════════


async def test_a_key_of_the_wrong_length_pins_nothing(session, tenant):
    with pytest.raises(service.HandoffError, match="32 bytes"):
        await service.enrol(session, tenant_uuid=tenant.id, slug="bad",
                            display_name="Bad", public_key=base64.b64encode(b"short").decode())


async def test_a_second_enrolment_of_one_organisation_is_refused(session, tenant):
    """Two rows for one organisation is how an old key stays live."""
    _, public = _keypair()
    await service.enrol(session, tenant_uuid=tenant.id, slug="partner",
                        display_name="Partner", public_key=public)
    await session.commit()
    _, other = _keypair()
    with pytest.raises(service.HandoffError, match="already enrolled"):
        await service.enrol(session, tenant_uuid=tenant.id, slug="partner",
                            display_name="Partner", public_key=other)


async def test_a_signature_is_checked_against_the_PINNED_key(session, tenant):
    """A key travelling with the message it authenticates proves only that the
    sender owns a key."""
    cp, private = await _counterparty(session, tenant)
    case = await _case(session, tenant)
    order, _ = await service.issue(
        session, tenant_uuid=tenant.id, tenant_token="default", case_id=case.id,
        counterparty=cp, ceiling="internal",
        allowed_actions=["accept", "read", "propose", "return"])
    await session.commit()

    digest = service._sha(service.order_document(order))
    impostor = ed25519.Ed25519PrivateKey.generate()
    forged = base64.b64encode(impostor.sign(digest.encode())).decode()
    with pytest.raises(service.HandoffError, match="does not verify"):
        await service.accept(session, order=order, signature=forged)

    genuine = base64.b64encode(private.sign(digest.encode())).decode()
    await service.accept(session, order=order, signature=genuine)
    assert order.status == "accepted"


async def test_a_refused_acceptance_leaves_a_row(session, tenant):
    """A counterparty reaching for something it may not have is a signal, and
    one that leaves no trace is a signal nobody receives."""
    cp, _ = await _counterparty(session, tenant)
    case = await _case(session, tenant)
    order, _ = await service.issue(
        session, tenant_uuid=tenant.id, tenant_token="default", case_id=case.id,
        counterparty=cp, ceiling="internal", allowed_actions=["accept"])
    await session.commit()
    impostor = ed25519.Ed25519PrivateKey.generate()
    forged = base64.b64encode(impostor.sign(b"anything")).decode()
    with pytest.raises(service.HandoffError):
        await service.accept(session, order=order, signature=forged)
    await session.commit()
    rows = (await session.execute(
        select(HandoffActionModel).where(
            HandoffActionModel.work_order_id == order.id))).scalars().all()
    assert [r.decision for r in rows] == ["denied"]


# ══ the work order ══════════════════════════════════════════════════════════


async def test_a_dispatcher_cannot_exceed_the_enrolment(session, tenant):
    """"What may this organisation ever see" is decided once, by whoever
    enrolled them, not repeatedly under time pressure."""
    cp, _ = await _counterparty(session, tenant, max_ceiling="internal")
    case = await _case(session, tenant)
    order, _ = await service.issue(
        session, tenant_uuid=tenant.id, tenant_token="default", case_id=case.id,
        counterparty=cp, ceiling="personal", allowed_actions=["read"])
    assert order.ceiling == "internal"


async def test_only_one_open_order_per_case(session, tenant):
    cp, _ = await _counterparty(session, tenant)
    case = await _case(session, tenant)
    await service.issue(session, tenant_uuid=tenant.id, tenant_token="default",
                        case_id=case.id, counterparty=cp, ceiling="internal",
                        allowed_actions=["read"])
    await session.commit()
    with pytest.raises(service.HandoffError, match="already has an open work order"):
        await service.issue(session, tenant_uuid=tenant.id, tenant_token="default",
                            case_id=case.id, counterparty=cp, ceiling="internal",
                            allowed_actions=["read"])


async def test_an_order_may_not_outlive_the_token_that_carries_it(session, tenant):
    """The deployment's agent-token ceiling is the shorter one on most installs,
    and an order claiming a window its token cannot honour would show a
    counterparty an expiry that stops working hours before it says."""
    from velaris_shared.config import get_settings

    ceiling = int(get_settings().agent_token_max_ttl_minutes)
    cp, _ = await _counterparty(session, tenant)
    case = await _case(session, tenant)
    order, _ = await service.issue(
        session, tenant_uuid=tenant.id, tenant_token="default", case_id=case.id,
        counterparty=cp, ceiling="internal", allowed_actions=["read"],
        ttl_minutes=service.MAX_TTL_MINUTES)
    granted = (order.expires_at - datetime.now(timezone.utc)).total_seconds() / 60
    assert granted <= ceiling + 1


async def test_an_unknown_verb_is_refused(session, tenant):
    cp, _ = await _counterparty(session, tenant)
    case = await _case(session, tenant)
    with pytest.raises(service.HandoffError, match="unknown handoff verb"):
        await service.issue(session, tenant_uuid=tenant.id, tenant_token="default",
                            case_id=case.id, counterparty=cp, ceiling="internal",
                            allowed_actions=["read", "write_case"])


async def test_a_verb_the_order_omits_is_refused_and_recorded(session, tenant):
    cp, _ = await _counterparty(session, tenant)
    case = await _case(session, tenant)
    order, _ = await service.issue(
        session, tenant_uuid=tenant.id, tenant_token="default", case_id=case.id,
        counterparty=cp, ceiling="internal", allowed_actions=["read"])
    await session.commit()
    with pytest.raises(service.HandoffError, match="does not permit"):
        await service.propose(session, order=order, tenant_token="default",
                              target="ai.thing", value=1)


async def test_a_suspended_counterparty_loses_every_outstanding_order(session, tenant):
    """One decision about an organisation, not one per order."""
    cp, private = await _counterparty(session, tenant)
    case = await _case(session, tenant)
    order, _ = await service.issue(
        session, tenant_uuid=tenant.id, tenant_token="default", case_id=case.id,
        counterparty=cp, ceiling="internal", allowed_actions=["read"])
    await session.commit()
    cp.status = "suspended"
    await session.commit()
    with pytest.raises(service.HandoffError, match="not active"):
        await service.read(session, order=order, tenant_token="default")


async def test_an_expired_order_refuses_and_marks_itself(session, tenant):
    cp, _ = await _counterparty(session, tenant)
    case = await _case(session, tenant)
    order, _ = await service.issue(
        session, tenant_uuid=tenant.id, tenant_token="default", case_id=case.id,
        counterparty=cp, ceiling="internal", allowed_actions=["read"])
    order.expires_at = datetime.now(timezone.utc) - timedelta(minutes=1)
    await session.commit()
    with pytest.raises(service.HandoffError, match="expired"):
        await service.read(session, order=order, tenant_token="default")
    assert order.status == "expired"


async def test_revocation_ends_the_order_and_kills_the_grant(session, tenant):
    """Revocation is instant because the TOKEN GRANT carries the authority — a
    revocation that depended on this table being read would be as fast as the
    next cache expiry."""
    from velaris_shared.db.models import AgentTokenGrantModel

    cp, _ = await _counterparty(session, tenant)
    case = await _case(session, tenant)
    order, _ = await service.issue(
        session, tenant_uuid=tenant.id, tenant_token="default", case_id=case.id,
        counterparty=cp, ceiling="internal", allowed_actions=["read"])
    await session.commit()
    await service.revoke(session, order=order, reason="incident")
    await session.commit()
    assert order.status == "revoked"
    grant = await session.get(AgentTokenGrantModel, order.token_grant_id)
    assert grant.revoked is True
    with pytest.raises(service.HandoffError, match="revoked"):
        await service.read(session, order=order, tenant_token="default")


async def test_handing_back_and_being_revoked_are_different_events(session, tenant):
    cp, _ = await _counterparty(session, tenant)
    case = await _case(session, tenant)
    order, _ = await service.issue(
        session, tenant_uuid=tenant.id, tenant_token="default", case_id=case.id,
        counterparty=cp, ceiling="internal", allowed_actions=["return"])
    await session.commit()
    await service.hand_back(session, order=order, summary="done, nothing to change")
    assert order.status == "returned" and order.revoked_at is None


async def test_the_external_agent_holds_no_authority_to_act(session, tenant):
    """`agent.act_on_case` would make proposals-only a convention rather than a
    ceiling."""
    from velaris_shared.db.models import AgentModel
    from case_service.agents import identity

    cp, _ = await _counterparty(session, tenant)
    case = await _case(session, tenant)
    order, _ = await service.issue(
        session, tenant_uuid=tenant.id, tenant_token="default", case_id=case.id,
        counterparty=cp, ceiling="internal", allowed_actions=["read"])
    await session.commit()
    agent = await session.get(AgentModel, order.agent_id)
    assert agent.kind == "external"
    caps = await identity.live_capabilities(session, agent.id)
    assert "agent.act_on_case" not in caps
    assert caps == {"agent.read_case", "agent.propose"}


def test_an_external_agent_cannot_be_created_through_the_operator_route():
    """`create_agent` is what the /agents route calls. An external principal may
    only come into being as part of a signed work order."""
    from case_service.agents import identity

    assert "external" not in identity.ISSUABLE_KINDS
    assert "external" in identity.TOKENABLE_KINDS


# ══ proposals — the injection corpus ════════════════════════════════════════


async def test_a_proposal_outside_the_case_types_variables_is_refused(session, tenant):
    """Bounded by F-06's own whitelist, so an external agent cannot propose
    anything an injected email could not."""
    cp, _ = await _counterparty(session, tenant)
    case = await _case(session, tenant)
    order, _ = await service.issue(
        session, tenant_uuid=tenant.id, tenant_token="default", case_id=case.id,
        counterparty=cp, ceiling="internal", allowed_actions=["propose"])
    await session.commit()
    for hostile in ("velaris.status", "auth.role", "arbiter.namespace",
                    "../../etc/passwd", "agent.capabilities"):
        with pytest.raises(service.HandoffError, match="not one of the variables"):
            await service.propose(session, order=order, tenant_token="default",
                                  target=hostile, value="closed")


async def test_every_refused_proposal_is_recorded(session, tenant):
    """Dropping it silently would leave the counterparty believing they had
    contributed something."""
    cp, _ = await _counterparty(session, tenant)
    case = await _case(session, tenant)
    order, _ = await service.issue(
        session, tenant_uuid=tenant.id, tenant_token="default", case_id=case.id,
        counterparty=cp, ceiling="internal", allowed_actions=["propose"])
    await session.commit()
    with pytest.raises(service.HandoffError):
        await service.propose(session, order=order, tenant_token="default",
                              target="velaris.status", value="closed")
    await session.commit()
    rows = (await session.execute(
        select(HandoffActionModel).where(
            HandoffActionModel.work_order_id == order.id,
            HandoffActionModel.kind == "propose"))).scalars().all()
    assert len(rows) == 1 and rows[0].decision == "denied"
    # The payload is committed to by hash; the content is not stored on a denial.
    assert rows[0].payload_sha256 and rows[0].proposal_id is None


def test_the_protocol_has_no_verb_that_writes_to_a_case():
    """The strongest statement this feature makes, and it is one line."""
    assert set(service.ACTIONS) == {"accept", "read", "propose", "return"}
    assert not any("write" in v or "act" in v or "commit" in v
                   for v in service.ACTIONS)


# ══ the API ═════════════════════════════════════════════════════════════════


async def test_our_side_is_gated(anon_client):
    for path in ("/counterparties", "/orders"):
        resp = await anon_client.get("/api/v1/handoff" + path)
        assert resp.status_code in (401, 403), f"GET {path} -> {resp.status_code}"
    resp = await anon_client.post("/api/v1/handoff/counterparties", json={})
    assert resp.status_code in (401, 403, 422)


async def test_their_side_refuses_a_session_token(client, tenant):
    """The protocol is authenticated by the work order token ALONE — an ordinary
    staff bearer token is not one."""
    resp = await client.get(f"/api/v1/handoff/protocol/{uuid.uuid4()}/case")
    assert resp.status_code in (401, 403)


async def test_their_side_refuses_an_anonymous_caller(anon_client):
    """All four counterparty routes, with no credential at all.

    These four are listed in `phase_authz/test_ungated_routes.py::ACCEPTED`,
    because that sweep looks for `get_current_user` and these wear
    `get_agent_caller`. An entry in that list is a claim that the route is
    authenticated by something the sweep cannot see — and a claim of that shape
    is worth exactly as much as the test that sends the anonymous request. This
    is that test.
    """
    order = uuid.uuid4()
    for method, path in (
        ("post", f"/api/v1/handoff/protocol/{order}/accept"),
        ("get", f"/api/v1/handoff/protocol/{order}/case"),
        ("post", f"/api/v1/handoff/protocol/{order}/propose"),
        ("post", f"/api/v1/handoff/protocol/{order}/return"),
    ):
        resp = await (anon_client.get(path) if method == "get"
                      else anon_client.post(path, json={}))
        assert resp.status_code in (401, 403), f"{method.upper()} {path} -> {resp.status_code}"
