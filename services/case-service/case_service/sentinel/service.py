"""HxSentinel P1 — outcome labelling and signal extraction.

Everything here is deliberately deterministic. There is no model, no scoring
and no recommendation in P1; the job is to build a trustworthy labelled history
and a matchable signal index so that P2 can reason over something real.
"""
from __future__ import annotations

import logging
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from case_service.db.models import (
    CaseAuditLogModel,
    CaseInstanceModel,
    CaseOutcomeLabelModel,
    CaseOutcomeModel,
    CaseRiskSignalModel,
    DocumentVerificationModel,
)
from case_service.sentinel import taxonomy
from case_service.sentinel.hashing import signal_digest

logger = logging.getLogger(__name__)

#: Identity signal types are hashed; everything else is stored literally.
HASHED_TYPES = frozenset({"identity_email", "identity_phone", "identity_address"})

#: The tenantless sentinel. Both outcomes and signals normalise a missing tenant
#: to this string rather than NULL, for one concrete reason: cross-case matching
#: keys on tenant_id, and in SQL ``tenant_id = NULL`` is never true — two
#: tenantless cases sharing an identity would silently fail to link. A literal
#: keeps outcomes and signals symmetric and keeps matching working.
DEFAULT_TENANT = "default"


def norm_tenant(tenant_id) -> str:
    """Canonical tenant string used everywhere HxSentinel stores a tenant."""
    return str(tenant_id) if tenant_id else DEFAULT_TENANT

#: Case variables scanned for identity signals, by signal type. Matching is
#: substring-based on the variable name so tenant-specific names like
#: ``applicant_email`` or ``contact_phone_number`` are picked up without
#: per-tenant configuration.
_IDENTITY_HINTS: dict[str, tuple[str, ...]] = {
    "identity_email": ("email", "e_mail"),
    "identity_phone": ("phone", "mobile", "telephone", "msisdn"),
    "identity_address": ("address", "addr_line", "street"),
}


class UnknownOutcomeError(ValueError):
    """Raised when a label key is neither core nor a known tenant extension."""


# ── Taxonomy ────────────────────────────────────────────────────────────────


async def list_labels(session: AsyncSession, tenant_id: str | None) -> list[dict]:
    """Core labels plus this tenant's active extensions."""
    labels = taxonomy.core_as_dicts()
    if tenant_id:
        rows = (await session.execute(
            select(CaseOutcomeLabelModel).where(
                CaseOutcomeLabelModel.tenant_id == str(tenant_id),
                CaseOutcomeLabelModel.active.is_(True),
            )
        )).scalars().all()
        labels.extend({
            "key": r.key,
            "display": r.display,
            "description": r.description,
            "is_negative": r.is_negative,
            "is_core": False,
        } for r in rows)
    return labels


async def create_label(
    session: AsyncSession, *, tenant_id: str, key: str,
    display: str, description: str | None = None, is_negative: bool = True,
) -> CaseOutcomeLabelModel:
    """Add a tenant label. Core keys are reserved and cannot be shadowed."""
    key = (key or "").strip().lower()
    if not key:
        raise ValueError("label key is required")
    if taxonomy.is_core(key):
        # Allowing this would let 'fraud' mean something different per tenant,
        # which quietly breaks any cross-tenant comparison built on it.
        raise ValueError(f"'{key}' is a core label and cannot be redefined")

    row = CaseOutcomeLabelModel(
        tenant_id=str(tenant_id), key=key, display=display or key,
        description=description, is_negative=is_negative,
    )
    session.add(row)
    await session.flush()
    return row


async def validate_outcome(session: AsyncSession, tenant_id: str | None, outcome: str) -> None:
    if taxonomy.is_core(outcome):
        return
    if tenant_id:
        hit = (await session.execute(
            select(CaseOutcomeLabelModel.id).where(
                CaseOutcomeLabelModel.tenant_id == str(tenant_id),
                CaseOutcomeLabelModel.key == outcome,
                CaseOutcomeLabelModel.active.is_(True),
            )
        )).first()
        if hit:
            return
    raise UnknownOutcomeError(f"unknown outcome label '{outcome}'")


# ── Ground truth ────────────────────────────────────────────────────────────


async def set_outcome(
    session: AsyncSession, *, case_id: uuid.UUID, tenant_id: str | None,
    outcome: str, labelled_by: str, note: str | None = None,
    source: str = "manual", audit: bool = True,
) -> CaseOutcomeModel:
    """Append an outcome label. Never updates an existing row.

    Re-labelling is a first-class action, not an error: outcomes are often only
    confirmed months later (a chargeback lands, a prosecution concludes). The
    previous label stays in the table as part of the record.
    """
    outcome = (outcome or "").strip().lower()
    await validate_outcome(session, tenant_id, outcome)

    previous = await get_current_outcome(session, case_id)

    row = CaseOutcomeModel(
        case_id=case_id, tenant_id=norm_tenant(tenant_id),
        outcome=outcome, note=note, source=source, labelled_by=labelled_by,
    )
    session.add(row)
    await session.flush()

    if audit:
        session.add(CaseAuditLogModel(
            case_id=case_id,
            action="risk.outcome.labelled",
            actor_id=labelled_by,
            details={"outcome": outcome, "source": source, "note": note},
            previous_value={"outcome": previous.outcome} if previous else None,
            new_value={"outcome": outcome},
        ))
        await session.flush()
    return row


async def get_current_outcome(
    session: AsyncSession, case_id: uuid.UUID
) -> CaseOutcomeModel | None:
    """The most recent label for a case, or None if never labelled."""
    return (await session.execute(
        select(CaseOutcomeModel)
        .where(CaseOutcomeModel.case_id == case_id)
        .order_by(CaseOutcomeModel.created_at.desc(), CaseOutcomeModel.id.desc())
        .limit(1)
    )).scalars().first()


async def get_outcome_history(
    session: AsyncSession, case_id: uuid.UUID
) -> list[CaseOutcomeModel]:
    return list((await session.execute(
        select(CaseOutcomeModel)
        .where(CaseOutcomeModel.case_id == case_id)
        .order_by(CaseOutcomeModel.created_at.desc(), CaseOutcomeModel.id.desc())
    )).scalars().all())


# ── Signal extraction ───────────────────────────────────────────────────────


async def _tenant_dek(session: AsyncSession, tenant_id: str | None) -> bytes | None:
    """Fetch the tenant DEK used to key identity digests.

    Returns None when no key can be obtained — the caller then SKIPS identity
    signals rather than falling back to storing or hashing them unkeyed. An
    unkeyed digest would be reversible by dictionary attack for values with
    small domains (phone numbers especially), so failing closed is the only
    safe option.
    """
    try:
        from case_service.hxvault import keyring
        return await keyring.ensure_dek(session, tenant_id)
    except Exception as exc:                       # pragma: no cover - env dependent
        logger.warning("HxSentinel: no tenant DEK (%s) — identity signals skipped", exc)
        return None


def _identity_values(data: dict) -> list[tuple[str, str]]:
    """Pull (signal_type, raw_value) pairs out of a case's variables."""
    found: list[tuple[str, str]] = []
    for name, value in (data or {}).items():
        if not isinstance(value, str) or not value.strip():
            continue
        lowered = name.lower()
        for sig_type, hints in _IDENTITY_HINTS.items():
            if any(h in lowered for h in hints):
                found.append((sig_type, value))
                break
    return found


async def extract_signals(
    session: AsyncSession, case_id: uuid.UUID, *, commit: bool = False
) -> list[CaseRiskSignalModel]:
    """Extract this case's signals and upsert them.

    Idempotent: a unique index on (case_id, signal_type, signal_key) means
    re-running after new data arrives adds only what is new.
    """
    case = await session.get(CaseInstanceModel, case_id)
    if case is None:
        return []

    tenant_id = norm_tenant(case.tenant_id)
    existing = {
        (r.signal_type, r.signal_key)
        for r in (await session.execute(
            select(CaseRiskSignalModel).where(CaseRiskSignalModel.case_id == case_id)
        )).scalars().all()
    }
    added: list[CaseRiskSignalModel] = []

    def _add(signal_type: str, key: str, *, hashed: bool, value: dict) -> None:
        if (signal_type, key) in existing:
            return
        row = CaseRiskSignalModel(
            tenant_id=tenant_id, case_id=case_id, signal_type=signal_type,
            signal_key=key, hashed=hashed, value=value,
        )
        session.add(row)
        added.append(row)
        existing.add((signal_type, key))

    # ── Document hashes: no personal data, stored literally so a finding can
    # name the exact document that recurs.
    for ver in (await session.execute(
        select(DocumentVerificationModel).where(DocumentVerificationModel.case_id == case_id)
    )).scalars().all():
        checks = ver.checks if isinstance(ver.checks, list) else []
        for check in checks:
            if not isinstance(check, dict):
                continue
            digest = check.get("sha256") or check.get("hash")
            if digest:
                _add("doc_hash", str(digest), hashed=False,
                     value={"document_id": str(ver.document_id), "status": ver.status})

    # ── Identity signals: hashed with the tenant DEK, or skipped entirely.
    identity_values = _identity_values(case.data or {})
    if identity_values:
        dek = await _tenant_dek(session, tenant_id)
        if dek:
            for sig_type, raw in identity_values:
                try:
                    key = signal_digest(raw, dek=dek, signal_type=sig_type)
                except ValueError:
                    continue          # empty after normalisation — nothing to match on
                _add(sig_type, key, hashed=True, value={"kind": sig_type})

    # Velocity ("N cases share this identity in a window") is DELIBERATELY not
    # a stored signal in P1. A stored count is frozen at extraction time and
    # goes stale as new cases arrive, and it is order-dependent — the first
    # case of a matching pair would never carry it. P2 computes velocity at
    # query time against the (already indexed) signal store instead, where it
    # is always current. The lookup index this needs exists today.

    await session.flush()
    if commit:
        await session.commit()
    return added


async def get_signals(
    session: AsyncSession, case_id: uuid.UUID
) -> list[CaseRiskSignalModel]:
    return list((await session.execute(
        select(CaseRiskSignalModel)
        .where(CaseRiskSignalModel.case_id == case_id)
        .order_by(CaseRiskSignalModel.signal_type)
    )).scalars().all())
