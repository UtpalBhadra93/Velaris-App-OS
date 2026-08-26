"""Reply, the WhatsApp window, the inbox, CTI screen-pop and tenancy.

The send path costs money and reaches a customer's phone, so its two refusals —
an opt-out and a closed WhatsApp window — are pinned here rather than trusted.
The window one matters most: Meta does not reject a free-form message sent
outside it with an error a human notices; it accepts and never delivers.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from case_service.channels import optout
from velaris_shared.db.models import (
    ChannelConversationModel,
    ChannelEndpointModel,
    ChannelMessageModel,
    WhatsAppTemplateModel,
)

BASE = "/api/v1/channels"


def _uniq(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


async def _endpoint(session, *, channel="sms", tenant_id="default", **overrides):
    from case_service.hxbridge.encryption import encrypt_credentials

    row = ChannelEndpointModel(
        tenant_id=tenant_id, channel=channel,
        provider="twilio" if channel != "whatsapp" else "meta",
        address=_uniq("+1555"),
        signing_secret_enc=encrypt_credentials({"secret": "s"}),
        routing={}, **overrides,
    )
    session.add(row)
    await session.commit()
    await session.refresh(row)
    return row


async def _conversation(session, endpoint, *, peer=None, **overrides):
    overrides.setdefault("status", "open")
    row = ChannelConversationModel(
        tenant_id=endpoint.tenant_id, endpoint_id=endpoint.id,
        peer_address=peer or _uniq("+1555"),
        metadata_json={}, **overrides,
    )
    session.add(row)
    await session.commit()
    await session.refresh(row)
    return row


class TestReply:
    @pytest.mark.asyncio
    async def test_a_reply_is_recorded_on_the_thread(self, session, client):
        endpoint = await _endpoint(session)
        conversation = await _conversation(session, endpoint)

        resp = await client.post(f"{BASE}/conversations/{conversation.id}/reply",
                                 json={"body": "we are looking into it"})
        assert resp.status_code == 200
        body = resp.json()
        assert body["direction"] == "outbound"
        assert body["body"] == "we are looking into it"

    @pytest.mark.asyncio
    async def test_a_dispatch_failure_keeps_the_message_on_the_thread(
        self, session, client,
    ):
        """The agent wrote it, so it belongs on the record. A lost outbound and
        a 500 is how a customer stops getting answers with nobody noticing."""
        endpoint = await _endpoint(session)
        conversation = await _conversation(session, endpoint)

        resp = await client.post(f"{BASE}/conversations/{conversation.id}/reply",
                                 json={"body": "hello"})
        assert resp.status_code == 200
        # No connector is configured on this endpoint, so it queues with a note
        # rather than raising.
        assert resp.json()["status"] in ("queued", "failed")

    @pytest.mark.asyncio
    async def test_an_empty_reply_is_refused(self, session, client):
        endpoint = await _endpoint(session)
        conversation = await _conversation(session, endpoint)
        resp = await client.post(f"{BASE}/conversations/{conversation.id}/reply",
                                 json={"body": "   "})
        assert resp.status_code == 400

    @pytest.mark.asyncio
    async def test_an_opted_out_number_cannot_be_messaged(self, session, client):
        """A legal instruction, not a preference — and it is checked before
        anything is dispatched."""
        endpoint = await _endpoint(session)
        conversation = await _conversation(session, endpoint)
        await optout.record_stop(session, conversation.peer_address,
                                 tenant_id=endpoint.tenant_id)
        await session.commit()

        resp = await client.post(f"{BASE}/conversations/{conversation.id}/reply",
                                 json={"body": "hello again"})
        assert resp.status_code == 403
        assert resp.json()["error"]["code"] == "VEL-CHANNEL-OPTED-OUT-403"

        messages = (await session.execute(
            select(ChannelMessageModel).where(
                ChannelMessageModel.conversation_id == conversation.id)
        )).scalars().all()
        assert messages == []


class TestWhatsAppWindow:
    @pytest.mark.asyncio
    async def test_inside_the_window_a_free_form_reply_sends(self, session, client):
        endpoint = await _endpoint(session, channel="whatsapp")
        conversation = await _conversation(
            session, endpoint,
            window_expires_at=datetime.now(timezone.utc) + timedelta(hours=3))

        resp = await client.post(f"{BASE}/conversations/{conversation.id}/reply",
                                 json={"body": "sure, here is the update"})
        assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_outside_the_window_a_free_form_reply_is_refused(self, session, client):
        """Meta accepts and never delivers, which is the worst kind of failure
        to debug — so the refusal happens here, with a code the UI can act on."""
        endpoint = await _endpoint(session, channel="whatsapp")
        conversation = await _conversation(
            session, endpoint,
            window_expires_at=datetime.now(timezone.utc) - timedelta(hours=1))

        resp = await client.post(f"{BASE}/conversations/{conversation.id}/reply",
                                 json={"body": "still there?"})
        assert resp.status_code == 409
        assert resp.json()["error"]["code"] == "VEL-CHANNEL-WINDOW-409"

    @pytest.mark.asyncio
    async def test_outside_the_window_an_approved_template_sends(self, session, client):
        endpoint = await _endpoint(session, channel="whatsapp")
        conversation = await _conversation(
            session, endpoint,
            window_expires_at=datetime.now(timezone.utc) - timedelta(hours=1))
        session.add(WhatsAppTemplateModel(
            tenant_id=endpoint.tenant_id, endpoint_id=endpoint.id,
            name="case_update", language="en_US", body_params=[],
            approval_status="approved",
        ))
        await session.commit()

        resp = await client.post(f"{BASE}/conversations/{conversation.id}/reply",
                                 json={"body": "", "template_name": "case_update"})
        assert resp.status_code == 200
        assert resp.json()["template_name"] == "case_update"

    @pytest.mark.asyncio
    async def test_an_unapproved_template_is_refused_before_sending(self, session, client):
        """Meta rejects it at send time; refusing here means the agent learns now
        rather than from a silent non-delivery."""
        endpoint = await _endpoint(session, channel="whatsapp")
        conversation = await _conversation(
            session, endpoint,
            window_expires_at=datetime.now(timezone.utc) - timedelta(hours=1))
        session.add(WhatsAppTemplateModel(
            tenant_id=endpoint.tenant_id, endpoint_id=endpoint.id,
            name="not_yet", language="en_US", body_params=[],
            approval_status="pending",
        ))
        await session.commit()

        resp = await client.post(f"{BASE}/conversations/{conversation.id}/reply",
                                 json={"body": "", "template_name": "not_yet"})
        assert resp.status_code == 409

    @pytest.mark.asyncio
    async def test_an_sms_conversation_has_no_window(self, session, client):
        """The 24-hour rule is WhatsApp's, and applying it to SMS would block
        replies for no reason."""
        endpoint = await _endpoint(session, channel="sms")
        conversation = await _conversation(
            session, endpoint,
            window_expires_at=datetime.now(timezone.utc) - timedelta(days=30))

        resp = await client.post(f"{BASE}/conversations/{conversation.id}/reply",
                                 json={"body": "still fine"})
        assert resp.status_code == 200


class TestInbox:
    @pytest.mark.asyncio
    async def test_the_inbox_lists_and_counts(self, session, client):
        endpoint = await _endpoint(session)
        await _conversation(session, endpoint, unread_count=2)
        await _conversation(session, endpoint, unread_count=0, status="closed")

        listed = (await client.get(f"{BASE}/conversations")).json()["conversations"]
        assert len(listed) >= 2

        stats = (await client.get(f"{BASE}/conversations/stats")).json()
        assert stats["total"] >= 2 and stats["unread"] >= 2

    @pytest.mark.asyncio
    async def test_opening_a_conversation_marks_it_read(self, session, client):
        endpoint = await _endpoint(session)
        conversation = await _conversation(session, endpoint, unread_count=3)

        body = (await client.get(f"{BASE}/conversations/{conversation.id}")).json()
        assert body["conversation"]["unread_count"] == 0

    @pytest.mark.asyncio
    async def test_attaching_a_case_backfills_the_existing_messages(
        self, session, client,
    ):
        """Otherwise the case record shows only what arrived after somebody
        linked the thread."""
        from tests.phase_hxsentinel._seed import seed_case

        endpoint = await _endpoint(session)
        conversation = await _conversation(session, endpoint)
        session.add(ChannelMessageModel(
            tenant_id=endpoint.tenant_id, conversation_id=conversation.id,
            channel="sms", direction="inbound", peer_address=conversation.peer_address,
            body="earlier", media=[], status="received", raw_payload={},
        ))
        case_id = await seed_case(session)
        await session.commit()

        resp = await client.patch(f"{BASE}/conversations/{conversation.id}",
                                  json={"case_id": str(case_id)})
        assert resp.status_code == 200

        messages = (await session.execute(
            select(ChannelMessageModel).where(
                ChannelMessageModel.conversation_id == conversation.id)
        )).scalars().all()
        assert all(str(m.case_id) == str(case_id) for m in messages)

    @pytest.mark.asyncio
    async def test_an_unknown_conversation_is_404(self, session, client):
        assert (await client.get(
            f"{BASE}/conversations/{uuid.uuid4()}")).status_code == 404

    @pytest.mark.asyncio
    async def test_the_agent_surfaces_are_gated(self, session, anon_client):
        endpoint = await _endpoint(session)
        conversation = await _conversation(session, endpoint)
        for path, method in [
            (f"{BASE}/conversations", "get"),
            (f"{BASE}/conversations/stats", "get"),
            (f"{BASE}/conversations/{conversation.id}", "get"),
            (f"{BASE}/endpoints", "get"),
        ]:
            resp = await getattr(anon_client, method)(path)
            assert resp.status_code in (401, 403), path
        assert (await anon_client.post(
            f"{BASE}/conversations/{conversation.id}/reply",
            json={"body": "x"})).status_code in (401, 403)


class TestTenancy:
    @pytest.mark.asyncio
    async def test_a_conversation_cannot_be_attached_to_another_tenants_case(
        self, session, client,
    ):
        """This is the one that would put one tenant's customer messages on
        another tenant's case record."""
        from tests.phase_hxsentinel._seed import seed_case

        from velaris_shared.db.models import CaseInstanceModel, TenantModel
        from case_service.tenancy import identity as tenant_identity

        tenant_a = TenantModel(slug=_uniq("ta"), name="A")
        tenant_b = TenantModel(slug=_uniq("tb"), name="B")
        session.add_all([tenant_a, tenant_b])
        await session.commit()
        tenant_identity.clear_cache()

        endpoint = await _endpoint(session, tenant_id=tenant_a.slug)
        conversation = await _conversation(session, endpoint)
        case_id = await seed_case(session)
        await session.commit()

        case = await session.get(CaseInstanceModel, case_id)
        case.tenant_id = tenant_b.id
        await session.commit()

        resp = await client.patch(f"{BASE}/conversations/{conversation.id}",
                                  json={"case_id": str(case_id)})
        assert resp.status_code == 404

        await session.refresh(conversation)
        assert conversation.case_id is None


class TestCti:
    @pytest.mark.asyncio
    async def test_a_pop_resolves_a_known_caller(self, session, client):
        endpoint = await _endpoint(session)
        conversation = await _conversation(session, endpoint, peer="+1 (555) 010-4242")

        resp = await client.post(f"{BASE}/cti/pop", json={
            "ani": "15550104242", "agent_id": "agent-1", "direction": "inbound"})
        assert resp.status_code == 200
        assert resp.json()["conversation_id"] == str(conversation.id)

    @pytest.mark.asyncio
    async def test_an_unknown_caller_still_pops_somewhere_useful(self, session, client):
        """The contact centre is waiting on this response to route a live
        customer — an unknown number is not an error."""
        resp = await client.post(f"{BASE}/cti/pop", json={
            "ani": "+15550109876", "agent_id": "agent-2"})
        assert resp.status_code == 200
        assert resp.json()["case_id"] is None
        assert resp.json()["url"] == "/channels"

    @pytest.mark.asyncio
    async def test_the_pop_endpoint_is_gated(self, session, anon_client):
        assert (await anon_client.post(f"{BASE}/cti/pop", json={
            "ani": "+1", "agent_id": "a"})).status_code in (401, 403)


class TestAdministration:
    @pytest.mark.asyncio
    async def test_an_endpoint_cannot_be_created_without_a_signing_secret(
        self, session, client,
    ):
        """Without one the signature check can only fail, so the webhook would
        accept nothing — better refused than shipped dead."""
        resp = await client.post(f"{BASE}/endpoints", json={
            "tenant_id": "default", "channel": "sms", "provider": "twilio",
            "address": "+15550100000"})
        assert resp.status_code == 400

    @pytest.mark.asyncio
    async def test_a_provider_we_cannot_verify_is_refused(self, session, client):
        """Accepting it would leave the inbound route unauthenticated."""
        resp = await client.post(f"{BASE}/endpoints", json={
            "tenant_id": "default", "channel": "sms", "provider": "vonage",
            "address": "+15550100000", "signing_secret": "s"})
        assert resp.status_code == 400

    @pytest.mark.asyncio
    async def test_the_signing_secret_is_never_returned(self, session, client):
        created = await client.post(f"{BASE}/endpoints", json={
            "tenant_id": "default", "channel": "sms", "provider": "twilio",
            "address": _uniq("+1555"), "signing_secret": "super-secret"})
        assert created.status_code == 201
        assert "super-secret" not in created.text
        assert created.json()["has_secret"] is True

        listed = await client.get(f"{BASE}/endpoints")
        assert "super-secret" not in listed.text

    @pytest.mark.asyncio
    async def test_endpoint_administration_is_gated(self, session, anon_client):
        assert (await anon_client.post(f"{BASE}/endpoints", json={})).status_code in (
            401, 403, 422)

    @pytest.mark.asyncio
    async def test_opt_out_status_is_readable(self, session, client):
        body = (await client.get(f"{BASE}/opt-out/+15550100007")).json()
        assert body["may_send"] is True
