"""The channel spine — conversation resolution, ingest, and outbound reply.

**The resolution ladder is the design**, and its order is load-bearing:

    1. an open conversation for (endpoint, peer) inside the TTL  → append
    2. the peer matches a portal customer with exactly one open case → attach
    3. the endpoint auto-creates                                   → intake.ingest
    4. otherwise                                                   → park unassigned

Rung 1 comes first because a reply to an existing thread must never open a
second case; rung 2 is deliberately narrow (*exactly* one open case), because
"probably this one" is how a customer's message lands on a stranger's case;
rung 4 is the email inbox's behaviour, and exists so an unrecognised message is
visible rather than silently dropped.

Ingest goes through `intake.service.ingest()`, which was extracted from the
webhook handler for exactly this reason and takes no HTTP types.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from case_service.channels import optout
from velaris_shared.db.models import (
    CaseInstanceModel,
    ChannelConversationModel,
    ChannelEndpointModel,
    ChannelMessageModel,
)

log = logging.getLogger(__name__)

#: WhatsApp's rule, not ours: 24 hours from the customer's last inbound message,
#: after which only a pre-approved template may be sent.
WHATSAPP_WINDOW_HOURS = 24

#: A single peer flooding one endpoint must not become a case per message. Over
#: the limit the message is still recorded on the conversation — dropping it
#: would lose a customer's words — but it does not open a new case.
CASE_CREATION_LIMIT = 5
CASE_CREATION_WINDOW_MINUTES = 10


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _aware(value: datetime | None) -> datetime | None:
    """SQLite hands back naive datetimes where PostgreSQL returns aware ones."""
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


@dataclass
class InboundResult:
    conversation_id: str
    message_id: str
    case_id: str | None = None
    created_case: bool = False
    duplicate: bool = False
    #: stop | start | help, when the message was a keyword rather than content.
    keyword: str | None = None
    rung: str = ""
    detail: dict = field(default_factory=dict)


# ── conversation resolution ───────────────────────────────────────────────

async def _open_conversation(
    session: AsyncSession, endpoint: ChannelEndpointModel, peer: str, *, now: datetime,
) -> ChannelConversationModel | None:
    """Rung 1 — an open thread inside the endpoint's TTL.

    The TTL is what makes a months-old thread not swallow a new enquiry, and it
    is per endpoint because a support line and a marketing line reasonably
    differ.
    """
    # FOR UPDATE: a tenant move closes and detaches this row in its own
    # transaction (Oct item 8a). Without the lock, a message read just before
    # that commit would be filed with the moved case's id; with it, the inbound
    # waits for the move, re-reads the row, finds it closed, and opens a fresh
    # thread at the source. One endpoint+peer, so the lock is never contended
    # beyond the customer's own messages. (SQLite ignores it.)
    rows = (await session.execute(
        select(ChannelConversationModel).where(
            ChannelConversationModel.endpoint_id == endpoint.id,
            ChannelConversationModel.peer_address == peer,
            ChannelConversationModel.status != "closed",
        ).order_by(ChannelConversationModel.created_at.desc())
        .with_for_update()
    )).scalars().all()

    ttl = timedelta(hours=max(1, endpoint.conversation_ttl_hours or 72))
    for row in rows:
        last = _aware(row.last_message_at) or _aware(row.created_at)
        if last is None or now - last <= ttl:
            return row
    return None


async def _customer_single_open_case(
    session: AsyncSession, endpoint: ChannelEndpointModel, peer: str,
) -> uuid.UUID | None:
    """Rung 2 — the peer is a known portal customer with **exactly one** open case.

    Exactly one, deliberately. With two open cases there is no honest way to
    choose, and guessing puts one customer's message on another matter's record.
    Ambiguity falls through to rung 3 or 4, where a human decides.
    """
    from velaris_shared.db.models import PortalCustomerModel
    from case_service.tenancy.identity import resolve_tenant

    normalised = _normalise_phone(peer)
    if not normalised:
        return None

    # `portal_customers.tenant_id` is a GUID FK while `channel_endpoints.tenant_id`
    # is a free-text token; the resolver is the only thing that makes those two
    # comparable, and comparing them raw is the mistake this platform has already
    # made once.
    resolved = await resolve_tenant(session, endpoint.tenant_id)
    if resolved is None:
        return None

    customers = (await session.execute(
        select(PortalCustomerModel).where(
            PortalCustomerModel.tenant_id == resolved[0],
        )
    )).scalars().all()
    matched = [
        c for c in customers
        if _normalise_phone(c.phone or "") == normalised
    ]
    if len(matched) != 1:
        return None

    email = (matched[0].primary_email or "").strip().lower()
    if not email:
        return None

    # The endpoint's tenant only. Until 2026-09-28 this looked for the email in
    # EVERY tenant's cases, so a message to tenant A's number could be filed on
    # tenant B's case for a customer who happened to have one there — and a case
    # moved out to another tenant (Oct item 8a) kept catching the source's
    # inbound. A case with no tenant keeps matching exactly as before, unless
    # strict reads are on — the same rule `case_visible_to` states.
    from sqlalchemy import or_
    from case_service.tenancy.identity import strict_case_reads

    tenant_clause = CaseInstanceModel.tenant_id == resolved[0]
    if not strict_case_reads():
        tenant_clause = or_(tenant_clause, CaseInstanceModel.tenant_id.is_(None))
    open_cases = (await session.execute(
        select(CaseInstanceModel).where(
            CaseInstanceModel.portal_submitter_email == email,
            CaseInstanceModel.status.notin_(("closed", "resolved", "cancelled")),
            tenant_clause,
        )
    )).scalars().all()
    return open_cases[0].id if len(open_cases) == 1 else None


def _normalise_phone(value: str) -> str:
    """Digits only, so `+1 (555) 010-1234` and `15550101234` are one person."""
    return "".join(ch for ch in (value or "") if ch.isdigit())


async def _recent_case_creations(
    session: AsyncSession, endpoint: ChannelEndpointModel, peer: str, *, now: datetime,
) -> int:
    since = now - timedelta(minutes=CASE_CREATION_WINDOW_MINUTES)
    rows = (await session.execute(
        select(ChannelConversationModel).where(
            ChannelConversationModel.endpoint_id == endpoint.id,
            ChannelConversationModel.peer_address == peer,
        )
    )).scalars().all()
    return sum(
        1 for r in rows
        if r.case_id is not None and (_aware(r.created_at) or now) >= since
    )


# ── ingest ────────────────────────────────────────────────────────────────

async def ingest_inbound(
    session: AsyncSession,
    *,
    endpoint: ChannelEndpointModel,
    peer_address: str,
    body: str,
    provider_message_id: str | None,
    raw_payload: dict[str, Any],
    media: list[dict] | None = None,
    now: datetime | None = None,
) -> InboundResult:
    """Record one inbound message and place it. Does not commit.

    The tenant comes from the endpoint row and never from the payload — a
    provider posts whatever it likes, and a tenant taken from the body would be
    a tenant the sender chose.
    """
    now = now or _utcnow()
    peer = (peer_address or "").strip()
    body = body or ""

    # Idempotency first. Twilio and Meta both retry, and a retry must resolve to
    # the row that already exists rather than opening a second case.
    if provider_message_id:
        existing = (await session.execute(
            select(ChannelMessageModel).where(
                ChannelMessageModel.provider_message_id == provider_message_id)
        )).scalars().first()
        if existing is not None:
            return InboundResult(
                conversation_id=str(existing.conversation_id),
                message_id=str(existing.id),
                case_id=str(existing.case_id) if existing.case_id else None,
                duplicate=True, rung="duplicate",
            )

    keyword = optout.classify(body) if endpoint.channel in ("sms", "whatsapp") else None
    if keyword == "stop":
        await optout.record_stop(session, peer, tenant_id=endpoint.tenant_id)
    elif keyword == "start":
        await optout.record_start(session, peer, tenant_id=endpoint.tenant_id)

    conversation = await _open_conversation(session, endpoint, peer, now=now)
    rung = "existing_conversation"
    created_case = False

    if conversation is None:
        case_id = await _customer_single_open_case(session, endpoint, peer)
        rung = "customer_open_case" if case_id else ""

        # A keyword is an instruction to us, not a matter to work: opening a
        # case because somebody texted STOP would be absurd, and it is the
        # obvious way to turn an opt-out into a support queue.
        if case_id is None and endpoint.auto_create_case and keyword is None:
            flooding = await _recent_case_creations(session, endpoint, peer, now=now)
            if flooding >= CASE_CREATION_LIMIT:
                rung = "rate_limited"
            elif endpoint.default_case_type_id is not None:
                case_id = await _create_case(session, endpoint, peer, body, now=now)
                created_case = case_id is not None
                rung = "created_case" if created_case else "intake_refused"
        if not rung:
            rung = "parked"

        conversation = ChannelConversationModel(
            tenant_id=endpoint.tenant_id,
            endpoint_id=endpoint.id,
            peer_address=peer,
            case_id=case_id,
            status="open",
            metadata_json={},
        )
        session.add(conversation)
        await session.flush()

    conversation.last_message_at = now
    conversation.unread_count = (conversation.unread_count or 0) + 1
    if endpoint.channel == "whatsapp":
        # Reset on every inbound: the window is measured from the customer's
        # last message, which is the whole point of it.
        conversation.window_expires_at = now + timedelta(hours=WHATSAPP_WINDOW_HOURS)

    message = ChannelMessageModel(
        tenant_id=endpoint.tenant_id,
        conversation_id=conversation.id,
        case_id=conversation.case_id,
        channel=endpoint.channel,
        direction="inbound",
        peer_address=peer,
        body=body,
        media=media or [],
        provider_message_id=provider_message_id,
        status="received",
        raw_payload=raw_payload or {},
        received_at=now,
    )
    session.add(message)
    await session.flush()

    return InboundResult(
        conversation_id=str(conversation.id),
        message_id=str(message.id),
        case_id=str(conversation.case_id) if conversation.case_id else None,
        created_case=created_case,
        keyword=keyword,
        rung=rung,
    )


async def _endpoint_tenant_uuid(session: AsyncSession, endpoint: ChannelEndpointModel):
    """The endpoint's tenant as the GUID a case row stores, or None."""
    from velaris_shared.tenancy.identity import resolve_tenant
    resolved = await resolve_tenant(session, str(endpoint.tenant_id)) if endpoint.tenant_id else None
    return resolved[0] if resolved is not None else None


async def _create_case(
    session: AsyncSession, endpoint: ChannelEndpointModel, peer: str, body: str,
    *, now: datetime,
) -> uuid.UUID | None:
    """Rung 3 — hand the message to the shared intake core.

    An intake refusal (a case type that does not accept the trigger, a filter
    that dropped it) is **not** an error for the caller: the message is still
    recorded and the conversation is still parked, so a misconfigured case type
    loses no customer words.
    """
    from case_service.intake.service import IntakeError, ingest

    try:
        result = await ingest(
            session,
            case_type_id=endpoint.default_case_type_id,
            payload={
                "from": peer,
                "body": body,
                "channel": endpoint.channel,
                "endpoint_address": endpoint.address,
                "received_at": now.isoformat(),
            },
            source_ref=peer,
            source_kind=endpoint.channel,
            now=now,
            tenant_id=await _endpoint_tenant_uuid(session, endpoint),
        )
    except IntakeError as exc:
        log.warning("channel intake refused for endpoint %s: %s", endpoint.id, exc)
        return None

    case_id = result.get("case_id") if isinstance(result, dict) else None
    if not case_id:
        return None
    try:
        return uuid.UUID(str(case_id))
    except (ValueError, TypeError):
        return None


# ── outbound ──────────────────────────────────────────────────────────────

class ChannelSendError(Exception):
    """A refusal on the send path. `reason` is stable; the message is for a human."""

    def __init__(self, reason: str, message: str, *, status: int = 400):
        super().__init__(message)
        self.reason = reason
        self.status = status


async def check_can_send(
    session: AsyncSession,
    *,
    conversation: ChannelConversationModel,
    endpoint: ChannelEndpointModel,
    template_name: str | None,
    now: datetime | None = None,
) -> None:
    """The two rules that make an outbound send legal and deliverable.

    Opt-out first, because it is a legal obligation rather than a provider
    preference; then the WhatsApp window, which is a hard provider rule and
    fails *silently* at Meta if ignored — the message is accepted and never
    delivered, which is the worst kind of failure to debug.
    """
    now = now or _utcnow()

    if not await optout.may_send(session, conversation.peer_address,
                                 tenant_id=endpoint.tenant_id):
        raise ChannelSendError(
            "opted_out",
            "This number has opted out of messages. They can text START to opt back in.",
            status=403,
        )

    if endpoint.channel != "whatsapp":
        return

    expires = _aware(conversation.window_expires_at)
    if expires is not None and now < expires:
        return
    if template_name:
        return
    raise ChannelSendError(
        "whatsapp_window_closed",
        "More than 24 hours have passed since this customer's last message, so "
        "WhatsApp only accepts a pre-approved template. Choose one to reply.",
        status=409,
    )


async def record_outbound(
    session: AsyncSession,
    *,
    conversation: ChannelConversationModel,
    endpoint: ChannelEndpointModel,
    body: str,
    author: str,
    template_name: str | None = None,
    provider_message_id: str | None = None,
    status: str = "queued",
    error: str | None = None,
    now: datetime | None = None,
) -> ChannelMessageModel:
    now = now or _utcnow()
    message = ChannelMessageModel(
        tenant_id=endpoint.tenant_id,
        conversation_id=conversation.id,
        case_id=conversation.case_id,
        channel=endpoint.channel,
        direction="outbound",
        author=author,
        peer_address=conversation.peer_address,
        body=body,
        media=[],
        provider_message_id=provider_message_id,
        template_name=template_name,
        status=status,
        error=error,
        raw_payload={},
        is_read=True,
        sent_at=now if status in ("sent", "delivered") else None,
    )
    session.add(message)
    conversation.last_message_at = now
    await session.flush()
    return message
