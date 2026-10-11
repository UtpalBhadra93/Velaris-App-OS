"""Oct item 2 — the rep desk's action rail, and the consent gate it found broken.

What must hold, from the item:

* each action asks the consent ledger first and is **disabled with the reason**
  when consent is missing — a rep must not promise a channel the customer opted
  out of;
* each is gated by the rep's own privilege;
* anything shown respects the call's verification tier.

And one defect found on the way: `interactions.consent_gate` passed `subject=`
to `has_consent`, whose parameter is `subject_ref`. The TypeError was caught, so
every on-behalf check since item 12 shipped answered "consent could not be
read". Its only test asserted the refusal, which the bug produced for free.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio
from sqlalchemy import select

from velaris_shared.auth.models import AuthenticatedUser
from velaris_shared.db.models import (
    CaseInstanceModel,
    CaseTypeModel,
    ChannelConversationModel,
    ChannelEndpointModel,
    ConsentRecordModel,
    MarketplaceInstallModel,
    ScheduledReleaseModel,
    TenantModel,
)

from case_service.channels import optout
from case_service.compliance import consent
from case_service.engage import action_rail, entitlement, identity, interactions
from tests.conftest import LIFECYCLE_PROCESS_ID  # type: ignore[attr-defined]

pytestmark = pytest.mark.asyncio

PHONE = "+447700900123"
EMAIL = "jane@example.test"


@pytest_asyncio.fixture
async def world(session):
    """Tenant `default`, entitled; Jane with an email and a phone; one open case
    of hers with an SMS conversation on it; one call from her, unverified."""
    from case_service.tenancy import identity as _identity

    _identity.clear_cache()
    tenant = TenantModel(id=uuid.uuid4(), slug="default", name="Default",
                         settings={"portal": {"enabled": True}})
    other = TenantModel(id=uuid.uuid4(), slug="other", name="Other")
    session.add_all([tenant, other])
    session.add(ScheduledReleaseModel(
        id=uuid.uuid4(), feature_key=entitlement.FEATURE_KEY,
        title="Customer Engagement", status="released", enabled="true"))
    session.add(MarketplaceInstallModel(
        id=uuid.uuid4(), tenant_id="default", package_id=entitlement.PACKAGE_ID,
        package_version="1.0.0", package_type="module", approved_by="alice"))
    await session.commit()

    jane = await identity.create_profile(session, tenant_uuid=tenant.id,
                                         display_name="Jane Smith")
    await identity.add_identifier(session, tenant_uuid=tenant.id, customer_id=jane.id,
                                  kind="email", value=EMAIL, is_primary=True)
    await identity.add_identifier(session, tenant_uuid=tenant.id, customer_id=jane.id,
                                  kind="phone", value=PHONE)
    ct = CaseTypeModel(
        id=uuid.uuid4(), name="Claims", version="1.0.0",
        lifecycle_process_id=LIFECYCLE_PROCESS_ID,
        definition_json={"stages": [{"id": "s1", "name": "Pay", "order": 1, "steps": [
            {"id": "fee", "name": "Excess fee", "step_type": "payment_request"}]}]})
    session.add(ct)
    await session.flush()
    case = CaseInstanceModel(
        id=uuid.uuid4(), case_number="CLM-1", case_type_id=ct.id,
        case_type_version="1.0.0", status="open", priority="medium",
        tenant_id=tenant.id, created_by="rep", data={})
    # Another tenant's case, wrongly linked to Jane. It must never be offered.
    foreign = CaseInstanceModel(
        id=uuid.uuid4(), case_number="OTH-1", case_type_id=ct.id,
        case_type_version="1.0.0", status="open", priority="medium",
        tenant_id=other.id, created_by="x", data={})
    session.add_all([case, foreign])
    await session.flush()
    for c in (case, foreign):
        await identity.link(session, tenant_uuid=tenant.id, customer_id=jane.id,
                            object_kind="case", object_id=c.id)
    endpoint = ChannelEndpointModel(id=uuid.uuid4(), tenant_id="default", channel="sms",
                                    provider="twilio", address="+15550000000")
    session.add(endpoint)
    await session.flush()
    conv = ChannelConversationModel(id=uuid.uuid4(), tenant_id="default",
                                    endpoint_id=endpoint.id, peer_address=PHONE,
                                    case_id=case.id, status="open")
    session.add(conv)
    await session.commit()

    pop = await interactions.screen_pop(session, tenant_uuid=tenant.id,
                                        tenant_token="default", channel="voice", ani=PHONE)
    await session.commit()
    return {"tenant": tenant, "jane": jane, "case": case, "foreign": foreign,
            "endpoint": endpoint, "conv": conv,
            "interaction_id": uuid.UUID(pop["interaction_id"])}


async def _call(session, world):
    from velaris_shared.db.models import CustomerInteractionModel
    return await session.get(CustomerInteractionModel, world["interaction_id"])


async def _verify(session, world, tier="full"):
    row = await _call(session, world)
    await interactions.verify(session, interaction=row, factor="otp", ok=True, to_tier=tier)
    await session.commit()


async def _rail(session, world, roles=("case_worker",)):
    user = AuthenticatedUser(user_id="rep", roles=list(roles))
    return await action_rail.rail(
        session, user=user, tenant_uuid=world["tenant"].id, tenant_token="default",
        profile=world["jane"], interaction=await _call(session, world))


def _action(out, key):
    return next(a for a in out["actions"] if a["key"] == key)


# ══ the tier ════════════════════════════════════════════════════════════════


async def test_an_unverified_caller_cannot_be_charged_or_consented_for(session, world):
    out = await _rail(session, world)
    assert out["verification_level"] == "none"
    for key in ("payment", "consent"):
        kinds = {b["kind"] for b in _action(out, key)["blocked_by"]}
        assert "tier" in kinds, key
    # A callback needs nobody proven and names no case.
    assert _action(out, "callback")["enabled"] is True


async def test_an_unverified_call_shows_nothing_the_360_withholds(session, world):
    """Found in the 2026-09-29 audit of the rail itself: it listed the
    customer's case numbers, conversations and consent record on an ANONYMOUS
    call — three panels the 360 withholds below partial/Full."""
    out = await _rail(session, world)
    assert out["cases"] == []
    assert "CLM-1" not in str(out)
    assert out["consent"]["email"] == {"readable": False}
    for key in ("reply", "live_session", "follow_up"):
        kinds = [b["kind"] for b in _action(out, key)["blocked_by"]]
        assert kinds == ["tier"], (key, kinds)          # once, and not "no open case"


async def test_consent_states_need_full_verification(session, world):
    await _verify(session, world, tier="partial")
    out = await _rail(session, world)
    assert [c["case_number"] for c in out["cases"]] == ["CLM-1"]
    assert out["consent"]["email"] == {"readable": False}


async def test_full_verification_lifts_the_tier_reason_and_only_that(session, world):
    await _verify(session, world)
    out = await _rail(session, world)
    assert _action(out, "consent")["enabled"] is True
    payment = _action(out, "payment")
    # Still blocked — but now ONLY because no payment connector exists, and it
    # says so, rather than repeating "verify the caller".
    assert {b["kind"] for b in payment["blocked_by"]} == {"prerequisite"}
    assert "connector" in payment["blocked_by"][0]["reason"]


# ══ consent: disabled WITH THE REASON ═══════════════════════════════════════


async def test_a_number_that_texted_stop_cannot_be_replied_to(session, world):
    await _verify(session, world)
    assert _action(await _rail(session, world), "reply")["enabled"] is True

    await optout.record_stop(session, PHONE, tenant_id="default")
    await session.commit()

    out = await _rail(session, world)
    reply = _action(out, "reply")
    assert reply["enabled"] is False
    assert [b["kind"] for b in reply["blocked_by"]] == ["consent"]
    assert "START" in reply["blocked_by"][0]["reason"]
    # The same rule as the send path, so the conversation says it too.
    assert out["cases"][0]["conversations"][0]["opted_out"] is True
    # A document request goes out on the same channel, so it is refused alike.
    assert _action(out, "document_request")["enabled"] is False


async def test_silence_is_not_an_opt_out(session, world):
    """The trap `optout.py` records: `has_consent` fails closed, and wiring the
    rail to it would disable reply for every customer nobody recorded a grant
    for — all of them."""
    await _verify(session, world)
    out = await _rail(session, world, roles=("admin",))
    assert out["consent"]["phone"]["purposes"]["marketing"] == "none"
    assert _action(out, "reply")["enabled"] is True


async def test_a_closed_whatsapp_window_is_a_note_not_a_refusal(session, world):
    await _verify(session, world)
    world["endpoint"].channel = "whatsapp"
    world["conv"].window_expires_at = datetime.now(timezone.utc) - timedelta(hours=1)
    await session.commit()
    reply = _action(await _rail(session, world), "reply")
    assert reply["enabled"] is True
    assert any("template" in n for n in reply["notes"])


# ══ privilege ═══════════════════════════════════════════════════════════════


async def test_a_viewer_is_told_it_is_their_role(session, world):
    await _verify(session, world)
    out = await _rail(session, world, roles=("viewer",))
    for key in ("reply", "payment", "callback", "consent", "follow_up"):
        kinds = {b["kind"] for b in _action(out, key)["blocked_by"]}
        assert "privilege" in kinds, key


# ══ what is listed ══════════════════════════════════════════════════════════


async def test_another_tenants_case_is_never_offered(session, world):
    await _verify(session, world, tier="partial")
    out = await _rail(session, world)
    assert [c["case_number"] for c in out["cases"]] == ["CLM-1"]
    assert out["cases"][0]["payment_steps"] == [{"id": "fee", "name": "Excess fee"}]


async def test_a_closed_case_is_not_offered(session, world):
    await _verify(session, world, tier="partial")
    world["case"].status = "closed"
    await session.commit()
    out = await _rail(session, world)
    assert out["cases"] == []
    reasons = [b["reason"] for b in _action(out, "reply")["blocked_by"]]
    assert any("no open case" in r for r in reasons)


async def test_the_conversation_hint_does_not_carry_the_number(session, world):
    await _verify(session, world)
    out = await _rail(session, world)
    assert PHONE not in str(out["cases"])


async def test_a_tenant_without_a_portal_cannot_send_a_document_request(session, world):
    await _verify(session, world)
    from velaris_shared.config import get_settings
    monkey = get_settings()
    old_base, monkey.public_base_url = monkey.public_base_url, "https://velaris.example"
    try:
        assert _action(await _rail(session, world), "document_request")["enabled"] is True
    finally:
        monkey.public_base_url = old_base
    world["tenant"].settings = {}
    await session.commit()
    out = await _rail(session, world)
    assert out["portal_link"] is None
    assert _action(out, "document_request")["enabled"] is False


# ══ capturing consent ═══════════════════════════════════════════════════════


async def _capture(session, world, **over):
    kw = dict(purpose="marketing", action="granted", subject_kind="email")
    kw.update(over)
    return await action_rail.capture_consent(
        session, tenant_token="default", profile=world["jane"],
        interaction=await _call(session, world), actor="rep", **kw)


async def test_consent_is_not_recorded_for_an_unproven_caller(session, world):
    await _verify(session, world, tier="partial")
    with pytest.raises(PermissionError):
        await _capture(session, world)


async def test_a_captured_consent_names_the_call_the_rep_and_the_tier(session, world):
    await _verify(session, world)
    await _capture(session, world)
    await session.commit()
    row = (await session.execute(select(ConsentRecordModel))).scalars().one()
    assert row.subject_ref == EMAIL and row.source == "rep_desk"
    assert row.actor_id == "rep"
    assert row.evidence["interaction_id"] == str(world["interaction_id"])
    assert row.evidence["verification_level"] == "full"
    out = await _rail(session, world, roles=("admin",))
    assert out["consent"]["email"]["purposes"]["marketing"] == "granted"
    # A case worker may RECORD consent but, like the 360's consent panel, not
    # read the record back — `compliance.consent.read` is not theirs.
    assert (await _rail(session, world))["consent"]["email"] == {"readable": False}


async def test_case_processing_cannot_be_withdrawn_on_a_call(session, world):
    """It rests on contract. A rep recording its withdrawal would claim case
    work stopped when it lawfully continues."""
    await _verify(session, world)
    with pytest.raises(ValueError):
        await _capture(session, world, purpose="case_processing", action="withdrawn")


async def test_a_phone_marketing_grant_is_an_sms_opt_back_in(session, world):
    """The rail and the SMS suppression list read one ledger row, so a customer
    who asks on the phone to get texts again is honoured by the send path."""
    await _verify(session, world)
    await optout.record_stop(session, PHONE, tenant_id="default")
    await session.commit()
    assert await optout.may_send(session, PHONE, tenant_id="default") is False
    await _capture(session, world, subject_kind="phone")
    await session.commit()
    assert await optout.may_send(session, PHONE, tenant_id="default") is True


# ══ the consent gate on "raise a case for them" ═════════════════════════════


async def test_a_recorded_grant_is_finally_seen(session, world):
    """Before the fix this returned `granted: False` whatever the ledger held."""
    await consent.record(session, subject_ref=EMAIL, purpose="marketing",
                         tenant_id="default", source="test")
    await session.commit()
    out = await interactions.consent_gate(session, tenant_token="default",
                                          profile=world["jane"], purpose="marketing")
    assert out["granted"] is True


async def test_raising_a_case_needs_no_consent_record(session, world):
    """What Studio sends. `case_management` was never in the vocabulary, and case
    work rests on contract — so every rep was told consent was missing."""
    out = await interactions.consent_gate(session, tenant_token="default",
                                          profile=world["jane"], purpose="case_management")
    assert out["granted"] is True and out["purpose"] == "case_processing"


async def test_but_a_recorded_withdrawal_of_case_processing_is_honoured(session, world):
    await consent.record(session, subject_ref=EMAIL, purpose="case_processing",
                         action="withdrawn", tenant_id="default", source="test")
    await session.commit()
    out = await interactions.consent_gate(session, tenant_token="default",
                                          profile=world["jane"], purpose="case_management")
    assert out["granted"] is False and out["action"] == "capture_consent_now"


# ══ the API ═════════════════════════════════════════════════════════════════


async def test_the_rail_route_refuses_another_customers_call(client, session, world):
    bob = await identity.create_profile(session, tenant_uuid=world["tenant"].id,
                                        display_name="Bob")
    await session.commit()
    r = await client.get(f"/api/v1/engage/profiles/{bob.id}/action-rail",
                         params={"interaction_id": str(world["interaction_id"])})
    assert r.status_code == 409, r.text


async def test_the_rail_and_consent_routes_end_to_end(client, session, world):
    jane, iid = world["jane"], str(world["interaction_id"])
    r = await client.get(f"/api/v1/engage/profiles/{jane.id}/action-rail",
                         params={"interaction_id": iid})
    assert r.status_code == 200, r.text
    assert {a["key"] for a in r.json()["actions"]} == {s.key for s in action_rail.SPECS}

    body = {"interaction_id": iid, "purpose": "marketing", "action": "granted"}
    r = await client.post(f"/api/v1/engage/profiles/{jane.id}/consent", json=body)
    assert r.status_code == 409, r.text                 # not verified yet
    await _verify(session, world)
    r = await client.post(f"/api/v1/engage/profiles/{jane.id}/consent", json=body)
    assert r.status_code == 201, r.text
    r = await client.post(f"/api/v1/engage/profiles/{jane.id}/consent",
                          json={**body, "purpose": "biometric"})
    assert r.status_code == 422, r.text


async def test_a_relative_portal_link_is_not_sent(session, world):
    """Without a public address the link would be `/portal/…`, which opens
    nothing on a customer's phone."""
    await _verify(session, world)
    from velaris_shared.config import get_settings
    s = get_settings()
    old, s.public_base_url = s.public_base_url, ""
    try:
        out = await _rail(session, world)
    finally:
        s.public_base_url = old
    reasons = [b["reason"] for b in _action(out, "document_request")["blocked_by"]]
    assert any("public address" in r for r in reasons)


async def test_a_live_session_is_not_offered_when_meet_cannot_start_one(session, world, monkeypatch):
    from case_service.meet import livekit
    from case_service.meet import service as meet

    await _verify(session, world)

    async def embedded(*_a, **_k):
        return "embedded"
    monkeypatch.setattr(meet, "resolve_driver", embedded)
    monkeypatch.setattr(livekit, "configured", lambda: False)
    out = await _rail(session, world)
    assert _action(out, "live_session")["enabled"] is False
    assert "LiveKit" in _action(out, "live_session")["blocked_by"][0]["reason"]


async def test_the_360_cases_panel_is_shown_at_all(client, session, world):
    """Found in the same audit: the 360 asked `case.read` once, without a case
    id, and HxGuard fails that closed — so the cases panel was withheld from
    every reader, admin included, from item 12's release until 2026-09-29.
    Now decided per case: this tenant's readable cases, and a count of the
    rest. The other tenant's case linked to Jane is never listed."""
    await _verify(session, world, tier="partial")
    r = await client.get(f"/api/v1/engage/profiles/{world['jane'].id}/360",
                         params={"interaction_id": str(world["interaction_id"])})
    assert r.status_code == 200, r.text
    body = r.json()
    assert "cases" in body["panels"], body["withheld"]
    assert [c["object_id"] for c in body["panels"]["cases"]] == [str(world["case"].id)]
    assert str(world["foreign"].id) not in str(body)
    assert body["cases_not_shown"] == 1
