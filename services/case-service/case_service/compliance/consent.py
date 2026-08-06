"""HxConsent — consent & purpose-limitation ledger (§8.1).

Article 15/17 is reactive — it answers "what do you hold on me" afterwards.
This is proactive: it records *why* each use of a subject's data was lawful, at
the moment it became lawful.

Three decisions worth stating, because each is easy to get subtly wrong:

**Append-only.** A withdrawal is a new row, never an update. The question a
regulator asks is "was this use lawful **at the time**", and an overwritten row
cannot answer it. Current state is the latest row per (subject, purpose).

**Lawful basis is not a synonym for consent.** Contract, legal obligation and
legitimate interest are bases that do not require consent and cannot be
withdrawn by the subject. Collapsing them into a boolean would misstate the
legal position, so `basis` is recorded alongside every decision and a
withdrawal against a non-consent basis is refused.

**The purpose vocabulary is closed.** An unknown purpose is rejected rather
than stored, because `has_consent(subject, "marketng")` returning False forever
is a silent compliance failure that looks exactly like a lawful refusal.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from case_service.db.models import ConsentRecordModel

logger = logging.getLogger(__name__)

# ── Purposes (closed vocabulary) ──────────────────────────────────
CASE_PROCESSING = "case_processing"
MARKETING = "marketing"
RECORDING = "recording"
BIOMETRIC = "biometric"
VOICE_PROCESSING = "voice_processing"
AI_PROCESSING = "ai_processing"
DATA_SHARING = "data_sharing"

PURPOSES = frozenset({
    CASE_PROCESSING, MARKETING, RECORDING, BIOMETRIC,
    VOICE_PROCESSING, AI_PROCESSING, DATA_SHARING,
})

#: Purposes for which the ledger is a **control**, not just evidence: a
#: processing path consults `has_consent()` and refuses without a lawful basis.
#:
#: Deliberately much smaller than PURPOSES, and `CASE_PROCESSING` is
#: deliberately absent — case correspondence is lawful under contract or legal
#: obligation, so gating it on a consent record would stop the platform doing
#: the job it was engaged to do, and would misstate the legal position besides.
#:
#: `RECORDING` and `BIOMETRIC` are absent for a different reason: HxMeet
#: already enforces them at the participant row before a recording starts
#: (see `meet/service.py::_bridge_to_consent_ledger`). A second mechanism that
#: can disagree with the first about whether processing was lawful is worse
#: than one.
#:
#: `VOICE_PROCESSING` is absent for a third reason, and permanently: the
#: subject of a voice recording is the speaker, and diarisation identifies
#: speakers only as "Speaker 1"/"Speaker 2". There is no identity to look up,
#: so a gate there would resolve the same way for everyone regardless of what
#: they consented to — worse than no gate, because it would look like one.
#:
#: Adding a purpose here turns an existing recorded decision into an
#: enforcement point — check that every caller of the gated path passes a
#: subject the ledger actually knows, or every send starts failing closed.
#: `allows_subject` is the shared rule for the paths where the subject may
#: legitimately be absent.
GATED_PURPOSES = frozenset({MARKETING, DATA_SHARING, AI_PROCESSING})

# ── Lawful bases (GDPR Art. 6) ────────────────────────────────────
CONSENT = "consent"
CONTRACT = "contract"
LEGAL_OBLIGATION = "legal_obligation"
LEGITIMATE_INTEREST = "legitimate_interest"
VITAL_INTERESTS = "vital_interests"
PUBLIC_TASK = "public_task"

BASES = frozenset({
    CONSENT, CONTRACT, LEGAL_OBLIGATION,
    LEGITIMATE_INTEREST, VITAL_INTERESTS, PUBLIC_TASK,
})

#: Only these can be withdrawn by the subject. Withdrawing a legal obligation
#: is not a thing a subject can do, and pretending otherwise would produce a
#: ledger that claims processing stopped when it lawfully continued.
WITHDRAWABLE_BASES = frozenset({CONSENT})

GRANTED = "granted"
WITHDRAWN = "withdrawn"

#: Article 9 special-category purposes — explicit consent required, never
#: implied by a broader grant.
EXPLICIT_ONLY = frozenset({BIOMETRIC})


class ConsentError(ValueError):
    """The record would misstate the legal position — refused."""


class ProcessingRefused(Exception):
    """A gated processing path has no lawful basis for its subject.

    Raised rather than returning an empty or partial result, so a caller
    cannot mistake "refused on legal grounds" for "produced nothing".
    Routers translate it to `VEL-CONSENT-REFUSED-403`.

    `mail.service.ConsentRefused` is the email-specific ancestor of this and
    stays where it is — it carries the dropped address list, which only makes
    sense for a multi-recipient send.
    """

    def __init__(self, purpose: str, reason: str, subject: str | None = None) -> None:
        self.purpose = purpose
        self.reason = reason
        self.subject = subject
        super().__init__(f"no lawful basis for {purpose!r} ({reason})")


def _normalise_subject(subject_ref: str) -> str:
    """Subjects are matched case-insensitively.

    Email is the identifier in the portal path, and `Alice@x.com` withdrawing
    consent must affect `alice@x.com`'s record or the withdrawal silently does
    nothing.
    """
    return (subject_ref or "").strip().lower()


async def record(
    session: AsyncSession,
    *,
    subject_ref: str,
    purpose: str,
    action: str = GRANTED,
    basis: str = CONSENT,
    subject_type: str = "email",
    source: str = "api",
    evidence: dict[str, Any] | None = None,
    case_id: uuid.UUID | None = None,
    tenant_id: str | None = None,
    actor_id: str | None = None,
) -> ConsentRecordModel:
    """Append one consent decision. Never updates an existing row."""
    subject = _normalise_subject(subject_ref)
    if not subject:
        raise ConsentError("subject_ref is required")
    if purpose not in PURPOSES:
        raise ConsentError(
            f"{purpose!r} is not a known purpose. A typo here would make every "
            f"later check return 'no consent' indistinguishably from a real "
            f"refusal. Known: {', '.join(sorted(PURPOSES))}"
        )
    if basis not in BASES:
        raise ConsentError(f"{basis!r} is not a lawful basis. Known: {', '.join(sorted(BASES))}")
    if action not in (GRANTED, WITHDRAWN):
        raise ConsentError(f"action must be {GRANTED} or {WITHDRAWN}")
    if action == WITHDRAWN and basis not in WITHDRAWABLE_BASES:
        raise ConsentError(
            f"a {basis!r} basis cannot be withdrawn by the subject — only "
            f"{', '.join(sorted(WITHDRAWABLE_BASES))} can. Recording this would "
            f"claim processing stopped when it lawfully continues."
        )

    row = ConsentRecordModel(
        subject_ref=subject, subject_type=subject_type, purpose=purpose,
        action=action, basis=basis, source=source, evidence=evidence or {},
        case_id=case_id, tenant_id=tenant_id, actor_id=actor_id,
    )
    session.add(row)
    await session.flush()
    return row


async def current_state(
    session: AsyncSession, subject_ref: str, *, tenant_id: str | None = None,
) -> dict[str, dict[str, Any]]:
    """Latest decision per purpose for a subject.

    Derived from the ledger rather than stored, so it cannot drift from the
    evidence that backs it.
    """
    subject = _normalise_subject(subject_ref)
    q = (
        select(ConsentRecordModel)
        .where(ConsentRecordModel.subject_ref == subject)
        .order_by(ConsentRecordModel.recorded_at)
    )
    if tenant_id:
        q = q.where(ConsentRecordModel.tenant_id == tenant_id)

    state: dict[str, dict[str, Any]] = {}
    for row in (await session.execute(q)).scalars().all():
        state[row.purpose] = {
            "purpose": row.purpose,
            "granted": row.action == GRANTED,
            "basis": row.basis,
            "source": row.source,
            "at": row.recorded_at.isoformat() if row.recorded_at else None,
        }
    return state


async def has_consent(
    session: AsyncSession, subject_ref: str, purpose: str,
    *, tenant_id: str | None = None,
) -> bool:
    """Whether processing for `purpose` is currently lawful for this subject.

    **Fails closed.** An unknown purpose, an unknown subject or no record at
    all returns False — the absence of a recorded basis is not permission.
    """
    if purpose not in PURPOSES:
        logger.warning("consent check for unknown purpose %r — refusing", purpose)
        return False

    state = await current_state(session, subject_ref, tenant_id=tenant_id)
    entry = state.get(purpose)
    if entry is None:
        return False
    # A non-consent basis is lawful without the subject's agreement, so it
    # remains true unless the basis itself was withdrawn (which is refused
    # above for non-withdrawable bases).
    return bool(entry["granted"])


#: Returned by `allows_subject` when the thing being processed has no
#: identified data subject at all.
NO_SUBJECT = "no_subject"


async def allows_subject(
    session: AsyncSession, subject_ref: str | None, purpose: str,
    *, tenant_id: str | None = None,
) -> tuple[bool, str]:
    """The gate rule shared by every subject-scoped enforcement point.

    Returns `(allowed, reason)`. The reason is for the audit trail and the
    operator-facing skip message: "refused" and "there was nobody to ask" must
    not look the same afterwards.

    **A missing subject is not a refusal.** `case_instances.portal_submitter_email`
    is NULL on every staff-created case, so a gate that failed closed on an
    absent subject would disable AI and warehouse sync for most of the platform
    while protecting nobody — there is no person there whose consent could be
    required. Decided 2026-08-06.

    The limit is worth stating plainly: a staff-created case can still contain
    personal data about someone the platform never identified. This rule does
    not protect that person, because nothing here knows who they are. Finding
    them is HxPrivacy's job, not the ledger's.

    Everything else fails closed, exactly as `has_consent` does.
    """
    subject = (subject_ref or "").strip()
    if not subject:
        return True, NO_SUBJECT
    if await has_consent(session, subject, purpose, tenant_id=tenant_id):
        return True, "granted"
    return False, "no_lawful_basis"


async def allows_case_purpose(
    session: AsyncSession, case_id, purpose: str, *, tenant_id: str | None = None,
) -> tuple[bool, str]:
    """`allows_subject`, with the subject resolved from a case.

    The data subject of a case is its portal submitter. A staff-created case
    has none, and is allowed through — see `allows_subject` for why, and for
    what that does not cover.

    A case that cannot be loaded is refused: an unresolvable case is not
    evidence of a lawful basis.
    """
    from case_service.db.models import CaseInstanceModel

    case = await session.get(CaseInstanceModel, case_id)
    if case is None:
        return False, "unknown_case"
    return await allows_subject(
        session, case.portal_submitter_email, purpose, tenant_id=tenant_id,
    )


async def require_case_purpose(
    session: AsyncSession, case, purpose: str, *, tenant_id: str | None = None,
) -> None:
    """Enforce `allows_case_purpose` on a request path — raise or return.

    Takes the loaded case rather than an id: every caller already has it, and
    re-fetching would add a query to the hot path for no new information.

    Fails closed on an error in the check itself. A gate that opens when the
    ledger is unreachable is not a gate.
    """
    subject = getattr(case, "portal_submitter_email", None)
    try:
        allowed, reason = await allows_subject(
            session, subject, purpose,
            tenant_id=tenant_id or getattr(case, "tenant_id", None),
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("consent check failed for %s — refusing: %s", purpose, exc)
        allowed, reason = False, "check_failed"
    if not allowed:
        raise ProcessingRefused(purpose, reason, subject)


async def history(
    session: AsyncSession, subject_ref: str, *, tenant_id: str | None = None,
    limit: int = 500,
) -> list[ConsentRecordModel]:
    """Full ledger for a subject — the Article 15 answer."""
    q = (
        select(ConsentRecordModel)
        .where(ConsentRecordModel.subject_ref == _normalise_subject(subject_ref))
        .order_by(ConsentRecordModel.recorded_at.desc())
        .limit(max(1, min(int(limit), 1000)))
    )
    if tenant_id:
        q = q.where(ConsentRecordModel.tenant_id == tenant_id)
    return list((await session.execute(q)).scalars().all())
