"""HxSentinel — the seam between signal EXTRACTION and rule EVALUATION.

Every P2-P4 engine test hand-inserts ``CaseRiskSignalModel`` rows for the case
under evaluation, so none of them ever asks the production question: *does a case
actually carry signals at the moment the engine runs on it?*

Four of the five rules (``shared_doc_fraud``, ``identity_velocity``,
``shared_signal_negative_outcome``, ``resembles_confirmed_cases``) start from
``_signals_of(session, case.id, ...)`` — the SUBJECT case's own stored signals —
and return None when that list is empty. Extraction only runs at a terminal
status (``extract_on_close``) or over terminal cases (``sweep``). An open case
therefore has no signals, and those four rules cannot fire on it.

That matters because the verify gate exists precisely to hold an OPEN case before
it advances.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import uuid
from unittest.mock import patch

import pytest
from sqlalchemy import select

from velaris_shared.db.models import (
    CaseInstanceModel,
    CaseInstanceVariableModel,
    DocumentModel,
    DocumentVerificationModel,
)
from case_service.sentinel import engine as risk_engine
from case_service.sentinel import extraction as risk_extraction
from case_service.sentinel import service as sentinel
from tests.phase_hxsentinel._seed import seed_case

DIGEST = "a" * 64
DEK = b"k" * 32


def _with_dek():
    """Pin a tenant DEK so identity extraction is exercised without the vault."""
    return patch.object(sentinel, "_tenant_dek", return_value=DEK)


def _without_dek():
    """The vault is shut — identity signals must fail closed AND not be retired."""
    return patch.object(sentinel, "_tenant_dek", return_value=None)


@pytest.fixture(autouse=True)
def _enable_hxsentinel():
    from case_service.api.routers.releases import _ENABLED_VERSIONS
    prior = _ENABLED_VERSIONS.get("hxsentinel")
    _ENABLED_VERSIONS["hxsentinel"] = "v1.9.0"
    yield
    if prior is None:
        _ENABLED_VERSIONS.pop("hxsentinel", None)
    else:
        _ENABLED_VERSIONS["hxsentinel"] = prior


async def _case_with_document(session, digest: str) -> uuid.UUID:
    """A case carrying one verified document whose sha256 is ``digest``."""
    cid = await seed_case(session)
    doc_id = uuid.uuid4()
    session.add(DocumentModel(id=doc_id, case_id=cid, filename="id.pdf"))
    await session.flush()
    session.add(DocumentVerificationModel(
        case_id=cid, document_id=doc_id, status="verified",
        checks=[{"sha256": digest}], verified_by="tester"))
    await session.commit()
    return cid


async def _confirmed_fraud_case(session) -> uuid.UUID:
    """A closed, fraud-labelled case carrying DIGEST — the corpus side."""
    bad = await _case_with_document(session, DIGEST)
    await sentinel.set_outcome(session, case_id=bad, tenant_id="default",
                               outcome="fraud", labelled_by="tester", audit=False)
    await risk_extraction.extract_on_close(session, bad, "closed")
    await session.commit()
    return bad


@pytest.mark.asyncio
async def test_open_case_fires_without_any_prior_extraction(session):
    """THE regression: a live case sharing a document with a confirmed-fraud case.

    Nothing extracts the open case's signals before this call — that is the whole
    point. ``evaluate_case`` has to do it, or four of the five rules stay blind on
    exactly the cases the verify gate exists to hold.
    """
    bad = await _confirmed_fraud_case(session)
    live = await _case_with_document(session, DIGEST)

    assert await sentinel.get_signals(session, bad), "closed case should have signals"
    assert not await sentinel.get_signals(session, live), "open case starts with none"

    findings = await risk_engine.evaluate_case(session, live)
    await session.commit()

    assert "shared_doc_fraud" in [f.rule_key for f in findings], (
        "the open case must fire without a manual extraction step")
    assert await sentinel.get_signals(session, live), (
        "evaluation should have populated the subject case's signals")


@pytest.mark.asyncio
async def test_evaluation_is_still_idempotent_after_self_extraction(session):
    """Re-evaluating must not duplicate signals or findings."""
    await _confirmed_fraud_case(session)
    live = await _case_with_document(session, DIGEST)

    first = await risk_engine.evaluate_case(session, live)
    await session.commit()
    second = await risk_engine.evaluate_case(session, live)
    await session.commit()

    assert [f.rule_key for f in first] == [f.rule_key for f in second]
    assert len(await sentinel.get_signals(session, live)) == 1


# ── Coverage: where an identity value is allowed to live ─────────────────────


@pytest.mark.asyncio
async def test_identity_is_extracted_from_a_typed_variable(session):
    """Typed variables never touch case.data, and used to be invisible.

    ``case_vars`` upserts values into ``case_instance_variables``; a case type
    that captures email as a typed variable produced no identity signal at all.
    """
    cid = await seed_case(session)
    session.add(CaseInstanceVariableModel(
        case_id=cid, full_key="legacy.email", value_text="Person@Example.com ",
        written_by="tester"))
    await session.commit()

    with _with_dek():
        await sentinel.extract_signals(session, cid)
    await session.commit()

    types = {s.signal_type for s in await sentinel.get_signals(session, cid)}
    assert "identity_email" in types


@pytest.mark.asyncio
async def test_identity_is_extracted_from_the_portal_submitter_column(session):
    """The anonymous portal path stores the email in a COLUMN, not in case.data."""
    cid = await seed_case(session)
    case = await session.get(CaseInstanceModel, cid)
    case.portal_submitter_email = "walkin@example.com"
    case.data = {"subject": "x", "description": "y"}      # what portal.py writes
    await session.commit()

    with _with_dek():
        await sentinel.extract_signals(session, cid)
    await session.commit()

    types = {s.signal_type for s in await sentinel.get_signals(session, cid)}
    assert "identity_email" in types


@pytest.mark.asyncio
async def test_case_data_identity_still_works(session):
    """The original blob path must not regress."""
    cid = await seed_case(session)
    case = await session.get(CaseInstanceModel, cid)
    case.data = {"applicant_email": "blob@example.com"}
    await session.commit()

    with _with_dek():
        await sentinel.extract_signals(session, cid)
    await session.commit()

    types = {s.signal_type for s in await sentinel.get_signals(session, cid)}
    assert "identity_email" in types


# ── Retirement: re-extraction reconciles instead of accumulating ─────────────


@pytest.mark.asyncio
async def test_corrected_identity_retires_the_old_signal(session):
    """A typo'd email corrected on a live case must not leave both digests.

    Both would otherwise keep matching other cases and inflating
    identity_velocity on a value this case no longer carries.
    """
    cid = await seed_case(session)
    case = await session.get(CaseInstanceModel, cid)
    case.data = {"email": "typo@example.com"}
    await session.commit()

    with _with_dek():
        await sentinel.extract_signals(session, cid)
        await session.commit()
        first = {s.signal_key for s in await sentinel.get_signals(session, cid)}

        case.data = {"email": "correct@example.com"}
        await session.commit()
        result = await sentinel.extract_signals(session, cid)
        await session.commit()

    keys = {s.signal_key for s in await sentinel.get_signals(session, cid)}
    assert len(keys) == 1, "the superseded digest must be retired, not kept"
    assert keys.isdisjoint(first)
    assert result.removed == ["identity_email"]


@pytest.mark.asyncio
async def test_removed_document_retires_its_hash(session):
    cid = await _case_with_document(session, DIGEST)
    await sentinel.extract_signals(session, cid)
    await session.commit()
    assert await sentinel.get_signals(session, cid)

    for ver in (await session.execute(
        select(DocumentVerificationModel)
        .where(DocumentVerificationModel.case_id == cid)
    )).scalars().all():
        await session.delete(ver)
    await session.commit()

    await sentinel.extract_signals(session, cid)
    await session.commit()
    assert not await sentinel.get_signals(session, cid)


@pytest.mark.asyncio
async def test_a_shut_vault_never_retires_identity_signals(session):
    """The dangerous case: no DEK reads as "no identity values".

    Retiring on that would wipe a good index the moment the vault hiccuped, so
    identity types are only reconciled when a key was actually obtainable.
    """
    cid = await seed_case(session)
    case = await session.get(CaseInstanceModel, cid)
    case.data = {"email": "keep@example.com"}
    await session.commit()

    with _with_dek():
        await sentinel.extract_signals(session, cid)
        await session.commit()
    before = {s.signal_key for s in await sentinel.get_signals(session, cid)}
    assert before

    with _without_dek():
        result = await sentinel.extract_signals(session, cid)
        await session.commit()

    assert {s.signal_key for s in await sentinel.get_signals(session, cid)} == before
    assert result.removed == []
    assert result.skipped, "a shut vault must still be said out loud"


# ── Anonymized cases must not enter the identity index ───────────────────────


async def _anonymized_case(session, *, submitter="real.person@example.com"):
    """Exactly what compliance.retention.anonymize leaves behind.

    It REPLACES stripped values with a placeholder rather than removing the key,
    and it rewrites only ``case.data`` — the ``portal_submitter_email`` column
    survives untouched.
    """
    cid = await seed_case(session)
    case = await session.get(CaseInstanceModel, cid)
    case.data = {"email": "[anonymized]", "phone": "[anonymized]",
                 "_anonymized_at": "2026-01-01T00:00:00+00:00"}
    case.portal_submitter_email = submitter
    await session.commit()
    return cid


@pytest.mark.asyncio
async def test_anonymized_cases_do_not_collide_on_a_placeholder(session):
    """Two unrelated erased cases must not look like one shared identity.

    Hashing "[anonymized]" would give every erased case in a tenant the same
    digest, firing identity_velocity and resemblance across unrelated people.
    """
    keys = []
    for _ in range(2):
        cid = await _anonymized_case(session)
        with _with_dek():
            await sentinel.extract_signals(session, cid)
        await session.commit()
        keys.append({(s.signal_type, s.signal_key)
                     for s in await sentinel.get_signals(session, cid)})

    assert not (keys[0] & keys[1]), "erased cases share a digest"
    assert not any(keys), "an erased case contributes no identity signal at all"


@pytest.mark.asyncio
async def test_erasure_retires_identity_signals_extracted_before_it(session):
    """Erasure must reach the signal index, not just the case row."""
    cid = await seed_case(session)
    case = await session.get(CaseInstanceModel, cid)
    case.data = {"email": "person@example.com"}
    await session.commit()

    with _with_dek():
        await sentinel.extract_signals(session, cid)
        await session.commit()
        assert await sentinel.get_signals(session, cid)

        case.data = {"email": "[anonymized]",
                     "_anonymized_at": "2026-01-01T00:00:00+00:00"}
        await session.commit()
        result = await sentinel.extract_signals(session, cid)
        await session.commit()

    assert not await sentinel.get_signals(session, cid)
    assert result.removed == ["identity_email"]


# ── True end-to-end: nothing is hand-seeded on the case being advanced ───────


async def _attach_document(session, case_id, digest):
    doc_id = uuid.uuid4()
    session.add(DocumentModel(id=doc_id, case_id=case_id, filename="id.pdf"))
    await session.flush()
    session.add(DocumentVerificationModel(
        case_id=case_id, document_id=doc_id, status="verified",
        checks=[{"sha256": digest}], verified_by="tester"))


@pytest.mark.asyncio
async def test_gate_blocks_a_real_advance_with_no_seeded_signals(session, client):
    """The whole chain, through HTTP, with nothing faked on the subject case.

    case document -> extraction (inside evaluate) -> shared_doc_fraud finding ->
    gating hold -> 409 on the manual stage transition.

    Every other gate test hand-inserts the subject case's signals, so none of
    them would notice if extraction never ran on a live case.
    """
    from velaris_shared.db.models import CaseRiskConfigModel
    from tests.conftest import create_case, deploy_case_type

    ct = await deploy_case_type(client, name=f"E2E {uuid.uuid4().hex[:6]}")
    a = await create_case(client, ct["id"])
    b = await create_case(client, ct["id"])
    ca, cb = uuid.UUID(a["id"]), uuid.UUID(b["id"])

    # Both carry the same document. Only the CLOSED one gets extracted, by the
    # real on-close hook — the corpus side.
    await _attach_document(session, ca, DIGEST)
    await _attach_document(session, cb, DIGEST)
    await sentinel.set_outcome(session, case_id=cb, tenant_id="default",
                               outcome="fraud", labelled_by="t", audit=False)
    await risk_extraction.extract_on_close(session, cb, "closed")
    session.add(CaseRiskConfigModel(tenant_id="default",
                verify_gate_severity="high", updated_by="admin"))
    await session.commit()

    assert not await sentinel.get_signals(session, ca), (
        "the case being advanced must start with no signals — that is the point")

    stages = [{"id": "s1", "name": "Open", "order": 0},
              {"id": "s2", "name": "Closed", "order": 1}]
    r = await client.patch(f"/api/v1/case-types/{ct['id']}",
                           json={"definition_json": {"stages": stages}})
    assert r.status_code == 200, r.text

    blocked = await client.post(f"/api/v1/cases/{ca}/stage",
                                json={"target_stage_id": "s2"})
    assert blocked.status_code == 409, (
        f"the gate must arm and refuse the advance; got {blocked.status_code} {blocked.text}")


# ── Retirement leaves a trail ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_retiring_a_signal_is_audited_without_the_raw_value(session):
    """Evidence that vanishes silently is a way to launder a case.

    Edit the value, let the next evaluation drop the link, and nothing records
    that the link ever existed. The trail must survive — but it must not
    re-introduce the plaintext the hashing exists to avoid.
    """
    from velaris_shared.db.models import CaseAuditLogModel

    cid = await seed_case(session)
    case = await session.get(CaseInstanceModel, cid)
    case.data = {"email": "before@example.com"}
    await session.commit()

    with _with_dek():
        await sentinel.extract_signals(session, cid)
        await session.commit()
        old_key = (await sentinel.get_signals(session, cid))[0].signal_key

        case.data = {"email": "after@example.com"}
        await session.commit()
        await sentinel.extract_signals(session, cid)
        await session.commit()

    rows = (await session.execute(
        select(CaseAuditLogModel)
        .where(CaseAuditLogModel.case_id == cid)
        .where(CaseAuditLogModel.action == "risk.signals.retired")
    )).scalars().all()

    assert len(rows) == 1, "retirement must be recorded"
    entry = rows[0].previous_value["signals"][0]
    assert entry["signal_type"] == "identity_email"
    assert entry["hashed"] is True
    # A prefix is enough to correlate; the full digest and the address are not there.
    assert old_key.startswith(entry["signal_key_prefix"])
    assert entry["signal_key_prefix"] != old_key
    assert "before@example.com" not in str(rows[0].previous_value)


@pytest.mark.asyncio
async def test_no_audit_row_when_nothing_is_retired(session):
    """Re-extracting an unchanged case must not spam the audit log."""
    from velaris_shared.db.models import CaseAuditLogModel

    cid = await seed_case(session)
    case = await session.get(CaseInstanceModel, cid)
    case.data = {"email": "steady@example.com"}
    await session.commit()

    with _with_dek():
        for _ in range(3):
            await sentinel.extract_signals(session, cid)
            await session.commit()

    rows = (await session.execute(
        select(CaseAuditLogModel)
        .where(CaseAuditLogModel.case_id == cid)
        .where(CaseAuditLogModel.action == "risk.signals.retired")
    )).scalars().all()
    assert rows == []


# ── The hot path stays cold when the module is off ───────────────────────────


@pytest.mark.asyncio
async def test_flag_off_extracts_nothing_on_the_advance_path(session, monkeypatch):
    """`advance_blocked` runs for EVERY case in EVERY tenant.

    Evaluation now writes signals, so the short-circuit matters more than it did:
    with the flag off, nothing may read or write a single row.
    """
    from case_service.api.routers.releases import _ENABLED_VERSIONS

    cid = await _case_with_document(session, DIGEST)

    called = False

    async def _tripwire(*a, **kw):
        nonlocal called
        called = True
        return sentinel.SignalExtraction()

    monkeypatch.setattr(sentinel, "extract_signals", _tripwire)

    _ENABLED_VERSIONS.pop("hxsentinel", None)          # module off platform-wide
    assert await risk_engine.advance_blocked(session, cid) is False
    assert not called, "a disabled module must not touch the signal store"
    assert not await sentinel.get_signals(session, cid)


@pytest.mark.asyncio
async def test_gate_off_for_the_tenant_also_extracts_nothing(session, monkeypatch):
    """Flag on, gate off: still no evaluation, so still no extraction."""
    cid = await _case_with_document(session, DIGEST)

    called = False

    async def _tripwire(*a, **kw):
        nonlocal called
        called = True
        return sentinel.SignalExtraction()

    monkeypatch.setattr(sentinel, "extract_signals", _tripwire)

    assert await risk_engine.advance_blocked(session, cid) is False
    assert not called, "no tenant gate means no evaluation, so no extraction"
