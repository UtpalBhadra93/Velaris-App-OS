"""Voice form-fill (voice input P3) — propose field values from speech.

The API is deliberately one-way: extraction returns candidates and the case is
untouched. Accepting a value happens in the worker's form and saves through
that form's own path, so "never auto-commit" is a property of the surface
area rather than a rule this router has to enforce.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from case_service import hxguard
from case_service.auth.dependencies import get_current_user
from case_service.auth.models import AuthenticatedUser
from case_service.db.models import CaseInstanceModel
from case_service.db.session import get_session
from case_service.tenancy.identity import same_tenant
from case_service.hxnexus.guard import _RateLimiter
from case_service.voicefill import service as voicefill

router = APIRouter(prefix="/voicefill", tags=["voicefill"])

#: Extraction is a local-LLM pass over a whole transcript — far heavier than a
#: dictation clip, and the worker reviews each result before moving on.
_rate_limiter = _RateLimiter(max_calls=20, window_seconds=60)

MAX_TEXT_CHARS = 20_000


class ExtractRequest(BaseModel):
    """Either a dictation (`text`) or a sealed session transcript."""
    text:       str | None       = Field(default=None, max_length=MAX_TEXT_CHARS)
    session_id: uuid.UUID | None = None
    form_id:    uuid.UUID | None = None


def _require_flag() -> None:
    from case_service.api.routers.releases import is_feature_enabled

    if not is_feature_enabled("voice_fill"):
        raise HTTPException(404, "Not found")


async def _get_case_or_404(session: AsyncSession, case_id: uuid.UUID,
                           user: AuthenticatedUser) -> CaseInstanceModel:
    case = await session.get(CaseInstanceModel, case_id)
    if case is None or not await same_tenant(session, case.tenant_id, user.tenant_id):
        raise HTTPException(404, "Case not found")
    return case


@router.get("/status")
async def voicefill_status(
    user: AuthenticatedUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Should the UI offer voice fill at all? Same stance as the mic: an
    unavailable capability is absent, never a dead button."""
    _require_flag()
    from case_service.hxnexus.case_qa import tenant_ai_settings

    ai_cfg = await tenant_ai_settings(session, user.tenant_id)
    return {
        "available": bool(ai_cfg.get("case_qa", False)),
        "egress":    (ai_cfg.get("egress") or "local_only").lower(),
    }


@router.post("/cases/{case_id}/extract")
async def extract_fields(
    case_id: uuid.UUID,
    body: ExtractRequest,
    user: AuthenticatedUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Propose form-field values from a dictation or a session transcript.

    Returns candidates only — each with the field it targets, the verbatim
    quote it came from, who said it, and a confidence. The worker accepts them
    one at a time; this endpoint never writes a case value.
    """
    _require_flag()

    # Ahead of any DB or PDP work: a caller who is over quota must not be able
    # to spend the server's case lookups getting told so.
    allowed, retry_after = _rate_limiter.is_allowed(user.user_id)
    if not allowed:
        raise HTTPException(429, f"Too many extraction requests — retry in {retry_after}s")

    if body.text and body.session_id:
        raise HTTPException(400, "Provide either text or session_id, not both")

    case = await _get_case_or_404(session, case_id, user)
    await hxguard.require_case(session, user, "voicefill.extract", case_id)

    # Reading a transcript rides the same gate as viewing the recording.
    try:
        await hxguard.require_case(session, user, "meet.recording.view", case_id)
        can_view_transcripts = True
    except HTTPException:
        can_view_transcripts = False

    try:
        transcript, speakers, source = await voicefill.load_transcript(
            session, case, text=body.text, session_id=body.session_id,
            can_view_transcripts=can_view_transcripts)
    except PermissionError as e:
        raise HTTPException(403, str(e))
    except LookupError as e:
        raise HTTPException(404, str(e))

    try:
        catalogue = await voicefill.build_catalogue(session, case, body.form_id)
    except LookupError as e:
        raise HTTPException(404, str(e))

    try:
        result = await voicefill.extract(
            session, case=case, user=user, transcript=transcript,
            speakers=speakers, catalogue=catalogue)
    except PermissionError as e:          # tenant has not opted into case AI
        raise HTTPException(400, str(e))
    except RuntimeError as e:             # no backend — honest, never a silent []
        raise HTTPException(503, str(e))

    # The audit names WHICH fields were proposed, never their values: the
    # values are the transcript's content, and a rejected proposal must not
    # leave a copy of it behind. Accepted values audit themselves on save.
    from case_service.api.routers.cases import _audit
    await _audit(session, case_id, "voicefill_extracted", actor_id=user.user_id,
                 details={
                     "source":            source,
                     "session_id":        str(body.session_id) if body.session_id else None,
                     "form_id":           str(body.form_id) if body.form_id else None,
                     "fields_considered": result["fields_considered"],
                     "proposed_fields":   [c["field_key"] for c in result["candidates"]],
                     "external_ai":       result["external_ai"],
                 })
    await session.commit()

    return {"case_id": str(case_id), "source": source, **result}
