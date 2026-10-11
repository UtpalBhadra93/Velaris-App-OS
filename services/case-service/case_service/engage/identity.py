"""The identity spine: normalise, resolve, adopt, merge.

Design: docs/Future/Done/CustomerEngagement.md §3. The only genuinely hard part.

**The resolution ladder is a ladder of confidence, not a lookup.** Each rung is
weaker than the one above, and what falls out of the bottom is an *unresolved*
interaction rather than a guess:

    1. an exact identifier hit            → that party, certainly
    2. `portal_customers` email/phone     → adopt
    3. `case_instances.portal_submitter_email` → adopt
    4. `channel_conversations.peer_address`    → adopt
    5. nothing                            → UNRESOLVED, and a stub the rep can act on

**Never auto-create a profile from an unverified inbound number.** That
manufactures a person out of a wrong number, and the person it manufactures then
accumulates somebody else's calls. Adoption at rungs 2–4 writes an identifier
with `verified = False` and `source = resolution`: **resolution is a suggestion
until a human or a challenge confirms it.**

Normalisation is the whole value of the identifier table — two reps typing the
same number differently must reach the same party — so it lives here, once, and
both the writer and the lookup use it.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import hashlib
import logging
import re
import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from velaris_shared.db.models import (
    CustomerIdentifierModel,
    CustomerLinkModel,
    CustomerMergeModel,
    CustomerProfileModel,
    CustomerRelationModel,
)

log = logging.getLogger(__name__)

IDENTIFIER_KINDS: tuple[str, ...] = (
    "email", "phone", "portal_customer", "external_crm", "national_id_hash",
    "account_no",
)
PARTY_KINDS: tuple[str, ...] = ("person", "household", "organisation")
SOURCES_OF_TRUTH: tuple[str, ...] = ("velaris", "crm_mastered", "read_through")
RELATIONS: tuple[str, ...] = ("member_of", "contact_for", "authorised_by", "related_to")

#: How stale a CRM-mastered attribute may be before the 360 stops presenting it
#: as current. Shown, never hidden — and a profile that has never been refreshed
#: is stale, not fresh.
STALE_AFTER_MINUTES = 60


class IdentityError(ValueError):
    """The spine cannot do that. The message is for a human."""


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


# ── normalisation ────────────────────────────────────────────────────────────


def normalise(kind: str, value: str) -> str:
    """The stored form. One implementation, used by writer and lookup alike.

    A phone is reduced to digits with a leading `+` kept, which is E.164 in
    practice for the shapes a contact centre sees; an email is lowercased and
    trimmed. Anything else is trimmed and lowercased, because a case-sensitive
    account number is a support ticket waiting to happen.
    """
    raw = str(value or "").strip()
    if not raw:
        raise IdentityError("an identifier needs a value")
    if kind == "phone":
        digits = re.sub(r"[^0-9+]", "", raw)
        if digits.startswith("00"):
            digits = "+" + digits[2:]
        if not digits.lstrip("+").isdigit() or len(digits.lstrip("+")) < 6:
            raise IdentityError(f"{raw!r} is not a phone number this can normalise")
        return digits if digits.startswith("+") else digits
    if kind == "email":
        if "@" not in raw:
            raise IdentityError(f"{raw!r} is not an email address")
        return raw.lower()
    if kind == "national_id_hash":
        # Never stored in the clear. The caller may pass the raw value; what
        # lands is a hash, because a national id in an identifier table is a
        # breach waiting for a SELECT.
        return hashlib.sha256(raw.encode()).hexdigest()
    return raw.lower()


# ── profiles ─────────────────────────────────────────────────────────────────


async def create_profile(
    session: AsyncSession, *, tenant_uuid: uuid.UUID, display_name: str,
    party_kind: str = "person", source: str = "agent",
    source_of_truth: str = "velaris", external_system: str | None = None,
    created_by: str | None = None,
) -> CustomerProfileModel:
    if party_kind not in PARTY_KINDS:
        raise IdentityError(f"a party is one of {', '.join(PARTY_KINDS)}")
    if source_of_truth not in SOURCES_OF_TRUTH:
        raise IdentityError(f"source_of_truth is one of {', '.join(SOURCES_OF_TRUTH)}")
    if source_of_truth != "velaris" and not external_system:
        # A profile mastered by "somewhere else" is a profile nobody can refresh.
        raise IdentityError(
            "a profile mastered outside Velaris must name the system that "
            "masters it")
    row = CustomerProfileModel(
        id=uuid.uuid4(), tenant_id=tenant_uuid, party_kind=party_kind,
        display_name=str(display_name)[:255] or "Unnamed",
        source=source, source_of_truth=source_of_truth,
        external_system=external_system, created_by=created_by)
    session.add(row)
    await session.flush()
    return row


async def add_identifier(
    session: AsyncSession, *, tenant_uuid: uuid.UUID, customer_id: uuid.UUID,
    kind: str, value: str, verified: bool = False, verified_by: str | None = None,
    source: str = "agent", is_primary: bool = False,
) -> CustomerIdentifierModel:
    """Attach a way the world names this party.

    A value already claimed by ANOTHER party is refused rather than moved: two
    profiles sharing a phone number is the duplicate this feature exists to
    resolve, and silently reassigning it would hide the duplicate instead.
    """
    if kind not in IDENTIFIER_KINDS:
        raise IdentityError(f"an identifier is one of {', '.join(IDENTIFIER_KINDS)}")
    normalised = normalise(kind, value)
    existing = (await session.execute(
        select(CustomerIdentifierModel).where(
            CustomerIdentifierModel.tenant_id == tenant_uuid,
            CustomerIdentifierModel.kind == kind,
            CustomerIdentifierModel.value_normalised == normalised))).scalars().first()
    if existing is not None:
        if existing.customer_id == customer_id:
            if verified and not existing.verified:
                existing.verified = True
                existing.verified_at = _utcnow()
                existing.verified_by = verified_by
                await session.flush()
            return existing
        raise IdentityError(
            f"that {kind} already belongs to another profile. Merge the two "
            f"rather than moving the identifier — a duplicate that is quietly "
            f"reassigned is a duplicate nobody ever sees.")
    row = CustomerIdentifierModel(
        id=uuid.uuid4(), tenant_id=tenant_uuid, customer_id=customer_id,
        kind=kind, value_normalised=normalised,
        value_display=str(value)[:255] if kind != "national_id_hash" else "(hashed)",
        verified=bool(verified), verified_at=_utcnow() if verified else None,
        verified_by=verified_by, is_primary=bool(is_primary), source=source)
    session.add(row)
    await session.flush()
    return row


async def resolve(
    session: AsyncSession, *, tenant_uuid: uuid.UUID, tenant_token: str,
    kind: str, value: str, adopt: bool = True,
) -> dict[str, Any]:
    """The ladder. Returns the party and HOW confidently it was reached.

    `confidence` is part of the answer, not decoration: a rung-1 hit is an
    identifier somebody attached, and a rung-4 hit is a number that once appeared
    in a chat. A screen that showed both as "customer found" would be lying about
    one of them.
    """
    try:
        normalised = normalise(kind, value)
    except IdentityError as exc:
        return {"resolved": False, "rung": 0, "reason": str(exc)}

    # ── 1. an identifier somebody attached ──────────────────────────────────
    hit = (await session.execute(
        select(CustomerIdentifierModel).where(
            CustomerIdentifierModel.tenant_id == tenant_uuid,
            CustomerIdentifierModel.kind == kind,
            CustomerIdentifierModel.value_normalised == normalised))).scalars().first()
    if hit is not None:
        profile = await follow_merges(session, hit.customer_id)
        return {"resolved": True, "rung": 1, "customer_id": str(profile.id),
                "confidence": "identifier",
                "verified": bool(hit.verified),
                "reason": "an identifier on this profile matches exactly"}

    # ── 2–4. adoption rungs ─────────────────────────────────────────────────
    for rung, finder, why in (
        (2, _from_portal_customer, "a portal customer record carries this value"),
        (3, _from_case_submitter, "a case was submitted from this address"),
        (4, _from_conversation, "a conversation came from this address"),
    ):
        try:
            found = await finder(session, tenant_uuid, tenant_token, kind, normalised)
        except Exception as exc:                                 # noqa: BLE001
            log.info("engage: resolution rung %s unavailable: %s", rung, exc)
            continue
        if not found:
            continue
        if not adopt:
            return {"resolved": False, "rung": rung, "candidate": found,
                    "reason": why + ", but adoption was not requested"}
        profile = await create_profile(
            session, tenant_uuid=tenant_uuid,
            display_name=found.get("display_name") or str(value),
            source="resolution", created_by="resolution")
        await add_identifier(
            session, tenant_uuid=tenant_uuid, customer_id=profile.id, kind=kind,
            value=value, verified=False, source="resolution", is_primary=True)
        return {"resolved": True, "rung": rung, "customer_id": str(profile.id),
                "confidence": "adopted", "verified": False,
                "reason": why + " — adopted as a SUGGESTION; nothing here is "
                                "verified until a person or a challenge confirms it"}

    # ── 5. nothing ──────────────────────────────────────────────────────────
    return {
        "resolved": False, "rung": 5, "customer_id": None,
        "reason": ("no profile matches. A stub is offered rather than a new "
                   "profile: creating one from an unverified inbound number "
                   "manufactures a person out of a wrong number."),
    }


# The adoption rungs read OTHER tables, so each carries the tenant clause itself.
# Until 2026-09-29 rungs 3 and 4 had none: a number or address that appeared on
# ANOTHER tenant's case or chat adopted a profile here named after that tenant's
# customer — their name on this tenant's screen, and a phantom person besides.
# Rung 2 compared a UUID column to the tenant token string, so on PostgreSQL it
# never matched at all. (Found building Oct item 3, which reads the same sources.)


async def _conversation_tenants(session, tenant_uuid, tenant_token) -> list[str]:
    from velaris_shared.tenancy.identity import resolve_tenant

    out = {str(tenant_token), str(tenant_uuid)}
    resolved = await resolve_tenant(session, str(tenant_token))
    if resolved is not None:
        out |= {str(resolved[0]), str(resolved[1])}
    return sorted(out)


def phone_matches(stored: str | None, normalised: str) -> bool:
    """Compare a free-text stored phone with a normalised one. Never raises."""
    if not stored:
        return False
    try:
        return normalise("phone", stored).lstrip("+") == normalised.lstrip("+")
    except IdentityError:
        return False


async def _from_portal_customer(session, tenant_uuid, tenant_token, kind, normalised):
    from sqlalchemy import func, or_

    from velaris_shared.db.models import PortalCustomerModel

    if kind == "email":
        row = (await session.execute(
            select(PortalCustomerModel).where(
                PortalCustomerModel.tenant_id == tenant_uuid,
                or_(func.lower(PortalCustomerModel.primary_email) == normalised,
                    func.lower(PortalCustomerModel.alt_email) == normalised),
            ).limit(1))).scalars().first()
    elif kind == "phone":
        # Stored free-text, so narrowed by the last digits in SQL and compared
        # normalised in Python.
        tail = normalised.lstrip("+")[-6:]
        rows = (await session.execute(
            select(PortalCustomerModel).where(
                PortalCustomerModel.tenant_id == tenant_uuid,
                PortalCustomerModel.phone.like(f"%{tail[-2:]}"),
            ).limit(500))).scalars().all()
        row = next((r for r in rows if phone_matches(r.phone, normalised)), None)
    else:
        return None
    if row is None:
        return None
    return {"display_name": getattr(row, "display_name", None) or normalised,
            "portal_customer_id": str(row.id)}


async def _from_case_submitter(session, tenant_uuid, tenant_token, kind, normalised):
    from sqlalchemy import func

    from velaris_shared.db.models import CaseInstanceModel

    if kind != "email":
        return None
    row = (await session.execute(
        select(CaseInstanceModel).where(
            CaseInstanceModel.tenant_id == tenant_uuid,
            func.lower(CaseInstanceModel.portal_submitter_email) == normalised)
        .limit(1))).scalars().first()
    if row is None:
        return None
    return {"display_name": getattr(row, "portal_submitter_name", None) or normalised}


async def _from_conversation(session, tenant_uuid, tenant_token, kind, normalised):
    from velaris_shared.db.models import ChannelConversationModel

    row = (await session.execute(
        select(ChannelConversationModel).where(
            ChannelConversationModel.tenant_id.in_(
                await _conversation_tenants(session, tenant_uuid, tenant_token)),
            ChannelConversationModel.peer_address == normalised)
        .limit(1))).scalars().first()
    if row is None:
        return None
    return {"display_name": getattr(row, "peer_name", None) or normalised}


async def follow_merges(session: AsyncSession, customer_id: uuid.UUID,
                        depth: int = 8) -> CustomerProfileModel:
    """A merged profile redirects. Bounded, because a cycle is a bug not a loop."""
    profile = await session.get(CustomerProfileModel, customer_id)
    if profile is None:
        raise IdentityError("no such profile")
    seen = {profile.id}
    while profile.merged_into_id and depth > 0:
        if profile.merged_into_id in seen:
            log.warning("engage: merge cycle at %s", profile.id)
            break
        seen.add(profile.merged_into_id)
        nxt = await session.get(CustomerProfileModel, profile.merged_into_id)
        if nxt is None:
            break
        profile, depth = nxt, depth - 1
    return profile


# ── links, relations, merges ─────────────────────────────────────────────────


async def link(
    session: AsyncSession, *, tenant_uuid: uuid.UUID, customer_id: uuid.UUID,
    object_kind: str, object_id: uuid.UUID, reason: str = "resolved",
    linked_by: str | None = None,
) -> CustomerLinkModel:
    existing = (await session.execute(
        select(CustomerLinkModel).where(
            CustomerLinkModel.customer_id == customer_id,
            CustomerLinkModel.object_kind == object_kind,
            CustomerLinkModel.object_id == object_id))).scalars().first()
    if existing is not None:
        return existing
    row = CustomerLinkModel(
        id=uuid.uuid4(), tenant_id=tenant_uuid, customer_id=customer_id,
        object_kind=object_kind, object_id=object_id, link_reason=reason,
        linked_by=linked_by)
    session.add(row)
    await session.flush()
    return row


async def relate(
    session: AsyncSession, *, tenant_uuid: uuid.UUID, from_id: uuid.UUID,
    to_id: uuid.UUID, relation: str, may_act_for: bool = False,
    note: str | None = None, created_by: str | None = None,
) -> CustomerRelationModel:
    if relation not in RELATIONS:
        raise IdentityError(f"a relation is one of {', '.join(RELATIONS)}")
    if from_id == to_id:
        raise IdentityError("a party cannot be related to itself")
    # Idempotent: confirming the same connection twice records it once.
    existing = (await session.execute(select(CustomerRelationModel).where(
        CustomerRelationModel.from_customer_id == from_id,
        CustomerRelationModel.to_customer_id == to_id,
        CustomerRelationModel.relation == relation))).scalars().first()
    if existing is not None:
        return existing
    row = CustomerRelationModel(
        id=uuid.uuid4(), tenant_id=tenant_uuid, from_customer_id=from_id,
        to_customer_id=to_id, relation=relation, may_act_for=bool(may_act_for),
        note=note, created_by=created_by)
    session.add(row)
    await session.flush()
    return row


async def merge(
    session: AsyncSession, *, tenant_uuid: uuid.UUID, winner_id: uuid.UUID,
    loser_id: uuid.UUID, reason: str, merged_by: str,
) -> CustomerMergeModel:
    """Duplicate people are the normal state of any contact centre.

    A merge is an **additive record**, never a row deletion: the loser stays
    readable and redirects, so it can be undone and so an audit can answer "why
    does this call show that person's cases".
    """
    if winner_id == loser_id:
        raise IdentityError("a profile cannot be merged into itself")
    if len((reason or "").strip()) < 5:
        raise IdentityError("a merge needs a reason somebody can read later")
    winner = await session.get(CustomerProfileModel, winner_id)
    loser = await session.get(CustomerProfileModel, loser_id)
    if winner is None or loser is None:
        raise IdentityError("both profiles must exist")
    if loser.status == "merged":
        raise IdentityError("that profile has already been merged")

    moved: dict[str, list[str]] = {"identifiers": [], "links": []}
    for ident in (await session.execute(
        select(CustomerIdentifierModel).where(
            CustomerIdentifierModel.customer_id == loser_id))).scalars().all():
        ident.customer_id = winner_id
        ident.is_primary = False
        moved["identifiers"].append(str(ident.id))
    for row in (await session.execute(
        select(CustomerLinkModel).where(
            CustomerLinkModel.customer_id == loser_id))).scalars().all():
        row.customer_id = winner_id
        moved["links"].append(str(row.id))

    loser.status = "merged"
    loser.merged_into_id = winner_id
    record = CustomerMergeModel(
        id=uuid.uuid4(), tenant_id=tenant_uuid, winner_id=winner_id,
        loser_id=loser_id, moved=moved, reason=reason, merged_by=merged_by)
    session.add(record)
    await session.flush()
    return record


async def unmerge(session: AsyncSession, *, record: CustomerMergeModel,
                  actor: str) -> CustomerMergeModel:
    """Put back exactly what the merge took, by id.

    Not "everything that looks like it came from there" — a profile that gained
    identifiers after the merge keeps them, because they were never the loser's.
    """
    if record.undone_at is not None:
        raise IdentityError("that merge has already been undone")
    moved = record.moved or {}
    for ident_id in moved.get("identifiers", []):
        row = await session.get(CustomerIdentifierModel, uuid.UUID(ident_id))
        if row is not None:
            row.customer_id = record.loser_id
    for link_id in moved.get("links", []):
        row = await session.get(CustomerLinkModel, uuid.UUID(link_id))
        if row is not None:
            row.customer_id = record.loser_id
    loser = await session.get(CustomerProfileModel, record.loser_id)
    if loser is not None:
        loser.status = "active"
        loser.merged_into_id = None
    record.undone_at = _utcnow()
    record.undone_by = actor
    await session.flush()
    return record


def staleness(profile: CustomerProfileModel, now: datetime | None = None
              ) -> dict[str, Any]:
    """Is what we hold about this party still current?

    A Velaris-owned profile is never stale — we are the source. A CRM-mastered
    one that has **never** been refreshed is stale, not fresh: a NULL must not
    read as "up to date", which is the mistake that makes a cache dangerous.
    """
    if profile.source_of_truth == "velaris":
        return {"stale": False, "reason": "Velaris is the source of truth here"}
    moment = now or _utcnow()
    refreshed = profile.refreshed_at
    if refreshed is None:
        return {"stale": True, "refreshed_at": None,
                "reason": (f"mastered by {profile.external_system or 'an external '
                           'system'} and never refreshed here")}
    if refreshed.tzinfo is None:
        refreshed = refreshed.replace(tzinfo=timezone.utc)
    minutes = (moment - refreshed).total_seconds() / 60
    return {
        "stale": minutes > STALE_AFTER_MINUTES,
        "refreshed_at": refreshed.isoformat(),
        "minutes_old": int(minutes),
        "reason": (f"not refreshed from {profile.external_system} for "
                   f"{int(minutes)} minutes" if minutes > STALE_AFTER_MINUTES
                   else "recently refreshed"),
    }
