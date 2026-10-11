"""Erasing a customer from the rep desk's identity spine.

Found building Oct item 3 (2026-09-29): GDPR erasure of a portal account
anonymised `portal_customers` and nothing else the rep desk holds. A profile
named after that person — with their email and phone as identifiers, their
number on every call record, and the backfill's proposals quoting all of it —
survived the erasure intact, and the backfill, which builds profiles FROM
portal accounts, would only have made more of them.

`forget()` is the one routine every erasure path calls. For the profiles that
hold the subject's identifiers (merges followed, both directions):

* the profile is anonymised in place (`status = anonymized`), not deleted — its
  interactions and links stay countable for the floor's analytics, and a
  deleted row would silently turn history into "unknown caller";
* every identifier on it is deleted — they ARE the personal data;
* its interactions lose the caller's number and the rep's free-text notes;
* every backfill proposal that mentions the subject is blanked, and a pending
  one is superseded, so it can never be accepted back into existence.

The consent ledger is deliberately untouched: it is the legal record of what
the person agreed to, and an erasure request does not rewrite that history.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Iterable

from sqlalchemy import delete, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from velaris_shared.db.models import (
    CustomerBackfillProposalModel,
    CustomerIdentifierModel,
    CustomerInteractionModel,
    CustomerProfileModel,
)

from case_service.engage import identity

log = logging.getLogger(__name__)

ERASED_NAME = "Erased customer"


def _norm_all(kind: str, values: Iterable[str | None]) -> set[str]:
    out = set()
    for v in values:
        if not v:
            continue
        try:
            out.add(identity.normalise(kind, v))
        except identity.IdentityError:
            continue
    return out


async def _related_profiles(session: AsyncSession, tenant_uuid: uuid.UUID,
                            seed: set[uuid.UUID]) -> set[uuid.UUID]:
    """The seed profiles, what they were merged into, and what was merged into them."""
    found = set(seed)
    frontier = set(seed)
    for _ in range(8):
        if not frontier:
            break
        rows = (await session.execute(select(CustomerProfileModel).where(
            CustomerProfileModel.tenant_id == tenant_uuid,
            or_(CustomerProfileModel.id.in_(frontier),
                CustomerProfileModel.merged_into_id.in_(frontier))))).scalars().all()
        nxt = set()
        for r in rows:
            for x in (r.id, r.merged_into_id):
                if x and x not in found:
                    nxt.add(x)
        found |= nxt
        frontier = nxt
    return found


async def forget(
    session: AsyncSession, *, tenant_uuid: uuid.UUID, emails: Iterable[str | None] = (),
    phones: Iterable[str | None] = (), portal_customer_ids: Iterable[str | None] = (),
    profile_ids: Iterable[uuid.UUID] = (), reason: str,
) -> dict[str, Any]:
    # A subject with a case under a legal hold is not erased (Oct item 13;
    # GDPR Art. 17(3)(e)). Here, in the one routine every erasure path calls,
    # so a path added later inherits the refusal. Raises `LegalHoldError`.
    from case_service.legal_hold import service as _legal_hold
    emails, phones = list(emails), list(phones)
    portal_customer_ids, profile_ids = list(portal_customer_ids), list(profile_ids)
    await _legal_hold.require_customer_erasable(
        session, tenant=tenant_uuid, emails=emails, phones=phones,
        portal_customer_ids=portal_customer_ids, profile_ids=profile_ids)

    em, ph = _norm_all("email", emails), _norm_all("phone", phones)
    pc = {str(x).lower() for x in portal_customer_ids if x}
    seed: set[uuid.UUID] = set(profile_ids)
    for kind, values in (("email", em), ("phone", ph), ("portal_customer", pc)):
        if values:
            seed |= set((await session.execute(select(CustomerIdentifierModel.customer_id).where(
                CustomerIdentifierModel.tenant_id == tenant_uuid,
                CustomerIdentifierModel.kind == kind,
                CustomerIdentifierModel.value_normalised.in_(sorted(values))))).scalars().all())
    profiles = await _related_profiles(session, tenant_uuid, seed) if seed else set()

    # Everything the profiles were known by, so proposals quoting ANY of it go.
    if profiles:
        for kind, value in (await session.execute(select(
                CustomerIdentifierModel.kind, CustomerIdentifierModel.value_normalised).where(
                CustomerIdentifierModel.customer_id.in_(profiles)))).all():
            {"email": em, "phone": ph, "portal_customer": pc}.get(kind, set()).add(value)
        await session.execute(delete(CustomerIdentifierModel).where(
            CustomerIdentifierModel.customer_id.in_(profiles))
            .execution_options(synchronize_session=False))
        await session.execute(update(CustomerProfileModel).where(
            CustomerProfileModel.id.in_(profiles)).values(
            display_name=ERASED_NAME, status="anonymized", notes=None,
            attribute_source=None).execution_options(synchronize_session=False))
        await session.execute(update(CustomerInteractionModel).where(
            CustomerInteractionModel.customer_id.in_(profiles)).values(
            ani=None, dnis=None, wrap_up_notes=None)
            .execution_options(synchronize_session=False))

    needles = [n for n in (em | ph | pc | {str(p) for p in profiles}) if n]
    blanked = 0
    if needles:
        now = datetime.now(timezone.utc)
        for row in (await session.execute(select(CustomerBackfillProposalModel).where(
                CustomerBackfillProposalModel.tenant_id == tenant_uuid))).scalars().all():
            text = json.dumps([row.payload, row.evidence, row.result], default=str).lower()
            if not any(n.lower() in text for n in needles):
                continue
            row.payload, row.evidence = {}, {}
            row.result = {"erased": True} if row.result else None
            if row.status == "proposed":
                row.status = "superseded"
            row.reason = f"subject erased: {reason}"[:2000]
            row.updated_at = now
            blanked += 1
    await session.flush()
    out = {"profiles_anonymized": len(profiles), "proposals_blanked": blanked}
    log.info("engage erasure (%s): %s", reason, out)
    return out
