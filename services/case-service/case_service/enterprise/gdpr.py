"""GDPR compliance tools — data export, deletion, rectification.

Copyright (c) 2024-2025 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select, update, delete
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)


async def export_user_data(
    session: AsyncSession, user_id: str, tenant_id: str | None = None,
) -> dict[str, Any]:
    """Compile all data about a user for GDPR export (Article 15).

    `tenant_id` is the CALLER's tenant, and it is only used by the HxArbiter
    PEP below. It defaults to None — which resolves to the platform-default
    tenant exactly as this function behaved before the parameter existed — so
    no existing caller changes behaviour by not passing it.
    """
    from velaris_shared.db.models import (
        CaseInstanceModel, CaseAuditLogModel, CaseAssignmentModel,
        TenantMembershipModel, SecurityEventModel,
    )

    # ── HxArbiter PEP (ARB-C) ────────────────────────────────────────────────
    # A DSAR export is data leaving the platform, which is exactly what the
    # `export` namespace governs. There is no legacy control here to mirror —
    # the route's hxguard gate is the only thing that stands in front of it —
    # so `legacy_allowed=True` records "the platform allows this today" and the
    # engine's answer is compared against it. Inert until `export` is promoted.
    #
    # It also declares the two obligations it can honour ITSELF (§4.5):
    # `watermark_export` and `pseudonymize` act on the payload this function is
    # about to build, and no central handler can reach that payload. Everything
    # else the policy attaches is discharged inside `pep`, and an obligation
    # neither side can honour DENIES — an allow qualified by a condition nobody
    # carried out is not the allow the author wrote.
    _obligations: list = []
    try:
        from case_service.hxarbiter import pep as _arb_pep

        _allowed, _obligations = await _arb_pep.check_with_obligations(
            session,
            caller_handles=("watermark_export", "pseudonymize"),
            # The CALLER's tenant, passed in. It used to read
            # `session.tenant_id`, which does not exist on an AsyncSession and
            # never has — so every tenant's DSAR was evaluated and logged
            # against the platform-default tenant. Proven on the live box: a
            # `dev`-tenant admin's export was recorded under `default`.
            #
            # Two consequences, and the second is the one that bites after a
            # promotion: the tenant's OWN `export.*` policy was never
            # consulted, and the parity evidence for every other tenant was
            # empty while `default`'s decision log accumulated their exports.
            tenant_id=str(tenant_id or "default"),
            action=_arb_pep.ACTION_EXPORT_CASE,
            subject_id=str(user_id),
            subject_kind=_arb_pep.SubjectKind.USER,
            resource_kind="data_subject",
            resource_id=str(user_id),
            context={"purpose": "dsar_article_15", "destination_region": "external"},
            legacy_allowed=True,
            claimed_by="enterprise.gdpr",
        )
        if not _allowed:
            raise PermissionError(
                "VEL-ARB-EXPORT-DENIED-403: policy refuses this export.")
    except PermissionError:
        raise
    except Exception as exc:                                    # noqa: BLE001
        logger.warning("gdpr export: HxArbiter PEP unavailable: %s", exc)

    export = {
        "subject_id": user_id,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "user_profile": [],
        "cases_created": [],
        "assignments": [],
        "audit_entries": [],
        "tenant_memberships": [],
        "security_events": [],
    }

    # User directory profile (covers internal operators; also match by email)
    try:
        from velaris_shared.db.models import UserDirectoryModel
        from sqlalchemy import or_
        ud_stmt = select(UserDirectoryModel).where(
            or_(UserDirectoryModel.user_id == user_id, UserDirectoryModel.email == user_id)
        )
        ud_result = await session.execute(ud_stmt)
        for u in ud_result.scalars().all():
            export["user_profile"].append({
                "user_id": u.user_id,
                "email": u.email,
                "display_name": u.display_name,
                "roles": u.roles,
                "is_active": u.is_active,
                "tenant_id": u.tenant_id,
                "created_at": u.created_at.isoformat() if u.created_at else None,
            })
            # Normalise to the canonical user_id if looked up by email
            if u.user_id != user_id:
                user_id = u.user_id
    except Exception:
        pass

    # Cases created by user
    cases_stmt = select(CaseInstanceModel).where(CaseInstanceModel.created_by == user_id)
    cases_result = await session.execute(cases_stmt)
    for c in cases_result.scalars().all():
        export["cases_created"].append({
            "id": str(c.id),
            "case_type_id": str(c.case_type_id),
            "status": c.status,
            "created_at": c.created_at.isoformat() if c.created_at else None,
        })

    # Assignments
    assign_stmt = select(CaseAssignmentModel).where(CaseAssignmentModel.assignee_id == user_id)
    assign_result = await session.execute(assign_stmt)
    for a in assign_result.scalars().all():
        export["assignments"].append({
            "id": str(a.id),
            "case_id": str(a.case_id),
            "step_id": a.step_id,
            "status": a.status,
            "created_at": a.created_at.isoformat() if a.created_at else None,
        })

    # Audit entries actor
    audit_stmt = select(CaseAuditLogModel).where(CaseAuditLogModel.actor_id == user_id)
    audit_result = await session.execute(audit_stmt)
    for a in audit_result.scalars().all():
        export["audit_entries"].append({
            "id": str(a.id),
            "case_id": str(a.case_id),
            "action": a.action,
            "timestamp": a.timestamp.isoformat() if a.timestamp else None,
        })

    # Tenant memberships
    try:
        tm_stmt = select(TenantMembershipModel).where(TenantMembershipModel.user_id == user_id)
        tm_result = await session.execute(tm_stmt)
        for m in tm_result.scalars().all():
            export["tenant_memberships"].append({
                "tenant_id": str(m.tenant_id),
                "role": m.role,
            })
    except Exception:
        pass  # Tenants table might not exist in older setups

    # Security events
    try:
        sec_stmt = select(SecurityEventModel).where(SecurityEventModel.user_id == user_id).limit(500)
        sec_result = await session.execute(sec_stmt)
        for e in sec_result.scalars().all():
            export["security_events"].append({
                "event_type": e.event_type,
                "action": e.action,
                "outcome": e.outcome,
                "timestamp": e.timestamp.isoformat() if e.timestamp else None,
            })
    except Exception:
        pass

    # ── obligations this call site said it would honour (§4.5) ───────────────
    # Applied to the finished payload, which is the only place they mean
    # anything. `pep` has already discharged everything else and denied above
    # if any obligation could not be honoured at all, so reaching here means
    # these are the ones left for us — and not applying them would be the one
    # way to defeat the fail-closed rule.
    for _ob in _obligations:
        kind = str(getattr(_ob, "kind", ""))
        params = getattr(_ob, "params", {}) or {}
        if kind == "pseudonymize":
            export = _pseudonymize_export(export, params)
        elif kind == "watermark_export":
            export["watermark"] = {
                "applied": True,
                "label": str(params.get("label") or "CONFIDENTIAL — subject access response"),
                "issued_at": datetime.now(timezone.utc).isoformat(),
                "subject_id": user_id,
            }
    if _obligations:
        export["policy_obligations"] = [str(getattr(o, "kind", "")) for o in _obligations]

    return export


#: Fields a pseudonymised export replaces rather than removes. Removing them
#: would change the SHAPE of the response, and a caller that branches on a
#: missing key behaves differently for a pseudonymised subject — which is how a
#: privacy control becomes an oracle.
_PSEUDONYMISABLE = ("email", "username", "display_name", "full_name",
                    "phone", "portal_submitter_email", "portal_submitter_name")


def _pseudonymize_export(export: dict[str, Any], params: dict[str, Any]) -> dict[str, Any]:
    """Replace direct identifiers with a stable, salted token.

    Stable so the same person reads as the same token across a report — an
    export where every occurrence differs is unusable for the purpose a DSAR
    exists to serve. Salted per subject so tokens cannot be compared between
    exports.

    It is pseudonymisation, not anonymisation, and the difference is legal as
    well as technical: the subject id remains, so this narrows exposure rather
    than taking the data out of scope.
    """
    import hashlib

    salt = str(params.get("salt") or export.get("subject_id") or "")

    def token(value: Any) -> str:
        digest = hashlib.sha256(f"{salt}|{value}".encode()).hexdigest()[:16]
        return f"pseudo:{digest}"

    def walk(node: Any) -> Any:
        if isinstance(node, dict):
            return {k: (token(v) if k in _PSEUDONYMISABLE and v else walk(v))
                    for k, v in node.items()}
        if isinstance(node, list):
            return [walk(v) for v in node]
        return node

    out = walk(export)
    out["pseudonymized"] = True
    return out


async def anonymize_user_data(
    session: AsyncSession, user_id: str,
) -> dict[str, Any]:
    """Anonymize (don't delete) user data — GDPR-compliant right to be forgotten.

    Replaces user_id with 'anon-{short-hash}' to preserve audit trail integrity
    while removing personal identification.
    """
    from velaris_shared.db.models import (
        CaseInstanceModel, CaseAuditLogModel, CaseAssignmentModel,
        TenantMembershipModel,
    )
    import hashlib

    anon_id = "anon-" + hashlib.sha256(user_id.encode()).hexdigest()[:12]

    counts = {}

    # The HxArbiter decision log. It was unreachable by any erasure path until
    # 2026-08-29 — retention purges by age and nothing here had heard of the
    # table — so every policy decision about a person kept their subject,
    # resource and context payloads indefinitely, in a log designed never to be
    # editable. `erase_subject` scrubs exactly the fields the chain hash does
    # NOT cover, so the seal still verifies afterwards.
    try:
        from case_service.hxarbiter import chain as _arb_chain

        counts.update(await _arb_chain.erase_subject(session, user_id))
    except Exception as exc:                                    # noqa: BLE001
        # Loud, and not fatal: an erasure that half-completes and then raises
        # leaves the caller unable to tell what was done.
        logger.error("gdpr: could not erase HxArbiter decisions for %s: %s",
                     user_id, exc)
        counts["arbiter_decisions"] = -1

    # Anonymize cases.created_by
    stmt = update(CaseInstanceModel).where(
        CaseInstanceModel.created_by == user_id
    ).values(created_by=anon_id)
    result = await session.execute(stmt)
    counts["cases"] = result.rowcount

    # Anonymize audit.actor_id
    stmt = update(CaseAuditLogModel).where(
        CaseAuditLogModel.actor_id == user_id
    ).values(actor_id=anon_id)
    result = await session.execute(stmt)
    counts["audit"] = result.rowcount

    # Anonymize assignments.assignee_id
    stmt = update(CaseAssignmentModel).where(
        CaseAssignmentModel.assignee_id == user_id
    ).values(assignee_id=anon_id)
    result = await session.execute(stmt)
    counts["assignments"] = result.rowcount

    # Remove tenant memberships entirely
    try:
        stmt = delete(TenantMembershipModel).where(TenantMembershipModel.user_id == user_id)
        result = await session.execute(stmt)
        counts["memberships_removed"] = result.rowcount
    except Exception:
        counts["memberships_removed"] = 0

    # HxReplay traces are DERIVED case data (design §9 retention): anonymize the
    # run creator and any recorded actor ids inside stored counterfactual traces.
    from velaris_shared.db.models import ReplayResultModel, ReplayRunModel

    stmt = update(ReplayRunModel).where(
        ReplayRunModel.created_by == user_id
    ).values(created_by=anon_id)
    counts["replay_runs"] = (await session.execute(stmt)).rowcount

    import copy as _copy
    scrubbed = 0
    results = (await session.execute(select(ReplayResultModel))).scalars().all()
    for res in results:
        if not res.trace or user_id not in str(res.trace):
            continue
        # deep-copy FIRST: mutating the loaded dict in place would leave the new
        # value == the old one at flush, and the JSON column would never update
        trace = _copy.deepcopy(res.trace)
        for node in trace.get("nodes", []) or []:
            if node.get("actor_id") == user_id:
                node["actor_id"] = anon_id
        res.trace = trace
        scrubbed += 1
    counts["replay_traces"] = scrubbed

    return {
        "subject_id": user_id,
        "anonymized_id": anon_id,
        "counts": counts,
        "anonymized_at": datetime.now(timezone.utc).isoformat(),
    }


async def create_request(
    session: AsyncSession,
    *,
    subject_id: str,
    request_type: str,
    requested_by: str | None = None,
    reason: str | None = None,
) -> uuid.UUID:
    """Record a GDPR data subject request."""
    from velaris_shared.db.models import GDPRRequestModel

    req = GDPRRequestModel(
        subject_id=subject_id,
        request_type=request_type,
        status="pending",
        requested_by=requested_by,
        reason=reason,
    )
    session.add(req)
    await session.flush()
    return req.id


async def complete_request(
    session: AsyncSession,
    request_id: uuid.UUID,
    result_file: str | None = None,
) -> None:
    """Mark a GDPR request as completed."""
    from velaris_shared.db.models import GDPRRequestModel

    stmt = update(GDPRRequestModel).where(
        GDPRRequestModel.id == request_id,
    ).values(
        status="completed",
        completed_at=datetime.now(timezone.utc),
        result_file=result_file,
    )
    await session.execute(stmt)
