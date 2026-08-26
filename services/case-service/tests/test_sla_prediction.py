"""Statistical SLA prediction (#40, §4.5).

Percentile arithmetic over history the platform already records — no model,
no training. The behaviour worth pinning is what it does with *thin* history:
a percentile drawn from three cases is not a weak signal, it is a misleading
one, so below MIN_SAMPLES it must refuse rather than emit a confident number.

Also covers the half that had to be built first: nothing recorded stage
entries, so stage-grain prediction had nothing to measure. `on_stage_entered`
now writes `stage_enter` / `stage_exit` to the process event log.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest

from case_service.core import sla_prediction as sp
from velaris_shared.db.models import (
    CaseEventLogModel,
    CaseInstanceModel,
    CaseTypeModel,
)

pytestmark = pytest.mark.asyncio

NOW = datetime(2026, 8, 1, 12, 0, tzinfo=timezone.utc)


class TestPercentileMath:
    async def test_percentile_of_empty_is_zero(self):
        assert sp.percentile_of([], 5.0) == 0.0

    async def test_percentile_counts_strictly_below(self):
        values = [float(i) for i in range(100)]
        assert sp.percentile_of(values, 0.0) == 0.0      # nothing is below 0
        assert sp.percentile_of(values, 50.0) == 50.0
        assert sp.percentile_of(values, 1000.0) == 100.0

    async def test_ties_do_not_inflate_the_percentile(self):
        """Ten identical cases: being equal to them is not being slower."""
        assert sp.percentile_of([5.0] * 10, 5.0) == 0.0

    async def test_value_at_percentile(self):
        values = [float(i) for i in range(101)]
        assert sp.value_at_percentile(values, 50.0) == 50.0
        assert sp.value_at_percentile(values, 0.0) == 0.0
        assert sp.value_at_percentile(values, 100.0) == 100.0

    async def test_value_at_percentile_empty(self):
        assert sp.value_at_percentile([], 85.0) == 0.0


async def _case_type(session, name=None) -> CaseTypeModel:
    ct = CaseTypeModel(
        name=name or f"CT {uuid.uuid4().hex[:6]}",
        version="1.0.0",
        definition_json={"stages": []},
    )
    session.add(ct)
    await session.flush()
    return ct


async def _resolved_case(session, ct, seconds: float, *, created=None):
    created = created or NOW - timedelta(days=30)
    c = CaseInstanceModel(
        case_type_id=ct.id,
        case_type_version="1.0.0",
        status="resolved",
        created_at=created,
        resolved_at=created + timedelta(seconds=seconds),
    )
    session.add(c)
    await session.flush()
    return c


async def _open_case(session, ct, age_seconds: float, stage_id=None):
    c = CaseInstanceModel(
        case_type_id=ct.id,
        case_type_version="1.0.0",
        status="open",
        created_at=NOW - timedelta(seconds=age_seconds),
        current_stage_id=stage_id,
    )
    session.add(c)
    await session.flush()
    return c


class TestCaseGrain:
    async def test_refuses_below_min_samples(self, session):
        """The whole point: thin history reports as thin, not as a percentile."""
        ct = await _case_type(session)
        for i in range(sp.MIN_SAMPLES - 1):
            await _resolved_case(session, ct, 100.0 + i)
        case = await _open_case(session, ct, 10_000.0)

        out = await sp.predict_for_case(session, case.id, now=NOW)
        assert out["case"]["status"] == "insufficient_data"
        assert out["case"]["percentile"] is None
        assert out["case"]["samples"] == sp.MIN_SAMPLES - 1
        assert out["at_risk"] is False

    async def test_predicts_at_min_samples(self, session):
        ct = await _case_type(session)
        for i in range(sp.MIN_SAMPLES):
            await _resolved_case(session, ct, float(i + 1))
        case = await _open_case(session, ct, 1.0)

        out = await sp.predict_for_case(session, case.id, now=NOW)
        assert out["case"]["status"] == "ok"
        assert out["case"]["samples"] == sp.MIN_SAMPLES

    async def test_old_case_is_flagged_at_risk(self, session):
        ct = await _case_type(session)
        for i in range(sp.MIN_SAMPLES):
            await _resolved_case(session, ct, float(i + 1))
        case = await _open_case(session, ct, 10_000.0)   # older than all history

        out = await sp.predict_for_case(session, case.id, now=NOW)
        assert out["case"]["percentile"] == 100.0
        assert out["case"]["at_risk"] is True
        assert out["at_risk"] is True

    async def test_young_case_is_not_flagged(self, session):
        ct = await _case_type(session)
        for i in range(sp.MIN_SAMPLES):
            await _resolved_case(session, ct, 1000.0 + i)
        case = await _open_case(session, ct, 1.0)

        out = await sp.predict_for_case(session, case.id, now=NOW)
        assert out["case"]["percentile"] == 0.0
        assert out["case"]["at_risk"] is False

    async def test_resolved_case_reports_final_position(self, session):
        """Once resolved the age stops growing — the question is no longer predictive."""
        ct = await _case_type(session)
        for i in range(sp.MIN_SAMPLES):
            await _resolved_case(session, ct, float(i + 1))
        subject = await _resolved_case(session, ct, 5.0)

        out = await sp.predict_for_case(session, subject.id, now=NOW + timedelta(days=999))
        assert out["resolved"] is True
        assert out["case"]["age_seconds"] == 5.0

    async def test_negative_duration_is_not_a_sample(self, session):
        """Backdated import / clock skew must not poison the distribution."""
        ct = await _case_type(session)
        c = CaseInstanceModel(
            case_type_id=ct.id, case_type_version="1.0.0", status="resolved",
            created_at=NOW, resolved_at=NOW - timedelta(hours=1),
        )
        session.add(c)
        await session.flush()
        assert await sp.case_type_durations(session, ct.id) == []

    async def test_unknown_case_raises(self, session):
        with pytest.raises(ValueError, match="not found"):
            await sp.predict_for_case(session, uuid.uuid4(), now=NOW)


async def _stage_event(session, case, stage_id, activity_type, ts):
    session.add(CaseEventLogModel(
        case_id=case.id, case_type_id=case.case_type_id,
        activity=stage_id, activity_type=activity_type,
        stage_id=stage_id, timestamp=ts,
    ))
    await session.flush()


class TestStageGrain:
    async def test_pairs_enter_with_exit(self, session):
        ct = await _case_type(session)
        c = await _open_case(session, ct, 100.0, stage_id="review")
        await _stage_event(session, c, "review", "stage_enter", NOW - timedelta(seconds=60))
        await _stage_event(session, c, "review", "stage_exit", NOW)

        assert await sp.stage_durations(session, ct.id, "review") == [60.0]

    async def test_loopback_counts_each_visit(self, session):
        """Re-entering a stage is two samples, not one merged span."""
        ct = await _case_type(session)
        c = await _open_case(session, ct, 100.0, stage_id="review")
        base = NOW - timedelta(hours=5)
        await _stage_event(session, c, "review", "stage_enter", base)
        await _stage_event(session, c, "review", "stage_exit", base + timedelta(seconds=10))
        await _stage_event(session, c, "review", "stage_enter", base + timedelta(seconds=100))
        await _stage_event(session, c, "review", "stage_exit", base + timedelta(seconds=130))

        assert await sp.stage_durations(session, ct.id, "review") == [10.0, 30.0]

    async def test_unclosed_stage_is_not_a_sample(self, session):
        ct = await _case_type(session)
        c = await _open_case(session, ct, 100.0, stage_id="review")
        await _stage_event(session, c, "review", "stage_enter", NOW - timedelta(seconds=60))

        assert await sp.stage_durations(session, ct.id, "review") == []

    async def test_missing_stage_event_reports_plainly(self, session):
        ct = await _case_type(session)
        c = await _open_case(session, ct, 100.0, stage_id="review")

        out = await sp.predict_for_case(session, c.id, now=NOW)
        assert out["stage"]["status"] == "insufficient_data"
        assert "no stage_enter event" in out["stage"]["reason"]

    async def test_case_with_no_stage(self, session):
        ct = await _case_type(session)
        c = await _open_case(session, ct, 100.0, stage_id=None)

        out = await sp.predict_for_case(session, c.id, now=NOW)
        assert out["stage"]["reason"] == "case is not in a stage"


class TestStageRecording:
    async def test_entering_a_stage_writes_stage_enter(self, session):
        """Nothing recorded this before #40 — log_stage_transition had no caller."""
        from case_service.core import case_lifecycle
        from sqlalchemy import select

        ct = await _case_type(session)
        c = await _open_case(session, ct, 10.0, stage_id=None)
        await session.commit()

        await case_lifecycle.on_stage_entered(
            session, case_id=c.id, stage={"id": "intake", "steps": []},
            case_type_def={"stages": [], "sla_policies": []},
        )
        await session.commit()

        rows = (await session.execute(
            select(CaseEventLogModel.activity_type, CaseEventLogModel.stage_id)
            .where(CaseEventLogModel.case_id == c.id)
        )).all()
        assert ("stage_enter", "intake") in rows

    async def test_moving_stages_closes_the_previous_one(self, session):
        from case_service.core import case_lifecycle
        from sqlalchemy import select

        ct = await _case_type(session)
        c = await _open_case(session, ct, 10.0, stage_id="intake")
        await session.commit()

        await case_lifecycle.on_stage_entered(
            session, case_id=c.id, stage={"id": "review", "steps": []},
            case_type_def={"stages": [], "sla_policies": []},
        )
        await session.commit()

        rows = (await session.execute(
            select(CaseEventLogModel.activity_type, CaseEventLogModel.stage_id)
            .where(CaseEventLogModel.case_id == c.id)
        )).all()
        assert ("stage_exit", "intake") in rows
        assert ("stage_enter", "review") in rows


class TestEndpoints:
    async def test_prediction_endpoint(self, session, client):
        ct = await _case_type(session)
        c = await _open_case(session, ct, 100.0)
        await session.commit()

        resp = await client.get(f"/api/v1/cases/{c.id}/sla/prediction")
        assert resp.status_code == 200, resp.text
        assert resp.json()["case"]["status"] == "insufficient_data"

    async def test_prediction_unknown_case_404(self, client):
        resp = await client.get(f"/api/v1/cases/{uuid.uuid4()}/sla/prediction")
        assert resp.status_code == 404

    async def test_distribution_endpoint_reports_insufficiency(self, session, client):
        ct = await _case_type(session)
        await _resolved_case(session, ct, 42.0)
        await session.commit()

        resp = await client.get(f"/api/v1/case-types/{ct.id}/sla/distribution")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["sufficient"] is False
        assert body["samples"] == 1
        assert body["grain"] == "case"
