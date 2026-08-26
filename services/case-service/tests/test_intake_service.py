"""Inbound intake, tested without HTTP.

The logic previously lived inside the webhook route, so it could only be
exercised through a request — which is why none of these cases had tests. The
extraction is the point: the ingest path is the same one any future transport
would use, and #42 (MQTT, §2.9) stays unbuilt precisely because a thin adapter
over this is all it would ever need.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import uuid

import pytest

from velaris_shared.db.models import CaseTypeModel
from case_service.intake import service as intake
from case_service.intake.filters import apply_field_mapping, evaluate_filters

pytestmark = pytest.mark.asyncio


async def _case_type(session, *, trigger="webhook", filters=None, mapping=None):
    ct = CaseTypeModel(
        name=f"IN {uuid.uuid4().hex[:6]}", version="1.0.0",
        definition_json={"stages": []}, intake_trigger=trigger,
        filter_conditions=filters or {}, field_mapping=mapping or {},
    )
    session.add(ct)
    await session.flush()
    return ct


class TestIngest:
    async def test_creates_a_case(self, session):
        ct = await _case_type(session)
        out = await intake.ingest(
            session, case_type_id=ct.id, payload={"hello": "world"},
        )
        assert out["accepted"] is True
        assert out["case_id"]

    async def test_unknown_case_type_raises_with_404(self, session):
        """A domain error carrying the status, not an HTTPException — a queue
        consumer has no 404 to raise."""
        with pytest.raises(intake.IntakeError) as exc:
            await intake.ingest(session, case_type_id=uuid.uuid4(), payload={})
        assert exc.value.status == 404

    async def test_wrong_trigger_is_refused(self, session):
        ct = await _case_type(session, trigger="manual")
        with pytest.raises(intake.IntakeError) as exc:
            await intake.ingest(session, case_type_id=ct.id, payload={})
        assert exc.value.status == 400

    async def test_non_dict_payload_is_tolerated(self, session):
        ct = await _case_type(session)
        out = await intake.ingest(session, case_type_id=ct.id, payload=None)  # type: ignore[arg-type]
        assert out["accepted"] is True

    async def test_source_kind_is_recorded_on_the_case(self, session):
        """What a second transport would set — proving it is not hardcoded."""
        from velaris_shared.db.models import CaseInstanceModel

        ct = await _case_type(session)
        out = await intake.ingest(
            session, case_type_id=ct.id, payload={"a": 1}, source_kind="mqtt",
        )
        case = await session.get(CaseInstanceModel, uuid.UUID(out["case_id"]))
        assert case.data["_intake_source"] == "mqtt"
        assert case.created_by == "intake_mqtt"

    async def test_service_does_not_commit(self, session):
        """The caller owns the transaction — a batching consumer needs that."""
        import inspect

        src = inspect.getsource(intake.ingest)
        assert "session.commit" not in src


class TestFiltering:
    async def test_filtered_payload_creates_no_case(self, session):
        ct = await _case_type(session, filters={
            "logic": "and",
            "rules": [{"field": "type", "operator": "eq", "value": "claim"}],
        })
        out = await intake.ingest(
            session, case_type_id=ct.id, payload={"type": "something_else"},
        )
        assert out["accepted"] is False
        assert out["reason"] == "filter_conditions not met"
        assert "case_id" not in out

    async def test_filtered_event_is_still_recorded(self, session):
        """So "why was nothing created" stays answerable."""
        from velaris_shared.db.models import IntakeEventModel

        ct = await _case_type(session, filters={
            "rules": [{"field": "type", "operator": "eq", "value": "claim"}],
        })
        out = await intake.ingest(session, case_type_id=ct.id, payload={"type": "no"})
        event = await session.get(IntakeEventModel, uuid.UUID(out["event_id"]))
        assert event.status == "filtered"

    async def test_matching_payload_passes(self, session):
        ct = await _case_type(session, filters={
            "rules": [{"field": "type", "operator": "eq", "value": "claim"}],
        })
        out = await intake.ingest(session, case_type_id=ct.id, payload={"type": "claim"})
        assert out["accepted"] is True

    async def test_no_filters_accepts_everything(self):
        assert evaluate_filters({"anything": 1}, {}) == (True, [])


class TestMapping:
    async def test_dot_paths_map_to_case_fields(self, session):
        ct = await _case_type(session, mapping={"payload.amount": "claim_amount"})
        out = await intake.ingest(
            session, case_type_id=ct.id, payload={"payload": {"amount": 500}},
        )
        assert "claim_amount" in out["mapped_fields"]

    async def test_missing_source_path_is_skipped_not_null(self):
        assert apply_field_mapping({}, {"a.b": "x"}) == {}

    async def test_raw_payload_is_kept_for_traceability(self, session):
        from velaris_shared.db.models import CaseInstanceModel

        ct = await _case_type(session)
        payload = {"original": "value"}
        out = await intake.ingest(session, case_type_id=ct.id, payload=payload)
        case = await session.get(CaseInstanceModel, uuid.UUID(out["case_id"]))
        assert case.data["_raw_payload"] == payload
