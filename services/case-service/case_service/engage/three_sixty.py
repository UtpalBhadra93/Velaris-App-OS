"""The 360, and the answer to "to what extent?".

Design: docs/Future/Done/CustomerEngagement.md §4–5. The customer's actual question
was *"when someone calls, will the rep see the customer's details, and to what
extent?"* — and the honest answer is a **policy**, not a hardcode.

**Three tiers, and the caller is Anonymous until they are verified** (decision 3,
taken with the user on 2026-09-04):

    Anonymous  resolved by number only — name initial, whether an account
               exists, open case count. No case detail, no addresses, no values.
    Partial    one knowledge factor confirmed — case titles, statuses, SLA,
               conversation subjects. PII masked.
    Full       OTP to a verified identifier, an IdP session, or a document
               challenge — everything the rep's OWN grants allow, unmasked on
               reveal.

**Two rules sit on top, and they are the whole safety of the feature.**

*The tier can only ever NARROW what HxGuard already permits. It is a second gate,
never a grant.* A rep with no `case.read` sees no cases at Full — the 360 must
never become an authorisation bypass, and it would be one the moment a panel
fetched data on the composer's authority rather than the reader's.

*A panel the rep may not read is ABSENT, not empty.* An empty panel says "this
customer has none of these"; an absent one says "this is not yours to see". They
are different facts and a rep will act on the first.

**Reveal-on-reason.** A masked field unmasks one at a time, each with a reason,
each an audit row. Bulk unmask does not exist: a "show everything" button is how
a disclosure tier becomes decoration.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import logging
import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from velaris_shared.db.models import (
    CustomerIdentifierModel,
    CustomerInteractionModel,
    CustomerLinkModel,
    CustomerProfileModel,
)

from case_service.engage import identity

log = logging.getLogger(__name__)

TIERS: tuple[str, ...] = ("none", "partial", "full")

#: Which panels each tier admits at all. A panel absent from a tier is not
#: rendered empty — it is not rendered.
TIER_PANELS: dict[str, tuple[str, ...]] = {
    "none":    ("summary",),
    "partial": ("summary", "cases", "conversations", "interactions"),
    "full":    ("summary", "cases", "conversations", "interactions",
                "identifiers", "relations", "consents"),
}

#: Every panel, and the HxGuard action a reader must hold for it. **The action
#: is the reader's own** — the panel is fetched on their authority, never the
#: composer's.
PANEL_ACTIONS: dict[str, str] = {
    "summary":       "engage.profile.read",
    "cases":         "case.read",
    "conversations": "channels.conversations.read",
    "interactions":  "engage.interactions.read",
    "identifiers":   "engage.profile.read",
    "relations":     "engage.profile.read",
    "consents":      "compliance.consent.read",
}

#: Fields that are masked below Full and unmask ONE AT A TIME with a reason.
MASKABLE: frozenset[str] = frozenset({
    "identifiers.phone", "identifiers.email", "identifiers.national_id_hash",
    "identifiers.account_no", "summary.full_name",
})


def rank(tier: str) -> int:
    if tier not in TIERS:
        raise ValueError(f"a verification tier is one of {', '.join(TIERS)}")
    return TIERS.index(tier)


def mask(kind: str, value: str) -> str:
    """What a rep sees before a reveal. Enough to confirm, never enough to use.

    A masked phone shows the last three digits because that is what a caller
    reads back to confirm; a masked email keeps the domain because that is what
    tells a rep which of two addresses this is.
    """
    raw = str(value or "")
    if not raw:
        return ""
    if kind == "phone":
        return ("•" * max(0, len(raw) - 3)) + raw[-3:]
    if kind == "email":
        name, _, domain = raw.partition("@")
        head = name[:1] if name else ""
        return f"{head}{'•' * max(1, len(name) - 1)}@{domain}"
    if kind == "national_id_hash":
        return "(held, hashed)"
    return ("•" * max(0, len(raw) - 2)) + raw[-2:]


async def compose(
    session: AsyncSession, *, profile: CustomerProfileModel, tier: str,
    allowed: dict[str, bool], revealed: set[str] | None = None,
) -> dict[str, Any]:
    """Build the 360 for ONE reader at ONE tier.

    `allowed` is the reader's own HxGuard answers, computed by the caller and
    passed in — so this function cannot accidentally fetch on the composer's
    authority, and a test can prove the narrowing without a request context.
    """
    revealed = revealed or set()
    tier_rank = rank(tier)
    panels: dict[str, Any] = {}
    withheld: list[dict[str, str]] = []

    for panel in PANEL_ACTIONS:
        by_tier = panel in TIER_PANELS[tier]
        by_grant = bool(allowed.get(PANEL_ACTIONS[panel]))
        if not by_grant:
            # ABSENT, not empty — and the reason distinguishes the two causes,
            # because "you may not see this" and "verify the caller first" send
            # a rep to different actions.
            withheld.append({"panel": panel, "why": "not permitted for this reader"})
            continue
        if not by_tier:
            withheld.append({"panel": panel,
                             "why": f"needs a higher verification tier than {tier!r}"})
            continue
        panels[panel] = await _panel(session, panel, profile, tier_rank, revealed)

    stale = identity.staleness(profile)
    return {
        "customer_id": str(profile.id),
        "party_kind": profile.party_kind,
        "verification": tier,
        "panels": panels,
        # Named, so a rep can see that something exists rather than concluding
        # the customer has none of it.
        "withheld": withheld,
        "source_of_truth": profile.source_of_truth,
        "external_system": profile.external_system,
        # Staleness is SHOWN, never hidden.
        "freshness": stale,
    }


async def _panel(session: AsyncSession, panel: str, profile: CustomerProfileModel,
                 tier_rank: int, revealed: set[str]) -> Any:
    if panel == "summary":
        return await _summary(session, profile, tier_rank, revealed)
    if panel == "identifiers":
        return await _identifiers(session, profile, tier_rank, revealed)
    if panel == "relations":
        return await _relations(session, profile)
    if panel == "interactions":
        return await _interactions(session, profile)
    if panel in ("cases", "conversations"):
        return await _linked(session, profile, "case" if panel == "cases" else "conversation")
    if panel == "consents":
        return {"note": "consent records are read through HxConsent"}
    return None


async def _summary(session, profile, tier_rank, revealed):
    """What Anonymous is allowed to know: that an account exists, and how busy.

    A name INITIAL rather than a name: enough for a rep to say "am I speaking to
    J?", not enough to tell a stranger who the account belongs to.
    """
    cases = len(list((await session.execute(
        select(CustomerLinkModel.id).where(
            CustomerLinkModel.customer_id == profile.id,
            CustomerLinkModel.object_kind == "case"))).scalars().all()))
    full_name = profile.display_name
    show_full = tier_rank >= rank("full") or "summary.full_name" in revealed
    return {
        "account_exists": True,
        "name_initial": (full_name or "?")[:1].upper(),
        "display_name": full_name if show_full else None,
        "display_name_masked": None if show_full else mask("name", full_name),
        "open_cases": cases,
        "status": profile.status,
    }


async def _identifiers(session, profile, tier_rank, revealed):
    rows = list((await session.execute(
        select(CustomerIdentifierModel).where(
            CustomerIdentifierModel.customer_id == profile.id)
        .order_by(CustomerIdentifierModel.kind))).scalars().all())
    out = []
    for row in rows:
        path = f"identifiers.{row.kind}"
        unmasked = tier_rank >= rank("full") and path in revealed
        out.append({
            "kind": row.kind,
            "value": row.value_display if unmasked else mask(row.kind, row.value_display),
            "masked": not unmasked,
            "revealable": path in MASKABLE,
            # An unverified identifier is shown AS unverified. It is how the
            # party was found, not proof of who they are.
            "verified": bool(row.verified),
            "verified_by": row.verified_by,
            "is_primary": bool(row.is_primary),
            "source": row.source,
        })
    return out


async def _relations(session, profile):
    from velaris_shared.db.models import CustomerRelationModel

    rows = list((await session.execute(
        select(CustomerRelationModel).where(
            CustomerRelationModel.from_customer_id == profile.id))).scalars().all())
    return [{"to": str(r.to_customer_id), "relation": r.relation,
             "may_act_for": bool(r.may_act_for), "note": r.note} for r in rows]


async def _interactions(session, profile):
    rows = list((await session.execute(
        select(CustomerInteractionModel)
        .where(CustomerInteractionModel.customer_id == profile.id)
        .order_by(CustomerInteractionModel.started_at.desc()).limit(20)
    )).scalars().all())
    return [{
        "id": str(r.id), "channel": r.channel, "direction": r.direction,
        "started_at": r.started_at.isoformat() if r.started_at else None,
        "verification_level": r.verification_level,
        "disposition_code": r.disposition_code, "status": r.status,
        "outcome": r.outcome,
    } for r in rows]


async def _linked(session, profile, object_kind):
    rows = list((await session.execute(
        select(CustomerLinkModel)
        .where(CustomerLinkModel.customer_id == profile.id,
               CustomerLinkModel.object_kind == object_kind)
        .order_by(CustomerLinkModel.linked_at.desc()).limit(50))).scalars().all())
    return [{"object_id": str(r.object_id), "reason": r.link_reason,
             "linked_at": r.linked_at.isoformat() if r.linked_at else None}
            for r in rows]


async def reveal(
    session: AsyncSession, *, tenant_uuid: uuid.UUID, profile: CustomerProfileModel,
    field_path: str, reason: str, tier: str, actor: str,
    interaction_id: uuid.UUID | None = None,
):
    """Unmask ONE field, with a reason, as a record.

    Refused below Full, and refused for a field that is not maskable — asking to
    reveal something that was never hidden is a caller bug, and answering it
    would teach the caller that reveal is a way to fetch.
    """
    from velaris_shared.db.models import CustomerDisclosureModel

    if field_path not in MASKABLE:
        raise identity.IdentityError(
            f"{field_path!r} is not a masked field; there is nothing to reveal")
    if rank(tier) < rank("full"):
        raise identity.IdentityError(
            "a field is revealed at Full verification only. Verify the caller "
            "first — the tier is what the customer's question was about.")
    if len((reason or "").strip()) < 5:
        raise identity.IdentityError(
            "a reveal needs a reason somebody can read later; it is the only "
            "record of why this was shown")
    row = CustomerDisclosureModel(
        id=uuid.uuid4(), tenant_id=tenant_uuid, customer_id=profile.id,
        interaction_id=interaction_id, field_path=field_path, reason=reason,
        # Frozen. Reading the tier live later would show what the caller
        # eventually reached, not what they had reached when this was shown.
        at_verification=tier, revealed_by=actor)
    session.add(row)
    await session.flush()
    return row
