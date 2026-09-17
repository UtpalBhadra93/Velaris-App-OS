"""F-01 — the AI decision ledger.

The tests that matter are the ones about the seal, because a hash chain is
trivially easy to write and easy to write *uselessly*:

* it must **detect an edit** to a sealed row — otherwise it is decoration;
* it must **survive a human verdict arriving later**, which is the ordinary
  case and the one a naive "hash the whole row" design breaks on;
* it must **survive retention**, tombstoning rather than failing — the exact
  defect migration 180 shipped for the arbiter chain, found by running a purge
  and not by reading the code;
* and an **unsealed row must never be purged**, because the hole it leaves is
  indistinguishable from a deletion.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio
from sqlalchemy import select

from velaris_shared.db.models import (
    AIDecisionRecordModel,
    AIDecisionVerdictModel,
    AILedgerChainModel,
    ArbiterNamespaceModel,
    TenantModel,
)

from case_service.ledger import chain, evidence, records

pytestmark = pytest.mark.asyncio

PREFIX = "/api/v1/ai-ledger"

SEED = [("ai", "deny"), ("egress", "deny"), ("export", "deny"),
        ("vault", "deny"), ("case", "allow"), ("auth", "allow"),
        ("agent", "deny")]


@pytest_asyncio.fixture(autouse=True)
async def tenant(session):
    from case_service.agents import identity as agent_identity
    from case_service.hxarbiter import pdp
    from case_service.tenancy import identity as _identity

    _identity.clear_cache()
    agent_identity._SLUG_CACHE.clear()
    pdp.invalidate_cache()
    for ns, eff in SEED:
        session.add(ArbiterNamespaceModel(
            namespace=ns, default_effect=eff, max_mode="shadow", description="",
            sample_allow_rate=1, sample_default_rate=1,
            updated_at=datetime.now(timezone.utc)))
    t = TenantModel(id=uuid.uuid4(), slug="default", name="Default")
    session.add(t)
    await session.commit()
    yield t
    agent_identity._SLUG_CACHE.clear()
    _identity.clear_cache()
    pdp.invalidate_cache()


async def _rec(session, tenant, **kw):
    row = await records.record(
        session,
        tenant_id=kw.pop("tenant_id", tenant.id),
        purpose=kw.pop("purpose", "case_qa"),
        model_id=kw.pop("model_id", "ollama/llama3.2"),
        input_text=kw.pop("input_text", "why was this refused?"),
        output_text=kw.pop("output_text", "because of rule 4"),
        **kw)
    await session.commit()
    return row


def _past(seconds: int) -> datetime:
    return datetime.now(timezone.utc) - timedelta(seconds=seconds)


# ══ writing ═════════════════════════════════════════════════════════════════


async def test_a_record_needs_an_input_commitment(session, tenant):
    """Refused, not defaulted. A row of zeros would look like an answer."""
    with pytest.raises(records.LedgerError) as exc:
        await records.record(
            session, tenant_id=tenant.id, purpose="case_qa",
            model_id="ollama/llama3.2")
    assert "input commitment" in str(exc.value)


async def test_the_raw_prompt_is_not_stored_by_default(session, tenant):
    """The ledger holds commitments, not content.

    A governance record that quietly becomes the largest PII store on the box
    is a worse outcome than no record at all.
    """
    row = await _rec(session, tenant, input_text="patient name is Ada Lovelace")
    assert row.prompt_sealed is None
    assert row.output_sealed is None
    assert row.input_sha256 == records.digest("patient name is Ada Lovelace")


async def test_an_empty_input_is_not_a_missing_input(session, tenant):
    """`digest("")` is a real hash; `digest(None)` is None. "The model was
    given nothing" and "we did not record what it was given" are different
    facts and must not collapse."""
    assert records.digest("") is not None
    assert records.digest(None) is None


async def test_the_verdict_vocabulary_is_closed(session, tenant):
    row = await _rec(session, tenant)
    with pytest.raises(records.LedgerError):
        await records.record_verdict(
            session, record_id=row.id, verdict="approved-ish")


async def test_a_second_verdict_appends(session, tenant):
    """'Accepted, then reversed three days later' is the sequence the override
    report exists to show. An UPDATE would erase it."""
    row = await _rec(session, tenant)
    await records.record_verdict(session, record_id=row.id, verdict="accepted",
                                 decided_by="alice")
    await records.record_verdict(session, record_id=row.id, verdict="rejected",
                                 decided_by="bob")
    await session.commit()

    all_rows = (await session.execute(
        select(AIDecisionVerdictModel)
        .where(AIDecisionVerdictModel.record_id == row.id)
    )).scalars().all()
    assert len(all_rows) == 2
    latest = await records.latest_verdicts(session, [row.id])
    assert latest[str(row.id)].verdict == "rejected"


# ══ the seal ════════════════════════════════════════════════════════════════


async def test_sealing_then_verifying(session, tenant):
    await _rec(session, tenant)
    # Backdate so the window is closed — the sealer refuses to seal up to now.
    await session.execute(
        AIDecisionRecordModel.__table__.update()
        .values(recorded_at=_past(600)))
    await session.commit()

    sealed = await chain.seal_once(session)
    assert sealed >= 1
    ok, note = await chain.verify_chain(session)
    assert ok, note


async def test_an_edit_to_a_sealed_row_is_detected(session, tenant):
    """The product claim, executed as code."""
    row = await _rec(session, tenant)
    await session.execute(
        AIDecisionRecordModel.__table__.update().values(recorded_at=_past(600)))
    await session.commit()
    await chain.seal_once(session)
    assert (await chain.verify_chain(session))[0]

    await session.execute(
        AIDecisionRecordModel.__table__.update()
        .where(AIDecisionRecordModel.id == row.id)
        .values(model_id="ollama/a-different-model"))
    await session.commit()

    ok, problem = await chain.verify_chain(session)
    assert not ok
    assert "does not match what was sealed" in (problem or "")


async def test_a_verdict_arriving_later_does_not_break_the_seal(session, tenant):
    """The ordinary case, and the one a 'hash the whole row' design breaks.

    A verdict lands minutes or days after the decision. If it lived on the
    record, the seal would fail the moment somebody reviewed something —
    reporting an ordinary human action as tampering.
    """
    row = await _rec(session, tenant)
    await session.execute(
        AIDecisionRecordModel.__table__.update().values(recorded_at=_past(600)))
    await session.commit()
    await chain.seal_once(session)

    await records.record_verdict(session, record_id=row.id, verdict="accepted",
                                 decided_by="alice")
    await session.commit()

    ok, note = await chain.verify_chain(session)
    assert ok, note


async def test_a_verdict_is_itself_sealed_and_then_tamper_evident(session, tenant):
    """A verdict is evidence, not a footnote. Sealing it separately would let a
    human's override be rewritten while the decision still verified."""
    row = await _rec(session, tenant)
    v = await records.record_verdict(session, record_id=row.id,
                                     verdict="accepted", decided_by="alice")
    await session.execute(
        AIDecisionRecordModel.__table__.update().values(recorded_at=_past(600)))
    await session.execute(
        AIDecisionVerdictModel.__table__.update().values(decided_at=_past(590)))
    await session.commit()
    await chain.seal_once(session)
    assert (await chain.verify_chain(session))[0]

    await session.execute(
        AIDecisionVerdictModel.__table__.update()
        .where(AIDecisionVerdictModel.id == v.id)
        .values(verdict="rejected"))
    await session.commit()
    ok, problem = await chain.verify_chain(session)
    assert not ok, "an altered verdict verified — the chain does not cover it"


async def test_a_deleted_sealed_row_is_detected(session, tenant):
    """A row removed WITHOUT a tombstone is tampering, and must read as such."""
    row = await _rec(session, tenant)
    await session.execute(
        AIDecisionRecordModel.__table__.update().values(recorded_at=_past(600)))
    await session.commit()
    await chain.seal_once(session)

    await session.execute(
        AIDecisionRecordModel.__table__.delete()
        .where(AIDecisionRecordModel.id == row.id))
    await session.commit()

    ok, problem = await chain.verify_chain(session)
    assert not ok
    assert "not tombstoned" in (problem or "")


async def test_retention_tombstones_instead_of_breaking_the_chain(session, tenant):
    """Migration 180's defect, not repeated.

    A retention horizon that deletes the rows a sealed batch covers makes the
    seal unverifiable forever. The tombstone keeps the link and says honestly
    that the content is gone.
    """
    await _rec(session, tenant)
    await session.execute(
        AIDecisionRecordModel.__table__.update().values(recorded_at=_past(600)))
    await session.commit()
    await chain.seal_once(session)

    result = await chain.purge_before(
        session, before=datetime.now(timezone.utc) - timedelta(seconds=300))
    assert result["records"] == 1
    assert result["batches"] == 1

    ok, note = await chain.verify_chain(session)
    assert ok, note
    assert "pruned by retention" in (note or "")
    batch = (await session.execute(select(AILedgerChainModel))).scalars().first()
    assert batch.pruned_at is not None
    assert batch.pruned_count == 1


async def test_an_unsealed_row_is_never_purged(session, tenant):
    """It is covered by no batch, so deleting it leaves a hole nothing records
    — and a hole in the windows is how removal is detected."""
    await _rec(session, tenant)
    await session.execute(
        AIDecisionRecordModel.__table__.update().values(recorded_at=_past(600)))
    await session.commit()
    # deliberately NOT sealed

    result = await chain.purge_before(
        session, before=datetime.now(timezone.utc))
    assert result["records"] == 0
    assert (await session.execute(
        select(AIDecisionRecordModel))).scalars().first() is not None


async def test_an_empty_window_is_still_sealed(session, tenant):
    """Skipping empty windows would leave gaps indistinguishable from deleted
    rows, and the whole value of contiguous windows is that a gap means
    something."""
    await _rec(session, tenant)
    await session.execute(
        AIDecisionRecordModel.__table__.update().values(recorded_at=_past(600)))
    await session.commit()
    await chain.seal_once(session)

    batches = (await session.execute(
        select(AILedgerChainModel).order_by(AILedgerChainModel.sequence)
    )).scalars().all()
    assert len(batches) > 1, "only the populated window was sealed"
    assert any(b.record_count == 0 for b in batches)
    # contiguous: each window starts exactly where the last ended
    for a, b in zip(batches, batches[1:]):
        assert a.window_end == b.window_start


async def test_sealing_is_idempotent(session, tenant):
    await _rec(session, tenant)
    await session.execute(
        AIDecisionRecordModel.__table__.update().values(recorded_at=_past(600)))
    await session.commit()
    first = await chain.seal_once(session)
    second = await chain.seal_once(session)
    assert first >= 1
    assert second == 0
    assert (await chain.verify_chain(session))[0]


async def test_a_verdict_older_than_any_record_still_starts_the_chain(
        session, tenant):
    """The sealer only walks forward, so the first window has to begin at the
    oldest unsealed row in EITHER table. Taking the records' minimum alone
    would leave an earlier verdict unsealable forever."""
    row = await _rec(session, tenant)
    v = await records.record_verdict(session, record_id=row.id,
                                     verdict="accepted", decided_by="a")
    await session.execute(
        AIDecisionRecordModel.__table__.update().values(recorded_at=_past(300)))
    await session.execute(
        AIDecisionVerdictModel.__table__.update().values(decided_at=_past(900)))
    await session.commit()

    await chain.seal_once(session)
    refreshed = await session.get(AIDecisionVerdictModel, v.id)
    await session.refresh(refreshed)
    assert refreshed.chain_sequence is not None, "the earlier verdict was skipped"


# ══ reporting ═══════════════════════════════════════════════════════════════


async def test_override_rate_is_none_when_nothing_was_reviewed(session, tenant):
    """0% over zero reviews reads as 'the model is never wrong' and means
    'nobody looked'."""
    await _rec(session, tenant)
    report = await records.override_rates(session, tenant_id=tenant.id)
    assert report["by_model"]["ollama/llama3.2"]["override_rate"] is None


async def test_override_rate_counts_the_newest_verdict_only(session, tenant):
    """A decision reviewed twice is one decision; counting both rows would
    report an override rate above 100% for a diligent reviewer."""
    row = await _rec(session, tenant)
    await records.record_verdict(session, record_id=row.id, verdict="rejected")
    await records.record_verdict(session, record_id=row.id, verdict="accepted")
    await session.commit()

    report = await records.override_rates(session, tenant_id=tenant.id)
    bucket = report["by_model"]["ollama/llama3.2"]
    assert bucket["reviewed"] == 1
    assert bucket["accepted"] == 1
    assert bucket["override_rate"] == 0.0


async def test_the_model_inventory_reports_what_ran(session, tenant):
    await _rec(session, tenant, model_id="ollama/llama3.2")
    await _rec(session, tenant, model_id="openai/gpt-4o",
               provider_class="external")
    inventory = await records.model_inventory(session, tenant_id=tenant.id)
    by_model = {m["model_id"]: m for m in inventory}
    assert by_model["openai/gpt-4o"]["provider_class"] == "external"
    assert by_model["ollama/llama3.2"]["calls"] == 1


async def test_the_evidence_pack_states_its_own_verification(session, tenant):
    """A dossier that says 'verified' without recomputing the links is worse
    than none: it is a claim an opposing expert falsifies in one command."""
    row = await _rec(session, tenant)
    await records.record_verdict(session, record_id=row.id, verdict="edited",
                                 decided_by="alice", diff_text='{"value": 7}')
    await session.commit()

    built = await evidence.build_pack(
        session, tenant_id=tenant.id,
        period_start=datetime.now(timezone.utc) - timedelta(days=1),
        period_end=datetime.now(timezone.utc) + timedelta(minutes=1))
    pack = built["pack"]
    assert pack["framework"] == "ai_governance"
    assert pack["evidence"]["integrity"]["chain_verified"] in (True, False)
    assert pack["evidence"]["human_oversight"]["overridden"] == 1
    assert pack["evidence"]["human_oversight"]["override_rate"] == 1.0
    d = pack["evidence"]["decisions"][0]
    assert d["human_review"]["verdict"] == "edited"
    assert d["input"]["raw_retained"] is False
    # The pack must not leak content it deliberately did not store.
    assert "prompt" not in d["input"]


async def test_the_pack_reports_unsealed_decisions_rather_than_hiding_them(
        session, tenant):
    await _rec(session, tenant)
    built = await evidence.build_pack(
        session, tenant_id=tenant.id,
        period_start=datetime.now(timezone.utc) - timedelta(days=1),
        period_end=datetime.now(timezone.utc) + timedelta(minutes=1))
    assert built["pack"]["evidence"]["integrity"]["decisions_not_yet_sealed"] == 1


# ══ the API ═════════════════════════════════════════════════════════════════


def _headers(roles: list[str]) -> dict[str, str]:
    from velaris_shared.auth.jwt_handler import create_dev_token
    from velaris_shared.config import get_settings

    s = get_settings()
    token = create_dev_token(
        user_id=str(uuid.uuid4()), username="ledger-user", roles=roles,
        secret=s.auth_secret, private_key=s.auth_rsa_private_key or "")
    return {"Authorization": f"Bearer {token}"}


async def test_every_route_refuses_an_anonymous_caller(anon_client, tenant):
    from case_service.api.routers.ai_ledger import router

    for route in router.routes:
        path = (route.path.replace("{record_id}", str(uuid.uuid4())))
        for method in sorted(route.methods):
            r = await anon_client.request(method, f"/api/v1{path}", json={})
            assert r.status_code in (401, 403), f"{method} {path} -> {r.status_code}"


async def test_a_case_worker_can_record_a_verdict_but_not_read_the_ledger(
        client, session, tenant):
    """The separation that stops an auditor manufacturing the oversight they
    are auditing — and its mirror: a case worker reviews without gaining the
    governance surface."""
    row = await _rec(session, tenant)
    h = _headers(["case_worker"])

    r = await client.post(f"{PREFIX}/records/{row.id}/verdict", headers=h,
                          json={"verdict": "accepted"})
    assert r.status_code == 201, r.text

    r = await client.get(f"{PREFIX}/records", headers=h)
    assert r.status_code == 403


async def test_an_auditor_reads_without_the_stored_text(client, session, tenant):
    row = await _rec(session, tenant)
    r = await client.get(f"{PREFIX}/records", headers=_headers(["security"]))
    assert r.status_code == 200
    body = r.json()["records"][0]
    assert body["input_sha256"] == row.input_sha256
    assert "prompt" not in body and "input_text" not in body


async def test_another_tenants_record_is_404(client, session, tenant):
    other = TenantModel(id=uuid.uuid4(), slug="other", name="Other")
    session.add(other)
    await session.commit()
    theirs = await records.record(
        session, tenant_id=other.id, purpose="case_qa",
        model_id="ollama/llama3.2", input_text="theirs")
    await session.commit()

    r = await client.get(f"{PREFIX}/records/{theirs.id}",
                         headers=_headers(["admin"]))
    assert r.status_code == 404


async def test_the_evidence_pack_route_returns_a_dossier(client, session, tenant):
    await _rec(session, tenant)
    r = await client.get(f"{PREFIX}/evidence-pack?days=1",
                         headers=_headers(["admin"]))
    assert r.status_code == 200, r.text
    assert r.json()["pack"]["framework"] == "ai_governance"
    assert r.json()["summary"]["decisions"] == 1


async def test_unknown_fields_on_a_verdict_are_refused(client, session, tenant):
    row = await _rec(session, tenant)
    r = await client.post(f"{PREFIX}/records/{row.id}/verdict",
                          headers=_headers(["admin"]),
                          json={"verdict": "accepted", "resaon": "typo"})
    assert r.status_code == 422
