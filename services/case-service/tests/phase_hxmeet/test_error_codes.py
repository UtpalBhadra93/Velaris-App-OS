"""Error-catalog phase 5 — `VEL-MEET-*` codes on HxMeet.

Two things are worth pinning here, and they pull in opposite directions.

**Split what has different fixes.** Six sites answered "not configured / not
installed" identically across four different missing things — LiveKit
credentials, an egress directory, and two independent pip extras. Four codes,
because "set the LiveKit secret" and "pip install .[kyc]" are not the same
instruction. The tenant opt-ins sit next to them at 400 rather than 501 for the
same reason: the software is installed and an admin has not turned it on.

**Merge what must stay indistinguishable.** The guest endpoints are public and
answer a uniform 404 for six different invite failures, on purpose. A code per
failure would hand an anonymous caller the oracle the message denies them.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio

from velaris_shared.config import get_settings
from velaris_shared.core import error_codes as ec
from velaris_shared.db.models import CaseSessionParticipantModel
from case_service.meet import service as meet_service
from tests.conftest import create_case, deploy_case_type

MEET = "/api/v1/meet"


def _code(response) -> str | None:
    return (response.json().get("error") or {}).get("code")


@pytest_asyncio.fixture
async def embedded_cfg(tmp_path):
    """Embedded driver, LiveKit configured, recordings dir present."""
    s = get_settings()
    prior = (s.meet_driver, s.livekit_url, s.livekit_api_key,
             s.livekit_api_secret, s.meet_recordings_dir)
    s.meet_driver = "embedded"
    s.livekit_url = "wss://livekit.test"
    s.livekit_api_key = "lk_test_key"
    s.livekit_api_secret = "lk_test_secret_of_sufficient_length"
    s.meet_recordings_dir = str(tmp_path)
    yield s
    (s.meet_driver, s.livekit_url, s.livekit_api_key,
     s.livekit_api_secret, s.meet_recordings_dir) = prior


async def _recorded_session(client) -> dict:
    ct = await deploy_case_type(client, name=f"Code CT {uuid.uuid4().hex[:6]}")
    case = await create_case(client, ct["id"])
    r = await client.post(f"{MEET}/cases/{case['id']}/sessions",
                          json={"title": "KYC call", "record": True})
    assert r.status_code == 201, r.text
    return r.json()


async def _post_webhook(client, event: dict):
    """Mirror of the P3 helper — LiveKit webhooks are signature-verified."""
    from tests.phase_hxmeet.test_p3_recording import _post_webhook as inner
    return await inner(client, event)


# ── The four 501s answer four different questions ─────────────────

class TestDeploymentShape:
    async def test_missing_livekit_credentials_is_a_driver_fault(
        self, client, embedded_cfg,
    ):
        embedded_cfg.livekit_api_secret = ""
        ct = await deploy_case_type(client, name=f"D {uuid.uuid4().hex[:6]}")
        case = await create_case(client, ct["id"])

        r = await client.post(f"{MEET}/cases/{case['id']}/sessions", json={})
        assert r.status_code == 501
        assert _code(r) == ec.MEET_DRIVER_501

    async def test_missing_recordings_dir_is_a_recording_fault(
        self, client, embedded_cfg,
    ):
        """The distinction that matters: LiveKit is perfectly configured here,
        and recording still cannot run. Same 501 before, different fix."""
        embedded_cfg.meet_recordings_dir = ""
        ct = await deploy_case_type(client, name=f"R {uuid.uuid4().hex[:6]}")
        case = await create_case(client, ct["id"])

        r = await client.post(f"{MEET}/cases/{case['id']}/sessions",
                              json={"record": True})
        assert r.status_code == 501
        assert _code(r) == ec.MEET_RECORDING_501

    async def test_the_two_501s_are_not_the_same_code(self):
        """Guards the collapse this design deliberately rejected."""
        assert ec.MEET_DRIVER_501 != ec.MEET_RECORDING_501
        assert len({ec.MEET_DRIVER_501, ec.MEET_RECORDING_501,
                    ec.MEET_ASR_501, ec.MEET_KYC_501}) == 4


# ── Opt-in is 400, not 501 ────────────────────────────────────────

class TestTenantOptIn:
    async def test_kyc_disabled_reports_optin_not_missing_software(
        self, client, session, embedded_cfg,
    ):
        """400 + OPTIN, never 501: the code is installed, an admin has not
        switched it on. Telling an operator to pip install would waste the
        one thing an error code is for."""
        sess = await _recorded_session(client)

        r = await client.post(f"{MEET}/sessions/{sess['id']}/challenges", json={})
        assert r.status_code == 400
        assert _code(r) == ec.MEET_OPTIN_400


# ── Consent is not a state error ──────────────────────────────────

class TestConsentIsItsOwnCode:
    async def test_participant_without_consent_gets_the_consent_code(
        self, client, session, embedded_cfg,
    ):
        sess = await _recorded_session(client)
        await client.post(f"{MEET}/sessions/{sess['id']}/token")   # worker consents
        session.add(CaseSessionParticipantModel(
            session_id=uuid.UUID(sess["id"]), tenant_id="default",
            identity="email:lurker@example.com", role="guest",
        ))
        await session.commit()
        await _post_webhook(client, {"event": "participant_joined",
                                     "room": {"name": sess["external_meeting_id"]},
                                     "participant": {"identity": "email:lurker@example.com"}})

        with patch("case_service.meet.livekit.start_room_recording",
                   new=AsyncMock(return_value="EG_x")):
            r = await client.post(f"{MEET}/sessions/{sess['id']}/recording/start")

        assert r.status_code == 409
        assert _code(r) == ec.MEET_CONSENT_409
        # The identities still reach the operator — the code adds to `detail`.
        assert "lurker@example.com" in r.json()["detail"]

    async def test_lifecycle_refusal_on_the_same_endpoint_is_a_state_code(
        self, client, embedded_cfg,
    ):
        """Same endpoint, same 409, same `except` clause before the subclass
        split — and a completely different thing to go fix."""
        ct = await deploy_case_type(client, name=f"S {uuid.uuid4().hex[:6]}")
        case = await create_case(client, ct["id"])
        sess = (await client.post(f"{MEET}/cases/{case['id']}/sessions",
                                  json={})).json()   # no record intent

        r = await client.post(f"{MEET}/sessions/{sess['id']}/recording/start")
        assert r.status_code == 409
        assert _code(r) == ec.MEET_STATE_409

    def test_consent_error_is_a_valueerror_subclass(self):
        """Every existing `except ValueError` must keep catching it — that is
        what makes the subclass additive rather than a breaking change."""
        exc = meet_service.RecordingConsentMissing(["email:a@x.com"])
        assert isinstance(exc, ValueError)
        assert exc.identities == ["email:a@x.com"]
        assert "a@x.com" in str(exc)


# ── The guest endpoints must not gain an oracle ───────────────────

class TestGuestInviteStaysUniform:
    async def test_unknown_and_consumed_invites_answer_identically(
        self, client, anon_client, embedded_cfg,
    ):
        sess = await _recorded_session(client)
        inv = (await client.post(f"{MEET}/sessions/{sess['id']}/invites",
                                 json={"email": "g@example.com"})).json()

        first = await anon_client.post(f"{MEET}/guest/token",
                                       json={"invite_token": inv["invite_token"]})
        assert first.status_code == 200          # single use — now consumed

        consumed = await anon_client.post(f"{MEET}/guest/token",
                                          json={"invite_token": inv["invite_token"]})
        fabricated = await anon_client.post(f"{MEET}/guest/token",
                                            json={"invite_token": "not-a-real-token"})

        assert consumed.status_code == fabricated.status_code == 404
        assert _code(consumed) == _code(fabricated) == ec.MEET_INVITE_404
        assert consumed.json()["detail"] == fabricated.json()["detail"]

    async def test_preview_uses_the_same_code_as_exchange(
        self, client, anon_client, embedded_cfg,
    ):
        """Preview is non-consuming, so it would be the softer probe of the
        two if its answers differed."""
        r = await anon_client.post(f"{MEET}/guest/preview",
                                   json={"invite_token": "nope"})
        assert _code(r) == ec.MEET_INVITE_404


# ── Session lookups stay anti-oracle ──────────────────────────────

class TestSessionLookupStaysUniform:
    async def test_malformed_and_absent_session_ids_match(self, client):
        malformed = await client.post(f"{MEET}/sessions/not-a-uuid/end")
        absent = await client.post(f"{MEET}/sessions/{uuid.uuid4()}/end")

        assert malformed.status_code == absent.status_code == 404
        assert _code(malformed) == _code(absent) == ec.MEET_NOTFOUND_404

    async def test_no_sealed_recording_is_distinct_from_no_session(
        self, client, embedded_cfg,
    ):
        """The session exists and is fine — the evidence simply is not there
        yet, and 'check the id' would be the wrong advice."""
        sess = await _recorded_session(client)
        r = await client.get(f"{MEET}/sessions/{sess['id']}/recording")

        assert r.status_code == 404
        assert _code(r) == ec.MEET_NORECORDING_404
        assert ec.MEET_NORECORDING_404 != ec.MEET_NOTFOUND_404


# ── Registry hygiene ──────────────────────────────────────────────

class TestRegistry:
    def test_every_meet_code_is_documented(self):
        declared = {v for k, v in vars(ec).items()
                    if k.startswith("MEET_") and isinstance(v, str)}
        assert len(declared) == 13
        assert declared <= ec.KNOWN_CODES

    def test_bare_sites_were_left_bare(self):
        """10 uncoded raises: UUID parses, unknown challenge kinds, the webhook
        401/422, and request-shape misuse. If this moves, it should move
        deliberately."""
        from pathlib import Path
        src = (Path(__file__).resolve().parents[2]
               / "case_service" / "api" / "routers" / "meet.py")
        assert src.read_text().count("raise HTTPException") == 10
