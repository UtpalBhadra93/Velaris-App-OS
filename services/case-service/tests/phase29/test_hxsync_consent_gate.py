"""HxSync egress under the HxConsent `data_sharing` gate.

HxSync is the platform's second way personal data leaves the building; outbound
email was the first and, until now, the only gated one. Redaction already ran
here, but redaction answers *which columns may leave* — it cannot answer whether
a given person's row may leave at all, because that is a fact about the subject
rather than about the schema. The two are complementary.

Load-bearing details these tests pin:

* **Off by default.** Turning the gate on changes what an existing destination
  receives, so it is opt-in per destination and a migration must not do it to a
  running pipeline.
* **The subject is the case's portal submitter.** Staff-created cases have none
  and sync as before — the same rule the AI gate uses.
* **A refused row never reaches the adapter.** Not redacted-and-sent, not sent
  and deleted later: never built.
* **Consent skips are counted separately** from transform failures. One is the
  system working; the other is a bug to fix.

The destination adapter is a stub throughout — this is about which rows are
permitted to leave, not about warehouse connectivity.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import uuid

import pytest

from case_service.compliance import consent as c
from velaris_shared.db.models import (
    CaseInstanceModel,
    CaseTypeModel,
    SyncDestinationModel,
)
from case_service.hxsync.pipeline import run_sync

pytestmark = pytest.mark.asyncio

SUBJECT = "warehouse-subject@example.test"


class _CapturingAdapter:
    """Stands in for a warehouse. Records exactly what it was asked to receive."""

    def __init__(self) -> None:
        self.rows: list[dict] = []

    def ensure_schema(self, _table, _columns):
        return None

    def push_rows(self, _table, rows):
        self.rows.extend(rows)
        return len(rows)


@pytest.fixture
def adapter(monkeypatch):
    a = _CapturingAdapter()
    monkeypatch.setattr(
        "case_service.hxsync.pipeline.get_destination", lambda *_a, **_k: a,
    )
    return a


async def _dest(session, *, gated: bool) -> SyncDestinationModel:
    dest = SyncDestinationModel(
        name="warehouse", dest_type="duckdb", connection_config={},
        consent_gated=gated,
    )
    session.add(dest)
    await session.flush()
    return dest


async def _case(session, submitter: str | None) -> CaseInstanceModel:
    ct = CaseTypeModel(
        name=f"SY {uuid.uuid4().hex[:6]}", version="1.0.0",
        definition_json={"stages": []},
    )
    session.add(ct)
    await session.flush()
    case = CaseInstanceModel(
        case_type_id=ct.id,
        case_type_version=ct.version,
        case_number=f"C-{uuid.uuid4().hex[:8]}",
        status="open",
        portal_submitter_email=submitter,
    )
    session.add(case)
    await session.flush()
    return case


async def _grant(session, subject=SUBJECT):
    await c.record(session, subject_ref=subject, purpose=c.DATA_SHARING,
                   action=c.GRANTED, basis=c.CONSENT)
    await session.flush()


# ── The gate is opt-in ────────────────────────────────────────────


class TestOptIn:
    async def test_ungated_destination_syncs_a_refused_subject(self, session, adapter):
        """Existing pipelines must be untouched until someone turns the gate on."""
        await _case(session, SUBJECT)              # no consent recorded at all
        dest = await _dest(session, gated=False)
        await session.commit()

        result = await run_sync(dest.id, session)

        assert result["rows_synced"] == 1
        assert result["consent_skipped"] == 0
        assert len(adapter.rows) == 1

    async def test_new_destinations_default_to_ungated(self, session):
        dest = SyncDestinationModel(name="d", dest_type="duckdb", connection_config={})
        session.add(dest)
        await session.commit()
        assert dest.consent_gated is False


# ── The gate, on ──────────────────────────────────────────────────


class TestGated:
    async def test_refused_subject_never_reaches_the_adapter(self, session, adapter):
        await _case(session, SUBJECT)              # present, unrecorded → refused
        dest = await _dest(session, gated=True)
        await session.commit()

        result = await run_sync(dest.id, session)

        assert result["rows_synced"] == 0
        assert result["consent_skipped"] == 1
        assert adapter.rows == [], (
            "a refused row must never be built, let alone pushed and retracted"
        )

    async def test_granted_subject_syncs(self, session, adapter):
        await _case(session, SUBJECT)
        await _grant(session)
        dest = await _dest(session, gated=True)
        await session.commit()

        result = await run_sync(dest.id, session)

        assert result["rows_synced"] == 1
        assert result["consent_skipped"] == 0

    async def test_staff_case_with_no_subject_still_syncs(self, session, adapter):
        """The decision that makes this gate usable at all."""
        await _case(session, None)
        dest = await _dest(session, gated=True)
        await session.commit()

        result = await run_sync(dest.id, session)

        assert result["rows_synced"] == 1
        assert result["consent_skipped"] == 0

    async def test_withdrawal_stops_a_previously_syncing_case(self, session, adapter):
        await _case(session, SUBJECT)
        await _grant(session)
        await c.record(session, subject_ref=SUBJECT, purpose=c.DATA_SHARING,
                       action=c.WITHDRAWN, basis=c.CONSENT)
        dest = await _dest(session, gated=True)
        await session.commit()

        result = await run_sync(dest.id, session)

        assert result["consent_skipped"] == 1
        assert adapter.rows == []

    async def test_one_refusal_does_not_stop_the_others(self, session, adapter):
        """Per row, like the email gate is per recipient."""
        await _case(session, SUBJECT)              # refused
        await _case(session, "other@example.test")
        await _case(session, None)                 # staff case
        await c.record(session, subject_ref="other@example.test",
                       purpose=c.DATA_SHARING, action=c.GRANTED, basis=c.CONSENT)
        dest = await _dest(session, gated=True)
        await session.commit()

        result = await run_sync(dest.id, session)

        assert result["rows_synced"] == 2
        assert result["consent_skipped"] == 1
        assert result["status"] == "success", (
            "a lawful refusal is the system working — it must not fail the run"
        )

    async def test_a_failing_ledger_refuses_rather_than_allows(
        self, session, adapter, monkeypatch,
    ):
        async def _boom(*_a, **_k):
            raise RuntimeError("ledger unreachable")

        monkeypatch.setattr(
            "case_service.hxsync.pipeline.consent_svc.allows_subject", _boom)
        await _case(session, SUBJECT)
        dest = await _dest(session, gated=True)
        await session.commit()

        result = await run_sync(dest.id, session)

        assert result["consent_skipped"] == 1
        assert adapter.rows == []


# ── Reporting ─────────────────────────────────────────────────────


class TestReporting:
    async def test_consent_skips_are_counted_apart_from_failures(self, session, adapter):
        """An operator must not go hunting for a transform bug that is not there."""
        await _case(session, SUBJECT)
        dest = await _dest(session, gated=True)
        await session.commit()

        result = await run_sync(dest.id, session)

        assert result["consent_skipped"] == 1
        assert result["rows_skipped"] == 1
        reasons = [s["error"] for s in result["skipped"]]
        assert any("consent" in r for r in reasons)

    async def test_the_drop_is_provable_afterwards(self, session, adapter):
        """"We stopped sharing their data" must be a record, not an assertion."""
        from sqlalchemy import select

        from velaris_shared.db.models import SecurityEventModel

        await _case(session, SUBJECT)
        dest = await _dest(session, gated=True)
        await session.commit()

        await run_sync(dest.id, session)

        events = (await session.execute(
            select(SecurityEventModel).where(
                SecurityEventModel.event_type == "consent.sync_filtered")
        )).scalars().all()
        assert len(events) == 1
        assert events[0].details["rows_dropped"] == 1
        assert events[0].details["purpose"] == c.DATA_SHARING

    async def test_no_audit_row_when_nothing_was_dropped(self, session, adapter):
        from sqlalchemy import select

        from velaris_shared.db.models import SecurityEventModel

        await _case(session, None)
        dest = await _dest(session, gated=True)
        await session.commit()

        await run_sync(dest.id, session)

        events = (await session.execute(
            select(SecurityEventModel).where(
                SecurityEventModel.event_type == "consent.sync_filtered")
        )).scalars().all()
        assert events == [], "a clean run must not log a filtering event"
