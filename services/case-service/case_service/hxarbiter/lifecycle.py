"""HxArbiter ARB-F — the draft state machine, and four-eyes.

    drafted ──▶ analyzed ──▶ readback_pending ──▶ approved ──▶ activated
                    │                                 │
                    └── rejected ◀────────────────────┘

Two rules here are the whole control, and both are easy to erode:

**Four-eyes: the approver may not be the author.** A policy the platform obeys,
written and put into effect by one person, is not reviewed. OD-4 settles what to
do on a single-admin install — self-approval is permitted after a **cooling-off
delay**, with a louder audit event — because hard-requiring a second admin
bricks the many installs that have exactly one. The delay is what turns "click
twice" into a decision somebody slept on, and it is unimplementable without
`approved_at`, which is why that column exists.

**A draft cannot skip analysis.** Approval requires that the deterministic
analyzer has run and found no errors. An operator may accept warnings — a
conflict is often intended — but a contradiction means the rule can never fire,
and approving a rule that cannot fire produces a control that exists only on
the screen.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta, timezone
from enum import StrEnum

from sqlalchemy.ext.asyncio import AsyncSession

from velaris_shared.db.models import ArbiterDraftModel

log = logging.getLogger(__name__)


class DraftState(StrEnum):
    DRAFTED = "drafted"
    ANALYZED = "analyzed"
    READBACK_PENDING = "readback_pending"
    APPROVED = "approved"
    ACTIVATED = "activated"
    REJECTED = "rejected"


_ALLOWED: dict[DraftState, set[DraftState]] = {
    DraftState.DRAFTED: {DraftState.ANALYZED, DraftState.REJECTED},
    DraftState.ANALYZED: {DraftState.READBACK_PENDING, DraftState.REJECTED},
    DraftState.READBACK_PENDING: {DraftState.APPROVED, DraftState.REJECTED},
    DraftState.APPROVED: {DraftState.ACTIVATED, DraftState.REJECTED},
    DraftState.ACTIVATED: set(),
    DraftState.REJECTED: set(),
}

#: How long a single-admin install must wait between authoring and approving.
#: Not security theatre: the point is that the second look happens on a
#: different train of thought from the first.
SELF_APPROVAL_COOLING_OFF = timedelta(minutes=10)


class LifecycleError(RuntimeError):
    """The transition is not permitted. Callers map it to 409."""


def can_transition(current: DraftState, target: DraftState) -> bool:
    return target in _ALLOWED.get(current, set())


def require_transition(current: DraftState, target: DraftState) -> None:
    if not can_transition(current, target):
        raise LifecycleError(
            f"a draft cannot go from {current} to {target}")


def check_four_eyes(
    *,
    author: uuid.UUID | None,
    approver: uuid.UUID | None,
    authored_at: datetime,
    now: datetime | None = None,
    allow_self_approval: bool = True,
    cooling_off: timedelta = SELF_APPROVAL_COOLING_OFF,
) -> str:
    """Returns "two_person" or "self_cooling_off". Raises otherwise.

    An **unidentified approver is refused outright**. Four-eyes is an
    attribution control before it is a counting one: "somebody approved this"
    with no name is not a second pair of eyes, and it is exactly what a service
    token would produce.
    """
    if approver is None:
        raise LifecycleError(
            "the approver could not be identified; a policy cannot be approved "
            "by an unattributable principal")

    if author is not None and approver != author:
        return "two_person"

    if not allow_self_approval:
        raise LifecycleError(
            "this tenant requires a second administrator to approve a policy")

    now = now or datetime.now(timezone.utc)
    if authored_at.tzinfo is None:
        authored_at = authored_at.replace(tzinfo=timezone.utc)
    waited = now - authored_at
    if waited < cooling_off:
        remaining = cooling_off - waited
        raise LifecycleError(
            f"self-approval requires a cooling-off period; "
            f"{int(remaining.total_seconds())}s remaining. A policy the "
            f"platform obeys, written and enacted in one sitting by one person, "
            f"has not been reviewed.")
    return "self_cooling_off"


def blocking_findings(analysis: list[dict] | None) -> list[dict]:
    """Findings that must be resolved before approval.

    Errors only. A warning — a conflict, a shadowed rule — is frequently the
    author's intent, and blocking on those would train people to disable the
    analyzer. An error means a rule cannot fire, which nobody intends.
    """
    return [f for f in (analysis or []) if f.get("severity") == "error"]


async def transition(
    session: AsyncSession,
    draft: ArbiterDraftModel,
    target: DraftState,
    *,
    approver: uuid.UUID | None = None,
    allow_self_approval: bool = True,
) -> str | None:
    """Move a draft, enforcing the machine and (on approval) four-eyes."""
    current = DraftState(draft.state)
    require_transition(current, target)

    mode: str | None = None
    if target is DraftState.APPROVED:
        errors = blocking_findings(draft.analysis_report)
        if errors:
            raise LifecycleError(
                "this draft has "
                f"{len(errors)} error-level finding(s) and cannot be approved: "
                + "; ".join(e.get("message", "") for e in errors))
        if not (draft.readback or "").strip():
            raise LifecycleError(
                "a draft cannot be approved before its readback exists — the "
                "readback is what gets signed off on")
        mode = check_four_eyes(
            author=draft.created_by, approver=approver,
            authored_at=draft.created_at,
            allow_self_approval=allow_self_approval)
        if mode == "self_cooling_off":
            log.warning(
                "hxarbiter: draft %s SELF-APPROVED after cooling-off by %s "
                "(no second administrator was involved)", draft.id, approver)

    draft.state = str(target)
    draft.updated_at = datetime.now(timezone.utc)
    await session.flush()
    return mode
