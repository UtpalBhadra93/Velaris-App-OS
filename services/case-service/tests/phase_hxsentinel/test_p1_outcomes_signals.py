"""HxSentinel P1 — ground truth + signal store.

Pins the invariants P2/P3 will depend on:
  * the module is DARK until flagged, and 404s rather than 403s when off;
  * outcomes are APPEND-ONLY — re-labelling adds a row, history is preserved;
  * the core taxonomy cannot be shadowed by a tenant label;
  * identity signals are HASHED with the tenant DEK, never stored raw, and
    two tenants never produce the same digest for the same value;
  * extraction is idempotent;
  * bulk labelling reports per-case outcomes instead of failing the batch.

Copyright (c) 2024-2026 HELIX Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import uuid

import pytest
import pytest_asyncio
from sqlalchemy import select

from case_service.db.models import CaseOutcomeModel, CaseRiskSignalModel
from case_service.sentinel import service as sentinel
from case_service.sentinel import taxonomy
from case_service.sentinel.hashing import normalize, signal_digest
from tests.conftest import create_case, deploy_case_type

RISK = "/api/v1/risk"


@pytest.fixture(autouse=True)
def _enable_hxsentinel():
    from case_service.api.routers.releases import _ENABLED_VERSIONS
    prior = _ENABLED_VERSIONS.get("hxsentinel")
    _ENABLED_VERSIONS["hxsentinel"] = "v1.0.0"
    yield
    if prior is None:
        _ENABLED_VERSIONS.pop("hxsentinel", None)
    else:
        _ENABLED_VERSIONS["hxsentinel"] = prior


@pytest_asyncio.fixture
async def case(client):
    ct = await deploy_case_type(client, name="Sentinel CT")
    return await create_case(client, ct["id"], data={"applicant_email": "Alice@Example.COM ",
                                                     "contact_phone": "+44 20 7946 0018"})


# ── Flag gating ─────────────────────────────────────────────────────────────


class TestFlagGate:
    async def test_disabled_is_404_not_403(self, client, case):
        """Dark means indistinguishable from absent — never 403."""
        from case_service.api.routers.releases import _ENABLED_VERSIONS
        _ENABLED_VERSIONS.pop("hxsentinel", None)
        r = await client.get(f"{RISK}/taxonomy")
        assert r.status_code == 404

    async def test_enabled_lists_core_taxonomy(self, client):
        r = await client.get(f"{RISK}/taxonomy")
        assert r.status_code == 200
        keys = {lbl["key"] for lbl in r.json()["labels"]}
        assert taxonomy.CORE_KEYS <= keys
        assert all(lbl["is_core"] for lbl in r.json()["labels"])


# ── Taxonomy ────────────────────────────────────────────────────────────────


class TestTaxonomy:
    async def test_core_label_cannot_be_shadowed(self, client):
        """'fraud' must mean the same thing in every tenant."""
        r = await client.post(f"{RISK}/taxonomy", json={
            "key": "fraud", "display": "Our Fraud"})
        assert r.status_code == 400
        assert "core label" in r.json()["detail"]

    async def test_tenant_label_added_and_usable(self, client, case):
        r = await client.post(f"{RISK}/taxonomy", json={
            "key": "sim_swap", "display": "SIM swap", "description": "Telco-specific."})
        assert r.status_code == 201
        keys = {lbl["key"] for lbl in (await client.get(f"{RISK}/taxonomy")).json()["labels"]}
        assert "sim_swap" in keys

        r = await client.put(f"{RISK}/cases/{case['id']}/outcome",
                             json={"outcome": "sim_swap"})
        assert r.status_code == 200, r.text

    async def test_unknown_outcome_rejected(self, client, case):
        r = await client.put(f"{RISK}/cases/{case['id']}/outcome",
                             json={"outcome": "not_a_label"})
        assert r.status_code == 400


# ── Ground truth ────────────────────────────────────────────────────────────


class TestOutcomes:
    async def test_label_then_read_back(self, client, case):
        r = await client.put(f"{RISK}/cases/{case['id']}/outcome",
                             json={"outcome": "fraud", "note": "confirmed by issuer"})
        assert r.status_code == 200
        body = (await client.get(f"{RISK}/cases/{case['id']}/outcome")).json()
        assert body["current"]["outcome"] == "fraud"
        assert body["current"]["note"] == "confirmed by issuer"

    async def test_relabelling_appends_and_keeps_history(self, client, session, case):
        """A case cleared today and confirmed fraud in six months tells a
        different story from one labelled fraud at closure — keep both."""
        await client.put(f"{RISK}/cases/{case['id']}/outcome", json={"outcome": "clean"})
        await client.put(f"{RISK}/cases/{case['id']}/outcome", json={"outcome": "fraud"})

        body = (await client.get(f"{RISK}/cases/{case['id']}/outcome")).json()
        assert body["current"]["outcome"] == "fraud"
        assert [h["outcome"] for h in body["history"]] == ["fraud", "clean"]

        rows = (await session.execute(
            select(CaseOutcomeModel).where(
                CaseOutcomeModel.case_id == uuid.UUID(case["id"]))
        )).scalars().all()
        assert len(rows) == 2, "re-labelling must INSERT, never UPDATE"

    async def test_labelling_writes_audit(self, client, session, case):
        from case_service.db.models import CaseAuditLogModel
        await client.put(f"{RISK}/cases/{case['id']}/outcome", json={"outcome": "abuse"})
        actions = [r.action for r in (await session.execute(
            select(CaseAuditLogModel).where(
                CaseAuditLogModel.case_id == uuid.UUID(case["id"]))
        )).scalars().all()]
        assert "risk.outcome.labelled" in actions

    async def test_unknown_case_is_404(self, client):
        r = await client.put(f"{RISK}/cases/{uuid.uuid4()}/outcome",
                             json={"outcome": "fraud"})
        assert r.status_code == 404


class TestBulkLabelling:
    async def test_bulk_labels_many_and_reports_skips(self, client):
        ct = await deploy_case_type(client, name="Bulk CT")
        cases = [await create_case(client, ct["id"]) for _ in range(3)]
        missing = str(uuid.uuid4())

        r = await client.post(f"{RISK}/outcomes/bulk", json={
            "case_ids": [c["id"] for c in cases] + [missing],
            "outcome": "clean", "note": "retro sweep"})
        assert r.status_code == 200
        body = r.json()
        # One bad id must not discard the good work.
        assert body["labelled_count"] == 3
        assert body["skipped_count"] == 1
        assert body["skipped"][0]["case_id"] == missing

    async def test_bulk_rejects_unknown_label_before_writing(self, client):
        ct = await deploy_case_type(client, name="Bulk Reject CT")
        c = await create_case(client, ct["id"])
        r = await client.post(f"{RISK}/outcomes/bulk", json={
            "case_ids": [c["id"]], "outcome": "nope"})
        assert r.status_code == 400
        body = (await client.get(f"{RISK}/cases/{c['id']}/outcome")).json()
        assert body["current"] is None

    async def test_unlabelled_queue_is_tenant_scoped_before_limit(self, client, session):
        """Another tenant's closed cases must not consume the caller's limit.

        The test caller is tenantless, so its own cases are NULL-tenant. Fill
        the queue with more-recent closed cases in a DIFFERENT tenant that
        exceed the limit: the caller must still see its own case, not an empty
        page. Applying the tenant filter after LIMIT (the original bug) fails
        this — the other-tenant rows sort first and eat the whole limit.
        """
        import uuid as _uuid
        from datetime import datetime, timedelta, timezone
        from case_service.db.models import CaseInstanceModel

        from case_service.db.models import TenantModel

        # Create everything through the API FIRST, so no write transaction is
        # held open across a client call (the request has its own connection now
        # and would wait on our lock). Then mutate in one committed pass.
        ct = await deploy_case_type(client, name="Scoped CT")
        mine = await create_case(client, ct["id"])
        others = [await create_case(client, ct["id"]) for _ in range(6)]  # exceed limit=3

        other_tenant = _uuid.uuid4()
        row = await session.get(CaseInstanceModel, uuid.UUID(mine["id"]))
        row.status = "closed"
        row.closed_at = datetime.now(timezone.utc) - timedelta(days=10)  # older
        # Real engines enforce case_instances.tenant_id -> tenants; a fabricated
        # UUID would violate the FK. One unit of work, so the dependency sort
        # inserts the parent tenant before the dependent case_instances UPDATEs.
        session.add(TenantModel(id=other_tenant, slug=f"rt-{other_tenant}", name="risk-test-tenant"))
        for i, c in enumerate(others):
            r = await session.get(CaseInstanceModel, uuid.UUID(c["id"]))
            r.status = "closed"
            r.tenant_id = other_tenant                       # a different tenant
            r.closed_at = datetime.now(timezone.utc) - timedelta(minutes=i)  # newer
        await session.commit()

        ids = {x["case_id"] for x in
               (await client.get(f"{RISK}/outcomes/unlabelled?limit=3")).json()["cases"]}
        assert mine["id"] in ids
        # And none of the other tenant's cases leaked in.
        assert len(ids) == 1

    async def test_unlabelled_queue_excludes_labelled(self, client, session):
        ct = await deploy_case_type(client, name="Queue CT")
        c = await create_case(client, ct["id"])
        from case_service.db.models import CaseInstanceModel
        row = await session.get(CaseInstanceModel, uuid.UUID(c["id"]))
        row.status = "closed"
        await session.commit()

        ids = {x["case_id"] for x in
               (await client.get(f"{RISK}/outcomes/unlabelled")).json()["cases"]}
        assert c["id"] in ids

        await client.put(f"{RISK}/cases/{c['id']}/outcome", json={"outcome": "clean"})
        ids = {x["case_id"] for x in
               (await client.get(f"{RISK}/outcomes/unlabelled")).json()["cases"]}
        assert c["id"] not in ids


# ── Hashing ─────────────────────────────────────────────────────────────────


class TestHashing:
    def test_normalisation_collapses_formatting(self):
        assert normalize("identity_email", " Alice@Example.COM ") == "alice@example.com"
        # +44 and 0044 are the same number written two ways.
        assert normalize("identity_phone", "+44 20 7946 0018") == \
               normalize("identity_phone", "0044 (20) 7946-0018")

    def test_same_value_same_tenant_matches(self):
        dek = b"k" * 32
        a = signal_digest("alice@example.com", dek=dek, signal_type="identity_email")
        b = signal_digest("ALICE@example.com ", dek=dek, signal_type="identity_email")
        assert a == b

    def test_different_tenants_never_collide(self):
        """The whole cross-tenant isolation claim rests on this."""
        a = signal_digest("alice@example.com", dek=b"a" * 32, signal_type="identity_email")
        b = signal_digest("alice@example.com", dek=b"b" * 32, signal_type="identity_email")
        assert a != b

    def test_signal_type_is_folded_in(self):
        dek = b"k" * 32
        assert signal_digest("12345", dek=dek, signal_type="identity_phone") != \
               signal_digest("12345", dek=dek, signal_type="identity_address")

    def test_digest_is_not_the_plaintext(self):
        d = signal_digest("alice@example.com", dek=b"k" * 32, signal_type="identity_email")
        assert "alice" not in d and "example" not in d
        assert len(d) == 64

    def test_empty_and_keyless_refused(self):
        with pytest.raises(ValueError):
            signal_digest("   ", dek=b"k" * 32, signal_type="identity_email")
        with pytest.raises(ValueError):
            signal_digest("a@b.com", dek=b"", signal_type="identity_email")


# ── Signal extraction ───────────────────────────────────────────────────────


class TestSignals:
    async def test_identity_signals_are_hashed_never_raw(self, client, session, case):
        r = await client.post(f"{RISK}/cases/{case['id']}/signals/extract")
        assert r.status_code == 200

        rows = (await session.execute(
            select(CaseRiskSignalModel).where(
                CaseRiskSignalModel.case_id == uuid.UUID(case["id"]))
        )).scalars().all()
        identity = [r for r in rows if r.signal_type.startswith("identity_")]
        assert identity, "expected email/phone signals from the case variables"
        for row in identity:
            assert row.hashed is True
            # The raw value must appear nowhere — not in the key, not in value.
            blob = f"{row.signal_key}{row.value}".lower()
            assert "alice" not in blob
            assert "7946" not in blob

    async def test_extraction_is_idempotent(self, client, session, case):
        first = (await client.post(f"{RISK}/cases/{case['id']}/signals/extract")).json()
        second = (await client.post(f"{RISK}/cases/{case['id']}/signals/extract")).json()
        assert first["added"] > 0
        assert second["added"] == 0, "re-extraction must not duplicate rows"

    async def test_same_identity_across_cases_shares_digest(self, client, session):
        """Two cases with the same email must produce the same signal_key —
        this is the linkage P2 reasons over."""
        ct = await deploy_case_type(client, name="Link CT")
        a = await create_case(client, ct["id"], data={"applicant_email": "bob@example.com"})
        b = await create_case(client, ct["id"], data={"contact_email": "BOB@example.com"})
        await client.post(f"{RISK}/cases/{a['id']}/signals/extract")
        await client.post(f"{RISK}/cases/{b['id']}/signals/extract")

        keys = {}
        for cid in (a["id"], b["id"]):
            rows = (await session.execute(
                select(CaseRiskSignalModel).where(
                    CaseRiskSignalModel.case_id == uuid.UUID(cid),
                    CaseRiskSignalModel.signal_type == "identity_email")
            )).scalars().all()
            keys[cid] = {r.signal_key for r in rows}
        assert keys[a["id"]] and keys[a["id"]] == keys[b["id"]]

    async def test_signals_endpoint_returns_no_plaintext(self, client, case):
        await client.post(f"{RISK}/cases/{case['id']}/signals/extract")
        r = await client.get(f"{RISK}/cases/{case['id']}/signals")
        assert r.status_code == 200
        assert "alice" not in r.text.lower()

    async def test_no_identity_variables_yields_no_identity_signals(self, client, session):
        ct = await deploy_case_type(client, name="Bare CT")
        c = await create_case(client, ct["id"], data={"amount": "100", "notes": "none"})
        await client.post(f"{RISK}/cases/{c['id']}/signals/extract")
        rows = (await session.execute(
            select(CaseRiskSignalModel).where(
                CaseRiskSignalModel.case_id == uuid.UUID(c["id"]))
        )).scalars().all()
        assert not [r for r in rows if r.signal_type.startswith("identity_")]


class TestServiceLayer:
    async def test_get_current_outcome_none_when_unlabelled(self, session, client):
        ct = await deploy_case_type(client, name="Unlabelled CT")
        c = await create_case(client, ct["id"])
        assert await sentinel.get_current_outcome(session, uuid.UUID(c["id"])) is None

    async def test_extract_on_missing_case_is_empty_not_error(self, session):
        assert await sentinel.extract_signals(session, uuid.uuid4()) == []

    async def test_tenantless_case_stores_default_in_both_tables(self, client, session, case):
        """Outcomes and signals must agree on the tenant of a tenantless case.

        If one stored NULL and the other 'default', cross-case signal matching
        (which keys on tenant_id, and NULL never equals NULL in SQL) would
        silently fail to link two tenantless cases. Both must be 'default'.
        """
        await client.put(f"{RISK}/cases/{case['id']}/outcome", json={"outcome": "clean"})
        await client.post(f"{RISK}/cases/{case['id']}/signals/extract")

        oc = (await session.execute(
            select(CaseOutcomeModel.tenant_id).where(
                CaseOutcomeModel.case_id == uuid.UUID(case["id"]))
        )).scalars().all()
        sig = (await session.execute(
            select(CaseRiskSignalModel.tenant_id).where(
                CaseRiskSignalModel.case_id == uuid.UUID(case["id"]))
        )).scalars().all()
        assert oc and set(oc) == {"default"}
        assert sig and set(sig) == {"default"}
        assert None not in oc and None not in sig
