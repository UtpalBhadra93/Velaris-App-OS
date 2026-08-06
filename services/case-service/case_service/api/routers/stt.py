"""Voice input P1 — sovereign speech-to-text (local Whisper, transcribe-and-discard).

One endpoint: raw PCM in, text out. The audio never leaves the platform and is
never stored — this is deliberately NOT the HxMeet recording path, which seals
recordings on purpose. Nothing here writes a row or a file.

Wire format is raw little-endian int16 mono 16 kHz PCM, not a MediaRecorder
container: the ASR layer (``meet/asr.py``) auto-selects whisper.cpp on
AMD/Intel GPU boxes and pywhispercpp has no demuxer, so a webm body would
either fail on that path or drag in ffmpeg. The browser already produces this
exact format for HxMeet live captions (the ``vx-pcm`` worklet), so the client
side is shared, not new.

Model + engine are HxMeet's warm process-global singleton
(``meet_asr_live_model``) — no second model download, no third setting.

Authorization: dictation into a Studio AI box has no case, so the global path
is authenticated-user-only. When the caller names a case, the case-level PDP
decides (``stt.transcribe``). Honest 501 when no ASR engine is installed —
same contract as HxMeet intelligence, never a silent degrade.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, Request

from case_service import hxguard
from case_service.auth.dependencies import get_current_user
from case_service.auth.models import AuthenticatedUser
from case_service.db import repository as repo
from case_service.db.session import get_session
from case_service.tenancy.identity import same_tenant
from case_service.hxnexus.guard import _RateLimiter
from case_service.meet import asr as meet_asr
from sqlalchemy.ext.asyncio import AsyncSession

router = APIRouter(prefix="/stt", tags=["stt"])

#: Transcription is CPU/GPU work serialized behind one lock — a hot loop of
#: clips would starve HxMeet captions. Push-to-talk dictation is bursty but
#: nowhere near this ceiling.
_rate_limiter = _RateLimiter(max_calls=30, window_seconds=60)

#: Clip cap. Dictation is utterances, not recordings; 60 s of PCM16 @16 kHz is
#: ~1.9 MB, comfortably inside the 10 MB global body limit.
MAX_CLIP_SECONDS = 60
MAX_PCM_BYTES = MAX_CLIP_SECONDS * meet_asr.SAMPLE_RATE * 2

#: Below this there is no utterance to transcribe — reject instead of handing
#: Whisper silence to hallucinate over.
MIN_PCM_BYTES = int(meet_asr.SAMPLE_RATE * 2 * 0.2)


def _require_flag() -> None:
    from case_service.api.routers.releases import is_feature_enabled

    if not is_feature_enabled("voice_input"):
        raise HTTPException(404, "Not found")


@router.get("/status")
async def stt_status(user: AuthenticatedUser = Depends(get_current_user)) -> dict:
    """Can this box transcribe, and how fast? The mic UI uses this to decide
    whether to render at all and whether to warn about CPU lag mode."""
    _require_flag()
    info = meet_asr.info()
    return {
        "available":       meet_asr.available(),
        "engine":          info.engine if info else None,
        "device":          info.device if info else None,
        "model":           info.model if info else None,
        "realtime":        bool(info.realtime) if info else None,
        "max_clip_seconds": MAX_CLIP_SECONDS,
    }


@router.post("/transcribe")
async def transcribe(
    request: Request,
    case_id: str | None = None,
    user: AuthenticatedUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Transcribe one PCM16 clip. Returns text; keeps nothing."""
    _require_flag()

    # Ahead of the case lookup and the PDP: a caller who is over quota must not
    # be able to spend the server's DB work getting told so.
    allowed, retry_after = _rate_limiter.is_allowed(user.user_id)
    if not allowed:
        raise HTTPException(429, f"Too many transcription requests — retry in {retry_after}s")

    if case_id is not None:
        try:
            cid = uuid.UUID(case_id)
        except ValueError:
            raise HTTPException(404, "Case not found")
        case = await repo.get_case_instance(session, cid)
        if case is None or not await same_tenant(session, case.tenant_id, user.tenant_id):
            raise HTTPException(404, "Case not found")
        await hxguard.require_case(session, user, "stt.transcribe", cid)

    if not meet_asr.available():
        raise HTTPException(501, "Speech recognition is not installed on this server")

    pcm = await request.body()
    if len(pcm) > MAX_PCM_BYTES:
        raise HTTPException(413, f"Clip exceeds the {MAX_CLIP_SECONDS}s limit")
    if len(pcm) < MIN_PCM_BYTES or len(pcm) % 2:
        raise HTTPException(400, "Body must be at least 0.2s of 16 kHz mono PCM16 audio")

    text = await meet_asr.transcribe_pcm16(pcm)
    del pcm  # the clip dies with the request — nothing is stored

    info = meet_asr.info()
    return {
        "text":     text,
        "engine":   info.engine if info else None,
        "device":   info.device if info else None,
        "realtime": bool(info.realtime) if info else None,
    }
