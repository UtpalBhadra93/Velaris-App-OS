"""The interaction record: screen-pop, verification, warm transfer, wrap-up.

Design: docs/Future/Done/CustomerEngagement.md §3.4, §6, §7.

**Screen-pop pops the PERSON, on an indexed lookup.** Today `channels/cti/pop`
reads the last 200 conversations hunting for a matching number and returns a
*case*. That is a scan with a horizon: a customer who last called 201
conversations ago simply does not exist. `customer_identifiers` replaces it with
a lookup, and what comes back is the party.

**An unresolved call is still an interaction.** The row is written either way,
with `customer_id` NULL and a stub for the rep to act on. Never auto-create a
profile from an unverified inbound number: that manufactures a person out of a
wrong number, and the person it manufactures then accumulates somebody else's
calls.

**Verification is on the interaction, not the profile.** It is something that
happened on a call. A customer verified yesterday is not verified today, and a
tier that lived on the profile would say they were.

**A warm transfer carries the interaction.** The receiving rep inherits the tier
already established — being re-verified is the number one complaint about every
contact centre, and it is entirely within our power not to do it.

**Failed verification is not an oracle.** "We could not verify that" is one
message whichever factor was wrong, the same shape as the portal OTP fix: an
error that says *which* answer was wrong is a machine for guessing the others.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from velaris_shared.db.models import (
    CustomerIdentifierModel,
    CustomerInteractionModel,
    CustomerProfileModel,
)

from case_service.engage import identity

log = logging.getLogger(__name__)

CHANNELS: tuple[str, ...] = ("voice", "sms", "whatsapp", "email", "portal",
                             "chat", "walk_in")
#: How a tier was reached. `asserted` is deliberately absent — this build
#: verifies for itself (decision 3), and a tier somebody else asserted would be
#: a claim we enforce without being able to check it.
FACTORS: tuple[str, ...] = ("otp", "call", "document", "idp")

#: One message, whichever factor failed.
VERIFY_FAILED = "We could not verify that."

#: An interaction nobody wrapped up ages into `abandoned`. Wrap-up is required by
#: configuration, never by force: one that blocks the next call is one that gets
#: faked.
ABANDON_AFTER_HOURS = 12


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


async def screen_pop(
    session: AsyncSession, *, tenant_uuid: uuid.UUID, tenant_token: str,
    channel: str, ani: str | None = None, address: str | None = None,
    dnis: str | None = None, queue: str | None = None,
    external_call_id: str | None = None, agent_user_id: str | None = None,
    transferred_from: uuid.UUID | None = None,
) -> dict[str, Any]:
    """Open an interaction and resolve the party. Returns both.

    The interaction is written **before** resolution succeeds or fails, because
    an unresolved call still happened and a contact centre that only records the
    calls it could identify has no idea how many it could not.
    """
    if channel not in CHANNELS:
        raise identity.IdentityError(f"a channel is one of {', '.join(CHANNELS)}")

    kind = "phone" if channel in ("voice", "sms", "whatsapp") else "email"
    value = ani or address

    resolution: dict[str, Any] = {"resolved": False, "rung": 0,
                                  "reason": "no caller identifier was supplied"}
    if value:
        resolution = await identity.resolve(
            session, tenant_uuid=tenant_uuid, tenant_token=tenant_token,
            kind=kind, value=value, adopt=True)

    customer_id = (uuid.UUID(resolution["customer_id"])
                   if resolution.get("customer_id") else None)

    inherited = "none"
    if transferred_from is not None:
        previous = await session.get(CustomerInteractionModel, transferred_from)
        if previous is not None and previous.tenant_id == tenant_uuid:
            # The whole point of a warm transfer.
            inherited = previous.verification_level
            customer_id = customer_id or previous.customer_id

    row = CustomerInteractionModel(
        id=uuid.uuid4(), tenant_id=tenant_uuid, customer_id=customer_id,
        channel=channel, direction="inbound", agent_user_id=agent_user_id,
        external_call_id=external_call_id, ani=ani, dnis=dnis, queue=queue,
        transferred_from=transferred_from, verification_level=inherited,
        status="open")
    session.add(row)
    await session.flush()

    if customer_id is not None:
        await identity.link(
            session, tenant_uuid=tenant_uuid, customer_id=customer_id,
            object_kind="interaction", object_id=row.id, reason="resolved",
            linked_by=agent_user_id)

    return {
        "interaction_id": str(row.id),
        "customer_id": str(customer_id) if customer_id else None,
        "verification_level": row.verification_level,
        "resolution": resolution,
        # What the rep should be offered when nobody was found. Not an error:
        # an unknown caller is an ordinary event.
        "stub": None if customer_id else {
            "actions": ["link_to_customer", "create_profile"],
            "prefill": {"kind": kind, "value": value},
            "note": ("No profile matches. Link this call to an existing customer "
                     "or create one — Velaris will not invent a person from an "
                     "unverified number."),
        },
        "inherited_verification": transferred_from is not None,
    }


async def verify(
    session: AsyncSession, *, interaction: CustomerInteractionModel,
    factor: str, ok: bool, to_tier: str, actor: str | None = None,
) -> dict[str, Any]:
    """Record a verification attempt. One failure message, whatever failed.

    `ok` is the caller's answer about whether the challenge passed — the
    challenge itself belongs to whichever mechanism ran it (OTP, IdP, a document
    check). What this owns is the CONSEQUENCE: the tier, on the interaction,
    with the factor that reached it.
    """
    if factor not in FACTORS:
        raise identity.IdentityError(f"a factor is one of {', '.join(FACTORS)}")
    if to_tier not in ("partial", "full"):
        raise identity.IdentityError("verification reaches 'partial' or 'full'")
    if not ok:
        # Deliberately says nothing about WHICH factor or WHY. An error that
        # distinguishes them is a machine for guessing the rest.
        return {"verified": False, "verification_level": interaction.verification_level,
                "message": VERIFY_FAILED}

    from case_service.engage.three_sixty import rank

    if rank(to_tier) > rank(interaction.verification_level):
        interaction.verification_level = to_tier
        interaction.verified_by = factor
        interaction.verified_at = _utcnow()
        await session.flush()

    # A Full verification confirms the identifier that was used to find them —
    # the adoption at resolution time was a suggestion, and this is the
    # confirmation it was waiting for.
    if to_tier == "full" and interaction.customer_id and interaction.ani:
        ident = (await session.execute(
            select(CustomerIdentifierModel).where(
                CustomerIdentifierModel.customer_id == interaction.customer_id,
                CustomerIdentifierModel.value_normalised ==
                identity.normalise("phone", interaction.ani)))).scalars().first()
        if ident is not None and not ident.verified:
            ident.verified = True
            ident.verified_at = _utcnow()
            ident.verified_by = factor
            await session.flush()

    return {"verified": True, "verification_level": interaction.verification_level,
            "verified_by": factor}


async def transfer(
    session: AsyncSession, *, tenant_uuid: uuid.UUID,
    interaction: CustomerInteractionModel, to_agent: str, queue: str | None = None,
) -> CustomerInteractionModel:
    """Warm transfer: a NEW interaction that inherits the tier and the party.

    A new row rather than reassigning the old one, because two reps handled this
    contact and an analytics answer that says one did would be wrong about
    handling time, and about who spoke to the customer.
    """
    interaction.status = "wrapped"
    interaction.ended_at = interaction.ended_at or _utcnow()
    interaction.outcome = interaction.outcome or "transferred"

    row = CustomerInteractionModel(
        id=uuid.uuid4(), tenant_id=tenant_uuid, customer_id=interaction.customer_id,
        channel=interaction.channel, direction=interaction.direction,
        agent_user_id=to_agent, external_call_id=interaction.external_call_id,
        ani=interaction.ani, dnis=interaction.dnis, queue=queue or interaction.queue,
        transferred_from=interaction.id,
        # Inherited, on purpose. Re-verifying a customer who was verified ninety
        # seconds ago by a colleague is the thing everybody complains about.
        verification_level=interaction.verification_level,
        verified_by=interaction.verified_by, verified_at=interaction.verified_at,
        status="open", handoff_kind="transfer")
    session.add(row)
    await session.flush()
    return row


async def wrap_up(
    session: AsyncSession, *, interaction: CustomerInteractionModel,
    disposition_code: str, notes: str | None = None, outcome: str | None = None,
) -> CustomerInteractionModel:
    interaction.status = "wrapped"
    interaction.ended_at = interaction.ended_at or _utcnow()
    interaction.disposition_code = str(disposition_code)[:64]
    interaction.wrap_up_notes = notes
    interaction.outcome = outcome or interaction.outcome
    await session.flush()
    return interaction


async def age_out(session: AsyncSession, *, tenant_uuid: uuid.UUID,
                  now: datetime | None = None) -> int:
    """Unclosed interactions age into `abandoned`.

    Never blocks the next call — a wrap-up that blocks the next call is a
    wrap-up that gets faked, and a faked disposition code is worse than a
    missing one because analytics believes it.
    """
    moment = now or _utcnow()
    cutoff = moment - timedelta(hours=ABANDON_AFTER_HOURS)
    rows = list((await session.execute(
        select(CustomerInteractionModel).where(
            CustomerInteractionModel.tenant_id == tenant_uuid,
            CustomerInteractionModel.status == "open",
            CustomerInteractionModel.started_at < cutoff))).scalars().all())
    for row in rows:
        row.status = "abandoned"
        row.ended_at = row.ended_at or row.started_at
    await session.flush()
    return len(rows)


# ── raise a case on the customer's behalf ────────────────────────────────────


async def on_behalf_attribution(
    session: AsyncSession, *, profile: CustomerProfileModel,
    interaction: CustomerInteractionModel, rep_user_id: str,
) -> dict[str, Any]:
    """What a case raised on a call must carry.

    `created_by` is the REP — they are accountable for the record. The customer
    is `on_behalf_of_customer_id`, and the **verification level at the moment of
    creation is stamped on the case**, so a later reader knows whether the person
    on the phone was ever proven to be who they said.
    """
    identifier = (await session.execute(
        select(CustomerIdentifierModel)
        .where(CustomerIdentifierModel.customer_id == profile.id)
        .order_by(CustomerIdentifierModel.is_primary.desc()).limit(1)
    )).scalars().first()
    return {
        "created_by": rep_user_id,
        "on_behalf_of_customer_id": str(profile.id),
        "on_behalf_of_identifier": (identifier.value_display if identifier else None),
        "intake_channel": interaction.channel,
        "interaction_id": str(interaction.id),
        # Frozen at creation. Reading it live later would report the tier the
        # call eventually reached, not the one it had when the case was raised.
        "verification_level": interaction.verification_level,
        "raised_by_agent": True,
    }


async def consent_gate(
    session: AsyncSession, *, tenant_token: str, profile: CustomerProfileModel,
    purpose: str,
) -> dict[str, Any]:
    """Is there consent for this purpose — and if not, what happens?

    Decision 4, taken with the user: **a hard refusal is correct and useless on a
    live call.** The rep is talking to the very person whose consent it is, so
    the answer is a capture-consent-now step rather than "go away and do it
    yourself". A rep typing on somebody's behalf still must not LAUNDER a
    refusal — capturing is an act with a record, not a checkbox the rep ticks.
    """
    email = None
    ident = (await session.execute(
        select(CustomerIdentifierModel).where(
            CustomerIdentifierModel.customer_id == profile.id,
            CustomerIdentifierModel.kind == "email"))).scalars().first()
    if ident is not None:
        email = ident.value_normalised

    from case_service.compliance import consent as consent_mod

    # Studio has always sent `case_management`, which is not in the ledger's
    # closed vocabulary. Kept working as the name it meant.
    purpose = {"case_management": consent_mod.CASE_PROCESSING}.get(purpose, purpose)

    granted = False
    reason = "no consent record was found for this purpose"
    try:
        if purpose == consent_mod.CASE_PROCESSING:
            # Case work rests on contract, not consent (`GATED_PURPOSES` leaves
            # it out for exactly that reason), so silence is lawful. Only a
            # recorded withdrawal stops it.
            state = (await consent_mod.current_state(session, email, tenant_id=tenant_token)
                     if email else {})
            entry = state.get(purpose)
            granted = entry is None or bool(entry["granted"])
            reason = "" if granted else "they withdrew consent for case processing"
        elif email:
            # Until 2026-09-29 this passed `subject=` to a function whose
            # parameter is `subject_ref`. The TypeError was caught below, so
            # every call answered "consent could not be read" — a recorded
            # grant was never seen, and nothing said so.
            granted = await consent_mod.has_consent(
                session, email, purpose, tenant_id=tenant_token)
            reason = "" if granted else "consent for this purpose is not on record"
    except Exception as exc:                                     # noqa: BLE001
        log.info("engage: consent unreadable: %s", exc)
        granted = False
        reason = "consent could not be read, so it is treated as absent"

    if granted:
        return {"granted": True, "action": None, "purpose": purpose}
    return {
        "granted": False,
        "reason": reason,
        # The whole of decision 4 in one field.
        "action": "capture_consent_now",
        "purpose": purpose,
        "subject": email,
        "note": ("Ask the customer on the call and record their answer. The "
                 "capture names who took it, when, and on which interaction — a "
                 "rep may not simply assert it."),
    }
