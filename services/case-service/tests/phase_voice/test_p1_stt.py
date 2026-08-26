"""Voice input P1 — the sovereign STT endpoint.

Pins the properties that keep dictation from becoming a recording feature or a
silent-degrade feature:
  * OFF unless the `voice_input` release flag is on (404, not 403 — no oracle);
  * honest 501 when no ASR engine is installed on the box, never a fake empty
    transcript (same contract as HxMeet intelligence / captions code 4501);
  * auth required, and a case-scoped clip goes through the case-level PDP with
    404 anti-oracle on an unknown/other-tenant case;
  * clip bounds enforced (too short / too long / odd byte count) BEFORE the
    engine is touched;
  * nothing is persisted — the clip dies with the request.

The ASR engine is stubbed throughout: these tests pin the endpoint contract,
not Whisper's accuracy, and CI boxes have no model installed.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import uuid

import pytest

from case_service.api.routers import stt
from case_service.meet import asr as meet_asr
from tests.conftest import create_case, deploy_case_type

STT = "/api/v1/stt"


def _pcm(seconds: float) -> bytes:
    """A silent PCM16 mono 16 kHz clip of the requested length."""
    return b"\x00" * (int(meet_asr.SAMPLE_RATE * seconds) * 2)


@pytest.fixture(autouse=True)
def _enable_voice_input():
    from case_service.api.routers.releases import _ENABLED_VERSIONS
    prior = _ENABLED_VERSIONS.get("voice_input")
    _ENABLED_VERSIONS["voice_input"] = "v1.0.0"
    yield
    if prior is None:
        _ENABLED_VERSIONS.pop("voice_input", None)
    else:
        _ENABLED_VERSIONS["voice_input"] = prior


@pytest.fixture
def asr_ready(monkeypatch):
    """An installed, loaded engine that transcribes to a fixed string."""
    calls: list[bytes] = []

    async def _fake_transcribe(pcm: bytes) -> str:
        calls.append(pcm)
        return "book the survey for tuesday"

    monkeypatch.setattr(meet_asr, "available", lambda: True)
    monkeypatch.setattr(meet_asr, "info", lambda: meet_asr.ASRInfo(
        engine="faster-whisper", device="cpu", model="small", realtime=False))
    monkeypatch.setattr(meet_asr, "transcribe_pcm16", _fake_transcribe)
    return calls


@pytest.fixture
def asr_absent(monkeypatch):
    """No engine installed — the honest-501 case."""
    monkeypatch.setattr(meet_asr, "available", lambda: False)
    monkeypatch.setattr(meet_asr, "info", lambda: None)


# ═══════════════════════════════════════════════════════
#  1. Flag gate
# ═══════════════════════════════════════════════════════

class TestFlagGate:
    """Default OFF. A disabled feature is invisible, not forbidden."""

    async def test_transcribe_404_when_flag_off(self, client, asr_ready):
        from case_service.api.routers.releases import _ENABLED_VERSIONS
        _ENABLED_VERSIONS.pop("voice_input", None)
        r = await client.post(f"{STT}/transcribe", content=_pcm(1.0))
        assert r.status_code == 404
        assert not asr_ready, "the engine must not be touched when the flag is off"

    async def test_status_404_when_flag_off(self, client):
        from case_service.api.routers.releases import _ENABLED_VERSIONS
        _ENABLED_VERSIONS.pop("voice_input", None)
        assert (await client.get(f"{STT}/status")).status_code == 404


# ═══════════════════════════════════════════════════════
#  2. Auth
# ═══════════════════════════════════════════════════════

class TestAuth:
    async def test_transcribe_requires_auth(self, anon_client, asr_ready):
        r = await anon_client.post(f"{STT}/transcribe", content=_pcm(1.0))
        assert r.status_code in (401, 403)

    async def test_status_requires_auth(self, anon_client):
        assert (await anon_client.get(f"{STT}/status")).status_code in (401, 403)


# ═══════════════════════════════════════════════════════
#  3. Honest 501 — never a silent degrade
# ═══════════════════════════════════════════════════════

class TestEngineAbsent:
    async def test_transcribe_501_without_engine(self, client, asr_absent):
        r = await client.post(f"{STT}/transcribe", content=_pcm(1.0))
        assert r.status_code == 501
        assert "not installed" in r.json()["detail"]

    async def test_status_reports_unavailable(self, client, asr_absent):
        body = (await client.get(f"{STT}/status")).json()
        assert body["available"] is False
        assert body["engine"] is None
        assert body["max_clip_seconds"] == stt.MAX_CLIP_SECONDS


# ═══════════════════════════════════════════════════════
#  4. Transcription contract
# ═══════════════════════════════════════════════════════

class TestTranscribe:
    async def test_returns_text_and_engine_identity(self, client, asr_ready):
        r = await client.post(f"{STT}/transcribe", content=_pcm(2.0))
        assert r.status_code == 200
        body = r.json()
        assert body["text"] == "book the survey for tuesday"
        assert body["engine"] == "faster-whisper"
        assert body["device"] == "cpu"
        assert body["realtime"] is False

    async def test_clip_reaches_the_engine_verbatim(self, client, asr_ready):
        clip = _pcm(1.5)
        await client.post(f"{STT}/transcribe", content=clip)
        assert asr_ready == [clip]

    async def test_status_reports_loaded_engine(self, client, asr_ready):
        body = (await client.get(f"{STT}/status")).json()
        assert body["available"] is True
        assert body["model"] == "small"
        assert body["realtime"] is False


# ═══════════════════════════════════════════════════════
#  5. Clip bounds — checked before the engine runs
# ═══════════════════════════════════════════════════════

class TestClipBounds:
    async def test_rejects_oversize_clip(self, client, asr_ready):
        r = await client.post(f"{STT}/transcribe", content=b"\x00" * (stt.MAX_PCM_BYTES + 2))
        assert r.status_code == 413
        assert not asr_ready

    async def test_rejects_too_short_clip(self, client, asr_ready):
        r = await client.post(f"{STT}/transcribe", content=_pcm(0.05))
        assert r.status_code == 400
        assert not asr_ready

    async def test_rejects_empty_body(self, client, asr_ready):
        r = await client.post(f"{STT}/transcribe", content=b"")
        assert r.status_code == 400
        assert not asr_ready

    async def test_rejects_odd_byte_count(self, client, asr_ready):
        """An odd length cannot be int16 frames — reject rather than truncate."""
        r = await client.post(f"{STT}/transcribe", content=_pcm(1.0) + b"\x00")
        assert r.status_code == 400
        assert not asr_ready


# ═══════════════════════════════════════════════════════
#  6. Case scoping — PDP only when a case is named
# ═══════════════════════════════════════════════════════

class TestCaseScoping:
    async def test_unknown_case_is_404(self, client, asr_ready):
        r = await client.post(f"{STT}/transcribe?case_id={uuid.uuid4()}", content=_pcm(1.0))
        assert r.status_code == 404
        assert not asr_ready

    async def test_malformed_case_id_is_404(self, client, asr_ready):
        r = await client.post(f"{STT}/transcribe?case_id=not-a-uuid", content=_pcm(1.0))
        assert r.status_code == 404
        assert not asr_ready

    async def test_authorized_case_transcribes(self, client, asr_ready):
        ct = await deploy_case_type(client, name=f"V {uuid.uuid4().hex[:6]}")
        case = await create_case(client, ct["id"])
        r = await client.post(f"{STT}/transcribe?case_id={case['id']}", content=_pcm(1.0))
        assert r.status_code == 200
        assert r.json()["text"] == "book the survey for tuesday"

    async def test_global_dictation_needs_no_case(self, client, asr_ready):
        """An AI-ask box has no case — the global path must not require one."""
        assert (await client.post(f"{STT}/transcribe", content=_pcm(1.0))).status_code == 200


# ═══════════════════════════════════════════════════════
#  7. Transcribe-and-discard
# ═══════════════════════════════════════════════════════

class TestNoPersistence:
    async def test_no_document_row_is_created(self, client, session, asr_ready):
        """Unlike HxMeet (which seals on purpose), dictation stores nothing."""
        from sqlalchemy import func, select

        from velaris_shared.db.models import DocumentModel

        before = (await session.execute(select(func.count(DocumentModel.id)))).scalar()
        await client.post(f"{STT}/transcribe", content=_pcm(2.0))
        await session.commit()      # see anything the request committed
        after = (await session.execute(select(func.count(DocumentModel.id)))).scalar()
        assert after == before


# ═══════════════════════════════════════════════════════
#  8. Rate limit
# ═══════════════════════════════════════════════════════

class TestRateLimit:
    async def test_burst_is_throttled(self, client, asr_ready, monkeypatch):
        """Transcription is serialized GPU/CPU work — a hot loop must 429
        rather than starve HxMeet captions."""
        from velaris_shared.llm.guard import _RateLimiter

        monkeypatch.setattr(stt, "_rate_limiter", _RateLimiter(max_calls=2, window_seconds=60))
        codes = [(await client.post(f"{STT}/transcribe", content=_pcm(1.0))).status_code
                 for _ in range(3)]
        assert codes == [200, 200, 429]


class TestTenantIsolation:
    async def test_other_tenants_case_is_404(self, client, asr_ready):
        """Fail closed, and say nothing: a case owned by another tenant is
        indistinguishable from one that does not exist."""
        from velaris_shared.db.models import CaseInstanceModel
        from tests.conftest import TestSessionFactory
        import uuid as _uuid

        ct = await deploy_case_type(client, name=f"V {uuid.uuid4().hex[:6]}")
        case = await create_case(client, ct["id"])
        async with TestSessionFactory() as db:
            row = await db.get(CaseInstanceModel, _uuid.UUID(case["id"]))
            row.tenant_id = _uuid.uuid4()          # somebody else's tenant
            db.add(row)
            await db.commit()

        r = await client.post(f"{STT}/transcribe?case_id={case['id']}", content=_pcm(1.0))
        assert r.status_code == 404
        assert not asr_ready, "the engine must not run for a case we cannot see"
