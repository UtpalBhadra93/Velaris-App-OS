"""Profile backfill — an operator action that PROPOSES. Oct item 3.

The identity tables start empty on every install, by design, so a rep's first
call about a customer of ten years finds nobody. The history is already here:
portal customers, the emails cases were submitted from, and the numbers that
wrote in on those cases. A scan reads it and writes PROPOSALS; a person accepts
or rejects each one.

The rules, from the design (§11) and the item:

* **Run explicitly by an operator.** Nothing here runs on a timer, on an
  upgrade, or as a side effect of anything else.
* **It never creates or merges on its own.** The scan writes proposals only.
  Accepting one is a person's act, and it is re-checked at that moment — the
  data may have moved since the scan.
* **Never manufacture a person from an unverified inbound number.** A number
  from a chat is used ONLY as extra evidence about somebody already known from
  a stronger source — the submitter of the case that chat was on. A number with
  nothing else behind it proposes nothing, and the run says how many it skipped.
* **Re-runnable.** A proposal's fingerprint is stable: a re-run refreshes what
  is still pending, never re-proposes what an operator rejected, and marks
  superseded what the data no longer supports.

Three kinds of proposal, following what the scan finds for one person:

* `create_profile` — none of their identifiers belongs to a profile yet;
* `enrich_profile` — one profile holds some of them; add the rest, link the cases;
* `merge_profiles` — their identifiers are spread over two or more profiles.
  Merging is the one operation that moves one person's history onto another's
  screen, so accepting it also needs `engage.profile.merge` and a reason.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import hashlib
import json
import logging
import uuid
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from velaris_shared.db.models import (
    CaseInstanceModel,
    ChannelConversationModel,
    CustomerBackfillProposalModel,
    CustomerBackfillRunModel,
    CustomerIdentifierModel,
    CustomerLinkModel,
    CustomerProfileModel,
    PortalCustomerModel,
)

from case_service.engage import identity

log = logging.getLogger(__name__)

KINDS = ("create_profile", "enrich_profile", "merge_profiles")
#: Rows read per source per run. A tenant with more is told the run was
#: truncated; the next run continues from the same data (proposals are
#: idempotent), so nothing is lost, it just takes more than one run.
MAX_SOURCE_ROWS = 20_000
#: Cases linked per proposal. A person with more is linked in later runs.
MAX_CASES_PER_PERSON = 200
#: Proposals listed per page.
MAX_PAGE = 200


class BackfillError(ValueError):
    def __init__(self, message: str, status: int = 422):
        super().__init__(message)
        self.status = status


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _fingerprint(*parts: Any) -> str:
    return hashlib.sha256(json.dumps(parts, sort_keys=True, default=str).encode()).hexdigest()


def _norm(kind: str, value: str | None) -> str | None:
    if not value:
        return None
    try:
        return identity.normalise(kind, value)
    except identity.IdentityError:
        return None


class _Person:
    """Everything the scan learned about one person, keyed by their email."""

    def __init__(self, email: str):
        self.email = email
        self.names: list[str] = []
        #: (kind, normalised) → {"display": str, "verified": bool, "via": str}
        self.identifiers: dict[tuple[str, str], dict[str, Any]] = {}
        self.case_ids: list[str] = []
        self.portal_customer_id: str | None = None

    def add(self, kind: str, normalised: str, display: str, *, verified: bool, via: str):
        cur = self.identifiers.get((kind, normalised))
        if cur is None or (verified and not cur["verified"]):
            self.identifiers[(kind, normalised)] = {
                "display": display, "verified": verified, "via": via}


# ── the scan ─────────────────────────────────────────────────────────────────


async def _gather(session: AsyncSession, tenant_uuid: uuid.UUID, tenant_token: str,
                  counts: dict[str, int]) -> dict[str, _Person]:
    people: dict[str, _Person] = {}

    def person(email: str) -> _Person:
        return people.setdefault(email, _Person(email))

    # 1. Portal customers — the strongest source: an account the customer made.
    #    Its email is VERIFIED only if the portal verified it by one-time code.
    rows = (await session.execute(
        select(PortalCustomerModel).where(PortalCustomerModel.tenant_id == tenant_uuid)
        .order_by(PortalCustomerModel.created_at).limit(MAX_SOURCE_ROWS + 1))).scalars().all()
    counts["portal_customers_read"] = min(len(rows), MAX_SOURCE_ROWS)
    counts["truncated"] = int(len(rows) > MAX_SOURCE_ROWS)
    for pc in rows[:MAX_SOURCE_ROWS]:
        email = _norm("email", pc.primary_email)
        if email and email.endswith("@deleted") and email.startswith("deleted_"):
            # An ERASED account (portal_customers erasure rewrites it to this).
            # Proposing a profile from it would bring an erased person back.
            counts["skipped_erased"] = counts.get("skipped_erased", 0) + 1
            continue
        if not email:
            counts["skipped_unusable"] = counts.get("skipped_unusable", 0) + 1
            continue
        p = person(email)
        p.names.append(pc.display_name or email)
        p.portal_customer_id = str(pc.id)
        p.add("email", email, pc.primary_email, verified=bool(pc.verified), via="portal account")
        alt = _norm("email", pc.alt_email)
        if alt and alt != email:
            p.add("email", alt, pc.alt_email, verified=False, via="portal account (alternate)")
        phone = _norm("phone", pc.phone)
        if phone:
            p.add("phone", phone, pc.phone, verified=False, via="portal account")
        p.add("portal_customer", str(pc.id), str(pc.id), verified=False, via="portal account")

    # 2. Case submitters — an address a case was filed from, in THIS tenant.
    subs = (await session.execute(
        select(CaseInstanceModel.id, CaseInstanceModel.portal_submitter_email,
               CaseInstanceModel.portal_submitter_name)
        .where(CaseInstanceModel.tenant_id == tenant_uuid,
               CaseInstanceModel.portal_submitter_email.isnot(None))
        .order_by(CaseInstanceModel.created_at.desc())
        .limit(MAX_SOURCE_ROWS + 1))).all()
    counts["cases_read"] = min(len(subs), MAX_SOURCE_ROWS)
    counts["truncated"] = int(counts["truncated"] or len(subs) > MAX_SOURCE_ROWS)
    case_owner: dict[str, str] = {}
    for cid, raw_email, name in subs[:MAX_SOURCE_ROWS]:
        email = _norm("email", raw_email)
        if not email:
            counts["skipped_unusable"] = counts.get("skipped_unusable", 0) + 1
            continue
        p = person(email)
        if name:
            p.names.append(name)
        p.add("email", email, raw_email, verified=False, via="case submitter")
        if len(p.case_ids) < MAX_CASES_PER_PERSON:
            p.case_ids.append(str(cid))
        case_owner[str(cid)] = email

    # 3. Chat numbers — ONLY as evidence about a case's known submitter. A
    #    number with nothing else behind it proposes nothing.
    from velaris_shared.tenancy.identity import resolve_tenant

    tokens = {str(tenant_token), str(tenant_uuid)}
    resolved = await resolve_tenant(session, str(tenant_token))
    if resolved is not None:
        tokens |= {str(resolved[0]), str(resolved[1])}
    convs = (await session.execute(
        select(ChannelConversationModel.peer_address, ChannelConversationModel.case_id)
        .where(ChannelConversationModel.tenant_id.in_(sorted(tokens)))
        .limit(MAX_SOURCE_ROWS + 1))).all()
    counts["conversations_read"] = min(len(convs), MAX_SOURCE_ROWS)
    skipped_numbers = 0
    for peer, case_id in convs[:MAX_SOURCE_ROWS]:
        owner = case_owner.get(str(case_id)) if case_id else None
        phone = _norm("phone", peer)
        if not owner or not phone:
            skipped_numbers += 1
            continue
        people[owner].add("phone", phone, peer, verified=False,
                          via=f"chat on case {case_id}")
    counts["numbers_skipped_no_other_evidence"] = skipped_numbers
    return people


async def _owners(session: AsyncSession, tenant_uuid: uuid.UUID,
                  keys: list[tuple[str, str]]) -> dict[tuple[str, str], uuid.UUID]:
    """Which LIVE profile holds each identifier — merges followed, in bulk.

    One query per 500 values and one for the merge map, rather than a profile
    lookup per identifier: a tenant with twenty thousand portal accounts must
    not turn a scan into a hundred thousand round trips.
    """
    raw: dict[tuple[str, str], uuid.UUID] = {}
    by_kind: dict[str, list[str]] = defaultdict(list)
    for k, v in keys:
        by_kind[k].append(v)
    for kind, values in by_kind.items():
        for i in range(0, len(values), 500):
            rows = (await session.execute(select(
                CustomerIdentifierModel.kind, CustomerIdentifierModel.value_normalised,
                CustomerIdentifierModel.customer_id).where(
                CustomerIdentifierModel.tenant_id == tenant_uuid,
                CustomerIdentifierModel.kind == kind,
                CustomerIdentifierModel.value_normalised.in_(values[i:i + 500])))).all()
            for k, v, cid in rows:
                raw[(k, v)] = cid
    merged = dict((await session.execute(select(
        CustomerProfileModel.id, CustomerProfileModel.merged_into_id).where(
        CustomerProfileModel.tenant_id == tenant_uuid,
        CustomerProfileModel.merged_into_id.isnot(None)))).all())

    def live(cid: uuid.UUID) -> uuid.UUID:
        seen = set()
        while cid in merged and cid not in seen and len(seen) < 8:
            seen.add(cid)
            cid = merged[cid]
        return cid

    return {k: live(cid) for k, cid in raw.items()}


async def _linked_cases(session: AsyncSession,
                        customer_ids: set[uuid.UUID]) -> dict[uuid.UUID, set[str]]:
    out: dict[uuid.UUID, set[str]] = defaultdict(set)
    ids = list(customer_ids)
    for i in range(0, len(ids), 500):
        for cid, oid in (await session.execute(select(
                CustomerLinkModel.customer_id, CustomerLinkModel.object_id).where(
                CustomerLinkModel.customer_id.in_(ids[i:i + 500]),
                CustomerLinkModel.object_kind == "case"))).all():
            out[cid].add(str(oid))
    return out


def _best_name(p: _Person) -> str:
    names = [n.strip() for n in p.names if n and n.strip() and "@" not in n]
    return names[0] if names else p.email


def _ident_list(p: _Person, only: set[tuple[str, str]] | None = None) -> list[dict[str, Any]]:
    return [{"kind": k, "value": v, "display": meta["display"],
             "verified": meta["verified"], "via": meta["via"]}
            for (k, v), meta in sorted(p.identifiers.items())
            if only is None or (k, v) in only]


async def run_scan(session: AsyncSession, *, tenant_uuid: uuid.UUID, tenant_token: str,
                   started_by: str) -> CustomerBackfillRunModel:
    """Read the history and write proposals. Applies nothing."""
    run = CustomerBackfillRunModel(id=uuid.uuid4(), tenant_id=tenant_uuid,
                                   started_by=started_by, started_at=_utcnow(), counts={})
    session.add(run)
    await session.flush()

    counts: dict[str, int] = {}
    people = await _gather(session, tenant_uuid, tenant_token, counts)
    owners = await _owners(session, tenant_uuid,
                           [k for p in people.values() for k in p.identifiers])
    links = await _linked_cases(session, set(owners.values()))
    names: dict[uuid.UUID, str] = {}
    created: dict[uuid.UUID, datetime] = {}
    held_ids = list(set(owners.values()))
    for i in range(0, len(held_ids), 500):      # asyncpg caps a statement at 32767 params
        for pid, name, made in (await session.execute(select(
                CustomerProfileModel.id, CustomerProfileModel.display_name,
                CustomerProfileModel.created_at).where(
                CustomerProfileModel.id.in_(held_ids[i:i + 500])))).all():
            names[pid], created[pid] = name, made

    wanted: dict[str, tuple[str, dict, dict]] = {}
    for email, p in people.items():
        held = {owners[k] for k in p.identifiers if k in owners}
        evidence = {"sources": sorted({m["via"] for m in p.identifiers.values()}),
                    "portal_customer_id": p.portal_customer_id,
                    "case_count": len(p.case_ids)}
        if not held:
            fp = _fingerprint("create_profile", email)
            wanted[fp] = ("create_profile", {
                "display_name": _best_name(p), "primary_email": email,
                "identifiers": _ident_list(p), "case_ids": p.case_ids}, evidence)
        elif len(held) == 1:
            cid = next(iter(held))
            missing = {k for k in p.identifiers if k not in owners}
            new_cases = [c for c in p.case_ids if c not in links.get(cid, set())]
            if not missing and not new_cases:
                continue
            fp = _fingerprint("enrich_profile", str(cid), sorted(missing), sorted(new_cases))
            wanted[fp] = ("enrich_profile", {
                "customer_id": str(cid), "display_name": _best_name(p),
                "identifiers": _ident_list(p, missing), "case_ids": new_cases}, evidence)
        else:
            ids = sorted(str(h) for h in held)
            # The OLDEST profile wins: it is the one reps have been using.
            order = sorted(held, key=lambda h: (created.get(h) or _utcnow()).replace(tzinfo=None))
            fp = _fingerprint("merge_profiles", ids)
            wanted[fp] = ("merge_profiles", {
                "winner_id": str(order[0]),
                "loser_ids": [str(x) for x in order[1:]],
                "profiles": [{"id": str(x), "display_name": names.get(x, "")} for x in order],
                "shared": _ident_list(p, {k for k in p.identifiers if k in owners})}, evidence)

    existing = {r.fingerprint: r for r in (await session.execute(
        select(CustomerBackfillProposalModel).where(
            CustomerBackfillProposalModel.tenant_id == tenant_uuid))).scalars().all()}
    now = _utcnow()
    made = refreshed = kept_rejected = superseded = 0
    for fp, (kind, payload, evidence) in wanted.items():
        row = existing.get(fp)
        if row is None:
            session.add(CustomerBackfillProposalModel(
                id=uuid.uuid4(), tenant_id=tenant_uuid, run_id=run.id, kind=kind,
                fingerprint=fp, payload=payload, evidence=evidence, status="proposed",
                created_at=now, updated_at=now))
            made += 1
        elif row.status == "proposed":
            row.payload, row.evidence, row.run_id, row.updated_at = payload, evidence, run.id, now
            refreshed += 1
        elif row.status == "rejected":
            kept_rejected += 1           # an operator said no; that stands
    # A pending proposal the data no longer supports is superseded, not left to
    # be accepted against a world that has moved.
    for fp, row in existing.items():
        if row.status == "proposed" and fp not in wanted:
            row.status, row.updated_at = "superseded", now
            row.reason = "the data no longer supports this proposal (re-scan)"
            # A dead proposal has no use for the personal data it quoted.
            row.payload, row.evidence = {}, {}
            superseded += 1

    counts.update({"people_found": len(people), "proposed_new": made,
                   "proposed_refreshed": refreshed, "rejected_not_reproposed": kept_rejected,
                   "superseded": superseded})
    run.counts = counts
    run.finished_at = _utcnow()
    await session.flush()
    return run


# ── deciding ─────────────────────────────────────────────────────────────────


async def _add_idents(session, tenant_uuid, customer_id, idents,
                      primary: str | None = None) -> list[str]:
    added = []
    for i in idents:
        verified = bool(i.get("verified"))
        await identity.add_identifier(
            session, tenant_uuid=tenant_uuid, customer_id=customer_id,
            kind=i["kind"], value=i.get("display") or i["value"], verified=verified,
            verified_by="portal" if verified else None, source="backfill",
            is_primary=bool(primary) and i["kind"] == "email" and i["value"] == primary)
        added.append(f"{i['kind']}:{i['value']}")
    return added


async def _link_cases(session, tenant_uuid, customer_id, case_ids, actor) -> list[str]:
    linked = []
    for c in case_ids:
        case = await session.get(CaseInstanceModel, uuid.UUID(c))
        # Re-checked: the case must still exist and still be this tenant's.
        if case is None or case.tenant_id != tenant_uuid:
            continue
        await identity.link(session, tenant_uuid=tenant_uuid, customer_id=customer_id,
                            object_kind="case", object_id=case.id, reason="backfill",
                            linked_by=actor)
        linked.append(c)
    return linked


async def accept(session: AsyncSession, *, tenant_uuid: uuid.UUID,
                 row: CustomerBackfillProposalModel, actor: Any,
                 reason: str | None = None) -> dict[str, Any]:
    """Apply ONE proposal, re-checking it against the data as it is now.

    An identifier that has since been claimed by another profile refuses the
    whole proposal (`identity.add_identifier` never moves one — moving it would
    hide the very duplicate this exists to surface), and the proposal is marked
    superseded so the next scan can say what is true now.
    """
    from velaris_shared import hxguard

    if row.status != "proposed":
        raise BackfillError(f"this proposal is {row.status}", 409)
    who = str(getattr(actor, "user_id", None) or "unknown")
    p = row.payload or {}
    try:
        if row.kind == "create_profile":
            profile = await identity.create_profile(
                session, tenant_uuid=tenant_uuid, display_name=p.get("display_name") or "Unnamed",
                source="backfill", created_by=who)
            added = await _add_idents(session, tenant_uuid, profile.id,
                                      p.get("identifiers") or [], p.get("primary_email"))
            linked = await _link_cases(session, tenant_uuid, profile.id, p.get("case_ids") or [], who)
            result = {"customer_id": str(profile.id), "identifiers": added, "cases": linked}
        elif row.kind == "enrich_profile":
            target = await identity.follow_merges(session, uuid.UUID(p["customer_id"]))
            if target.tenant_id != tenant_uuid or target.status != "active":
                raise identity.IdentityError("the profile this enriches is no longer active")
            added = await _add_idents(session, tenant_uuid, target.id, p.get("identifiers") or [])
            linked = await _link_cases(session, tenant_uuid, target.id, p.get("case_ids") or [], who)
            result = {"customer_id": str(target.id), "identifiers": added, "cases": linked}
        elif row.kind == "merge_profiles":
            if not hxguard.user_allows(actor, "engage.profile.merge"):
                raise BackfillError("merging two people needs engage.profile.merge", 403)
            if len((reason or "").strip()) < 5:
                raise BackfillError("a merge needs a reason somebody can read later")
            merges = []
            for loser in p.get("loser_ids") or []:
                rec = await identity.merge(
                    session, tenant_uuid=tenant_uuid, winner_id=uuid.UUID(p["winner_id"]),
                    loser_id=uuid.UUID(loser), reason=reason.strip(), merged_by=who)
                merges.append(str(rec.id))
            result = {"customer_id": p["winner_id"], "merges": merges}
        else:                                                     # pragma: no cover
            raise BackfillError("unknown proposal kind")
    except identity.IdentityError as exc:
        raise BackfillError(f"cannot apply this proposal any more: {exc}. Re-run the scan.",
                            409) from exc
    row.status, row.decided_by, row.decided_at = "accepted", who, _utcnow()
    row.reason, row.result, row.updated_at = reason, result, _utcnow()
    await session.flush()
    return result


async def reject(session: AsyncSession, *, row: CustomerBackfillProposalModel,
                 actor: Any, reason: str) -> None:
    if row.status != "proposed":
        raise BackfillError(f"this proposal is {row.status}", 409)
    if len((reason or "").strip()) < 5:
        raise BackfillError("a rejection needs a reason — it stops the scan proposing this again")
    row.status = "rejected"
    row.decided_by = str(getattr(actor, "user_id", None) or "unknown")
    row.decided_at = row.updated_at = _utcnow()
    row.reason = reason.strip()
    await session.flush()


def proposal_json(r: CustomerBackfillProposalModel) -> dict[str, Any]:
    from case_service.core.timestamps import iso_utc

    return {"id": str(r.id), "kind": r.kind, "status": r.status, "payload": r.payload,
            "evidence": r.evidence, "run_id": str(r.run_id) if r.run_id else None,
            "created_at": iso_utc(r.created_at), "updated_at": iso_utc(r.updated_at),
            "decided_by": r.decided_by, "decided_at": iso_utc(r.decided_at),
            "reason": r.reason, "result": r.result}


def run_json(r: CustomerBackfillRunModel) -> dict[str, Any]:
    from case_service.core.timestamps import iso_utc

    return {"id": str(r.id), "started_by": r.started_by, "started_at": iso_utc(r.started_at),
            "finished_at": iso_utc(r.finished_at), "counts": r.counts or {}}
