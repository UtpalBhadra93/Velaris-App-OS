"""Omnichannel inbound — the webhook, the ladder, idempotency and STOP.

The inbound route creates cases from unauthenticated input. Its signature check
is the only thing between a provider's traffic and anyone on the internet, and
the resolution ladder decides whose case a stranger's message lands on. Both get
the same attention here as the SAML ACS did.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from case_service.channels import optout, service, signatures
from velaris_shared.db.models import (
    ChannelConversationModel,
    ChannelEndpointModel,
    ChannelMessageModel,
)

SECRET = "webhook-secret-value"
WEBHOOK_URL = "https://velaris.test/api/v1/channels/twilio/inbound/{id}"


def _uniq(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


async def _endpoint(session, *, provider="twilio", channel="sms", **overrides):
    from case_service.hxbridge.encryption import encrypt_credentials

    row = ChannelEndpointModel(
        tenant_id=overrides.pop("tenant_id", "default"),
        channel=channel,
        provider=provider,
        address=overrides.pop("address", _uniq("+1555")),
        signing_secret_enc=encrypt_credentials({"secret": SECRET}),
        auto_create_case=overrides.pop("auto_create_case", False),
        conversation_ttl_hours=overrides.pop("conversation_ttl_hours", 72),
        routing={}, is_active=overrides.pop("is_active", True),
        **overrides,
    )
    session.add(row)
    await session.commit()
    await session.refresh(row)
    row.routing = {"webhook_url": WEBHOOK_URL.format(id=row.id)}
    await session.commit()
    return row


def _twilio_form(*, sid: str | None = None, body: str = "hello", frm: str = "+15550101234"):
    return {
        "From": frm,
        "Body": body,
        "MessageSid": sid or f"SM{uuid.uuid4().hex}",
        "To": "+15559990000",
    }


async def _post_twilio(client, endpoint, form: dict, *, secret: str = SECRET):
    url = WEBHOOK_URL.format(id=endpoint.id)
    signature = signatures.sign_twilio(secret, url=url, params=form)
    return await client.post(
        f"/api/v1/channels/twilio/inbound/{endpoint.id}",
        data=form, headers={"X-Twilio-Signature": signature},
    )


# ── signature ─────────────────────────────────────────────────────────────

class TestSignature:
    @pytest.mark.asyncio
    async def test_a_correctly_signed_message_is_accepted(self, session, anon_client):
        endpoint = await _endpoint(session)
        resp = await _post_twilio(anon_client, endpoint, _twilio_form())
        assert resp.status_code == 200
        assert resp.json()["received"] is True

    @pytest.mark.asyncio
    async def test_one_changed_parameter_invalidates_the_signature(
        self, session, anon_client,
    ):
        """Twilio signs the sorted parameters, so editing the body after signing
        must not verify — otherwise an attacker rewrites a captured message."""
        endpoint = await _endpoint(session)
        form = _twilio_form(body="hello")
        url = WEBHOOK_URL.format(id=endpoint.id)
        signature = signatures.sign_twilio(SECRET, url=url, params=form)
        form["Body"] = "transfer all the money"

        resp = await anon_client.post(
            f"/api/v1/channels/twilio/inbound/{endpoint.id}",
            data=form, headers={"X-Twilio-Signature": signature},
        )
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_a_missing_signature_is_refused(self, session, anon_client):
        """The defect class this codebase has already shipped once: four webhook
        handlers treated an ABSENT signature as verified."""
        endpoint = await _endpoint(session)
        resp = await anon_client.post(
            f"/api/v1/channels/twilio/inbound/{endpoint.id}", data=_twilio_form())
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_a_signature_from_another_secret_is_refused(self, session, anon_client):
        endpoint = await _endpoint(session)
        resp = await _post_twilio(anon_client, endpoint, _twilio_form(), secret="wrong")
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_a_signature_for_a_different_url_is_refused(self, session, anon_client):
        """The URL is signed material, which is what stops a captured request
        being replayed at a different endpoint of ours."""
        endpoint = await _endpoint(session)
        form = _twilio_form()
        signature = signatures.sign_twilio(
            SECRET, url="https://velaris.test/api/v1/channels/twilio/inbound/other",
            params=form)
        resp = await anon_client.post(
            f"/api/v1/channels/twilio/inbound/{endpoint.id}",
            data=form, headers={"X-Twilio-Signature": signature},
        )
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_every_refusal_looks_identical(self, session, anon_client):
        """A distinct "unknown endpoint" makes this an enumeration oracle, and a
        distinct "bad signature" confirms a guessed id."""
        endpoint = await _endpoint(session)
        inactive = await _endpoint(session, is_active=False)
        form = _twilio_form()

        responses = [
            await anon_client.post(
                f"/api/v1/channels/twilio/inbound/{uuid.uuid4()}", data=form),
            await anon_client.post(
                "/api/v1/channels/twilio/inbound/not-a-uuid", data=form),
            await _post_twilio(anon_client, inactive, form),
            await _post_twilio(anon_client, endpoint, form, secret="wrong"),
            await anon_client.post(
                f"/api/v1/channels/nosuchprovider/inbound/{endpoint.id}", data=form),
        ]
        assert {r.status_code for r in responses} == {404}
        assert len({r.text for r in responses}) == 1

    @pytest.mark.asyncio
    async def test_a_meta_payload_is_verified_over_the_raw_body(self, session, anon_client):
        """Meta signs the exact bytes — re-serialising parsed JSON changes
        whitespace and key order and the MAC stops matching."""
        endpoint = await _endpoint(session, provider="meta", channel="whatsapp")
        payload = {"entry": [{"changes": [{"value": {"messages": [{
            "from": "15550101234", "id": f"wamid.{uuid.uuid4().hex}",
            "text": {"body": "hello from whatsapp"},
        }]}}]}]}
        raw = json.dumps(payload).encode()

        resp = await anon_client.post(
            f"/api/v1/channels/meta/inbound/{endpoint.id}",
            content=raw,
            headers={"X-Hub-Signature-256": signatures.sign_body(SECRET, raw),
                     "Content-Type": "application/json"},
        )
        assert resp.status_code == 200

        bad = await anon_client.post(
            f"/api/v1/channels/meta/inbound/{endpoint.id}",
            content=raw,
            headers={"X-Hub-Signature-256": signatures.sign_body("wrong", raw),
                     "Content-Type": "application/json"},
        )
        assert bad.status_code == 404

    def test_no_verify_function_can_pass_without_a_mac(self):
        """Structural: an empty secret or an absent header is never verified,
        whatever the provider."""
        for provider in signatures.SUPPORTED:
            assert signatures.verify(provider, "", body=b"x", header="anything") is False
            assert signatures.verify(provider, SECRET, body=b"x", header=None) is False
        assert signatures.verify("unknown", SECRET, body=b"x", header="h") is False


# ── idempotency ───────────────────────────────────────────────────────────

class TestIdempotency:
    @pytest.mark.asyncio
    async def test_the_same_message_delivered_twice_is_stored_once(
        self, session, anon_client,
    ):
        """Twilio and Meta both retry. Without the unique `provider_message_id`
        a retry opens a second conversation and a second case."""
        endpoint = await _endpoint(session)
        form = _twilio_form()

        first = await _post_twilio(anon_client, endpoint, form)
        second = await _post_twilio(anon_client, endpoint, form)

        assert first.status_code == second.status_code == 200
        assert second.json()["duplicate"] is True
        assert first.json()["conversation_id"] == second.json()["conversation_id"]

        messages = (await session.execute(
            select(ChannelMessageModel).where(
                ChannelMessageModel.provider_message_id == form["MessageSid"])
        )).scalars().all()
        assert len(messages) == 1


# ── the resolution ladder ─────────────────────────────────────────────────

class TestResolutionLadder:
    @pytest.mark.asyncio
    async def test_a_reply_appends_to_the_open_conversation(self, session, anon_client):
        endpoint = await _endpoint(session)
        peer = "+15550107777"

        first = await _post_twilio(anon_client, endpoint, _twilio_form(frm=peer))
        second = await _post_twilio(
            anon_client, endpoint, _twilio_form(frm=peer, body="and another thing"))

        assert first.json()["conversation_id"] == second.json()["conversation_id"]
        rows = (await session.execute(
            select(ChannelConversationModel).where(
                ChannelConversationModel.peer_address == peer)
        )).scalars().all()
        assert len(rows) == 1

    @pytest.mark.asyncio
    async def test_a_conversation_past_its_ttl_does_not_swallow_a_new_enquiry(
        self, session, anon_client,
    ):
        """The TTL is what stops a months-old thread absorbing an unrelated
        message."""
        endpoint = await _endpoint(session, conversation_ttl_hours=1)
        peer = "+15550108888"
        await _post_twilio(anon_client, endpoint, _twilio_form(frm=peer))

        stale = (await session.execute(
            select(ChannelConversationModel).where(
                ChannelConversationModel.peer_address == peer)
        )).scalar_one()
        stale.last_message_at = datetime.now(timezone.utc) - timedelta(hours=5)
        stale.status = "open"
        await session.commit()

        await _post_twilio(anon_client, endpoint, _twilio_form(frm=peer, body="new"))
        rows = (await session.execute(
            select(ChannelConversationModel).where(
                ChannelConversationModel.peer_address == peer)
        )).scalars().all()
        assert len(rows) == 2

    @pytest.mark.asyncio
    async def test_an_unmatched_message_is_parked_not_dropped(self, session, anon_client):
        """The email-inbox behaviour: an unrecognised message must be visible to
        an operator rather than silently discarded."""
        endpoint = await _endpoint(session, auto_create_case=False)
        resp = await _post_twilio(anon_client, endpoint, _twilio_form(frm="+15550109999"))

        conversation = (await session.execute(
            select(ChannelConversationModel).where(
                ChannelConversationModel.id == uuid.UUID(resp.json()["conversation_id"]))
        )).scalar_one()
        assert conversation.case_id is None
        assert conversation.status == "open"

    @pytest.mark.asyncio
    async def test_auto_create_without_a_case_type_parks_rather_than_failing(
        self, session, anon_client,
    ):
        endpoint = await _endpoint(session, auto_create_case=True)
        resp = await _post_twilio(anon_client, endpoint, _twilio_form())
        assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_a_flood_from_one_peer_stops_opening_cases(self, session, anon_client):
        """An SMS bomb must not become a case per message. The words are still
        recorded — dropping a customer's message is never the right answer."""
        endpoint = await _endpoint(session, auto_create_case=True)
        peer = "+15550100001"
        now = datetime.now(timezone.utc)

        for _ in range(service.CASE_CREATION_LIMIT):
            session.add(ChannelConversationModel(
                tenant_id=endpoint.tenant_id, endpoint_id=endpoint.id,
                peer_address=peer, case_id=uuid.uuid4(), status="closed",
                metadata_json={}, created_at=now,
            ))
        await session.commit()

        assert await service._recent_case_creations(
            session, endpoint, peer, now=now) >= service.CASE_CREATION_LIMIT


# ── STOP / START ──────────────────────────────────────────────────────────

class TestOptOut:
    def test_the_keywords_carriers_expect_are_recognised(self):
        for word in ("STOP", "stop", "Stop.", "UNSUBSCRIBE", "cancel"):
            assert optout.classify(word) == "stop", word
        for word in ("START", "start", "Yes!"):
            assert optout.classify(word) == "start", word
        assert optout.classify("HELP") == "help"
        assert optout.classify("stop by the office tomorrow") is None
        assert optout.classify("") is None

    @pytest.mark.asyncio
    async def test_silence_means_permission(self, session):
        """`has_consent` fails closed, which is right for opt-IN purposes and
        exactly wrong here: wiring the send gate to it would stop every SMS to
        every number nobody had recorded a grant for — all of them."""
        assert await optout.may_send(session, "+15550100002") is True

    @pytest.mark.asyncio
    async def test_stop_suppresses_and_start_restores(self, session, anon_client):
        endpoint = await _endpoint(session)
        peer = "+15550100003"

        await _post_twilio(anon_client, endpoint, _twilio_form(frm=peer, body="STOP"))
        assert await optout.may_send(session, peer, tenant_id=endpoint.tenant_id) is False

        await _post_twilio(anon_client, endpoint, _twilio_form(frm=peer, body="START"))
        assert await optout.may_send(session, peer, tenant_id=endpoint.tenant_id) is True

    @pytest.mark.asyncio
    async def test_stop_is_recorded_on_the_consent_ledger(self, session, anon_client):
        """Not a private list: an operator reading the consent record must see
        the permission the platform is honouring, and a subject-access request
        must return it."""
        from case_service.compliance import consent

        endpoint = await _endpoint(session)
        peer = "+15550100004"
        await _post_twilio(anon_client, endpoint, _twilio_form(frm=peer, body="STOP"))

        state = await consent.current_state(session, peer, tenant_id=endpoint.tenant_id)
        assert state[optout.PURPOSE]["granted"] is False

    @pytest.mark.asyncio
    async def test_a_keyword_does_not_open_a_case(self, session, anon_client):
        """Opening a support case because somebody texted STOP turns an opt-out
        into a work queue."""
        endpoint = await _endpoint(session, auto_create_case=True)
        resp = await _post_twilio(
            anon_client, endpoint, _twilio_form(frm="+15550100005", body="STOP"))

        conversation = await session.get(
            ChannelConversationModel, uuid.UUID(resp.json()["conversation_id"]))
        assert conversation.case_id is None
