"""Customer Engagement — the rep desk. Item 12.

The customer's question was *"when someone calls, will the rep see the
customer's details, and to what extent?"*. These routes are the answer, and
three of their properties are the answer's substance:

* **Every route is behind two gates**: the platform released the feature, and
  this tenant installed the listing. Neither implies the other, and a
  platform-wide flip would switch a customer 360 on for every tenant on a shared
  box.
* **The 360 is composed on the READER's authority.** Each panel names the
  HxGuard action its reader must hold, and a panel they may not read is
  **absent, not empty** — an empty panel says "this customer has none of these",
  and a rep will act on that.
* **A field unmasks one at a time, with a reason.** There is no bulk reveal, and
  there is deliberately no endpoint that could become one.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import logging
import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from velaris_shared import hxguard
from velaris_shared.auth.models import AuthenticatedUser
from velaris_shared.db.models import (
    CustomerInteractionModel,
    CustomerLinkModel,
    CustomerMergeModel,
    CustomerProfileModel,
    TenantModel,
)
from velaris_shared.db.session import get_session
from velaris_shared.middleware.endpoint_rate_limit import rate_limit

from case_service.core.timestamps import iso_utc
from case_service.engage import (
    action_rail, backfill, entitlement, erasure, identity, insights, interactions, joins,
    three_sixty,
)
from case_service.tenancy.identity import resolve_tenant

log = logging.getLogger(__name__)

router = APIRouter(prefix="/engage", tags=["engage"])

#: A screen-pop happens on every inbound call. It is the hot path here.
_pop_rl = rate_limit(max_calls=120, window_seconds=60, name="engage-pop")
#: Minting a join link makes a credential; opening one is where a guesser would
#: try tokens. Both are bounded per client (Oct item 1).
_join_create_rl = rate_limit(max_calls=30, window_seconds=60, name="engage-join-create")
_join_redeem_rl = rate_limit(max_calls=20, window_seconds=60, name="engage-join-redeem")
#: A backfill scan reads every portal account and case submitter in the tenant.
_backfill_rl = rate_limit(max_calls=3, window_seconds=60, name="engage-backfill")
#: A recap spends a model call and writes a ledger row. Bounded per client so a
#: script cannot turn the rep desk into a free inference endpoint.
_recap_rl = rate_limit(max_calls=20, window_seconds=60, name="engage-recap")


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ProfileIn(_Strict):
    display_name: str = Field(min_length=1, max_length=255)
    party_kind: str = Field(default="person", max_length=16)
    source_of_truth: str = Field(default="velaris", max_length=16)
    external_system: str | None = Field(default=None, max_length=64)


class IdentifierIn(_Strict):
    kind: str = Field(max_length=24)
    value: str = Field(min_length=1, max_length=255)
    verified: bool = False
    is_primary: bool = False


class RelationIn(_Strict):
    to_customer_id: uuid.UUID
    relation: str = Field(max_length=24)
    may_act_for: bool = False
    note: str | None = Field(default=None, max_length=2000)


class MergeIn(_Strict):
    loser_id: uuid.UUID
    reason: str = Field(min_length=5, max_length=2000)


class PopIn(_Strict):
    channel: str = Field(max_length=16)
    ani: str | None = Field(default=None, max_length=64)
    address: str | None = Field(default=None, max_length=255)
    dnis: str | None = Field(default=None, max_length=64)
    queue: str | None = Field(default=None, max_length=128)
    external_call_id: str | None = Field(default=None, max_length=255)
    transferred_from: uuid.UUID | None = None


class VerifyIn(_Strict):
    factor: str = Field(max_length=16)
    #: Whether the challenge passed. The challenge itself belongs to whichever
    #: mechanism ran it; this route owns the consequence.
    ok: bool
    to_tier: str = Field(max_length=8)


class TransferIn(_Strict):
    to_agent: str = Field(min_length=1, max_length=255)
    queue: str | None = Field(default=None, max_length=128)


class WrapIn(_Strict):
    disposition_code: str = Field(min_length=1, max_length=64)
    notes: str | None = Field(default=None, max_length=4000)
    outcome: str | None = Field(default=None, max_length=64)


class RevealIn(_Strict):
    field_path: str = Field(max_length=128)
    reason: str = Field(min_length=5, max_length=2000)
    interaction_id: uuid.UUID | None = None


class OnBehalfIn(_Strict):
    interaction_id: uuid.UUID
    purpose: str = Field(default="case_management", max_length=64)


async def _tenant(session: AsyncSession, user: AuthenticatedUser
                  ) -> tuple[uuid.UUID, str]:
    token = getattr(user, "tenant_id", None) or "default"
    resolved = await resolve_tenant(session, token)
    if resolved is None:
        raise HTTPException(403, "VEL-ENGAGE-TENANT-UNRESOLVED-403")
    return resolved[0], token


async def _gate(session: AsyncSession, tenant_token: str) -> None:
    """Both gates, on every route. A 404 rather than a 403 when the feature is
    dark: a tenant without the entitlement should not learn it exists."""
    allowed, why = await entitlement.enabled_for(session, tenant_token)
    if not allowed:
        raise HTTPException(404, f"VEL-ENGAGE-404: {why}")


async def _profile(session: AsyncSession, customer_id: uuid.UUID,
                   tenant: uuid.UUID) -> CustomerProfileModel:
    row = await session.get(CustomerProfileModel, customer_id)
    if row is None or row.tenant_id != tenant:
        raise HTTPException(404, "VEL-ENGAGE-404: no such customer")
    # A merged profile redirects rather than 404s — a rep following an old link
    # should land on the surviving person, not on an error.
    return await identity.follow_merges(session, row.id)


async def _call_with(session: AsyncSession, interaction_id: uuid.UUID,
                     tenant: uuid.UUID,
                     profile: CustomerProfileModel) -> CustomerInteractionModel:
    """The call, and proof it is a call WITH THIS customer.

    The tier a route acts on comes from the interaction. Until 2026-09-17 no
    route checked whose call it was, so a rep who verified caller A to Full could
    name A's call while reading — and revealing — customer B. The tier is a fact
    about one conversation with one person; it must not travel to anybody else.

    An unresolved call (no customer yet) verifies nobody, so it is refused here
    too. Merges are followed on both sides: a call recorded against a profile
    that was later merged still belongs to the survivor.
    """
    row = await session.get(CustomerInteractionModel, interaction_id)
    if row is None or row.tenant_id != tenant:
        raise HTTPException(404, "VEL-ENGAGE-404: no such interaction")
    if row.customer_id is None:
        raise HTTPException(409, "VEL-ENGAGE-CALL-409: this call is not linked to a "
                                 "customer, so it verifies nobody")
    caller = await identity.follow_merges(session, row.customer_id)
    if caller.id != profile.id:
        raise HTTPException(409, "VEL-ENGAGE-CALL-409: this call is not with this "
                                 "customer; its verification does not apply here")
    return row


def _profile_json(p: CustomerProfileModel) -> dict[str, Any]:
    return {
        "id": str(p.id), "party_kind": p.party_kind,
        "display_name": p.display_name, "status": p.status,
        "merged_into_id": str(p.merged_into_id) if p.merged_into_id else None,
        "source_of_truth": p.source_of_truth,
        "external_system": p.external_system,
        "freshness": identity.staleness(p),
        "source": p.source, "created_at": iso_utc(p.created_at),
    }


@router.get("/status")
async def engage_status(
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(hxguard.guard("engage.profile.read")),
) -> dict[str, Any]:
    """Both gates, reported separately. Readable even when the feature is dark,
    so an operator can see WHICH gate is closed."""
    _, token = await _tenant(session, user)
    return await entitlement.status(session, token)


# ── profiles ────────────────────────────────────────────────────────────────


@router.post("/profiles", status_code=201)
async def create_profile(
    body: ProfileIn,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(hxguard.guard("engage.profile.write")),
) -> dict[str, Any]:
    tenant_uuid, token = await _tenant(session, user)
    await _gate(session, token)
    try:
        row = await identity.create_profile(
            session, tenant_uuid=tenant_uuid, display_name=body.display_name,
            party_kind=body.party_kind, source_of_truth=body.source_of_truth,
            external_system=body.external_system,
            created_by=getattr(user, "user_id", None))
    except identity.IdentityError as exc:
        raise HTTPException(422, f"VEL-ENGAGE-422: {exc}") from exc
    await session.commit()
    return {"customer": _profile_json(row)}


@router.post("/profiles/{customer_id}/identifiers", status_code=201)
async def add_identifier(
    customer_id: uuid.UUID,
    body: IdentifierIn,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(hxguard.guard("engage.profile.write")),
) -> dict[str, Any]:
    tenant_uuid, token = await _tenant(session, user)
    await _gate(session, token)
    profile = await _profile(session, customer_id, tenant_uuid)
    try:
        row = await identity.add_identifier(
            session, tenant_uuid=tenant_uuid, customer_id=profile.id,
            kind=body.kind, value=body.value, verified=body.verified,
            is_primary=body.is_primary)
    except identity.IdentityError as exc:
        raise HTTPException(409, f"VEL-ENGAGE-IDENT-409: {exc}") from exc
    await session.commit()
    return {"identifier": {"id": str(row.id), "kind": row.kind,
                           "verified": row.verified,
                           "value": row.value_display}}


@router.post("/profiles/{customer_id}/relations", status_code=201)
async def relate(
    customer_id: uuid.UUID,
    body: RelationIn,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(hxguard.guard("engage.profile.write")),
) -> dict[str, Any]:
    """A household's members, a business's contacts.

    `may_act_for` is set explicitly: being a member of a household does not by
    itself let somebody speak for it.
    """
    tenant_uuid, token = await _tenant(session, user)
    await _gate(session, token)
    profile = await _profile(session, customer_id, tenant_uuid)
    other = await _profile(session, body.to_customer_id, tenant_uuid)
    try:
        row = await identity.relate(
            session, tenant_uuid=tenant_uuid, from_id=profile.id, to_id=other.id,
            relation=body.relation, may_act_for=body.may_act_for, note=body.note,
            created_by=getattr(user, "user_id", None))
    except identity.IdentityError as exc:
        raise HTTPException(422, f"VEL-ENGAGE-422: {exc}") from exc
    await session.commit()
    return {"relation": {"id": str(row.id), "relation": row.relation,
                         "may_act_for": row.may_act_for}}


@router.post("/profiles/{customer_id}/merge")
async def merge_profiles(
    customer_id: uuid.UUID,
    body: MergeIn,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(hxguard.guard("engage.profile.merge")),
) -> dict[str, Any]:
    """Duplicates are the normal state of a contact centre.

    Its own action, and additive: the loser stays readable and redirects, so an
    audit can answer "why does this call show that person's cases".
    """
    tenant_uuid, token = await _tenant(session, user)
    await _gate(session, token)
    winner = await _profile(session, customer_id, tenant_uuid)
    try:
        record = await identity.merge(
            session, tenant_uuid=tenant_uuid, winner_id=winner.id,
            loser_id=body.loser_id, reason=body.reason,
            merged_by=getattr(user, "user_id", None) or "unknown")
    except identity.IdentityError as exc:
        raise HTTPException(409, f"VEL-ENGAGE-MERGE-409: {exc}") from exc
    await session.commit()
    return {"merge": {"id": str(record.id), "winner": str(record.winner_id),
                      "loser": str(record.loser_id), "moved": record.moved}}


@router.post("/merges/{merge_id}/undo")
async def undo_merge(
    merge_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(hxguard.guard("engage.profile.merge")),
) -> dict[str, Any]:
    """Put back exactly what the merge took — by id, not by resemblance."""
    tenant_uuid, token = await _tenant(session, user)
    await _gate(session, token)
    record = await session.get(CustomerMergeModel, merge_id)
    if record is None or record.tenant_id != tenant_uuid:
        raise HTTPException(404, "VEL-ENGAGE-404: no such merge")
    try:
        await identity.unmerge(session, record=record,
                              actor=getattr(user, "user_id", None) or "unknown")
    except identity.IdentityError as exc:
        raise HTTPException(409, f"VEL-ENGAGE-409: {exc}") from exc
    await session.commit()
    return {"merge": {"id": str(record.id), "undone_at": iso_utc(record.undone_at)}}


# ── the 360 ─────────────────────────────────────────────────────────────────


@router.get("/profiles/{customer_id}/360")
async def three_sixty_view(
    customer_id: uuid.UUID,
    interaction_id: uuid.UUID | None = Query(None),
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(hxguard.guard("engage.profile.read")),
) -> dict[str, Any]:
    """The 360, composed for THIS reader at THIS call's verification tier.

    The tier comes from the interaction, not from a query parameter — a caller
    who could name their own tier would be answering the customer's question for
    themselves.
    """
    tenant_uuid, token = await _tenant(session, user)
    await _gate(session, token)
    profile = await _profile(session, customer_id, tenant_uuid)

    tier = "none"
    if interaction_id is not None:
        row = await _call_with(session, interaction_id, tenant_uuid, profile)
        tier = row.verification_level

    # The reader's OWN grants, resolved one by one through HxGuard's normal
    # decision path. The 360 must never fetch on the composer's authority —
    # that is exactly how a 360 becomes an authorisation bypass.
    subject = hxguard.subject_from_user(user)
    allowed = {
        action: (await hxguard.check(session, subject, action)).allow
        for action in sorted(set(three_sixty.PANEL_ACTIONS.values()) - {"case.read"})
    }
    # `case.read` is answered per case, not once: asked without a case id it
    # fails closed, which withheld this panel from everybody until 2026-09-29.
    allowed["case.read"] = True
    readable = None
    if "cases" in three_sixty.TIER_PANELS[tier]:
        linked = list((await session.execute(
            select(CustomerLinkModel.object_id).where(
                CustomerLinkModel.customer_id == profile.id,
                CustomerLinkModel.object_kind == "case").limit(50))).scalars().all())
        readable = await three_sixty.readable_case_ids(
            session, user, tenant_uuid=tenant_uuid, case_ids=linked)
    revealed = await _revealed(session, profile.id, interaction_id)
    return await three_sixty.compose(
        session, profile=profile, tier=tier, allowed=allowed, revealed=revealed,
        readable_cases=readable)


async def _revealed(session: AsyncSession, customer_id: uuid.UUID,
                    interaction_id: uuid.UUID | None) -> set[str]:
    """Fields already unmasked ON THIS CALL. A reveal does not persist across
    calls: the reason was given for this conversation."""
    from velaris_shared.db.models import CustomerDisclosureModel

    if interaction_id is None:
        return set()
    rows = (await session.execute(
        select(CustomerDisclosureModel.field_path).where(
            CustomerDisclosureModel.customer_id == customer_id,
            CustomerDisclosureModel.interaction_id == interaction_id))).all()
    return {r[0] for r in rows}


@router.post("/profiles/{customer_id}/reveal")
async def reveal_field(
    customer_id: uuid.UUID,
    body: RevealIn,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(hxguard.guard("engage.profile.reveal")),
) -> dict[str, Any]:
    """Unmask ONE field, with a reason. There is no bulk endpoint, by design."""
    tenant_uuid, token = await _tenant(session, user)
    await _gate(session, token)
    profile = await _profile(session, customer_id, tenant_uuid)

    tier = "none"
    if body.interaction_id is not None:
        row = await _call_with(session, body.interaction_id, tenant_uuid, profile)
        tier = row.verification_level
    try:
        record = await three_sixty.reveal(
            session, tenant_uuid=tenant_uuid, profile=profile,
            field_path=body.field_path, reason=body.reason, tier=tier,
            actor=getattr(user, "user_id", None) or "unknown",
            interaction_id=body.interaction_id)
    except identity.IdentityError as exc:
        raise HTTPException(403, f"VEL-ENGAGE-REVEAL-403: {exc}") from exc
    await session.commit()
    return {"revealed": {"field_path": record.field_path,
                         "at_verification": record.at_verification,
                         "by": record.revealed_by}}


# ── the call ────────────────────────────────────────────────────────────────


@router.post("/screen-pop", status_code=201, dependencies=[Depends(_pop_rl)])
async def screen_pop(
    body: PopIn,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(hxguard.guard("engage.interactions.write")),
) -> dict[str, Any]:
    """Pop the PERSON, on an indexed lookup — and record the call either way.

    An unresolved call is still an interaction. A contact centre that only
    records the calls it could identify has no idea how many it could not.
    """
    tenant_uuid, token = await _tenant(session, user)
    await _gate(session, token)
    try:
        out = await interactions.screen_pop(
            session, tenant_uuid=tenant_uuid, tenant_token=token,
            channel=body.channel, ani=body.ani, address=body.address,
            dnis=body.dnis, queue=body.queue,
            external_call_id=body.external_call_id,
            agent_user_id=getattr(user, "user_id", None),
            transferred_from=body.transferred_from)
    except identity.IdentityError as exc:
        raise HTTPException(422, f"VEL-ENGAGE-422: {exc}") from exc
    await session.commit()
    return out


@router.post("/interactions/{interaction_id}/verify")
async def verify_caller(
    interaction_id: uuid.UUID,
    body: VerifyIn,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(hxguard.guard("engage.interactions.write")),
) -> dict[str, Any]:
    """Raise the tier on this call. A failure says one thing, whatever failed."""
    tenant_uuid, token = await _tenant(session, user)
    await _gate(session, token)
    row = await session.get(CustomerInteractionModel, interaction_id)
    if row is None or row.tenant_id != tenant_uuid:
        raise HTTPException(404, "VEL-ENGAGE-404: no such interaction")
    try:
        out = await interactions.verify(
            session, interaction=row, factor=body.factor, ok=body.ok,
            to_tier=body.to_tier, actor=getattr(user, "user_id", None))
    except identity.IdentityError as exc:
        raise HTTPException(422, f"VEL-ENGAGE-422: {exc}") from exc
    await session.commit()
    return out


@router.post("/interactions/{interaction_id}/transfer", status_code=201)
async def transfer_call(
    interaction_id: uuid.UUID,
    body: TransferIn,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(hxguard.guard("engage.interactions.write")),
) -> dict[str, Any]:
    """Warm transfer. The receiving rep inherits the tier already established."""
    tenant_uuid, token = await _tenant(session, user)
    await _gate(session, token)
    row = await session.get(CustomerInteractionModel, interaction_id)
    if row is None or row.tenant_id != tenant_uuid:
        raise HTTPException(404, "VEL-ENGAGE-404: no such interaction")
    new = await interactions.transfer(session, tenant_uuid=tenant_uuid,
                                      interaction=row, to_agent=body.to_agent,
                                      queue=body.queue)
    await session.commit()
    return {"interaction_id": str(new.id),
            "verification_level": new.verification_level,
            "transferred_from": str(row.id),
            "note": ("The tier carried over. The customer is not re-verified.")}


@router.post("/interactions/{interaction_id}/wrap-up")
async def wrap_up(
    interaction_id: uuid.UUID,
    body: WrapIn,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(hxguard.guard("engage.interactions.write")),
) -> dict[str, Any]:
    tenant_uuid, token = await _tenant(session, user)
    await _gate(session, token)
    row = await session.get(CustomerInteractionModel, interaction_id)
    if row is None or row.tenant_id != tenant_uuid:
        raise HTTPException(404, "VEL-ENGAGE-404: no such interaction")
    # P5: a tenant that has named its wrap-up codes gets them enforced. One that
    # has not keeps free text, exactly as before.
    try:
        insights.check_disposition(await session.get(TenantModel, tenant_uuid),
                                   body.disposition_code)
    except identity.IdentityError as exc:
        raise HTTPException(422, f"VEL-ENGAGE-422: {exc}") from exc
    await interactions.wrap_up(session, interaction=row,
                               disposition_code=body.disposition_code,
                               notes=body.notes, outcome=body.outcome)
    await session.commit()
    return {"interaction_id": str(row.id), "status": row.status}


@router.post("/profiles/{customer_id}/on-behalf")
async def on_behalf(
    customer_id: uuid.UUID,
    body: OnBehalfIn,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(hxguard.guard("engage.actions.on_behalf")),
) -> dict[str, Any]:
    """What a case raised on this call must carry — and whether consent allows it.

    This returns the attribution and the consent answer rather than creating the
    case: creation goes through the ordinary case API with these fields, so a
    case raised by a rep is the same object, made the same way, with more known
    about it. A second create path is how two kinds of case start diverging.
    """
    tenant_uuid, token = await _tenant(session, user)
    await _gate(session, token)
    profile = await _profile(session, customer_id, tenant_uuid)
    # The attribution stamps this call's tier on the case; it must be this
    # customer's call.
    row = await _call_with(session, body.interaction_id, tenant_uuid, profile)

    attribution = await interactions.on_behalf_attribution(
        session, profile=profile, interaction=row,
        rep_user_id=getattr(user, "user_id", None) or "unknown")
    consent = await interactions.consent_gate(
        session, tenant_token=token, profile=profile, purpose=body.purpose)
    return {"attribution": attribution, "consent": consent}


# ── Oct item 2: the action rail (2026-09-29) ─────────────────────────────────


class ConsentIn(_Strict):
    interaction_id: uuid.UUID
    purpose: str = Field(max_length=32)
    action: str = Field(max_length=16)
    subject_kind: str = Field(default="email", max_length=8)


@router.get("/profiles/{customer_id}/action-rail")
async def get_action_rail(
    customer_id: uuid.UUID,
    interaction_id: uuid.UUID = Query(...),
    rail: str | None = Query(None, max_length=64, pattern="^[a-z0-9][a-z0-9-]{1,62}$"),
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(hxguard.guard("engage.interactions.read")),
) -> dict[str, Any]:
    """Every rail action, and whether THIS rep may do it now on THIS call.

    Read-only. Each action is performed through the API it already lives on,
    which checks again; this is what lets a disabled button say why.
    """
    tenant_uuid, token = await _tenant(session, user)
    await _gate(session, token)
    profile = await _profile(session, customer_id, tenant_uuid)
    # The tier the rail judges by is this call's, so it must be this customer's.
    row = await _call_with(session, interaction_id, tenant_uuid, profile)
    return await action_rail.rail(session, user=user, tenant_uuid=tenant_uuid,
                                  tenant_token=token, profile=profile, interaction=row,
                                  rail_key=rail)


@router.post("/profiles/{customer_id}/consent", status_code=201)
async def capture_consent(
    customer_id: uuid.UUID,
    body: ConsentIn,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(hxguard.guard("engage.actions.consent")),
) -> dict[str, Any]:
    """Record the customer's own answer, given on this call.

    Its own route rather than `POST /compliance/consent`: that one takes any
    subject string from its caller, while this one takes the subject from the
    profile and binds the record to the interaction, the rep and the tier. A rep
    may record what the person on a verified call said about themselves, and
    nothing else.
    """
    tenant_uuid, token = await _tenant(session, user)
    await _gate(session, token)
    profile = await _profile(session, customer_id, tenant_uuid)
    row = await _call_with(session, body.interaction_id, tenant_uuid, profile)
    try:
        out = await action_rail.capture_consent(
            session, tenant_token=token, profile=profile, interaction=row,
            purpose=body.purpose, action=body.action, subject_kind=body.subject_kind,
            actor=getattr(user, "user_id", None) or "unknown")
    except PermissionError as exc:
        raise HTTPException(409, f"VEL-ENGAGE-TIER-409: {exc}") from exc
    except ValueError as exc:
        raise HTTPException(422, f"VEL-ENGAGE-422: {exc}") from exc
    await session.commit()
    return out


# ── Oct item 1: a join link for the next rep (2026-09-29) ────────────────────


class JoinLinkIn(_Strict):
    mode: str = Field(default="warm", max_length=8)
    for_user_id: str | None = Field(default=None, max_length=255)
    for_queue: str | None = Field(default=None, max_length=128)
    ttl_minutes: int | None = Field(default=None, ge=1, le=joins.MAX_TTL_MINUTES)


class RedeemIn(_Strict):
    token: str = Field(min_length=16, max_length=128)


@router.post("/interactions/{interaction_id}/join-links", status_code=201,
             dependencies=[Depends(_join_create_rl)])
async def create_join_link(
    interaction_id: uuid.UUID,
    body: JoinLinkIn,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(hxguard.guard("engage.interactions.write")),
) -> dict[str, Any]:
    """A link for a named rep, or a queue, to join this live contact.

    The token is in THIS response only; it is stored as a hash.
    """
    tenant_uuid, token = await _tenant(session, user)
    await _gate(session, token)
    row = await session.get(CustomerInteractionModel, interaction_id)
    if row is None or row.tenant_id != tenant_uuid:
        raise HTTPException(404, "VEL-ENGAGE-404: no such interaction")
    try:
        link, secret = await joins.create(
            session, tenant_uuid=tenant_uuid, tenant_token=token, interaction=row,
            actor=user, mode=body.mode, for_user_id=body.for_user_id,
            for_queue=body.for_queue, ttl_minutes=body.ttl_minutes)
    except joins.JoinError as exc:
        raise HTTPException(exc.status, f"VEL-ENGAGE-JOIN-{exc.status}: {exc}") from exc
    await session.commit()
    return {**joins.link_json(link), "token": secret,
            # A FRAGMENT, not a query string: a fragment never leaves the
            # browser, so the credential is not written to access logs.
            "path": f"/rep-desk#join={secret}",
            "note": "Share it now: it is shown once, works once, and expires."}


@router.get("/interactions/{interaction_id}/join-links")
async def list_join_links(
    interaction_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(hxguard.guard("engage.interactions.read")),
) -> dict[str, Any]:
    tenant_uuid, token = await _tenant(session, user)
    await _gate(session, token)
    row = await session.get(CustomerInteractionModel, interaction_id)
    if row is None or row.tenant_id != tenant_uuid:
        raise HTTPException(404, "VEL-ENGAGE-404: no such interaction")
    from velaris_shared.db.models import CustomerInteractionJoinModel

    rows = (await session.execute(select(CustomerInteractionJoinModel).where(
        CustomerInteractionJoinModel.interaction_id == row.id,
        CustomerInteractionJoinModel.tenant_id == tenant_uuid,
    ).order_by(CustomerInteractionJoinModel.created_at.desc()).limit(50))).scalars().all()
    return {"links": [joins.link_json(r) for r in rows]}


@router.delete("/join-links/{join_id}")
async def revoke_join_link(
    join_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(hxguard.guard("engage.interactions.write")),
) -> dict[str, Any]:
    tenant_uuid, token = await _tenant(session, user)
    await _gate(session, token)
    from velaris_shared.db.models import CustomerInteractionJoinModel

    row = await session.get(CustomerInteractionJoinModel, join_id)
    if row is None or row.tenant_id != tenant_uuid:
        raise HTTPException(404, "VEL-ENGAGE-404: no such join link")
    try:
        await joins.revoke(session, row=row, actor=user)
    except joins.JoinError as exc:
        raise HTTPException(exc.status, f"VEL-ENGAGE-JOIN-{exc.status}: {exc}") from exc
    await session.commit()
    return joins.link_json(row)


@router.post("/join-links/redeem", dependencies=[Depends(_join_redeem_rl)])
async def redeem_join_link(
    body: RedeemIn,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(hxguard.guard("engage.interactions.write")),
) -> dict[str, Any]:
    """Open the contact for the rep the link names. Screen-pop shaped, so the
    rep desk renders it exactly as it renders a call it took itself."""
    tenant_uuid, token = await _tenant(session, user)
    await _gate(session, token)
    try:
        link, origin, joined = await joins.redeem(
            session, tenant_uuid=tenant_uuid, token=body.token, actor=user)
    except joins.JoinError as exc:
        await session.rollback()
        raise HTTPException(exc.status, f"VEL-ENGAGE-JOIN-{exc.status}: {exc}") from exc
    await session.commit()
    return {
        "interaction_id": str(joined.id),
        "customer_id": str(joined.customer_id) if joined.customer_id else None,
        "verification_level": joined.verification_level,
        "resolution": {"resolved": joined.customer_id is not None, "rung": 0,
                       "reason": "joined from a colleague's contact",
                       "confidence": "join link",
                       "verified": joined.verification_level == "full"},
        "stub": None if joined.customer_id else {
            "actions": [], "prefill": {},
            "note": "The contact you joined is not linked to a customer yet."},
        "inherited_verification": True,
        "joined_from": str(origin.id),
        "mode": link.mode,
    }


# ── Oct item 5: industry action rails (2026-09-29) ───────────────────────────


class RailIn(_Strict):
    config: dict[str, Any]
    enabled: bool = True


@router.get("/rails")
async def list_rails(
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(hxguard.guard("engage.interactions.read")),
) -> dict[str, Any]:
    """This tenant's rails — from packs and its own. A rep sees them to pick one."""
    tenant_uuid, token = await _tenant(session, user)
    await _gate(session, token)
    from velaris_shared.db.models import EngageRailConfigModel as R
    from case_service.engage import rail_config

    rows = (await session.execute(select(R).where(R.tenant_id == tenant_uuid)
                                  .order_by(R.name))).scalars().all()
    return {"rails": [rail_config.row_json(r) for r in rows]}


@router.put("/rails/{rail_key}")
async def put_rail(
    rail_key: str,
    body: RailIn,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(hxguard.guard("engage.config.write")),
) -> dict[str, Any]:
    """Create a tenant rail, or edit one (a pack rail edited here is kept across
    pack upgrades). The same validation as a pack's."""
    tenant_uuid, token = await _tenant(session, user)
    await _gate(session, token)
    from datetime import datetime, timezone

    from velaris_shared.db.models import EngageRailConfigModel as R
    from case_service.engage import rail_config

    try:
        cfg = rail_config.validate({**body.config, "key": rail_key})
    except rail_config.RailError as exc:
        raise HTTPException(422, f"VEL-ENGAGE-RAIL-422: {exc}") from exc
    now = datetime.now(timezone.utc)
    who = getattr(user, "user_id", None) or "unknown"
    row = (await session.execute(select(R).where(
        R.tenant_id == tenant_uuid, R.rail_key == rail_key))).scalars().first()
    if row is None:
        row = R(id=uuid.uuid4(), tenant_id=tenant_uuid, rail_key=rail_key, name=cfg["name"],
                industry=cfg["industry"], source="tenant", config=cfg, enabled=body.enabled,
                created_at=now, updated_at=now, edited_at=now, edited_by=who)
        session.add(row)
    else:
        row.name, row.industry, row.config, row.enabled = (
            cfg["name"], cfg["industry"], cfg, body.enabled)
        row.updated_at, row.edited_at, row.edited_by = now, now, who
    await session.commit()
    return rail_config.row_json(row)


class RailToggleIn(_Strict):
    enabled: bool


@router.patch("/rails/{rail_key}")
async def toggle_rail(
    rail_key: str,
    body: RailToggleIn,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(hxguard.guard("engage.config.write")),
) -> dict[str, Any]:
    """Switch a rail on or off WITHOUT marking it edited — a pack rail that is
    merely switched off still takes its pack's upgrades."""
    tenant_uuid, token = await _tenant(session, user)
    await _gate(session, token)
    from datetime import datetime, timezone

    from velaris_shared.db.models import EngageRailConfigModel as R
    from case_service.engage import rail_config

    row = (await session.execute(select(R).where(
        R.tenant_id == tenant_uuid, R.rail_key == rail_key))).scalars().first()
    if row is None:
        raise HTTPException(404, "VEL-ENGAGE-404: no such rail")
    row.enabled, row.updated_at = body.enabled, datetime.now(timezone.utc)
    await session.commit()
    return rail_config.row_json(row)


@router.delete("/rails/{rail_key}")
async def delete_rail(
    rail_key: str,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(hxguard.guard("engage.config.write")),
) -> dict[str, Any]:
    """A tenant's own rail is deleted; a pack's is DISABLED (deleting it would let
    the next pack upgrade quietly bring it back)."""
    tenant_uuid, token = await _tenant(session, user)
    await _gate(session, token)
    from datetime import datetime, timezone

    from velaris_shared.db.models import EngageRailConfigModel as R
    from case_service.engage import rail_config

    row = (await session.execute(select(R).where(
        R.tenant_id == tenant_uuid, R.rail_key == rail_key))).scalars().first()
    if row is None:
        raise HTTPException(404, "VEL-ENGAGE-404: no such rail")
    if row.source == "pack":
        row.enabled, row.updated_at = False, datetime.now(timezone.utc)
        await session.commit()
        return {"disabled": True, "rail": rail_config.row_json(row)}
    await session.delete(row)
    await session.commit()
    return {"deleted": True, "key": rail_key}


# ── Oct item 3: profile backfill that PROPOSES (2026-09-29) ──────────────────


class EraseIn(_Strict):
    reason: str = Field(min_length=5, max_length=2000)


@router.post("/profiles/{customer_id}/erase")
async def erase_profile(
    customer_id: uuid.UUID,
    body: EraseIn,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(hxguard.guard("engage.profile.erase")),
) -> dict[str, Any]:
    """Erase one customer from the rep desk (a person with no portal account has
    no other door). Irreversible; the consent ledger is left as the legal record."""
    tenant_uuid, token = await _tenant(session, user)
    await _gate(session, token)
    row = await session.get(CustomerProfileModel, customer_id)
    if row is None or row.tenant_id != tenant_uuid:
        raise HTTPException(404, "VEL-ENGAGE-404: no such customer")
    out = await erasure.forget(session, tenant_uuid=tenant_uuid, profile_ids=[row.id],
                               reason=f"rep desk erasure by {getattr(user, 'user_id', '?')}: "
                                      f"{body.reason.strip()}")
    await session.commit()
    return out


class DecideIn(_Strict):
    reason: str | None = Field(default=None, max_length=2000)


@router.post("/backfill/runs", status_code=201, dependencies=[Depends(_backfill_rl)])
async def start_backfill(
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(hxguard.guard("engage.backfill.run")),
) -> dict[str, Any]:
    """Scan this tenant's history and write proposals. Applies nothing."""
    tenant_uuid, token = await _tenant(session, user)
    await _gate(session, token)
    run = await backfill.run_scan(session, tenant_uuid=tenant_uuid, tenant_token=token,
                                  started_by=getattr(user, "user_id", None) or "unknown")
    await session.commit()
    return backfill.run_json(run)


@router.get("/backfill/runs")
async def list_backfill_runs(
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(hxguard.guard("engage.backfill.decide")),
) -> dict[str, Any]:
    tenant_uuid, token = await _tenant(session, user)
    await _gate(session, token)
    from velaris_shared.db.models import CustomerBackfillRunModel

    rows = (await session.execute(select(CustomerBackfillRunModel).where(
        CustomerBackfillRunModel.tenant_id == tenant_uuid)
        .order_by(CustomerBackfillRunModel.started_at.desc()).limit(20))).scalars().all()
    return {"runs": [backfill.run_json(r) for r in rows]}


@router.get("/backfill/proposals")
async def list_backfill_proposals(
    status: str = Query("proposed", pattern="^(proposed|accepted|rejected|superseded)$"),
    kind: str | None = Query(None, pattern="^(create_profile|enrich_profile|merge_profiles)$"),
    limit: int = Query(50, ge=1, le=backfill.MAX_PAGE),
    offset: int = Query(0, ge=0),
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(hxguard.guard("engage.backfill.decide")),
) -> dict[str, Any]:
    tenant_uuid, token = await _tenant(session, user)
    await _gate(session, token)
    from sqlalchemy import func as _f

    from velaris_shared.db.models import CustomerBackfillProposalModel as P

    q = select(P).where(P.tenant_id == tenant_uuid, P.status == status)
    c = select(_f.count()).select_from(P).where(P.tenant_id == tenant_uuid, P.status == status)
    if kind:
        q, c = q.where(P.kind == kind), c.where(P.kind == kind)
    rows = (await session.execute(q.order_by(P.kind, P.updated_at.desc())
                                  .offset(offset).limit(limit))).scalars().all()
    return {"total": (await session.execute(c)).scalar_one(),
            "proposals": [backfill.proposal_json(r) for r in rows]}


async def _proposal(session, proposal_id, tenant_uuid):
    from velaris_shared.db.models import CustomerBackfillProposalModel

    row = await session.get(CustomerBackfillProposalModel, proposal_id)
    if row is None or row.tenant_id != tenant_uuid:
        raise HTTPException(404, "VEL-ENGAGE-404: no such proposal")
    return row


@router.post("/backfill/proposals/{proposal_id}/accept")
async def accept_backfill_proposal(
    proposal_id: uuid.UUID,
    body: DecideIn,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(hxguard.guard("engage.backfill.decide")),
) -> dict[str, Any]:
    """Apply one proposal, re-checked against the data as it is NOW."""
    tenant_uuid, token = await _tenant(session, user)
    await _gate(session, token)
    row = await _proposal(session, proposal_id, tenant_uuid)
    try:
        result = await backfill.accept(session, tenant_uuid=tenant_uuid, row=row,
                                       actor=user, reason=body.reason)
    except backfill.BackfillError as exc:
        await session.rollback()
        if exc.status == 409 and "Re-run" in str(exc):
            row = await _proposal(session, proposal_id, tenant_uuid)
            row.status, row.reason = "superseded", str(exc)
            row.payload, row.evidence = {}, {}
            await session.commit()
        raise HTTPException(exc.status, f"VEL-ENGAGE-BACKFILL-{exc.status}: {exc}") from exc
    await session.commit()
    return {"proposal": backfill.proposal_json(row), "result": result}


@router.post("/backfill/proposals/{proposal_id}/reject")
async def reject_backfill_proposal(
    proposal_id: uuid.UUID,
    body: DecideIn,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(hxguard.guard("engage.backfill.decide")),
) -> dict[str, Any]:
    tenant_uuid, token = await _tenant(session, user)
    await _gate(session, token)
    row = await _proposal(session, proposal_id, tenant_uuid)
    try:
        await backfill.reject(session, row=row, actor=user, reason=body.reason or "")
    except backfill.BackfillError as exc:
        raise HTTPException(exc.status, f"VEL-ENGAGE-BACKFILL-{exc.status}: {exc}") from exc
    await session.commit()
    return {"proposal": backfill.proposal_json(row)}


# ── P5: analytics and wrap-up codes (2026-09-17) ─────────────────────────────


class CodesIn(_Strict):
    #: null clears the list, which returns wrap-up to free text.
    codes: list[dict[str, str]] | None = Field(default=None, max_length=insights.MAX_CODES)


@router.get("/analytics")
async def engage_analytics(
    days: int = Query(30, ge=1, le=366),
    repeat_window_days: int = Query(7, ge=1, le=90),
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(hxguard.guard("engage.analytics.read")),
) -> dict[str, Any]:
    """AHT, wrap-up mix, repeat contact, FCR — each with its definition."""
    tenant_uuid, token = await _tenant(session, user)
    await _gate(session, token)
    return await insights.analytics(session, tenant_uuid=tenant_uuid, days=days,
                                    repeat_window_days=repeat_window_days)


@router.get("/config/disposition-codes")
async def get_disposition_codes(
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(hxguard.guard("engage.interactions.read")),
) -> dict[str, Any]:
    tenant_uuid, token = await _tenant(session, user)
    await _gate(session, token)
    codes = insights.disposition_codes(await session.get(TenantModel, tenant_uuid))
    return {"codes": codes, "free_text": codes is None}


@router.put("/config/disposition-codes")
async def put_disposition_codes(
    body: CodesIn,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(hxguard.guard("engage.config.write")),
) -> dict[str, Any]:
    tenant_uuid, token = await _tenant(session, user)
    await _gate(session, token)
    tenant = await session.get(TenantModel, tenant_uuid)
    if tenant is None:
        raise HTTPException(404, "VEL-ENGAGE-404: no such tenant")
    settings = dict(tenant.settings or {})
    engage_cfg = dict(settings.get("engage") or {})
    if body.codes is None:
        engage_cfg.pop("disposition_codes", None)
    else:
        try:
            engage_cfg["disposition_codes"] = insights.validate_codes(body.codes)
        except identity.IdentityError as exc:
            raise HTTPException(422, f"VEL-ENGAGE-422: {exc}") from exc
    settings["engage"] = engage_cfg
    # Reassigned, not mutated: a JSON column does not see an in-place change.
    tenant.settings = settings
    await session.commit()
    codes = insights.disposition_codes(tenant)
    return {"codes": codes, "free_text": codes is None}


# ── P6: next-best-action and grounded recap (2026-09-17) ─────────────────────


class FeedbackIn(_Strict):
    action_key: str = Field(max_length=64)
    verdict: str = Field(max_length=16)
    reason: str | None = Field(default=None, max_length=2000)
    interaction_id: uuid.UUID | None = None


async def _call_context(session: AsyncSession, user: AuthenticatedUser,
                        customer_id: uuid.UUID, interaction_id: uuid.UUID | None):
    """Tenant, profile, the call (if named) and the reader's own grants — the
    same inputs the 360 is composed from, so a suggestion or a recap can never
    see more than the 360 beside it."""
    tenant_uuid, token = await _tenant(session, user)
    await _gate(session, token)
    profile = await _profile(session, customer_id, tenant_uuid)
    row = None
    if interaction_id is not None:
        row = await _call_with(session, interaction_id, tenant_uuid, profile)
    subject = hxguard.subject_from_user(user)
    allowed = {
        action: (await hxguard.check(session, subject, action)).allow
        for action in sorted(set(three_sixty.PANEL_ACTIONS.values()))
    }
    return tenant_uuid, token, profile, row, allowed


@router.get("/profiles/{customer_id}/next-actions")
async def get_next_actions(
    customer_id: uuid.UUID,
    interaction_id: uuid.UUID | None = Query(None),
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(hxguard.guard("engage.profile.read")),
) -> dict[str, Any]:
    """Suggestions from named rules, each with why and the rows it read."""
    tenant_uuid, _, profile, row, allowed = await _call_context(
        session, user, customer_id, interaction_id)
    actions = await insights.next_actions(
        session, user=user, tenant_uuid=tenant_uuid, profile=profile,
        interaction=row, allowed=allowed)
    return {"customer_id": str(profile.id),
            "verification": row.verification_level if row is not None else "none",
            "actions": actions}


@router.post("/profiles/{customer_id}/next-actions/feedback", status_code=201)
async def next_action_feedback(
    customer_id: uuid.UUID,
    body: FeedbackIn,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(hxguard.guard("engage.interactions.write")),
) -> dict[str, Any]:
    """A rep agreeing or disagreeing with a suggestion, on the record."""
    tenant_uuid, token = await _tenant(session, user)
    await _gate(session, token)
    profile = await _profile(session, customer_id, tenant_uuid)
    if body.interaction_id is not None:
        await _call_with(session, body.interaction_id, tenant_uuid, profile)
    try:
        fb = await insights.record_feedback(
            session, tenant_uuid=tenant_uuid, profile=profile,
            interaction_id=body.interaction_id, action_key=body.action_key,
            verdict=body.verdict, reason=body.reason,
            decided_by=str(getattr(user, "user_id", None) or "unknown"))
    except identity.IdentityError as exc:
        raise HTTPException(422, f"VEL-ENGAGE-422: {exc}") from exc
    await session.commit()
    return {"id": str(fb.id), "action_key": fb.action_key, "verdict": fb.verdict}


@router.post("/profiles/{customer_id}/recap", dependencies=[Depends(_recap_rl)])
async def customer_recap(
    customer_id: uuid.UUID,
    interaction_id: uuid.UUID = Query(...),
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(hxguard.guard("engage.profile.read")),
) -> dict[str, Any]:
    """An AI recap of this customer's history, every point citing its rows.

    Needs a call at Partial or above — the tier comes from the interaction, as
    for the 360, so a caller cannot name their own. POST because it spends a
    model call and writes a ledger row.
    """
    tenant_uuid, token, profile, row, allowed = await _call_context(
        session, user, customer_id, interaction_id)
    if three_sixty.rank(row.verification_level) < three_sixty.rank("partial"):
        raise HTTPException(
            409, "VEL-ENGAGE-RECAP-409: verify the caller first — a recap below "
                 "partial verification would disclose what the 360 withholds")
    try:
        out = await insights.recap(
            session, user=user, tenant_uuid=tenant_uuid, tenant_token=token,
            profile=profile, tier=row.verification_level, allowed=allowed)
    except PermissionError as exc:
        raise HTTPException(403, f"VEL-ENGAGE-RECAP-403: {exc}") from exc
    except RuntimeError as exc:
        raise HTTPException(503, f"VEL-ENGAGE-RECAP-503: {exc}") from exc
    await session.commit()
    return out
