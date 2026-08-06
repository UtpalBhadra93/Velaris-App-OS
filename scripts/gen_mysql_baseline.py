#!/usr/bin/env python
"""Cut a MySQL baseline from the ORM metadata (DB SDK Phase 1).

Emits exactly the DDL `Base.metadata.create_all` would run on MySQL — wrapped with
FK-checks-off so the create order (sorted_tables) never trips a forward FK reference.

**This is NOT the tool for a schema change.** The baseline
(`migrations/mysql/000_baseline_pg154.sql`) is **frozen**. A schema change ships as
its own numbered file beside it, sharing the PostgreSQL migration's number — exactly
how the PostgreSQL track works.

Why it has to be that way: `start-velaris.sh` records applied filenames in
`schema_migrations` and skips what is already there. On an existing install the
baseline is already recorded, so a regenerated baseline is **silently skipped** and
the change never arrives — no error, no warning, nothing. Regeneration only ever
reaches installs that do not exist yet.

It also cannot be both. A baseline regenerated from current metadata already contains
every change, so a numbered file applied after it is a guaranteed duplicate-column
failure — and since the runner exits on the first failure, that is precisely how fresh
MySQL/MariaDB installs came to be broken until 2026-08-06 (13 of 27 files failed, the
first one fatally).

Legitimate uses: re-cutting a baseline at a major release, or inspecting the DDL the
models currently imply. Both are deliberate acts. If you re-cut, you must also delete
the numbered files the new baseline subsumes and rename it to its new PG high-water
mark.

Usage:
    uv run python scripts/gen_mysql_baseline.py            # rewrites the baseline
    uv run python scripts/gen_mysql_baseline.py --check     # differs from metadata?

`tests/phase_dbsdk/test_mysql_track_shape.py` enforces the track's shape; the
`ci-mysql`/`ci-mariadb` workflows apply the whole track to a real server and diff the
result against the models, which is what actually proves baseline + migrations agree.
"""
from __future__ import annotations

import sys
from pathlib import Path

# case-service must be importable (its db.models holds the metadata).
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "services" / "case-service"))

from sqlalchemy.dialects import mysql  # noqa: E402
from sqlalchemy.schema import CreateIndex, CreateTable  # noqa: E402

from case_service.db.models import Base  # noqa: E402

#: The frozen baseline. Re-cutting this is a deliberate release-engineering act,
#: NOT the response to a model change — see the module docstring.
OUT = ROOT / "migrations" / "mysql" / "000_baseline_pg154.sql"

HEADER = """\
-- ============================================================================
-- Velaris — MySQL 8 / MariaDB 10.6+ FROZEN baseline: PostgreSQL 001–154.
--
-- Generated from the SQLAlchemy ORM metadata (case_service.db.models) compiled
-- to the MySQL dialect — the same DDL `Base.metadata.create_all` emits.
--
-- ── This file is FROZEN as of 2026-08-06. Do not regenerate it. ─────────────
--
-- It is the starting point of the MySQL track, exactly as
-- `001_case_management_schema.sql` is for PostgreSQL — the difference being
-- only that this one begins at PG 154 rather than at nothing, because MySQL
-- had no installs to carry forward when the track was reset.
--
-- Every schema change from PG 155 onward ships as its own numbered file beside
-- this one, sharing the PostgreSQL migration's number. That is what makes an
-- EXISTING MySQL install upgradable: the runner records applied filenames in
-- schema_migrations, so a frozen baseline is skipped and only the new files
-- run. Regenerating this file instead would silently deliver nothing to every
-- existing install — it is already recorded as applied.
--
-- The gap between 003 and 155 is intentional and is the point: numbers 000–003
-- are the MySQL bootstrap (this baseline plus the seeds ORM metadata cannot
-- carry), and 004–154 are empty because *this file already contains them*.
--
-- Recommended: create the database as utf8mb4
--   (CREATE DATABASE velaris CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;)
-- The indexed-identifier columns are bounded for InnoDB's 3072-byte key limit at
-- utf8mb4's 4 bytes/char, so this schema is safe on any charset.
--
-- DO NOT EDIT BY HAND, and do not regenerate. Add a numbered migration.
-- ============================================================================

SET FOREIGN_KEY_CHECKS = 0;
"""
FOOTER = "\nSET FOREIGN_KEY_CHECKS = 1;\n"


def render() -> str:
    # Deterministic emission: `create_all` orders CREATE INDEX off a set (varies per
    # process), so build the DDL ourselves — tables in dependency order (sorted_tables,
    # so inline FKs always reference an already-created table), then each table's
    # indexes sorted by name. Content matches create_all; order is stable across runs.
    dialect = mysql.dialect()
    stmts: list[str] = []
    for table in Base.metadata.sorted_tables:
        stmts.append(str(CreateTable(table).compile(dialect=dialect)).strip())
        for idx in sorted(table.indexes, key=lambda i: i.name or ""):
            stmts.append(str(CreateIndex(idx).compile(dialect=dialect)).strip())
    body = ";\n\n".join(stmts) + ";\n"
    return HEADER + "\n" + body + FOOTER


def main() -> int:
    sql = render()
    if "--check" in sys.argv:
        current = OUT.read_text() if OUT.exists() else ""
        if current != sql:
            print(f"STALE: {OUT} is out of date — run: uv run python scripts/gen_mysql_baseline.py")
            return 1
        print(f"OK: {OUT} is up to date")
        return 0
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(sql)
    print(f"wrote {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
