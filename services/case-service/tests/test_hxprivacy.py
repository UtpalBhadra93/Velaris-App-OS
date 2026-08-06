"""HxPrivacy — PII discovery & classification (§8.10).

Detection is heuristic and drives retention/residency policy, so the tests
concentrate on the two ways that goes wrong:

* a **false positive** silently over-restricts a harmless field — so a bare
  "name" must be reported at low confidence, as a question, not as a verdict;
* a **false negative** leaves personal data untagged while the report looks
  clean — so weak signals are surfaced rather than discarded.

And the reconciliation rule that makes the list trustworthy: a rescan must
never overwrite a human decision, or every scan re-opens every dismissal.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import uuid

import pytest

from case_service.compliance import pii_detect as d
from case_service.compliance import pii_scan as sc
from case_service.db.models import FormDefinitionModel, PIIFindingModel

pytestmark = pytest.mark.asyncio


class TestDetector:
    async def test_declared_type_beats_a_name_guess(self):
        f = d.classify_field("contact", "Contact", "email")
        assert f["category"] == d.EMAIL
        assert f["confidence"] >= 0.95      # chosen deliberately, not inherited

    async def test_common_categories(self):
        cases = [
            ("customer_email", d.EMAIL),
            ("mobile_number", d.PHONE),
            ("date_of_birth", d.DATE_OF_BIRTH),
            ("ssn", d.NATIONAL_ID),
            ("iban", d.FINANCIAL),
            ("home_address", d.ADDRESS),
            ("first_name", d.NAME),
            ("client_ip", d.IP_ADDRESS),
        ]
        for key, expected in cases:
            got = d.classify_field(key)
            assert got is not None and got["category"] == expected, key

    async def test_special_categories_are_flagged(self):
        for key in ("medical_history", "fingerprint_hash"):
            f = d.classify_field(key)
            assert f["special_category"] is True, key

    async def test_ordinary_pii_is_not_special_category(self):
        assert d.classify_field("customer_email")["special_category"] is False

    async def test_bare_name_is_low_confidence_not_a_verdict(self):
        """As often a case or product name as a person's."""
        f = d.classify_field("name", "Name")
        assert f["category"] == d.NAME
        assert f["confidence"] < 0.5
        assert "human decision" in f["reason"]

    async def test_non_personal_fields_return_nothing(self):
        for key in ("amount", "status", "priority", "order_count"):
            assert d.classify_field(key) is None, key

    async def test_date_of_birth_is_not_swallowed_by_a_generic_rule(self):
        assert d.classify_field("dob")["category"] == d.DATE_OF_BIRTH

    async def test_never_raises_on_odd_input(self):
        """A crash would leave the rest of the form unexamined."""
        assert d.classify_field(None, None, None) is None       # type: ignore[arg-type]

    async def test_every_finding_carries_a_reason(self):
        f = d.classify_field("customer_email")
        assert f["reason"] and isinstance(f["reason"], str)


class TestFormScanning:
    async def test_scans_the_real_form_shape(self):
        definition = {"sections": [{"id": "s1", "fields": [
            {"field_key": "email", "label": "Email", "type": "email"},
            {"field_key": "amount", "label": "Amount", "type": "number"},
        ]}]}
        out = d.scan_form_definition(definition)
        assert len(out) == 1
        assert out[0]["field_path"] == "s1.email"

    async def test_empty_definition_is_safe(self):
        assert d.scan_form_definition({}) == []
        assert d.scan_data_model({}) == []


class TestReconciliation:
    async def _form(self, session, fields):
        form = FormDefinitionModel(
            name=f"F {uuid.uuid4().hex[:6]}", version="1.0.0",
            definition_json={"sections": [{"id": "s1", "fields": fields}]},
        )
        session.add(form)
        await session.flush()
        return form

    async def test_rescan_is_idempotent(self, session):
        await self._form(session, [{"field_key": "customer_email", "label": "Email"}])
        first = await sc.scan_all(session)
        second = await sc.scan_all(session)
        assert first["findings_created"] >= 1
        assert second["findings_created"] == 0     # updated, not duplicated

    async def test_rescan_never_overwrites_a_human_verdict(self, session):
        """Otherwise every scan re-opens every dismissal."""
        form = await self._form(session, [{"field_key": "ssn", "label": "SSN"}])
        await sc.scan_all(session)

        from sqlalchemy import select
        row = (await session.execute(
            select(PIIFindingModel).where(PIIFindingModel.resource_id == form.id)
        )).scalars().first()
        await sc.review(session, row.id, decision=sc.DISMISSED, actor_id="reviewer")
        await session.flush()

        await sc.scan_all(session)
        await session.refresh(row)
        assert row.status == sc.DISMISSED
        assert row.reviewed_by == "reviewer"

    async def test_disappearing_field_is_stale_not_deleted(self, session):
        """That personal data WAS collected is itself worth keeping."""
        form = await self._form(session, [{"field_key": "customer_email"}])
        await sc.scan_all(session)

        form.definition_json = {"sections": [{"id": "s1", "fields": [
            {"field_key": "amount"},
        ]}]}
        await session.flush()
        await sc.scan_all(session)

        from sqlalchemy import select
        row = (await session.execute(
            select(PIIFindingModel).where(PIIFindingModel.resource_id == form.id)
        )).scalars().first()
        assert row is not None                 # not deleted
        assert row.status == sc.STALE

    async def test_review_rejects_an_unknown_decision(self, session):
        form = await self._form(session, [{"field_key": "customer_email"}])
        await sc.scan_all(session)

        from sqlalchemy import select
        row = (await session.execute(
            select(PIIFindingModel).where(PIIFindingModel.resource_id == form.id)
        )).scalars().first()
        with pytest.raises(ValueError, match="decision must be"):
            await sc.review(session, row.id, decision="maybe", actor_id="x")


class TestApiSurface:
    async def test_endpoints_are_privileged(self, anon_client):
        for path in ("/api/v1/compliance/privacy/findings",):
            r = await anon_client.get(path)
            assert r.status_code in (401, 403), path

    async def test_scan_and_list(self, client, session):
        form = FormDefinitionModel(
            name=f"API {uuid.uuid4().hex[:6]}", version="1.0.0",
            definition_json={"sections": [{"id": "s1", "fields": [
                {"field_key": "passport_number", "label": "Passport"},
            ]}]},
        )
        session.add(form)
        await session.commit()

        r = await client.post("/api/v1/compliance/privacy/scan")
        assert r.status_code == 200, r.text

        got = await client.get("/api/v1/compliance/privacy/findings")
        assert got.status_code == 200, got.text
        body = got.json()
        assert body["summary"]["total"] >= 1
        assert any(f["category"] == d.NATIONAL_ID for f in body["findings"])
