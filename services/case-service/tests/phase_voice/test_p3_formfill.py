"""Voice input P3 — transcript to candidate form-field values.

Pins the properties that keep voice fill assistive rather than authoritative:
  * OFF unless the `voice_fill` release flag is on (404, not 403 — no oracle);
  * the endpoint NEVER writes — a case is byte-identical after an extraction,
    and acceptance happens in the worker's own form;
  * every candidate must quote the transcript VERBATIM — a fabricated quote is
    the tell for a fabricated value, and both are dropped server-side;
  * values are coerced into the field's own type, and a choice field only ever
    receives one of its own options;
  * a sealed transcript rides the same permission as viewing the recording;
  * the audit row names the fields proposed, never their values — a rejected
    proposal must not leave a copy of the transcript behind.

The LLM is mocked throughout: these tests pin the contract and the validation
gate, not a local model's extraction accuracy.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import json
import uuid
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select

from velaris_shared.db.models import (
    CaseSessionModel,
    CaseTypeVariableModel,
    FormDefinitionModel,
    TenantModel,
    VariableNamespaceModel,
)
from case_service.hxnexus import case_qa
from case_service.voicefill import service as voicefill
from tests.conftest import TestSessionFactory, create_case, deploy_case_type

pytestmark = pytest.mark.asyncio

VF = "/api/v1/voicefill"

DICTATION = (
    "the policy number is AB-77-31 and the claim amount is 3,500 pounds. "
    "the customer confirmed they want a callback, and the preferred channel is phone."
)


class FakeLocal:
    """A local backend that returns exactly the JSON we hand it."""
    available = True
    is_external = False
    backend_name = "ollama"
    _model = "llama3.2"

    def __init__(self, payload):
        self.payload = payload if isinstance(payload, str) else json.dumps(payload)
        self.prompts: list[str] = []

    async def complete(self, prompt, **kw):
        self.prompts.append(prompt)
        return self.payload


@pytest.fixture(autouse=True)
def _enable_voice_fill():
    from case_service.api.routers.releases import _ENABLED_VERSIONS
    prior = _ENABLED_VERSIONS.get("voice_fill")
    _ENABLED_VERSIONS["voice_fill"] = "v1.0.0"
    yield
    if prior is None:
        _ENABLED_VERSIONS.pop("voice_fill", None)
    else:
        _ENABLED_VERSIONS["voice_fill"] = prior


@pytest.fixture(autouse=True)
def _fresh_rate_limit():
    """The limiter is a module-level singleton keyed by user — every test here
    is the same user, so without this the suite rate-limits itself."""
    from case_service.api.routers.voicefill import _rate_limiter
    _rate_limiter._windows.clear()
    yield
    _rate_limiter._windows.clear()


# Every direct DB touch runs on its own short-lived session that commits and
# closes. The test harness shares ONE connection between the test session and
# the request's session, so a long-lived test session holding an open
# transaction across a client call makes row visibility order-dependent.

async def _tenant_ai(cfg: dict):
    async with TestSessionFactory() as db:
        tenant = (await db.execute(
            select(TenantModel).where(TenantModel.slug == "default"))).scalars().first()
        if tenant is None:
            tenant = TenantModel(slug="default", name="Default")
        tenant.settings = {**(tenant.settings or {}), "ai": cfg}
        db.add(tenant)
        await db.commit()


async def _case(client, **kw) -> str:
    """Case id — created through the API, like every other suite does."""
    ct = await deploy_case_type(client, name=f"VF CT {uuid.uuid4().hex[:6]}")
    return (await create_case(client, ct["id"], **kw))["id"]


async def _sealed_session(case_id: str, **kw) -> str:
    """A session carrying a sealed live transcript. Returns its id."""
    async with TestSessionFactory() as db:
        row = CaseSessionModel(
            case_id=uuid.UUID(case_id), tenant_id="default", driver="embedded",
            provider="livekit", status="ended", title=kw.pop("title", "Claim call"),
            started_by="user:t", record_intent=True,
            transcript_status=kw.pop("transcript_status", "sealed"),
            transcript_document_id=kw.pop("transcript_document_id", uuid.uuid4()),
            external_meeting_id=f"vx-default-{uuid.uuid4()}",
        )
        db.add(row)
        await db.commit()
        return str(row.id)


async def _case_row(case_id: str, db):
    from velaris_shared.db.models import CaseInstanceModel
    return await db.get(CaseInstanceModel, uuid.UUID(case_id))


async def _form(client) -> dict:
    """A claim form covering every coercion path worth pinning.

    Created through the API on purpose: rows written on the test's own session
    are not reliably visible to the request's session under the shared-connection
    test harness.
    """
    resp = await client.post("/api/v1/forms", json={
        "name": f"Claim {uuid.uuid4().hex[:6]}", "version": "1.0.0",
        "definition_json": {"sections": [{
            "id": "s1", "title": "Claim", "order": 1,
            "fields": [
                {"id": "f1", "type": "text", "field_key": "policy_number",
                 "label": "Policy number", "required": True},
                {"id": "f2", "type": "currency", "field_key": "claim_amount",
                 "label": "Claim amount", "required": False},
                {"id": "f3", "type": "checkbox", "field_key": "callback",
                 "label": "Callback requested", "required": False},
                {"id": "f4", "type": "date", "field_key": "incident_date",
                 "label": "Incident date", "required": False},
                {"id": "f5", "type": "dropdown", "field_key": "channel",
                 "label": "Preferred channel", "required": False,
                 "options": [{"label": "Phone", "value": "phone"},
                             {"label": "Email", "value": "email"}]},
                # Not dictatable — must never appear in the catalogue.
                {"id": "f6", "type": "file_upload", "field_key": "evidence",
                 "label": "Evidence", "required": False},
            ],
        }]},
    })
    assert resp.status_code == 201, resp.text
    return resp.json()


def _candidate(field_key, value, quote, confidence=0.9, speaker=None):
    return {"field_key": field_key, "value": value, "quote": quote,
            "speaker": speaker, "confidence": confidence}


async def _extract(client, case_id, payload, **body):
    with patch.object(case_qa, "_local_backend", return_value=FakeLocal(payload)):
        r = await client.post(f"{VF}/cases/{case_id}/extract", json=body)
    if r.status_code != 200:
        print("DEBUG-NON200", r.status_code, r.text[:200])
    return r


# ═══════════════════════════════════════════════════════
#  1. Flag gate — a disabled feature is invisible
# ═══════════════════════════════════════════════════════

class TestFlagGate:
    async def test_extract_404_when_flag_off(self, client):
        from case_service.api.routers.releases import _ENABLED_VERSIONS
        _ENABLED_VERSIONS.pop("voice_fill", None)
        case_id = await _case(client)
        r = await client.post(f"{VF}/cases/{case_id}/extract", json={"text": DICTATION})
        assert r.status_code == 404

    async def test_status_404_when_flag_off(self, client):
        from case_service.api.routers.releases import _ENABLED_VERSIONS
        _ENABLED_VERSIONS.pop("voice_fill", None)
        assert (await client.get(f"{VF}/status")).status_code == 404


# ═══════════════════════════════════════════════════════
#  2. Auth + guard registration
# ═══════════════════════════════════════════════════════

class TestAuth:
    async def test_extract_requires_auth(self, anon_client):
        r = await anon_client.post(f"{VF}/cases/{uuid.uuid4()}/extract",
                                   json={"text": DICTATION})
        assert r.status_code in (401, 403)

    async def test_status_requires_auth(self, anon_client):
        assert (await anon_client.get(f"{VF}/status")).status_code in (401, 403)

    async def test_unknown_case_is_404(self, client):
        r = await client.post(f"{VF}/cases/{uuid.uuid4()}/extract",
                              json={"text": DICTATION})
        assert r.status_code == 404

    async def test_extract_action_registered_as_write_prep(self):
        """Proposing values is preparation for a write — viewers are out."""
        from velaris_shared.hxguard.service import CASE_ACTIONS
        assert CASE_ACTIONS["voicefill.extract"] == {"assignee", "editor"}


# ═══════════════════════════════════════════════════════
#  3. Tenant sovereignty gate
# ═══════════════════════════════════════════════════════

class TestTenantGate:
    async def test_tenant_not_opted_in_400(self, client):
        await _tenant_ai({})
        case_id = await _case(client)
        r = await client.post(f"{VF}/cases/{case_id}/extract", json={"text": DICTATION})
        assert r.status_code == 400
        assert "not enabled" in r.json()["detail"]

    async def test_no_backend_503_never_empty_candidates(self, client):
        """An unavailable model must not look like 'nothing was said'."""
        await _tenant_ai({"case_qa": True})
        case_id = await _case(client)
        dead = FakeLocal([]); dead.available = False
        with patch.object(case_qa, "_local_backend", return_value=dead):
            r = await client.post(f"{VF}/cases/{case_id}/extract", json={"text": DICTATION})
        assert r.status_code == 503

    async def test_status_reports_tenant_readiness(self, client):
        await _tenant_ai({"case_qa": True, "egress": "local_only"})
        body = (await client.get(f"{VF}/status")).json()
        assert body == {"available": True, "egress": "local_only"}

    async def test_local_only_never_resolves_platform_provider(self, client):
        await _tenant_ai({"case_qa": True})     # egress omitted = local_only
        case_id = await _case(client)

        def _boom():
            raise AssertionError("local_only must never resolve the platform provider")

        with patch.object(case_qa, "_local_backend", return_value=FakeLocal([])), \
             patch.object(case_qa, "get_llm_backend", _boom):
            r = await client.post(f"{VF}/cases/{case_id}/extract", json={"text": DICTATION})
        assert r.status_code == 200


# ═══════════════════════════════════════════════════════
#  4. Extraction contract + type coercion
# ═══════════════════════════════════════════════════════

class TestExtraction:
    async def test_returns_typed_candidates_with_evidence(self, client):
        await _tenant_ai({"case_qa": True})
        case_id = await _case(client)
        form = await _form(client)
        payload = [
            _candidate("policy_number", "AB-77-31", "the policy number is AB-77-31"),
            _candidate("claim_amount", "3,500", "the claim amount is 3,500 pounds", 0.8),
            _candidate("callback", "yes", "the customer confirmed they want a callback", 0.7),
            _candidate("channel", "Phone", "the preferred channel is phone", 0.6),
        ]
        r = await _extract(client, case_id, payload, text=DICTATION, form_id=form["id"])
        assert r.status_code == 200
        body = r.json()
        by_key = {c["field_key"]: c for c in body["candidates"]}

        assert by_key["policy_number"]["value"] == "AB-77-31"
        assert by_key["claim_amount"]["value"] == 3500.0      # "3,500" coerced
        assert by_key["callback"]["value"] is True            # "yes" coerced
        assert by_key["channel"]["value"] == "phone"          # label mapped to value
        assert by_key["policy_number"]["label"] == "Policy number"
        assert by_key["policy_number"]["quote"] in DICTATION
        assert body["source"] == "Dictation"
        assert body["external_ai"] is False

    async def test_candidates_ordered_by_confidence(self, client):
        await _tenant_ai({"case_qa": True})
        case_id = await _case(client)
        form = await _form(client)
        payload = [
            _candidate("claim_amount", "3,500", "the claim amount is 3,500 pounds", 0.4),
            _candidate("policy_number", "AB-77-31", "the policy number is AB-77-31", 0.95),
        ]
        r = await _extract(client, case_id, payload, text=DICTATION, form_id=form["id"])
        assert [c["field_key"] for c in r.json()["candidates"]] == \
               ["policy_number", "claim_amount"]

    async def test_duplicate_field_keeps_the_confident_one(self, client):
        await _tenant_ai({"case_qa": True})
        case_id = await _case(client)
        form = await _form(client)
        payload = [
            _candidate("policy_number", "AB-77", "the policy number is AB-77-31", 0.3),
            _candidate("policy_number", "AB-77-31", "the policy number is AB-77-31", 0.9),
        ]
        r = await _extract(client, case_id, payload, text=DICTATION, form_id=form["id"])
        cands = r.json()["candidates"]
        assert len(cands) == 1 and cands[0]["value"] == "AB-77-31"

    async def test_undictatable_field_types_are_not_offered(self, client):
        """Nobody dictates a file upload — it must not reach the model."""
        created = await _form(client)
        async with TestSessionFactory() as db:
            form = await db.get(FormDefinitionModel, uuid.UUID(created["id"]))
            catalogue = voicefill._catalogue_from_form(form)
        assert "evidence" not in {f["key"] for f in catalogue}
        assert "policy_number" in {f["key"] for f in catalogue}


# ═══════════════════════════════════════════════════════
#  5. The anti-hallucination gate
# ═══════════════════════════════════════════════════════

class TestFabricationDropped:
    async def test_quote_not_in_transcript_is_dropped(self, client):
        """The load-bearing test: an invented value comes with invented
        evidence, and evidence is checkable."""
        await _tenant_ai({"case_qa": True})
        case_id = await _case(client)
        form = await _form(client)
        payload = [
            _candidate("policy_number", "ZZ-00-00", "the policy number is ZZ-00-00"),
            _candidate("claim_amount", "3,500", "the claim amount is 3,500 pounds"),
        ]
        r = await _extract(client, case_id, payload, text=DICTATION, form_id=form["id"])
        keys = {c["field_key"] for c in r.json()["candidates"]}
        assert keys == {"claim_amount"}

    async def test_missing_quote_is_dropped(self, client):
        await _tenant_ai({"case_qa": True})
        case_id = await _case(client)
        form = await _form(client)
        payload = [{"field_key": "policy_number", "value": "AB-77-31", "confidence": 1.0}]
        r = await _extract(client, case_id, payload, text=DICTATION, form_id=form["id"])
        assert r.json()["candidates"] == []

    async def test_unknown_field_key_is_dropped(self, client):
        await _tenant_ai({"case_qa": True})
        case_id = await _case(client)
        form = await _form(client)
        payload = [_candidate("secret_admin_flag", True, "the policy number is AB-77-31")]
        r = await _extract(client, case_id, payload, text=DICTATION, form_id=form["id"])
        assert r.json()["candidates"] == []

    async def test_unparseable_date_is_dropped_not_guessed(self, client):
        """A wrong date is worse than an unfilled one."""
        await _tenant_ai({"case_qa": True})
        case_id = await _case(client)
        form = await _form(client)
        payload = [_candidate("incident_date", "last tuesday",
                              "the policy number is AB-77-31")]
        r = await _extract(client, case_id, payload, text=DICTATION, form_id=form["id"])
        assert r.json()["candidates"] == []

    async def test_choice_outside_its_options_is_dropped(self, client):
        await _tenant_ai({"case_qa": True})
        case_id = await _case(client)
        form = await _form(client)
        payload = [_candidate("channel", "carrier pigeon", "the preferred channel is phone")]
        r = await _extract(client, case_id, payload, text=DICTATION, form_id=form["id"])
        assert r.json()["candidates"] == []

    async def test_garbage_model_output_yields_no_candidates(self, client):
        await _tenant_ai({"case_qa": True})
        case_id = await _case(client)
        form = await _form(client)
        r = await _extract(client, case_id, "I'm sorry, I cannot help with that.",
                           text=DICTATION, form_id=form["id"])
        assert r.status_code == 200
        assert r.json()["candidates"] == []


# ═══════════════════════════════════════════════════════
#  6. The endpoint writes nothing
# ═══════════════════════════════════════════════════════

class TestNeverWrites:
    async def test_case_is_untouched_by_an_extraction(self, client):
        await _tenant_ai({"case_qa": True})
        case_id = await _case(client, data={"policy_number": "ORIGINAL"})
        form = await _form(client)

        payload = [_candidate("policy_number", "AB-77-31", "the policy number is AB-77-31")]
        r = await _extract(client, case_id, payload, text=DICTATION, form_id=form["id"])
        assert r.status_code == 200
        assert r.json()["candidates"], "the proposal was made…"

        after = await client.get(f"/api/v1/cases/{case_id}")
        assert after.json()["data"]["policy_number"] == "ORIGINAL", \
            "…but the case still holds its original value until a human saves"

    async def test_no_variable_rows_are_written(self, client):
        from velaris_shared.db.models import CaseInstanceVariableModel
        await _tenant_ai({"case_qa": True})
        case_id = await _case(client)
        form = await _form(client)
        payload = [_candidate("policy_number", "AB-77-31", "the policy number is AB-77-31")]
        await _extract(client, case_id, payload, text=DICTATION, form_id=form["id"])
        async with TestSessionFactory() as db:
            rows = (await db.execute(
                select(CaseInstanceVariableModel)
                .where(CaseInstanceVariableModel.case_id == uuid.UUID(case_id)))).scalars().all()
        assert rows == []


# ═══════════════════════════════════════════════════════
#  7. Audit — field names, never values
# ═══════════════════════════════════════════════════════

class TestAudit:
    async def test_audit_names_fields_but_not_values(self, client):
        from velaris_shared.db.models import CaseAuditLogModel
        await _tenant_ai({"case_qa": True})
        case_id = await _case(client)
        form = await _form(client)
        payload = [_candidate("policy_number", "AB-77-31", "the policy number is AB-77-31")]
        await _extract(client, case_id, payload, text=DICTATION, form_id=form["id"])

        async with TestSessionFactory() as db:
            entry = (await db.execute(
                select(CaseAuditLogModel)
                .where(CaseAuditLogModel.case_id == uuid.UUID(case_id))
                .where(CaseAuditLogModel.action == "voicefill_extracted"))).scalars().first()
            assert entry is not None
            details = entry.details or {}
        assert details["proposed_fields"] == ["policy_number"]
        assert "AB-77-31" not in json.dumps(details), \
            "a rejected proposal must not leave the transcript's content behind"


# ═══════════════════════════════════════════════════════
#  8. Sources — dictation vs sealed session transcript
# ═══════════════════════════════════════════════════════

class TestSources:
    async def test_both_inputs_at_once_is_400(self, client):
        await _tenant_ai({"case_qa": True})
        case_id = await _case(client)
        r = await client.post(f"{VF}/cases/{case_id}/extract",
                              json={"text": DICTATION, "session_id": str(uuid.uuid4())})
        assert r.status_code == 400

    async def test_no_input_at_all_is_404(self, client):
        await _tenant_ai({"case_qa": True})
        case_id = await _case(client)
        r = await client.post(f"{VF}/cases/{case_id}/extract", json={})
        assert r.status_code == 404

    async def test_session_from_another_case_is_404(self, client):
        await _tenant_ai({"case_qa": True})
        mine = await _case(client)
        theirs = await _case(client)
        other = await _sealed_session(theirs)
        r = await client.post(f"{VF}/cases/{mine}/extract", json={"session_id": other})
        assert r.status_code == 404

    async def test_session_without_sealed_transcript_is_404(self, client):
        await _tenant_ai({"case_qa": True})
        case_id = await _case(client)
        sid = await _sealed_session(case_id, title="No captions",
                                    transcript_status="none",
                                    transcript_document_id=None)
        r = await client.post(f"{VF}/cases/{case_id}/extract", json={"session_id": sid})
        assert r.status_code == 404
        assert "no sealed transcript" in r.json()["detail"], r.text

    async def test_transcript_refused_without_recording_permission(self, client):
        """Reading a conversation is reading a conversation, whatever we do
        with it afterwards — same gate as viewing the recording."""
        case_id = await _case(client)
        sid = await _sealed_session(case_id)
        async with TestSessionFactory() as db:
            case = await _case_row(case_id, db)
            with pytest.raises(PermissionError):
                await voicefill.load_transcript(
                    db, case, text=None, session_id=uuid.UUID(sid),
                    can_view_transcripts=False)

    async def test_speakers_come_from_verified_identities(self, client):
        case_id = await _case(client)
        sid = await _sealed_session(case_id)
        sealed = (b"Live transcript\n\n"
                  b"[10:00:01] Priya Raman: what is your policy number?\n"
                  b"[10:00:06] Guest (customer): the policy number is AB-77-31\n"
                  b"[10:00:09] Guest (customer): (line crackles)\n")
        async with TestSessionFactory() as db:
            case = await _case_row(case_id, db)
            with patch("case_service.api.routers.meet._unseal_transcript",
                       new=AsyncMock(return_value=sealed)):
                transcript, speakers, label = await voicefill.load_transcript(
                    db, case, text=None, session_id=uuid.UUID(sid),
                    can_view_transcripts=True)
        assert speakers == ["Priya Raman", "Guest (customer)"]
        assert "Claim call" in label
        assert "(line crackles)" not in transcript, "caption noise is dropped"

    async def test_speaker_attribution_survives_to_the_candidate(self, client):
        await _tenant_ai({"case_qa": True})
        case_id = await _case(client)
        sid = await _sealed_session(case_id)
        form = await _form(client)
        sealed = b"[10:00:06] Guest (customer): the policy number is AB-77-31\n"
        payload = [_candidate("policy_number", "AB-77-31",
                              "the policy number is AB-77-31",
                              speaker="Guest (customer)")]
        with patch("case_service.api.routers.meet._unseal_transcript",
                   new=AsyncMock(return_value=sealed)), \
             patch.object(case_qa, "_local_backend", return_value=FakeLocal(payload)):
            r = await client.post(f"{VF}/cases/{case_id}/extract",
                                  json={"session_id": sid, "form_id": form["id"]})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["candidates"][0]["speaker"] == "Guest (customer)"
        assert body["speakers"] == ["Guest (customer)"]

    async def test_invented_speaker_is_not_attributed(self, client):
        """Attribution is only as good as the room's verified identities."""
        await _tenant_ai({"case_qa": True})
        case_id = await _case(client)
        sid = await _sealed_session(case_id)
        form = await _form(client)
        sealed = b"[10:00:06] Guest (customer): the policy number is AB-77-31\n"
        payload = [_candidate("policy_number", "AB-77-31",
                              "the policy number is AB-77-31", speaker="Some Other Person")]
        with patch("case_service.api.routers.meet._unseal_transcript",
                   new=AsyncMock(return_value=sealed)), \
             patch.object(case_qa, "_local_backend", return_value=FakeLocal(payload)):
            r = await client.post(f"{VF}/cases/{case_id}/extract",
                                  json={"session_id": sid, "form_id": form["id"]})
        assert r.status_code == 200, r.text
        assert r.json()["candidates"][0]["speaker"] is None


# ═══════════════════════════════════════════════════════
#  9. Catalogue without a form — declared form.* variables only
# ═══════════════════════════════════════════════════════

class TestVariableCatalogue:
    async def test_only_form_owned_variables_are_offered(self, client):
        """A connector-owned variable cannot be saved from a form, so
        proposing one would dead-end the worker."""
        case_id = await _case(client)
        async with TestSessionFactory() as db:
            case = await _case_row(case_id, db)
            namespaces = {}
            for name, owner in (("form", "form"), ("crm", "connector")):
                ns = (await db.execute(select(VariableNamespaceModel)
                                       .where(VariableNamespaceModel.name == name))).scalars().first()
                if ns is None:
                    ns = VariableNamespaceModel(name=name, owner_type=owner)
                    db.add(ns)
                    await db.flush()
                namespaces[name] = ns
            db.add_all([
                CaseTypeVariableModel(
                    case_type_id=case.case_type_id, namespace_id=namespaces["form"].id,
                    name="policy_number", full_key="form.policy_number",
                    var_type="str", label="Policy number"),
                CaseTypeVariableModel(
                    case_type_id=case.case_type_id, namespace_id=namespaces["crm"].id,
                    name="account_status", full_key="crm.account_status",
                    var_type="str", label="Account status"),
            ])
            await db.commit()

            catalogue = await voicefill.build_catalogue(db, case, None)
        assert {f["key"] for f in catalogue} == {"form.policy_number"}

    async def test_unknown_form_id_is_404(self, client):
        await _tenant_ai({"case_qa": True})
        case_id = await _case(client)
        r = await client.post(f"{VF}/cases/{case_id}/extract",
                              json={"text": DICTATION, "form_id": str(uuid.uuid4())})
        assert r.status_code == 404


# ═══════════════════════════════════════════════════════
#  10. Rate limit
# ═══════════════════════════════════════════════════════

class TestRateLimit:
    async def test_extraction_is_rate_limited(self, client):
        from case_service.api.routers import voicefill as router

        await _tenant_ai({"case_qa": True})
        case_id = await _case(client)
        with patch.object(case_qa, "_local_backend", return_value=FakeLocal([])):
            codes = [
                (await client.post(f"{VF}/cases/{case_id}/extract",
                                   json={"text": DICTATION})).status_code
                for _ in range(router._rate_limiter.max_calls + 1)
            ]
        assert codes[-1] == 429
        assert all(c == 200 for c in codes[:-1])


# ═══════════════════════════════════════════════════════
#  11. Hostile model output + tenant isolation
# ═══════════════════════════════════════════════════════

class TestUntrustedModelOutput:
    """The model is a dependency, not an authority — its output is bounded
    before anything expensive (JSON parse, regex, normalization) touches it."""

    async def test_giant_answer_is_bounded_not_parsed_whole(self, client):
        await _tenant_ai({"case_qa": True})
        case_id = await _case(client)
        form = await _form(client)
        # A model stuck in a loop: several MB of JSON.
        runaway = json.dumps([_candidate("policy_number", "AB-77-31",
                                         "the policy number is AB-77-31")] * 40_000)
        assert len(runaway) > 5_000_000
        r = await _extract(client, case_id, runaway, text=DICTATION, form_id=form["id"])
        assert r.status_code == 200
        assert len(r.json()["candidates"]) <= 1

    async def test_giant_field_values_are_clipped(self, client):
        await _tenant_ai({"case_qa": True})
        case_id = await _case(client)
        form = await _form(client)
        payload = [{"field_key": "policy_number", "value": "A" * 500_000,
                    "quote": "the policy number is AB-77-31" + "B" * 500_000,
                    "speaker": "C" * 500_000, "confidence": 0.9}]
        r = await _extract(client, case_id, payload, text=DICTATION, form_id=form["id"])
        assert r.status_code == 200
        # The padded quote no longer matches the transcript, so it drops — the
        # point is that it drops fast rather than melting the regex engine.
        assert r.json()["candidates"] == []

    async def test_absurd_confidence_is_clamped(self, client):
        await _tenant_ai({"case_qa": True})
        case_id = await _case(client)
        form = await _form(client)
        payload = [_candidate("policy_number", "AB-77-31",
                              "the policy number is AB-77-31", confidence=99.0)]
        r = await _extract(client, case_id, payload, text=DICTATION, form_id=form["id"])
        assert r.json()["candidates"][0]["confidence"] == 1.0


class TestTenantIsolation:
    async def test_other_tenants_case_is_404(self, client):
        """Fail closed, and say nothing: a case owned by another tenant is
        indistinguishable from one that does not exist.

        Asserted against the router's own gate rather than over HTTP — the
        shared-connection harness makes a mid-test tenant reassignment only
        intermittently visible to the request session, which would make an
        endpoint-level version of this pass for the wrong reason.
        """
        from fastapi import HTTPException

        from case_service.api.routers.voicefill import _get_case_or_404
        from velaris_shared.db.models import CaseInstanceModel

        case_id = await _case(client)
        async with TestSessionFactory() as db:
            row = await db.get(CaseInstanceModel, uuid.UUID(case_id))
            row.tenant_id = uuid.uuid4()          # somebody else's tenant
            db.add(row)
            await db.commit()

            caller = type("U", (), {"tenant_id": "default", "user_id": "u1"})()
            with pytest.raises(HTTPException) as excinfo:
                await _get_case_or_404(db, uuid.UUID(case_id), caller)
        assert excinfo.value.status_code == 404
        assert excinfo.value.detail == "Case not found"

    async def test_own_tenant_case_is_reachable(self, client):
        """The other half — the gate must not lock out the owning tenant."""
        from case_service.api.routers.voicefill import _get_case_or_404
        from velaris_shared.db.models import CaseInstanceModel

        tenant_uuid = uuid.uuid4()
        case_id = await _case(client)
        async with TestSessionFactory() as db:
            row = await db.get(CaseInstanceModel, uuid.UUID(case_id))
            row.tenant_id = tenant_uuid
            db.add(row)
            await db.commit()

            caller = type("U", (), {"tenant_id": str(tenant_uuid), "user_id": "u1"})()
            found = await _get_case_or_404(db, uuid.UUID(case_id), caller)
        assert found.id == uuid.UUID(case_id)
