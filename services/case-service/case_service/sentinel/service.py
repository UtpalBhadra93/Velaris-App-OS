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

from velaris_shared.db.models import (
    CaseAuditLogModel,
    CaseInstanceModel,
    CaseInstanceVariableModel,
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

#: Provenance marker on ``value``. Retirement may only ever delete a row THIS
#: extractor produced. Anything else in the table — a signal written by a future
#: producer, or a row seeded by hand — is not ours to reconcile away, and a
#: retirement pass that assumed sole ownership would silently delete it.
#: Rows written before reconciliation existed lack the marker and are
#: grandfathered, which is correct: they were extracted once, at close, on final
#: data, so they were never at risk of the staleness this retires.
EXTRACTED = "_extracted"

#: Sentinel strings that stand in for a removed value. They are not identities,
#: and hashing one makes every case carrying it match every other.
_REDACTION_PLACEHOLDERS = frozenset({"[anonymized]", "[redacted]"})

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


class SignalExtraction(list):
    """The signals added, plus why anything was deliberately left out.

    Subclasses ``list`` so every existing caller (``len()``, iteration) is
    unaffected; ``.skipped`` is additive. A skip is not an error — extraction
    fails closed on purpose — but it must be *sayable*, or a permanently empty
    signal store looks identical to a case that simply has nothing to extract.
    """

    def __init__(self, *args):
        super().__init__(*args)
        self.skipped: list[str] = []
        #: Signals retired because the value they came from is no longer on the
        #: case. Only meaningful now that extraction re-runs on open cases.
        self.removed: list[str] = []


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


def _is_anonymized(data: dict | None) -> bool:
    """True once HxPrivacy retention has anonymized this case."""
    from case_service.compliance.retention import ANON_MARKER
    return bool((data or {}).get(ANON_MARKER))


async def _identity_source_map(session: AsyncSession, case: CaseInstanceModel) -> dict:
    """Every place a case can carry an identity value, merged into one mapping.

    ``case.data`` alone is not enough, and relying on it silently under-extracted:

    * **Typed variables** live in ``case_instance_variables`` and NEVER touch the
      ``case.data`` blob (they are upserted by ``case_vars.service``). A case type
      that captures email/phone/address as typed variables produced no identity
      signals at all. Promoted keys are unaffected either way — promotion leaves
      the blob intact and suppresses it only at the read façade.
    * **``portal_submitter_email``** is a dedicated column, not a data key. The
      anonymous public intake path is the highest-risk channel and contributed
      nothing.

    Keys are only ever used for substring hint-matching, so the variable's
    ``full_key`` ("legacy.email") works unchanged.

    An **anonymized** case contributes nothing. Retention REPLACES stripped
    values with a literal ``"[anonymized]"`` rather than removing the key, so
    every anonymized case in a tenant would otherwise hash the same placeholder
    to the same digest and appear to share an identity with all the others —
    firing identity_velocity and resemblance findings across unrelated people.
    Worse, ``portal_submitter_email`` is a COLUMN and retention only rewrites
    ``case.data``, so reading it here would put an erased address back into the
    signal index. Returning nothing also lets reconciliation retire the identity
    signals the case had before erasure, which is the behaviour erasure wants.
    """
    if _is_anonymized(case.data):
        return {}

    merged: dict = dict(case.data or {})

    for row in (await session.execute(
        select(CaseInstanceVariableModel)
        .where(CaseInstanceVariableModel.case_id == case.id)
    )).scalars().all():
        # Only text-valued variables can be an identity; _identity_values drops
        # non-strings anyway, but skipping them here avoids the churn.
        if row.value_text:
            merged[row.full_key] = row.value_text

    if getattr(case, "portal_submitter_email", None):
        merged["portal_submitter_email"] = case.portal_submitter_email

    # Belt and braces for a partially-scrubbed case (a redacted value without the
    # marker): a placeholder is not an identity, and hashing it would still make
    # every case carrying it collide.
    return {k: v for k, v in merged.items() if v not in _REDACTION_PLACEHOLDERS}


async def extract_signals(
    session: AsyncSession, case_id: uuid.UUID, *, commit: bool = False
) -> list[CaseRiskSignalModel]:
    """Extract this case's signals and upsert them.

    Idempotent: a unique index on (case_id, signal_type, signal_key) means
    re-running after new data arrives adds only what is new.

    Anything deliberately *not* extracted is recorded on the returned list as
    ``.skipped`` (see `SignalExtraction`) so a caller can say why nothing was
    added instead of reporting a silent zero.
    """
    case = await session.get(CaseInstanceModel, case_id)
    if case is None:
        return SignalExtraction()

    tenant_id = norm_tenant(case.tenant_id)
    prior = list((await session.execute(
        select(CaseRiskSignalModel).where(CaseRiskSignalModel.case_id == case_id)
    )).scalars().all())
    existing = {(r.signal_type, r.signal_key) for r in prior}
    added = SignalExtraction()
    skipped = added.skipped

    #: Everything this pass computed, so signals whose source value is gone can be
    #: retired. Extraction used to run once, at close, on final data — re-running
    #: it on a live case makes staleness real: ``_add`` only ever inserts, so a
    #: corrected email would leave BOTH digests attached forever, each inflating
    #: identity_velocity and matching other cases on a value this one no longer has.
    desired: set[tuple[str, str]] = set()
    #: Types this pass can authoritatively enumerate. A type is only reconciled
    #: when its whole source was readable — see the DEK skip below, where deleting
    #: on an unreadable vault would wipe a good index.
    reconcilable: set[str] = set()

    def _add(signal_type: str, key: str, *, hashed: bool, value: dict) -> None:
        desired.add((signal_type, key))
        if (signal_type, key) in existing:
            return
        row = CaseRiskSignalModel(
            tenant_id=tenant_id, case_id=case_id, signal_type=signal_type,
            signal_key=key, hashed=hashed, value={**value, EXTRACTED: True},
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
    # Every verification row for the case was just read, so this type is complete.
    reconcilable.add("doc_hash")

    # ── Identity signals: hashed with the tenant DEK, or skipped entirely.
    identity_values = _identity_values(await _identity_source_map(session, case))
    if identity_values:
        dek = await _tenant_dek(session, tenant_id)
        if dek is None:
            # Fail-closed is correct (an unkeyed digest is dictionary-attackable),
            # but silence here is not: extraction returns "added 0" with a 200 and
            # the whole engine stays permanently empty with nothing said. Name the
            # cause so a broken vault is not mistaken for a case with no signals.
            skipped.append(
                f"{len(identity_values)} identity signal(s) skipped: no tenant key "
                f"available (HxVault DEK could not be loaded)"
            )
        if dek:
            for sig_type, raw in identity_values:
                try:
                    key = signal_digest(raw, dek=dek, signal_type=sig_type)
                except ValueError:
                    continue          # empty after normalisation — nothing to match on
                _add(sig_type, key, hashed=True, value={"kind": sig_type})
            # Only with a usable key is the identity set knowable. Reconciling
            # without one would read "no identity values" and delete a perfectly
            # good index the moment the vault hiccuped.
            reconcilable |= HASHED_TYPES
    else:
        # No identity values anywhere on the case. Distinguish "the vault is shut"
        # from "there is nothing to hash": only the latter may retire signals, and
        # only if a key is obtainable, since an unreadable vault yields the same
        # empty list either way.
        if any(r.signal_type in HASHED_TYPES for r in prior):
            if await _tenant_dek(session, tenant_id) is not None:
                reconcilable |= HASHED_TYPES

    # Velocity ("N cases share this identity in a window") is DELIBERATELY not
    # a stored signal in P1. A stored count is frozen at extraction time and
    # goes stale as new cases arrive, and it is order-dependent — the first
    # case of a matching pair would never carry it. P2 computes velocity at
    # query time against the (already indexed) signal store instead, where it
    # is always current. The lookup index this needs exists today.

    # ── Retire signals whose source value is gone ────────────────────────────
    # Deletion, not a status flag: a signal is a derived fact, and once the value
    # it was derived from is off the case the row is simply wrong — leaving it
    # readable would keep matching other cases on it. Findings that cited it are
    # recomputed and staled by the same evaluate pass, so evidence never points at
    # a row that silently kept counting.
    #
    # Retirement is AUDITED. A signal is fraud evidence, and evidence that can be
    # removed without a trace is a way to launder a case: edit the value, let the
    # next evaluation drop the link, and nothing records that the link ever
    # existed. The audit row keeps the signal_type and a truncated digest — never
    # the raw value, which is exactly what the hashing is for — so the trail is
    # reconstructable without re-introducing the plaintext the table avoids.
    retired: list[dict] = []
    for row in prior:
        if row.signal_type not in reconcilable:
            continue
        if (row.signal_type, row.signal_key) in desired:
            continue
        if not isinstance(row.value, dict) or not row.value.get(EXTRACTED):
            continue                    # not ours — see EXTRACTED
        retired.append({"signal_type": row.signal_type,
                        "signal_key_prefix": row.signal_key[:12],
                        "hashed": bool(row.hashed)})
        await session.delete(row)
        added.removed.append(row.signal_type)

    if retired:
        session.add(CaseAuditLogModel(
            case_id=case_id,
            action="risk.signals.retired",
            actor_id="system",
            details={"reason": "source value no longer on case",
                     "count": len(retired)},
            previous_value={"signals": retired},
            new_value=None,
        ))

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
