"""What the platform holds about a CUSTOMER — the GDPR Art. 15 export (Oct item 8f).

`enterprise/gdpr.py` answers subject-access for STAFF users. Nothing answered it
for the people the platform exists to serve: the portal account, the cases they
raised, the rep desk's profile of them, their calls, messages, orders and
consent. Erasure (Art. 17) reaches all of that since item 3; access did not
reach any of it. This is the other half, and it follows the same map as
`engage/erasure.forget` so that what can be erased can also be shown.

**Who.** The subject is found by what the tenant knows them by — emails,
phones, a portal account, a rep-desk profile — and the set is closed over once:
a portal account or profile found by email contributes its other identifiers,
and merged profiles are followed both ways. One pass, not a fixpoint: a second
hop would start matching other people who happen to share a line.

**Where.** Only the caller's tenant. Every query is filtered by it, whatever
shape that tenant column has (a GUID in some tables, a slug or uuid string in
others). A subject in two tenants is two exports, requested by each.

**What is shown, and what is not.**
* Case data is shown except keys starting `_` (platform internals) and keys that
  name credentials (`password`, `token`, `secret`, `otp`, `api_key`).
* Messages on their cases that were shared with them are shown. Internal staff
  notes are COUNTED and withheld unless the operator asks for them
  (`include_internal`) — they can hold third parties' data and an operator
  reviews before release. The customer's own self-service export never has them.
* A relation to another customer shows the relation, never the other person.
* Documents are listed (name, type, dates, whether shared), not attached: the
  files are retrieved separately through the ordinary document routes.
* Everything else the response leaves out is named in `not_included`, so a
  reader never mistakes a short answer for a complete one.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import logging
import re
import uuid
from datetime import datetime, timezone
from typing import Any, Iterable

from sqlalchemy import inspect as sa_inspect
from sqlalchemy import String, cast, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from velaris_shared.db.models import (
    CaseInstanceModel,
    CaseMessageModel,
    CaseTypeModel,
    ChannelConversationModel,
    ChannelMessageModel,
    CheckoutOrderModel,
    ConsentRecordModel,
    CustomerIdentifierModel,
    CustomerInteractionModel,
    CustomerLinkModel,
    CustomerProfileModel,
    CustomerRelationModel,
    DocumentModel,
    EmailMessageModel,
    PortalCustomerCaseLinkModel,
    PortalCustomerModel,
    SmsMessageModel,
)

log = logging.getLogger(__name__)

MAX_CASES = 1000
MAX_ROWS = 5000
_CREDENTIAL = re.compile(r"(password|passwd|token|secret|otp|api[_-]?key)", re.I)
#: Columns never exported, whichever table they are on.
_NEVER = {"otp_code", "otp_expires_at", "otp_attempts", "token_hash", "raw_payload",
          "raw_headers", "password_hash", "secret"}

NOT_INCLUDED = [
    "Case audit history and platform logs: they record what STAFF did, and are "
    "released on request after review.",
    "Internal staff notes, unless the operator chose to include them.",
    "Document files: listed here, retrieved separately.",
    "Analytics and AI-derived aggregates that do not identify the subject.",
    "Data held in other tenants, or in external systems a connector reaches.",
]


class ExportError(ValueError):
    pass


def _iso(v: Any) -> Any:
    return v.isoformat() if isinstance(v, datetime) else v


def row_dict(obj: Any, *, drop: Iterable[str] = ()) -> dict[str, Any]:
    """Every column of a row, minus the never-exported ones."""
    skip = _NEVER | set(drop)
    out: dict[str, Any] = {}
    for col in sa_inspect(obj).mapper.column_attrs:
        if col.key in skip:
            continue
        v = getattr(obj, col.key)
        out[col.key] = str(v) if isinstance(v, uuid.UUID) else _iso(v)
    return out


def clean_case_data(data: Any) -> dict[str, Any]:
    if not isinstance(data, dict):
        return {}
    return {k: v for k, v in data.items()
            if not str(k).startswith("_") and not _CREDENTIAL.search(str(k))}


async def _tenant(session: AsyncSession, tenant: Any) -> tuple[uuid.UUID, set[str]]:
    from case_service.tenancy.identity import resolve_tenant
    resolved = await resolve_tenant(session, tenant or "default")
    if resolved is None:
        raise ExportError("the tenant is not known here")
    return resolved[0], {str(resolved[0]), resolved[1]}


def _norm(kind: str, values: Iterable[str | None]) -> set[str]:
    from case_service.engage import identity
    out = set()
    for v in values:
        if not v:
            continue
        try:
            out.add(identity.normalise(kind, str(v)))
        except Exception:                                        # noqa: BLE001
            continue
    return out


def _uuids(values: Iterable[Any]) -> set[uuid.UUID]:
    out = set()
    for x in values:
        try:
            out.add(uuid.UUID(str(x)))
        except (TypeError, ValueError):
            continue
    return out


async def build(session: AsyncSession, *, tenant: Any, emails: Iterable[str | None] = (),
                phones: Iterable[str | None] = (), portal_customer_ids: Iterable[Any] = (),
                customer_ids: Iterable[Any] = (), include_internal: bool = False,
                ) -> dict[str, Any]:
    """The subject's data in this tenant. Read-only."""
    from case_service.engage.erasure import _related_profiles

    tenant_uuid, tokens = await _tenant(session, tenant)
    em, ph = _norm("email", emails), _norm("phone", phones)
    pcs, profiles = _uuids(portal_customer_ids), _uuids(customer_ids)
    if not (em or ph or pcs or profiles):
        raise ExportError("name the subject: an email, a phone, a portal account or a profile")
    searched = {"emails": sorted(em), "phones": sorted(ph)}

    # ── the portal account(s) ────────────────────────────────────────────────
    conds = []
    if em:
        conds += [PortalCustomerModel.primary_email.in_(sorted(em)),
                  PortalCustomerModel.alt_email.in_(sorted(em))]
    if pcs:
        conds.append(PortalCustomerModel.id.in_(sorted(pcs)))
    accounts = list((await session.execute(select(PortalCustomerModel).where(
        PortalCustomerModel.tenant_id == tenant_uuid, or_(*conds))))
        .scalars().all()) if conds else []
    pcs = {a.id for a in accounts}                 # only accounts IN this tenant
    for a in accounts:
        em |= _norm("email", [a.primary_email, a.alt_email])
        ph |= _norm("phone", [a.phone])

    # ── the rep desk's profile(s) ────────────────────────────────────────────
    for kind, values in (("email", em), ("phone", ph),
                         ("portal_customer", {str(p).lower() for p in pcs})):
        if values:
            profiles |= set((await session.execute(select(CustomerIdentifierModel.customer_id).where(
                CustomerIdentifierModel.tenant_id == tenant_uuid,
                CustomerIdentifierModel.kind == kind,
                CustomerIdentifierModel.value_normalised.in_(sorted(values))))).scalars().all())
    if profiles:
        profiles = await _related_profiles(session, tenant_uuid, profiles)
    profile_rows = list((await session.execute(select(CustomerProfileModel).where(
        CustomerProfileModel.tenant_id == tenant_uuid,
        CustomerProfileModel.id.in_(sorted(profiles))))).scalars().all()) if profiles else []
    profiles = {p.id for p in profile_rows}        # only profiles IN this tenant
    identifiers = list((await session.execute(select(CustomerIdentifierModel).where(
        CustomerIdentifierModel.tenant_id == tenant_uuid,
        CustomerIdentifierModel.customer_id.in_(sorted(profiles))))).scalars().all()) \
        if profiles else []
    for i in identifiers:
        if i.kind == "email":
            em.add(i.value_normalised)
        elif i.kind == "phone":
            ph.add(i.value_normalised)

    # ── cases ────────────────────────────────────────────────────────────────
    case_ids: set[uuid.UUID] = set()
    if em:
        case_ids |= set((await session.execute(select(CaseInstanceModel.id).where(
            CaseInstanceModel.tenant_id == tenant_uuid,
            CaseInstanceModel.portal_submitter_email.in_(sorted(em))))).scalars().all())
    if pcs:
        case_ids |= set((await session.execute(select(PortalCustomerCaseLinkModel.case_id).where(
            PortalCustomerCaseLinkModel.customer_id.in_(sorted(pcs))))).scalars().all())
    if profiles:
        case_ids |= set((await session.execute(select(CustomerLinkModel.object_id).where(
            CustomerLinkModel.customer_id.in_(sorted(profiles)),
            CustomerLinkModel.object_kind == "case"))).scalars().all())
    orders: list[CheckoutOrderModel] = []
    if em:
        # A JSON column reads differently on each dialect, so: a portable text
        # prefilter to find candidates, then the exact check here. Escaped,
        # because `_` and `%` are wildcards and an email may contain `_`.
        def _like(v: str) -> str:
            return "%" + v.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        candidates = (await session.execute(select(CheckoutOrderModel).where(
            CheckoutOrderModel.tenant_id.in_(sorted(tokens)),
            or_(*[func.lower(cast(CheckoutOrderModel.customer, String)).like(_like(e), escape="\\")
                  for e in sorted(em)]))
            .limit(MAX_ROWS))).scalars().all()
        orders = [o for o in candidates
                  if isinstance(o.customer, dict) and _norm("email", [o.customer.get("email")]) & em]
        case_ids |= {o.case_id for o in orders if o.case_id}
    cases: list[CaseInstanceModel] = []
    if case_ids:
        rows = list((await session.execute(select(CaseInstanceModel).where(
            CaseInstanceModel.id.in_(sorted(case_ids)),
            CaseInstanceModel.tenant_id == tenant_uuid)            # a link never crosses
            .order_by(CaseInstanceModel.created_at).limit(MAX_CASES + 1))).scalars().all())
        cases = rows[:MAX_CASES]
    visible_case_ids = [c.id for c in cases]
    type_names: dict[Any, Any] = {}
    for c in cases:
        if c.case_type_id not in type_names:
            ct = await session.get(CaseTypeModel, c.case_type_id)
            type_names[c.case_type_id] = ct.name if ct else None

    messages, internal_count, documents = [], 0, []
    if visible_case_ids:
        for m in (await session.execute(select(CaseMessageModel).where(
                CaseMessageModel.case_id.in_(visible_case_ids))
                .order_by(CaseMessageModel.created_at).limit(MAX_ROWS))).scalars().all():
            if m.portal_visible or include_internal:
                messages.append({**row_dict(m), "internal": not m.portal_visible})
            else:
                internal_count += 1
        for d in (await session.execute(select(DocumentModel).where(
                DocumentModel.case_id.in_(visible_case_ids),
                DocumentModel.is_deleted.is_(False)).limit(MAX_ROWS))).scalars().all():
            documents.append({"id": str(d.id), "case_id": str(d.case_id), "filename": d.filename,
                              "content_type": d.content_type, "shared_with_you": d.portal_visible,
                              "created_at": _iso(d.created_at)})

    # ── communications ───────────────────────────────────────────────────────
    addresses = sorted(em | ph | {f"whatsapp:{p}" for p in ph})
    conv_conds = [ChannelConversationModel.peer_address.in_(addresses)] if addresses else []
    if profiles:
        conv_conds.append(ChannelConversationModel.customer_id.in_(sorted(profiles)))
    conversations = list((await session.execute(select(ChannelConversationModel).where(
        ChannelConversationModel.tenant_id.in_(sorted(tokens)), or_(*conv_conds))))
        .scalars().all()) if conv_conds else []
    channel_messages = list((await session.execute(select(ChannelMessageModel).where(
        ChannelMessageModel.conversation_id.in_([c.id for c in conversations]))
        .order_by(ChannelMessageModel.created_at).limit(MAX_ROWS)))
        .scalars().all()) if conversations else []
    email_conds = []
    if em:
        email_conds.append(EmailMessageModel.from_address.in_(sorted(em)))
    if visible_case_ids:
        email_conds.append(EmailMessageModel.case_id.in_(visible_case_ids))
    email_rows = list((await session.execute(select(EmailMessageModel).where(
        EmailMessageModel.tenant_id.in_(sorted(tokens)), or_(*email_conds))
        .order_by(EmailMessageModel.created_at).limit(MAX_ROWS)))
        .scalars().all()) if email_conds else []
    sms = list((await session.execute(select(SmsMessageModel).where(
        SmsMessageModel.tenant_id.in_(sorted(tokens)),
        SmsMessageModel.to_number.in_(sorted(ph))).limit(MAX_ROWS)))
        .scalars().all()) if ph else []

    # ── rep desk activity ────────────────────────────────────────────────────
    interactions = list((await session.execute(select(CustomerInteractionModel).where(
        CustomerInteractionModel.tenant_id == tenant_uuid,
        CustomerInteractionModel.customer_id.in_(sorted(profiles)))
        .order_by(CustomerInteractionModel.started_at).limit(MAX_ROWS))).scalars().all()) \
        if profiles else []
    relations = list((await session.execute(select(CustomerRelationModel).where(
        CustomerRelationModel.tenant_id == tenant_uuid,
        or_(CustomerRelationModel.from_customer_id.in_(sorted(profiles)),
            CustomerRelationModel.to_customer_id.in_(sorted(profiles)))))).scalars().all()) \
        if profiles else []

    # ── consent ──────────────────────────────────────────────────────────────
    refs = sorted(em | ph | {str(p) for p in pcs} | {str(p) for p in profiles})
    consent = list((await session.execute(select(ConsentRecordModel).where(
        ConsentRecordModel.subject_ref.in_(refs),
        ConsentRecordModel.tenant_id.in_(sorted(tokens)))
        .order_by(ConsentRecordModel.recorded_at).limit(MAX_ROWS))).scalars().all()) \
        if refs else []

    return {
        "export": "GDPR Article 15 — data held about a customer",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "tenant": sorted(tokens),
        "searched_for": searched,
        "portal_accounts": [row_dict(a) for a in accounts],
        # A rep's notes about the subject, and which staff member took a call,
        # are internal: withheld unless the operator reviewing chose them.
        "profiles": [row_dict(p, drop=() if include_internal else ("notes",))
                     for p in profile_rows],
        "identifiers": [row_dict(i) for i in identifiers],
        "cases": [{"id": str(c.id), "case_number": c.case_number,
                   "case_type": type_names.get(c.case_type_id), "status": c.status,
                   "priority": c.priority, "created_at": _iso(c.created_at),
                   "resolved_at": _iso(c.resolved_at), "closed_at": _iso(c.closed_at),
                   "data": clean_case_data(c.data)} for c in cases],
        "cases_truncated": len(case_ids) > MAX_CASES,
        "case_messages": messages,
        "internal_notes_withheld": internal_count,
        "documents": documents,
        "orders": [row_dict(o) for o in orders],
        "conversations": [row_dict(c) for c in conversations],
        "channel_messages": [row_dict(m) for m in channel_messages],
        "emails": [row_dict(e) for e in email_rows],
        "sms": [row_dict(s) for s in sms],
        "interactions": [row_dict(i, drop=() if include_internal else
                                  ("wrap_up_notes", "agent_user_id"))
                         for i in interactions],
        "relations": [{"relation": r.relation, "may_act_for": r.may_act_for,
                       "direction": "from you" if r.from_customer_id in profiles else "to you",
                       "created_at": _iso(r.created_at)} for r in relations],
        "consent": [row_dict(c) for c in consent],
        "not_included": NOT_INCLUDED,
    }


def counts(export: dict[str, Any]) -> dict[str, int]:
    return {k: len(v) for k, v in export.items()
            if isinstance(v, list) and k not in ("tenant", "not_included")}
