"""The rep desk's action rail — Oct item 2.

A rep on a call should not switch modules to act for the person they are
talking to. Every action here already exists elsewhere in Velaris; this module
does not re-implement any of them. It answers one question per action, for this
rep, on this call, with this customer: **may it be done now, and if not, why?**
The rep's screen then calls the existing API, which enforces its own rules
again — the rail is advice drawn from the same facts, never a second gate that
could disagree with the first by being more generous.

Three rules, from the item:

* **Consent first, and a disabled action says why.** A rep must not promise a
  channel the customer opted out of. The ledger is read with the semantics each
  purpose actually has: SMS opt-out is *silence is permission, an explicit
  withdrawal suppresses* (`channels/optout.py`), while a gated purpose is
  *no record means no*. Using one rule for both is either an outage or a
  breach — see `optout.py`'s docstring for the day it nearly was the first.
* **The rep's own privilege.** Each action names the HxGuard action its API
  enforces, and the rail asks the same question of the same subject.
* **The call's verification tier.** Taking money, or recording a person's
  consent, from somebody who has not been proven to be them is exactly what the
  tier exists to stop.

The reasons are separate fields (`blocked_by`) rather than one sentence,
because the fixes differ: *verify the caller*, *ask your administrator*, and
*they opted out* are three different things for a rep to do next.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from velaris_shared import hxguard
from velaris_shared.auth.models import AuthenticatedUser
from velaris_shared.db.models import (
    CaseInstanceModel,
    CaseTypeModel,
    ChannelConversationModel,
    ChannelEndpointModel,
    ConnectorRegistryModel,
    CustomerIdentifierModel,
    CustomerInteractionModel,
    CustomerLinkModel,
    CustomerProfileModel,
    TenantModel,
)

from case_service.channels import optout
from case_service.compliance import consent
from case_service.engage.three_sixty import rank

log = logging.getLogger(__name__)

#: A case in one of these is finished; the rail offers nothing on it.
TERMINAL = ("resolved", "closed", "cancelled")

#: The most cases the rail lists. A customer with more has a history the rep
#: opens in the case list, not a picker.
MAX_CASES = 20

#: Purposes a rep may capture or withdraw on a call. `case_processing` is absent
#: on purpose: it rests on contract, not consent, so there is nothing for the
#: customer to grant — and a rep "withdrawing" it would record that processing
#: stopped when it lawfully continues. `biometric` is absent because it needs
#: EXPLICIT consent, which HxMeet takes at the participant row with its own
#: record; a rep ticking it on somebody's behalf is not that.
CAPTURABLE = ("marketing", "recording", "ai_processing", "data_sharing")


@dataclass(frozen=True)
class Spec:
    key: str
    label: str
    #: The HxGuard action the underlying API enforces. None when the API decides
    #: per case through ReBAC (HxMeet), which a platform check cannot predict.
    privilege: str | None
    min_tier: str
    needs_case: bool
    rides_on: str


SPECS: tuple[Spec, ...] = (
    Spec("reply", "Reply on their channel", "channels.conversations.write",
         "partial", True, "POST /channels/conversations/{id}/reply"),
    Spec("document_request", "Send a document request link",
         "channels.conversations.write", "partial", True,
         "POST /channels/conversations/{id}/reply (portal link)"),
    Spec("payment", "Send a payment link", "payments.requests.write",
         "full", True, "POST /payments/cases/{id}/charge"),
    Spec("callback", "Book a callback", "calendar.events.write",
         "none", False, "POST /calendar/events"),
    Spec("live_session", "Start a live session / KYC check", None,
         "partial", True, "POST /meet/cases/{id}/sessions"),
    Spec("consent", "Capture or withdraw consent", "engage.actions.consent",
         "full", False, "POST /engage/profiles/{id}/consent"),
    Spec("follow_up", "Set a follow-up", "case.actions.note",
         "partial", True, "POST /cases/{id}/actions/add_note (+ calendar)"),
)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


async def _identifiers(session: AsyncSession,
                       profile: CustomerProfileModel) -> dict[str, CustomerIdentifierModel]:
    """The primary identifier per kind (primary first, then oldest)."""
    rows = (await session.execute(
        select(CustomerIdentifierModel)
        .where(CustomerIdentifierModel.customer_id == profile.id)
        .order_by(CustomerIdentifierModel.is_primary.desc(),
                  CustomerIdentifierModel.created_at))).scalars().all()
    out: dict[str, CustomerIdentifierModel] = {}
    for row in rows:
        out.setdefault(row.kind, row)
    return out


async def _cases(session: AsyncSession, user: AuthenticatedUser, *,
                 tenant_uuid: uuid.UUID,
                 profile: CustomerProfileModel) -> list[CaseInstanceModel]:
    """This customer's open cases that the rep may read.

    A case the rep may not read is left out rather than listed and refused: its
    case number alone tells a rep something about a person they were not
    permitted to learn. Read on the rep's authority through `require_case`, the
    same gate the case API uses, so the list and the case screen cannot differ.
    """
    ids = list((await session.execute(
        select(CustomerLinkModel.object_id).where(
            CustomerLinkModel.customer_id == profile.id,
            CustomerLinkModel.object_kind == "case",
        ).order_by(CustomerLinkModel.linked_at.desc()).limit(MAX_CASES * 3)
    )).scalars().all())
    if not ids:
        return []
    rows = (await session.execute(
        select(CaseInstanceModel).where(
            CaseInstanceModel.id.in_(ids),
            CaseInstanceModel.tenant_id == tenant_uuid,
            CaseInstanceModel.status.notin_(TERMINAL),
        ).order_by(CaseInstanceModel.created_at.desc()))).scalars().all()
    out = []
    for case in rows:
        try:
            await hxguard.require_case(session, user, "case.read", case.id)
        except Exception:                                        # noqa: BLE001
            continue
        out.append(case)
        if len(out) >= MAX_CASES:
            break
    return out


def _payment_steps(case_type: CaseTypeModel | None) -> list[dict[str, str]]:
    steps: list[dict[str, str]] = []
    for stage in ((case_type.definition_json or {}).get("stages") or []) if case_type else []:
        for step in stage.get("steps") or []:
            if step.get("step_type") in ("payment_request", "payment"):
                steps.append({"id": str(step.get("id")),
                              "name": str(step.get("name") or step.get("id"))})
    return steps


async def _conversations(session: AsyncSession, *, cases: list[CaseInstanceModel],
                         now: datetime) -> dict[str, list[dict[str, Any]]]:
    if not cases:
        return {}
    rows = (await session.execute(
        select(ChannelConversationModel, ChannelEndpointModel)
        .join(ChannelEndpointModel,
              ChannelEndpointModel.id == ChannelConversationModel.endpoint_id)
        .where(ChannelConversationModel.case_id.in_([c.id for c in cases]),
               ChannelConversationModel.status == "open")
        .order_by(ChannelConversationModel.last_message_at.desc()))).all()
    out: dict[str, list[dict[str, Any]]] = {}
    for conv, endpoint in rows:
        # Opt-out is read with the SEND path's own rule, so the rail cannot
        # offer a reply the send would refuse — or refuse one it would allow.
        allowed = await optout.may_send(session, conv.peer_address,
                                        tenant_id=endpoint.tenant_id)
        expires = _aware(conv.window_expires_at)
        window_closed = endpoint.channel == "whatsapp" and not (expires and now < expires)
        out.setdefault(str(conv.case_id), []).append({
            "id": str(conv.id),
            "channel": endpoint.channel,
            # Enough to tell two conversations apart; the full address is not
            # the rail's to show.
            "peer_hint": "…" + conv.peer_address[-3:] if conv.peer_address else None,
            "opted_out": not allowed,
            "template_only": window_closed,
        })
    return out


async def _stripe_ready(session: AsyncSession, tenant_token: str,
                        tenant_uuid: uuid.UUID) -> bool:
    """Whether the charge route would find a connector without being told one.

    The charge service matches `tenant_id` exactly, so the same spellings are
    asked here — a rail that said "ready" to a charge that then 400s is the
    worse failure.
    """
    row = (await session.execute(
        select(ConnectorRegistryModel.id).where(
            ConnectorRegistryModel.connector_type == "stripe",
            ConnectorRegistryModel.enabled == True,             # noqa: E712
            ConnectorRegistryModel.tenant_id.in_([tenant_token, str(tenant_uuid)]),
        ).limit(1))).scalar_one_or_none()
    return row is not None


async def _portal_link(session: AsyncSession, tenant_uuid: uuid.UUID) -> str | None:
    """The tenant's customer portal sign-in page, or None when it has none.

    The SIGN-IN page, deliberately — not a tracking link. A tracking token is a
    bearer credential for the case (`compliance/retention.py` says so), and a
    message sent to a channel the rep picked is not the place to put one. The
    customer signs in as themselves and finds the case there.
    """
    from velaris_shared.config import get_settings

    tenant = await session.get(TenantModel, tenant_uuid)
    if tenant is None or not ((tenant.settings or {}).get("portal") or {}).get("enabled"):
        return None
    base = (get_settings().public_base_url or "").rstrip("/")
    return f"{base}/portal/{tenant.slug}/login" if base else f"/portal/{tenant.slug}/login"


async def consent_view(session: AsyncSession, *, tenant_token: str,
                       email: str | None, phone: str | None) -> dict[str, Any]:
    """What the ledger says, per purpose a rep may act on, per subject.

    `state` is one of `granted`, `withdrawn` or `none`. `none` is shown as
    itself — never folded into either of the other two — because what it MEANS
    depends on the purpose, and the rail's reader has to know which.
    """
    out: dict[str, Any] = {"email": None, "phone": None}
    for kind, subject in (("email", email), ("phone", phone)):
        if not subject:
            continue
        try:
            state = await consent.current_state(session, subject, tenant_id=tenant_token)
        except Exception as exc:                                 # noqa: BLE001
            log.info("engage rail: consent unreadable: %s", exc)
            out[kind] = {"readable": False}
            continue
        out[kind] = {
            "readable": True,
            "purposes": {
                p: ("granted" if state[p]["granted"] else "withdrawn") if p in state else "none"
                for p in CAPTURABLE
            },
        }
    return out


def _decide(spec: Spec, *, user: AuthenticatedUser, tier: str,
            min_tier: str | None = None) -> list[dict[str, str]]:
    """The reasons that apply to every case alike: privilege and tier.

    `min_tier` is an industry rail's RAISED tier (Oct item 5); a rail can only
    raise it, so the effective floor is whichever is higher.
    """
    need = spec.min_tier
    if min_tier and rank(min_tier) > rank(need):
        need = min_tier
    blocked: list[dict[str, str]] = []
    if spec.privilege and not hxguard.user_allows(user, spec.privilege):
        blocked.append({"kind": "privilege",
                        "reason": f"Your role does not include {spec.privilege}."})
    if rank(tier) < rank(need):
        blocked.append({"kind": "tier",
                        "reason": ("Verify the caller first — this needs "
                                   f"{need} verification.")})
    return blocked


async def _meet_ready(session: AsyncSession, tenant_token: str) -> str | None:
    """Why HxMeet cannot start a session here, or None when it can.

    Mirrors the start route's own refusals (a 501 either way), so the rail does
    not offer a button whose only outcome is an error.
    """
    from case_service.meet import livekit
    from case_service.meet import service as meet

    try:
        driver = await meet.resolve_driver(session, tenant_token)
    except Exception as exc:                                     # noqa: BLE001
        log.info("engage rail: meet driver unreadable: %s", exc)
        return "Live sessions could not be checked on this platform."
    if driver == "embedded":
        return None if livekit.configured() else "Live sessions are not configured (LiveKit)."
    if driver == "off_platform":
        return None
    return "Live sessions are not switched on for this tenant."


async def rail(
    session: AsyncSession, *, user: AuthenticatedUser, tenant_uuid: uuid.UUID,
    tenant_token: str, profile: CustomerProfileModel,
    interaction: CustomerInteractionModel, rail_key: str | None = None,
) -> dict[str, Any]:
    """The rail for ONE rep on ONE call.

    **It shows nothing the 360 would withhold.** The customer's cases, their
    conversations and their consent record are 360 panels, each behind a tier
    and a reader's grant (`three_sixty.TIER_PANELS` / `PANEL_ACTIONS`). The rail
    applies the same two tests with the same decision path, so a rep on an
    anonymous call is not handed case numbers the 360 refused them a line above.
    """
    from case_service.engage.three_sixty import PANEL_ACTIONS, TIER_PANELS

    now = _utcnow()
    tier = interaction.verification_level or "none"
    subject = hxguard.subject_from_user(user)
    grants = {a: (await hxguard.check(session, subject, a)).allow
              for a in sorted({PANEL_ACTIONS["conversations"], PANEL_ACTIONS["consents"]})}
    # `case.read` is per case (it fails closed without one); `_cases` asks it
    # of each case, so the panel-level grant is not the question here.
    grants[PANEL_ACTIONS["cases"]] = True

    def may(panel: str) -> bool:
        return panel in TIER_PANELS[tier] and grants[PANEL_ACTIONS[panel]]

    def why_not(panel: str) -> dict[str, str]:
        if not grants[PANEL_ACTIONS[panel]]:
            return {"kind": "privilege",
                    "reason": f"Your role cannot read their {panel} ({PANEL_ACTIONS[panel]})."}
        return {"kind": "tier",
                "reason": f"Verify the caller first — their {panel} show from partial verification."}

    idents = await _identifiers(session, profile)
    email = idents["email"].value_normalised if "email" in idents else None
    phone = idents["phone"].value_normalised if "phone" in idents else None

    cases = (await _cases(session, user, tenant_uuid=tenant_uuid, profile=profile)
             if may("cases") else [])
    type_ids = {c.case_type_id for c in cases}
    types = {t.id: t for t in (await session.execute(
        select(CaseTypeModel).where(CaseTypeModel.id.in_(type_ids)))).scalars().all()
    } if type_ids else {}
    convs = await _conversations(session, cases=cases, now=now) if may("conversations") else {}
    stripe = await _stripe_ready(session, tenant_token, tenant_uuid)
    portal_link = await _portal_link(session, tenant_uuid)
    meet_problem = await _meet_ready(session, tenant_token)
    sms_allowed = await optout.may_send(session, phone, tenant_id=tenant_token) if phone else None

    # Oct item 5: the industry rail for this call — named by the rep, or the one
    # that names one of the customer's open case types.
    from case_service.engage import rail_config

    configs = await rail_config.enabled_configs(session, tenant_uuid, tenant_token)
    chosen = rail_config.choose(
        configs, [t.name for t in types.values() if t is not None], rail_key)
    chosen_config = chosen.config if chosen is not None else None

    case_rows = [{
        "id": str(c.id), "case_number": c.case_number, "status": c.status,
        "conversations": convs.get(str(c.id), []),
        "payment_steps": _payment_steps(types.get(c.case_type_id)),
    } for c in cases]

    actions = []
    for spec in SPECS:
        blocked = _decide(spec, user=user, tier=tier,
                          min_tier=rail_config.raised_tier(chosen_config, spec.key))
        notes: list[str] = []
        if spec.needs_case:
            if not may("cases"):
                reason = why_not("cases")
                # One "verify the caller" per action, not two.
                if not any(b["kind"] == reason["kind"] for b in blocked):
                    blocked.append(reason)
            elif not cases:
                blocked.append({"kind": "prerequisite",
                                "reason": "They have no open case you can see. Raise one first."})
        if spec.key in ("reply", "document_request") and cases:
            if not may("conversations"):
                blocked.append(why_not("conversations"))
            else:
                open_convs = [v for c in case_rows for v in c["conversations"]]
                usable = [v for v in open_convs if not v["opted_out"]]
                if not open_convs:
                    blocked.append({"kind": "prerequisite",
                                    "reason": "No open channel conversation on their cases."})
                elif not usable:
                    # The consent reason — the one the item exists for.
                    blocked.append({"kind": "consent",
                                    "reason": ("They opted out of messages on every open "
                                               "conversation. They can text START to opt back in.")})
                elif any(v["template_only"] for v in usable):
                    notes.append("WhatsApp's 24-hour window has closed on at least one "
                                 "conversation — only an approved template will be sent there.")
        if spec.key == "document_request":
            if not portal_link:
                blocked.append({"kind": "prerequisite",
                                "reason": "This tenant has no customer portal switched on to upload to."})
            elif not portal_link.startswith("http"):
                # A relative link in a text message leads nowhere.
                blocked.append({"kind": "prerequisite",
                                "reason": "The platform's public address is not set, so a link "
                                          "would not open on their phone."})
        if spec.key == "payment":
            if not stripe:
                blocked.append({"kind": "prerequisite",
                                "reason": "No payment connector is configured for this tenant."})
            if cases and not any(c["payment_steps"] for c in case_rows):
                blocked.append({"kind": "prerequisite",
                                "reason": "None of their open cases has a payment step."})
        if spec.key == "live_session" and meet_problem:
            blocked.append({"kind": "prerequisite", "reason": meet_problem})
        if spec.key == "callback" and phone and sms_allowed is False:
            # A call is not a message, so this does not block — but a rep booking
            # a callback should know no text reminder can go.
            notes.append("Their number opted out of messages: no text reminder will go.")
        if spec.key == "consent" and not (email or phone):
            blocked.append({"kind": "prerequisite",
                            "reason": "No email or phone on the profile to record consent against."})
        if spec.key == "live_session" and not blocked:
            notes.append("Whether you may start one is decided per case when you do.")
        actions.append({
            "key": spec.key, "label": spec.label,
            "enabled": not blocked,
            "blocked_by": blocked,
            "notes": notes,
            "min_tier": rail_config.raised_tier(chosen_config, spec.key) or spec.min_tier,
            "privilege": spec.privilege,
            "rides_on": spec.rides_on,
        })
    actions = rail_config.apply(actions, chosen_config)

    return {
        "interaction_id": str(interaction.id),
        "customer_id": str(profile.id),
        "verification_level": tier,
        "cases": case_rows,
        # The consent panel's own rule: Full, and the reader may read consent.
        # Which KINDS of subject exist is not the record, so it is always given —
        # the capture form needs it.
        "consent": (await consent_view(session, tenant_token=tenant_token,
                                       email=email, phone=phone)
                    if may("consents") else
                    {"email": {"readable": False} if email else None,
                     "phone": {"readable": False} if phone else None}),
        "actions": actions,
        "capturable_purposes": list(CAPTURABLE),
        "portal_link": portal_link,
        "rail": ({"key": chosen.rail_key, "name": chosen.name, "industry": chosen.industry,
                  "guidance": (chosen_config or {}).get("guidance") or ""}
                 if chosen is not None else None),
        "available_rails": [{"key": c.rail_key, "name": c.name, "industry": c.industry}
                            for c in configs],
    }


async def capture_consent(
    session: AsyncSession, *, tenant_token: str, profile: CustomerProfileModel,
    interaction: CustomerInteractionModel, purpose: str, action: str,
    subject_kind: str, actor: str,
) -> dict[str, Any]:
    """Record the customer's answer, given on this call, in the consent ledger.

    Decision 4 of item 12: *capturing is an act with a record, not a checkbox the
    rep ticks.* So the row names the rep, the interaction and the tier the call
    had reached, and it is refused below Full — recording a stranger's "yes" on
    somebody else's behalf is the laundering the decision was written against.
    """
    if purpose not in CAPTURABLE:
        raise ValueError(f"{purpose!r} cannot be captured on a call; one of "
                         f"{', '.join(CAPTURABLE)}")
    if action not in (consent.GRANTED, consent.WITHDRAWN):
        raise ValueError("action is granted or withdrawn")
    tier = interaction.verification_level or "none"
    if rank(tier) < rank("full"):
        raise PermissionError("Consent is recorded only for a caller verified to Full.")
    idents = await _identifiers(session, profile)
    if subject_kind not in ("email", "phone") or subject_kind not in idents:
        raise ValueError(f"the profile has no {subject_kind} to record against")
    subject = idents[subject_kind].value_normalised
    row = await consent.record(
        session, subject_ref=subject, subject_type=subject_kind, purpose=purpose,
        action=action, basis=consent.CONSENT, source="rep_desk",
        tenant_id=tenant_token, actor_id=actor,
        evidence={"interaction_id": str(interaction.id), "channel": interaction.channel,
                  "verification_level": tier, "customer_id": str(profile.id)},
    )
    return {"id": str(row.id), "purpose": row.purpose, "action": row.action,
            "subject_type": row.subject_type, "source": row.source}
