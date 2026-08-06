"""HxDR — the ops recording CLI (§8.9).

`update-velaris.sh` calls this after every pre-update `pg_dump`. Two properties
matter more than the happy path, and both are asserted here:

* **it never fails its caller** — a bookkeeping row must not turn a successful
  backup into a failed update;
* **it never records an invisible row silently** — `posture()` reports per
  region, so a backup with no region is stored and then never shown. That is
  the one failure a human has to be told about.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import uuid

import pytest

from case_service.compliance import dr, dr_cli
from case_service.db.models import RegionModel

#: Applied per class rather than module-wide: `main()` is deliberately
#: synchronous (it owns the event loop), so its tests must not run inside one.
_async = pytest.mark.asyncio


async def _region(session, *, name=None, primary=False):
    region = RegionModel(
        name=name or f"r-{uuid.uuid4().hex[:8]}", provider="local",
        is_primary=primary, enabled=True,
    )
    session.add(region)
    await session.flush()
    return region


@_async
class TestRegionResolution:
    """The resolution order is the whole point — an unattached row is invisible."""

    async def test_explicit_region_wins(self, session):
        region = await _region(session, primary=True)
        other = uuid.uuid4()
        got, how = await dr_cli._resolve_region(session, str(other))
        assert got == other
        assert how == "explicit"

    async def test_hxglobal_outranks_the_env_var(self, session, monkeypatch):
        """`region_registry` is the source of truth for regions.

        An env var that silently overrode the primary flag would put two
        answers to "which region is this" in the system, with no way to tell
        from the UI which one won.
        """
        primary = await _region(session, primary=True)
        monkeypatch.setenv("VELARIS_DR_REGION_ID", str(uuid.uuid4()))

        got, how = await dr_cli._resolve_region(session, None)
        assert got == primary.id
        assert "primary" in how

    async def test_env_is_used_only_when_no_region_is_primary(self, session, monkeypatch):
        """The escape hatch: a deployment that cannot mark one."""
        await _region(session)
        await _region(session)
        target = uuid.uuid4()
        monkeypatch.setenv("VELARIS_DR_REGION_ID", str(target))

        got, how = await dr_cli._resolve_region(session, None)
        assert got == target
        assert "VELARIS_DR_REGION_ID" in how

    async def test_bad_env_is_ignored_not_fatal(self, session, monkeypatch):
        region = await _region(session, primary=True)
        monkeypatch.setenv("VELARIS_DR_REGION_ID", "not-a-uuid")
        got, _ = await dr_cli._resolve_region(session, None)
        assert got == region.id

    async def test_explicit_still_beats_everything(self, session, monkeypatch):
        """A human typing --region at the moment of the run wins outright."""
        await _region(session, primary=True)
        monkeypatch.setenv("VELARIS_DR_REGION_ID", str(uuid.uuid4()))
        target = uuid.uuid4()

        got, how = await dr_cli._resolve_region(session, str(target))
        assert got == target
        assert how == "explicit"

    async def test_single_primary_is_used(self, session, monkeypatch):
        monkeypatch.delenv("VELARIS_DR_REGION_ID", raising=False)
        await _region(session)
        primary = await _region(session, primary=True)
        got, how = await dr_cli._resolve_region(session, None)
        assert got == primary.id
        assert "primary" in how

    async def test_sole_region_is_used_even_without_primary(self, session, monkeypatch):
        monkeypatch.delenv("VELARIS_DR_REGION_ID", raising=False)
        only = await _region(session)
        got, _ = await dr_cli._resolve_region(session, None)
        assert got == only.id

    async def test_many_regions_none_primary_resolves_to_none(self, session, monkeypatch):
        """The live install's exact state — and it must not guess."""
        monkeypatch.delenv("VELARIS_DR_REGION_ID", raising=False)
        await _region(session)
        await _region(session)
        got, how = await dr_cli._resolve_region(session, None)
        assert got is None
        assert how == "none"

    async def test_two_primaries_refuses_to_choose(self, session, monkeypatch):
        monkeypatch.delenv("VELARIS_DR_REGION_ID", raising=False)
        await _region(session, primary=True)
        await _region(session, primary=True)
        got, _ = await dr_cli._resolve_region(session, None)
        assert got is None

    async def test_no_regions_at_all(self, session, monkeypatch):
        monkeypatch.delenv("VELARIS_DR_REGION_ID", raising=False)
        got, how = await dr_cli._resolve_region(session, None)
        assert got is None
        assert how == "none"


@_async
class TestChecksum:
    async def test_hashes_a_file(self, tmp_path):
        import hashlib

        f = tmp_path / "dump.sql"
        f.write_bytes(b"-- pg_dump output\n" * 100)
        assert dr_cli._checksum(f) == hashlib.sha256(f.read_bytes()).hexdigest()

    async def test_missing_file_returns_none_rather_than_raising(self, tmp_path):
        """A checksum is evidence about the backup, not a precondition."""
        assert dr_cli._checksum(tmp_path / "gone.sql") is None


class TestNeverFailsTheCaller:
    """`update-velaris.sh` runs this after a backup that already succeeded."""

    def test_exit_zero_when_recording_blows_up(self, monkeypatch, tmp_path):
        f = tmp_path / "dump.sql"
        f.write_text("x")

        def _boom(coro):
            coro.close()          # the caller owns the loop; don't leak the coroutine
            raise RuntimeError("database is on fire")

        monkeypatch.setattr(dr_cli.asyncio, "run", _boom)
        assert dr_cli.main(["--location", str(f)]) == 0

    def test_strict_surfaces_the_failure(self, monkeypatch, tmp_path):
        f = tmp_path / "dump.sql"
        f.write_text("x")

        def _boom(coro):
            coro.close()
            raise RuntimeError("database is on fire")

        monkeypatch.setattr(dr_cli.asyncio, "run", _boom)
        assert dr_cli.main(["--location", str(f), "--strict"]) == 1

    def test_a_missing_backup_file_still_records(self, monkeypatch):
        """The dump path may be unreadable from here; the backup still happened."""
        seen = {}

        def _fake_run(coro):
            coro.close()
            seen["ran"] = True
            return 0

        monkeypatch.setattr(dr_cli.asyncio, "run", _fake_run)
        assert dr_cli.main(["--location", "/nonexistent/dump.sql"]) == 0
        assert seen["ran"]


class TestArgs:
    def test_location_is_required(self):
        with pytest.raises(SystemExit):
            dr_cli.main([])

    def test_status_is_constrained(self):
        with pytest.raises(SystemExit):
            dr_cli.main(["--location", "/x", "--status", "probably-fine"])


@_async
class TestRecordedRowIsUsable:
    async def test_row_lands_untested_so_posture_says_untested(self, session):
        """The point of the whole wiring: a recorded backup is not yet healthy."""
        region = await _region(session, primary=True)
        await dr.record_backup(
            session, location="/opt/velaris/.backups/x.sql",
            region_id=region.id, stored_region_id=region.id,
            scope="database", status=dr.SUCCEEDED,
            size_bytes=194967, checksum="7bc0033f" * 8,
            recorded_by="update-velaris.sh v2.2.0",
        )
        await session.flush()

        out = await dr.posture(session)
        mine = [r for r in out["regions"] if r["region_id"] == str(region.id)][0]
        assert mine["state"] == "untested"
        assert out["all_regions_ok"] is False

    async def test_unattached_backup_is_invisible_to_posture(self, session):
        """Why `_resolve_region` shouts instead of returning None quietly."""
        region = await _region(session, primary=True)
        await dr.record_backup(
            session, location="/tmp/orphan.sql", region_id=None,
            scope="database", status=dr.SUCCEEDED,
        )
        await session.flush()

        out = await dr.posture(session)
        mine = [r for r in out["regions"] if r["region_id"] == str(region.id)][0]
        assert mine["state"] == "no_backup"
