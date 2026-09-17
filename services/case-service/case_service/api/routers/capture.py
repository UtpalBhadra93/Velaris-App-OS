"""F-06 — the ambient capture API.

Four routes, and the shape of the feature is in them: you ask for a source to
be read, you look at what it proposed, and you answer each proposal. Nothing
here writes case data on its own — `decide` applies an accepted change through
the ordinary variable path as the person who confirmed it.

Every route is **case-scoped**. The gates are ReBAC (`capture.read` /
`capture.run` / `capture.decide`) rather than privileges, matching the rest of
the case surface: whether you may capture from a case follows from your
relationship to that case, not from a role held platform-wide.

A refusal is a **201/200 with a reason**, never a 4xx. "Capture is off for this
tenant", "there is no lawful basis for AI processing" and "that meeting was
never analysed" are the gates working, and a client that has to parse an error
string to tell them apart will eventually stop trying.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import logging
import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession

from velaris_shared import hxguard
from velaris_shared.auth.dependencies import get_current_user
from velaris_shared.auth.models import AuthenticatedUser
from velaris_shared.db.models import CaptureProposalModel
from velaris_shared.db.session import get_session

from case_service.capture import service as capture
from case_service.capture import sources as src
from case_service.core.timestamps import iso_utc

log = logging.getLogger(__name__)

router = APIRouter(prefix="/capture", tags=["capture"])


class _Strict(BaseModel):
    """Unknown fields are refused, not ignored."""
    model_config = ConfigDict(extra="forbid")


class RunIn(_Strict):
    source_kind: str = Field(min_length=1, max_length=20)
    #: The session id or email message id. Omitted for a dictated note, whose
    #: text arrives in `text` and is never stored anywhere else.
    source_ref: str | None = Field(default=None, max_length=255)
    text: str | None = Field(default=None, max_length=40_000)


class DecideIn(_Strict):
    decision: str = Field(min_length=1, max_length=10)
    #: Required for `edit`, ignored otherwise. The human's value, kept beside
    #: the model's — which is what makes capture's real accuracy measurable.
    value: Any = None


def _run_json(row) -> dict[str, Any]:
    return {
        "id": str(row.id),
        "case_id": str(row.case_id),
        "source": {"kind": row.source_kind, "ref": row.source_ref,
                   "locator": row.source_locator, "sha256": row.source_sha256},
        "status": row.status,
        "refusal_reason": row.refusal_reason,
        "error": row.error,
        "provider": row.provider,
        "model": row.model,
        "prompt_sha256": row.prompt_sha256,
        # Surfaced: a run that only parsed on the repair round is worth a
        # closer read, and a source that always needs two is worth fixing.
        "attempts": row.attempts,
        "proposed": row.proposed_count,
        # Surfaced, not hidden. "It suggested nine things and six were outside
        # what capture may touch" is the sentence that tells an operator their
        # extraction is drifting — and a dropped proposal leaves no row, so
        # this count is the only trace it existed.
        "dropped": row.dropped_count,
        "requested_by": row.requested_by,
        "created_at": iso_utc(row.created_at),
        "completed_at": iso_utc(row.completed_at),
    }


def _proposal_json(row, names: dict[str, str] | None = None) -> dict[str, Any]:
    return {
        "id": str(row.id),
        "run_id": str(row.run_id),
        "case_id": str(row.case_id),
        "kind": row.kind,
        "target": row.target,
        # Unboxed on the way out: the storage wrapper is an artefact of the
        # JSON column, never part of the contract. See `capture.service.box`.
        "current_value": capture.unbox(row.current_value),
        "proposed_value": capture.unbox(row.proposed_value),
        # NULL stays NULL: "the model reported no confidence" and "the model is
        # not confident" are opposite readings of the same number.
        "confidence": (float(row.confidence) if row.confidence is not None else None),
        "snippet": row.snippet,
        "locator": row.locator,
        "status": row.status,
        "applied_value": capture.unbox(row.applied_value),
        "decided_by": row.decided_by,
        # The id stays — it is the durable key an auditor follows. The name is
        # added beside it because a screen whose whole job is accountability
        # cannot say "accepted by 60b34a70-9b1b-…" and expect to be read. An
        # unresolvable id (a deleted account, a system label) reports None
        # rather than inventing a name for it.
        "decided_by_name": (names or {}).get(str(row.decided_by or "")) or None,
        "decided_at": iso_utc(row.decided_at),
        "ledger_record_id": (str(row.ledger_record_id)
                             if row.ledger_record_id else None),
        "created_at": iso_utc(row.created_at),
    }


async def _decider_names(session: AsyncSession, rows) -> dict[str, str]:
    """Display names for whoever answered these proposals.

    Best effort by design: a name that cannot be resolved is simply absent, and
    the screen falls back to the id. A failure to pretty-print must never take
    the digest itself down.
    """
    ids: set[uuid.UUID] = set()
    for row in rows:
        if not row.decided_by:
            continue
        try:
            ids.add(uuid.UUID(str(row.decided_by)))
        except (ValueError, AttributeError, TypeError):
            continue          # a system label, not a user id
    if not ids:
        return {}
    from sqlalchemy import select

    from velaris_shared.db.models import HelixUserModel
    try:
        found = (await session.execute(
            select(HelixUserModel.id, HelixUserModel.display_name,
                   HelixUserModel.username)
            .where(HelixUserModel.id.in_(ids)))).all()
    except Exception as exc:                                   # noqa: BLE001
        log.warning("capture: decider names unresolved: %s", exc)
        return {}
    return {str(uid): (display or username or "") for uid, display, username in found}


@router.get("/cases/{case_id}")
async def case_digest(
    case_id: uuid.UUID,
    include_decided: bool = Query(False),
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(get_current_user),
) -> dict[str, Any]:
    """What is waiting on a person for this case, and where it came from.

    Also reports whether capture is switched on at all: a client looking at an
    empty digest should learn here that the feature is off for this tenant,
    rather than reading "no meetings yet" into it.
    """
    from velaris_shared.db.models import CaseInstanceModel

    await hxguard.require_case(session, user, "capture.read", case_id)
    case = await session.get(CaseInstanceModel, case_id)
    if case is None:
        raise HTTPException(404, "VEL-CAPTURE-CASE-404: no such case")

    out = await capture.digest(session, case_id=case_id,
                               include_decided=include_decided)
    names = await _decider_names(session, out["proposals"])
    return {
        "case_id": str(case_id),
        "enabled": await capture.enabled_for(session, case.tenant_id),
        "source_kinds": sorted(src.SOURCE_KINDS),
        "proposal_kinds": sorted(capture.PROPOSAL_KINDS),
        "runs": [_run_json(r) for r in out["runs"]],
        "proposals": [_proposal_json(p, names) for p in out["proposals"]],
    }


@router.get("/cases/{case_id}/targets")
async def case_targets(
    case_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(get_current_user),
) -> dict[str, Any]:
    """Exactly what capture may propose on this case, and nothing else.

    Published because the whitelist is the security story: a reviewer who can
    see the list can see that `auth`, `policy` and deployment surfaces are not
    on it — and are not on it by construction, not by a prompt asking nicely.
    """
    from velaris_shared.db.models import CaseInstanceModel

    await hxguard.require_case(session, user, "capture.read", case_id)
    case = await session.get(CaseInstanceModel, case_id)
    if case is None:
        raise HTTPException(404, "VEL-CAPTURE-CASE-404: no such case")
    targets = await capture.allowed_targets(session, case)
    return {
        "case_id": str(case_id),
        "targets": [{"key": k, **v} for k, v in sorted(targets.items())],
        "blocked_namespaces": sorted(capture.BLOCKED_NAMESPACES),
        "max_proposals": capture.MAX_PROPOSALS,
    }


@router.post("/cases/{case_id}/runs", status_code=201)
async def create_run(
    case_id: uuid.UUID,
    payload: RunIn,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(get_current_user),
) -> dict[str, Any]:
    """Read one source and propose what it says. Writes no case data.

    A refusal comes back as a 201 whose `status` is `refused` and whose
    `refusal_reason` names the gate — capture off, no lawful basis, nothing to
    read. Those are not faults, and a 4xx would file them next to real ones.
    """
    await hxguard.require_case(session, user, "capture.run", case_id)
    try:
        row = await capture.run(
            session, case_id=case_id, kind=payload.source_kind,
            ref=payload.source_ref, text=payload.text,
            requested_by=str(user.user_id))
    except capture.CaptureRefused as exc:
        if exc.reason == "no_such_case":
            raise HTTPException(404, "VEL-CAPTURE-CASE-404: no such case") from exc
        await session.rollback()
        return {"status": "refused", "refusal_reason": exc.reason,
                "detail": str(exc), "case_id": str(case_id)}
    await session.commit()
    return _run_json(row)


@router.post("/proposals/{proposal_id}/decide")
async def decide_proposal(
    proposal_id: uuid.UUID,
    payload: DecideIn,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(get_current_user),
) -> dict[str, Any]:
    """Accept, edit or reject one proposal — applied as the person deciding.

    The case check is resolved from the proposal rather than trusted from the
    path: a proposal id is not a capability, and the caller's relationship to
    the case it belongs to is what decides.
    """
    row = await session.get(CaptureProposalModel, proposal_id)
    if row is None:
        # 404 and not 403 on somebody else's proposal, matching the isolation
        # posture the rest of the platform settled on: a 403 confirms the id
        # exists, which is itself a leak.
        raise HTTPException(404, "VEL-CAPTURE-PROPOSAL-404: no such proposal")
    await hxguard.require_case(session, user, "capture.decide", row.case_id)

    privileged = bool(getattr(user, "is_admin", False))
    try:
        decided = await capture.decide(
            session, proposal_id=proposal_id, decision=payload.decision,
            actor_id=str(user.user_id), value=payload.value,
            privileged=privileged)
    except capture.CaptureError as exc:
        await session.rollback()
        raise HTTPException(422, f"VEL-CAPTURE-DECIDE-422: {exc}") from exc
    await session.commit()
    return _proposal_json(decided, await _decider_names(session, [decided]))
