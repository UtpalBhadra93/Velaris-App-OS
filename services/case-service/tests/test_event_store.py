"""Event sourcing for the Case aggregate (#30a, report §2.2).

The relational row stays as the read model; this adds a verifiable history
beside it. What the tests pin is what would be silently wrong otherwise:

* an append-only stream that can be written out of order or with gaps,
* a "history" reconstructed for a case that never had one (§7.5 forbids it),
* a projection that agrees with the row because it peeked at the row,
* and drift going unnoticed when something writes state around the chokepoints.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import uuid

import pytest

from case_service.core import event_store as es
from velaris_shared.db.models import CaseEventModel, CaseInstanceModel, CaseTypeModel

pytestmark = pytest.mark.asyncio


async def _case(session, *, event_sourced=True, status="open", stage=None):
    ct = CaseTypeModel(name=f"ES {uuid.uuid4().hex[:6]}", version="1.0.0",
                       definition_json={"stages": []})
    session.add(ct)
    await session.flush()
    c = CaseInstanceModel(
        case_type_id=ct.id, case_type_version="1.0.0", status=status,
        current_stage_id=stage, event_sourced=event_sourced,
    )
    session.add(c)
    await session.flush()
    return c


class TestAppend:
    async def test_sequence_starts_at_one_and_increments(self, session):
        c = await _case(session)
        a = await es.append(session, case_id=c.id, event_type=es.CASE_CREATED)
        b = await es.append(session, case_id=c.id, event_type=es.STAGE_ENTERED,
                            data={"stage_id": "intake"})
        assert (a.seq, b.seq) == (1, 2)

    async def test_sequences_are_per_case_not_global(self, session):
        a, b = await _case(session), await _case(session)
        e1 = await es.append(session, case_id=a.id, event_type=es.CASE_CREATED)
        e2 = await es.append(session, case_id=b.id, event_type=es.CASE_CREATED)
        assert e1.seq == e2.seq == 1

    async def test_duplicate_sequence_is_a_concurrency_error(self, session):
        """Two writers racing on the same seq: one must lose, not interleave."""
        c = await _case(session)
        await es.append(session, case_id=c.id, event_type=es.CASE_CREATED, expected_seq=1)
        with pytest.raises(es.ConcurrencyError):
            await es.append(session, case_id=c.id, event_type=es.STAGE_ENTERED,
                            data={"stage_id": "x"}, expected_seq=1)

    async def test_unknown_event_type_is_refused(self):
        """A stream containing events nothing can replay is not a stream."""
        assert "Frobnicated" not in es.KNOWN_EVENTS

    async def test_unknown_event_type_raises(self, session):
        c = await _case(session)
        with pytest.raises(es.EventStoreError, match="not a known case event"):
            await es.append(session, case_id=c.id, event_type="Frobnicated")

    async def test_fat_payload_is_refused(self, session):
        """Events are thin — a fat event makes every consumer a PII copy."""
        c = await _case(session)
        with pytest.raises(es.EventStoreError, match="thin by design"):
            await es.append(session, case_id=c.id, event_type=es.CASE_CREATED,
                            data={"blob": "x" * (es.MAX_EVENT_BYTES + 1)})


class TestCutover:
    async def test_record_is_a_no_op_when_disabled(self, session):
        """Off by default — the store must not start writing unasked."""
        from velaris_shared.config import get_settings

        assert get_settings().event_sourcing_enabled is False
        c = await _case(session)
        assert await es.record(session, case_id=c.id,
                               event_type=es.CASE_CREATED) is None

    async def test_record_skips_pre_cutover_cases(self, session, monkeypatch):
        """§7.5: a case that predates the store gets no stream, ever."""
        from velaris_shared.config import get_settings

        s = get_settings()
        monkeypatch.setattr(s, "event_sourcing_enabled", True, raising=False)
        old = await _case(session, event_sourced=False)
        assert await es.record(session, case_id=old.id,
                               event_type=es.STAGE_ENTERED,
                               data={"stage_id": "x"}) is None
        assert await es.stream(session, old.id) == []

    async def test_record_writes_for_post_cutover_cases(self, session, monkeypatch):
        from velaris_shared.config import get_settings

        monkeypatch.setattr(get_settings(), "event_sourcing_enabled", True, raising=False)
        c = await _case(session, event_sourced=True)
        ev = await es.record(session, case_id=c.id, event_type=es.CASE_CREATED)
        assert ev is not None and ev.seq == 1


class TestProjection:
    async def test_folds_a_stream_into_state(self, session):
        c = await _case(session)
        for et, data in [
            (es.CASE_CREATED, {"status": "open"}),
            (es.STAGE_ENTERED, {"stage_id": "intake"}),
            (es.ASSIGNED, {"assignee_id": "u1"}),
            (es.STEP_COMPLETED, {"step_id": "s1"}),
            (es.STAGE_ENTERED, {"stage_id": "review"}),
            (es.STATUS_CHANGED, {"from": "open", "to": "resolved"}),
            (es.SLA_BREACHED, {"sla_policy_id": "p1"}),
        ]:
            await es.append(session, case_id=c.id, event_type=et, data=data)

        state = es.project(await es.stream(session, c.id))
        assert state["exists"] is True
        assert state["status"] == "resolved"
        assert state["current_stage_id"] == "review"     # last stage wins
        assert state["assignee_ids"] == ["u1"]
        assert state["steps_completed"] == ["s1"]
        assert state["sla_breaches"] == 1
        assert state["last_seq"] == 7

    async def test_projection_is_derived_only_from_events(self):
        """It must not consult the row it is meant to verify."""
        import inspect

        src = inspect.getsource(es.project)
        assert "CaseInstanceModel" not in src
        assert "session" not in inspect.signature(es.project).parameters

    async def test_repeated_step_is_recorded_once(self, session):
        c = await _case(session)
        for _ in range(3):
            await es.append(session, case_id=c.id, event_type=es.STEP_COMPLETED,
                            data={"step_id": "s1"})
        assert es.project(await es.stream(session, c.id))["steps_completed"] == ["s1"]


class TestVerify:
    async def test_pre_cutover_case_is_reported_unverifiable(self, session):
        """Not an error — the honest answer for a case with no stream."""
        c = await _case(session, event_sourced=False)
        out = await es.verify(session, c.id)
        assert out["verifiable"] is False
        assert "§7.5" in out["reason"] or "cutover" in out["reason"]

    async def test_consistent_stream_verifies(self, session):
        c = await _case(session, status="open", stage="intake")
        await es.append(session, case_id=c.id, event_type=es.CASE_CREATED,
                        data={"status": "open"})
        await es.append(session, case_id=c.id, event_type=es.STAGE_ENTERED,
                        data={"stage_id": "intake"})
        out = await es.verify(session, c.id)
        assert out["consistent"] is True
        assert out["mismatches"] == []

    async def test_drift_is_detected(self, session):
        """Something wrote the row around the chokepoints — the point of verify."""
        c = await _case(session, status="open", stage="intake")
        await es.append(session, case_id=c.id, event_type=es.CASE_CREATED,
                        data={"status": "open"})
        await es.append(session, case_id=c.id, event_type=es.STAGE_ENTERED,
                        data={"stage_id": "intake"})
        c.status = "resolved"            # bypassing the lifecycle entirely
        await session.flush()

        out = await es.verify(session, c.id)
        assert out["consistent"] is False
        fields = {m["field"] for m in out["mismatches"]}
        assert "status" in fields

    async def test_sequence_gap_is_detected(self, session):
        """seq is contiguous by construction — a hole means an event was deleted."""
        c = await _case(session, status="open", stage="intake")
        await es.append(session, case_id=c.id, event_type=es.CASE_CREATED,
                        data={"status": "open"})
        await es.append(session, case_id=c.id, event_type=es.STAGE_ENTERED,
                        data={"stage_id": "intake"}, expected_seq=5)

        out = await es.verify(session, c.id)
        assert out["sequence_gaps"] == [2, 3, 4]
        assert out["consistent"] is False

    async def test_unknown_case_raises(self, session):
        with pytest.raises(es.EventStoreError, match="not found"):
            await es.verify(session, uuid.uuid4())


class TestEndpoints:
    async def test_stream_endpoint_marks_pre_cutover_cases(self, session, client):
        c = await _case(session, event_sourced=False)
        await session.commit()

        resp = await client.get(f"/api/v1/cases/{c.id}/events")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        # An empty list must never be mistaken for "this case did nothing".
        assert body["event_sourced"] is False
        assert body["events"] == []

    async def test_verify_endpoint(self, session, client):
        c = await _case(session, event_sourced=True, status="open")
        await es.append(session, case_id=c.id, event_type=es.CASE_CREATED,
                        data={"status": "open"})
        await session.commit()

        resp = await client.get(f"/api/v1/cases/{c.id}/events/verify")
        assert resp.status_code == 200, resp.text
        assert resp.json()["verifiable"] is True

    async def test_stream_endpoint_404s_unknown_case(self, client):
        resp = await client.get(f"/api/v1/cases/{uuid.uuid4()}/events")
        assert resp.status_code == 404
