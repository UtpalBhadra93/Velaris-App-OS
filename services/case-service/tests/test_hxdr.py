"""HxDR — disaster recovery posture (§8.9).

The failure this exists to prevent is discovering at the worst possible moment
that the backups never worked. So the tests concentrate on the states a naive
implementation would collapse: a recent backup that has never been restored is
NOT healthy, and a failed drill is worth keeping.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest

from case_service.compliance import dr
from case_service.db.models import BackupRecordModel, RegionModel, SovereigntyRuleModel

pytestmark = pytest.mark.asyncio


async def _region(session, *, name=None, primary=False, governed=False):
    region = RegionModel(
        name=name or f"r-{uuid.uuid4().hex[:8]}", provider="local",
        is_primary=primary, enabled=True,
    )
    session.add(region)
    await session.flush()
    if governed:
        session.add(SovereigntyRuleModel(region_id=region.id, regulation="GDPR"))
        await session.flush()
    return region


class TestRecording:
    async def test_records_a_backup(self, session):
        region = await _region(session)
        row = await dr.record_backup(
            session, location="/backups/db-1.sql.gz", region_id=region.id,
            size_bytes=1024, checksum="abc",
        )
        assert row.status == dr.SUCCEEDED

    async def test_location_is_required(self, session):
        """A backup nobody can find is not a backup."""
        with pytest.raises(dr.DRError, match="location is required"):
            await dr.record_backup(session, location="   ")

    async def test_failed_backups_are_recorded_too(self, session):
        region = await _region(session)
        row = await dr.record_backup(
            session, location="/backups/failed", region_id=region.id,
            status=dr.FAILED, error="disk full",
        )
        assert row.status == dr.FAILED

    async def test_a_failed_backup_cannot_be_restore_tested(self, session):
        region = await _region(session)
        row = await dr.record_backup(
            session, location="/x", region_id=region.id, status=dr.FAILED,
        )
        with pytest.raises(dr.DRError, match="did not succeed"):
            await dr.record_restore_test(session, row.id, result=dr.PASSED)


class TestSovereignty:
    async def test_governed_region_backup_stored_elsewhere_is_refused(self, session):
        """Residency follows the bytes — a backup is data."""
        eu = await _region(session, governed=True)
        other = await _region(session)
        with pytest.raises(dr.DRError, match="residency follows the bytes"):
            await dr.record_backup(
                session, location="/backups/eu.sql", region_id=eu.id,
                stored_region_id=other.id,
            )

    async def test_same_region_storage_is_fine(self, session):
        eu = await _region(session, governed=True)
        row = await dr.record_backup(
            session, location="/backups/eu.sql", region_id=eu.id,
            stored_region_id=eu.id,
        )
        assert row.id is not None

    async def test_ungoverned_region_may_store_elsewhere(self, session):
        a, b = await _region(session), await _region(session)
        row = await dr.record_backup(
            session, location="/backups/a.sql", region_id=a.id, stored_region_id=b.id,
        )
        assert row.id is not None


class TestPostureStates:
    async def _posture_for(self, session, region):
        out = await dr.posture(session)
        return next(r for r in out["regions"] if r["region_id"] == str(region.id))

    async def test_no_backup(self, session):
        region = await _region(session)
        assert (await self._posture_for(session, region))["state"] == "no_backup"

    async def test_rpo_breach(self, session):
        region = await _region(session)
        await dr.record_backup(
            session, location="/old", region_id=region.id,
            taken_at=datetime.now(timezone.utc) - timedelta(hours=48),
        )
        entry = await self._posture_for(session, region)
        assert entry["state"] == "rpo_breached"

    async def test_recent_but_never_tested_is_not_ok(self, session):
        """The whole point — a fresh untested backup is a hope, not a plan."""
        region = await _region(session)
        await dr.record_backup(session, location="/fresh", region_id=region.id)
        entry = await self._posture_for(session, region)
        assert entry["state"] == "untested"
        assert "hope" in entry["detail"]

    async def test_failed_drill_is_not_ok(self, session):
        region = await _region(session)
        row = await dr.record_backup(session, location="/f", region_id=region.id)
        await dr.record_restore_test(session, row.id, result=dr.FAILED_TEST,
                                     notes="restore aborted")
        entry = await self._posture_for(session, region)
        assert entry["state"] == "untested"
        assert "FAILED" in entry["detail"]

    async def test_stale_drill_is_not_ok(self, session):
        """Schema and tooling drift — an old drill demonstrates nothing."""
        region = await _region(session)
        row = await dr.record_backup(session, location="/s", region_id=region.id)
        row.restore_tested_at = (
            datetime.now(timezone.utc)
            - timedelta(days=dr.RESTORE_TEST_VALID_DAYS + 5)
        )
        row.restore_test_result = dr.PASSED
        await session.flush()
        entry = await self._posture_for(session, region)
        assert entry["state"] == "untested"

    async def test_recent_and_tested_is_ok(self, session):
        region = await _region(session)
        row = await dr.record_backup(session, location="/good", region_id=region.id)
        await dr.record_restore_test(session, row.id, result=dr.PASSED)
        entry = await self._posture_for(session, region)
        assert entry["state"] == "ok"

    async def test_one_bad_region_is_not_hidden_by_healthy_ones(self, session):
        """A single green light would mask a broken region."""
        good = await _region(session)
        row = await dr.record_backup(session, location="/g", region_id=good.id)
        await dr.record_restore_test(session, row.id, result=dr.PASSED)
        bad = await _region(session)

        out = await dr.posture(session)
        assert out["all_regions_ok"] is False
        assert bad.name in out["regions_at_risk"]

    async def test_invalid_drill_result_is_refused(self, session):
        region = await _region(session)
        row = await dr.record_backup(session, location="/x", region_id=region.id)
        with pytest.raises(dr.DRError, match="result must be"):
            await dr.record_restore_test(session, row.id, result="probably")


class TestMeasuredRto:
    """The RTO was declared and never checked against anything.

    `posture` reported `rto_hours` straight from a constant, and a drill
    recorded only pass or fail — so a backup that took twelve hours to restore
    against a four-hour objective reported exactly the same green as one that
    took ten minutes. The posture answered "can we recover?" and never "in
    time?", which is the question an RTO exists to ask.
    """

    async def _posture_for(self, session, region, **kw):
        out = await dr.posture(session, **kw)
        return next(r for r in out["regions"] if r["region_id"] == str(region.id))

    async def test_a_slow_restore_is_not_ok(self, session):
        region = await _region(session)
        row = await dr.record_backup(session, location="/slow", region_id=region.id)
        await dr.record_restore_test(
            session, row.id, result=dr.PASSED, duration_seconds=12 * 3600)

        entry = await self._posture_for(session, region, rto_hours=4)
        assert entry["state"] == "rto_breached"
        assert "12.0h" in entry["detail"] and "4h" in entry["detail"]

    async def test_a_fast_restore_is_ok(self, session):
        region = await _region(session)
        row = await dr.record_backup(session, location="/fast", region_id=region.id)
        await dr.record_restore_test(
            session, row.id, result=dr.PASSED, duration_seconds=600)

        entry = await self._posture_for(session, region, rto_hours=4)
        assert entry["state"] == "ok"
        assert entry["last_restore_seconds"] == 600
        assert entry["rto_measured"] is True

    async def test_an_unmeasured_drill_does_not_claim_the_rto_was_met(self, session):
        """Drills predating the column have no duration. Say so; do not imply."""
        region = await _region(session)
        row = await dr.record_backup(session, location="/old-drill", region_id=region.id)
        await dr.record_restore_test(session, row.id, result=dr.PASSED)

        entry = await self._posture_for(session, region, rto_hours=4)
        assert entry["state"] == "ok", "an unmeasured drill is not a breach either"
        assert entry["last_restore_seconds"] is None, "never report 0 for unmeasured"
        assert entry["rto_measured"] is False

    async def test_rto_breach_is_distinct_from_untested(self, session):
        """The backup IS restorable — conflating the two misdirects the fix."""
        region = await _region(session)
        row = await dr.record_backup(session, location="/slow2", region_id=region.id)
        await dr.record_restore_test(
            session, row.id, result=dr.PASSED, duration_seconds=99 * 3600)

        entry = await self._posture_for(session, region, rto_hours=1)
        assert entry["state"] == "rto_breached"
        assert "restorable" in entry["detail"]

    async def test_a_breached_region_is_named_at_risk(self, session):
        region = await _region(session)
        row = await dr.record_backup(session, location="/slow3", region_id=region.id)
        await dr.record_restore_test(
            session, row.id, result=dr.PASSED, duration_seconds=9 * 3600)

        out = await dr.posture(session, rto_hours=4)
        assert out["all_regions_ok"] is False
        assert region.name in out["regions_at_risk"]

    async def test_a_negative_duration_is_refused(self, session):
        region = await _region(session)
        row = await dr.record_backup(session, location="/neg", region_id=region.id)
        with pytest.raises(dr.DRError, match="negative"):
            await dr.record_restore_test(
                session, row.id, result=dr.PASSED, duration_seconds=-1)

    async def test_rpo_still_outranks_rto(self, session):
        """A stale backup is the bigger problem; report that one first."""
        region = await _region(session)
        row = await dr.record_backup(
            session, location="/stale", region_id=region.id,
            taken_at=datetime.now(timezone.utc) - timedelta(hours=48),
        )
        await dr.record_restore_test(
            session, row.id, result=dr.PASSED, duration_seconds=99 * 3600)

        entry = await self._posture_for(session, region, rto_hours=4)
        assert entry["state"] == "rpo_breached"


class TestApiSurface:
    async def test_posture_requires_admin(self, anon_client):
        r = await anon_client.get("/api/v1/compliance/dr/posture")
        assert r.status_code in (401, 403)

    async def test_record_and_report(self, client, session):
        region = await _region(session)
        await session.commit()

        made = await client.post("/api/v1/compliance/dr/backups", json={
            "location": "/backups/api.sql.gz", "region_id": str(region.id),
        })
        assert made.status_code == 200, made.text

        posture = await client.get("/api/v1/compliance/dr/posture")
        assert posture.status_code == 200, posture.text
        entry = next(r for r in posture.json()["regions"]
                     if r["region_id"] == str(region.id))
        assert entry["state"] == "untested"
