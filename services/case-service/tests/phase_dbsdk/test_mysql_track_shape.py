"""The MySQL track: a frozen baseline, seeds, then PG-numbered migrations.

`migrations/mysql/` follows the same model as `migrations/postgresql/` — a
starting point plus incremental files — with one difference: its starting point
is a generated snapshot at **PostgreSQL 154**, because MySQL had no installs to
carry forward when the track was reset on 2026-08-06.

    000_baseline_pg154.sql    frozen snapshot; PG 001–154
    001–003_seed_*.sql        rows ORM metadata cannot carry
    (004–154 deliberately absent — the baseline already contains them)
    155_*.sql onward          one file per PG migration, same number

**Why the baseline must stay frozen.** `start-velaris.sh` records applied
filenames in `schema_migrations` and skips what is already there. On an existing
install the baseline is already recorded, so regenerating it delivers the change
to **nobody** — silently, with no error. Only a new numbered file reaches an
install that already exists. This is the whole reason the layout changed.

**Why the two models cannot be mixed.** A baseline regenerated from current
metadata already contains every change, so a numbered file applied after it is a
guaranteed duplicate-column failure — and the runner exits on the first failure.
That is exactly how fresh MySQL/MariaDB installs came to be broken: on
2026-08-06, 13 of 27 files failed against MySQL 8.4, the first one fatally, and
CI had never noticed because both MySQL workflows build their schema from ORM
metadata and never executed the files at all.

So the rule this module enforces is narrow and load-bearing: **DDL may live in
the baseline, or in a file numbered above the freeze point, and nowhere else.**

The other half of the guard is in `ci-mysql.yml` / `ci-mariadb.yml`, which apply
the track to a real server and diff the result against the models. Only that can
prove baseline + migrations still agree with the ORM; this file is the cheap,
always-on shape check.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import re
from pathlib import Path

# services/case-service/tests/phase_dbsdk/… → repo root is parents[4]
_ROOT = Path(__file__).resolve().parents[4]
_MYSQL = _ROOT / "migrations" / "mysql"
_PG = _ROOT / "migrations" / "postgresql"

_BASELINE = "000_baseline_pg154.sql"
#: The PG migration the baseline snapshots. A numbered MySQL file is only
#: meaningful above this — at or below it, the baseline already has the change.
_FREEZE_POINT = 154
#: Bootstrap band: the baseline plus the seeds. Everything else must be > freeze.
_BOOTSTRAP_MAX = 3

_DDL = re.compile(
    r"^\s*(CREATE\s+TABLE|ALTER\s+TABLE|RENAME\s+TABLE|DROP\s+TABLE"
    r"|CREATE\s+(UNIQUE\s+)?INDEX|DROP\s+INDEX)",
    re.IGNORECASE | re.MULTILINE,
)
_NUM = re.compile(r"^(\d+)_")


def _live_track() -> list[Path]:
    """Exactly what `start-velaris.sh` applies: maxdepth 1, sorted."""
    return sorted(p for p in _MYSQL.glob("*.sql") if p.is_file())


def _number(path: Path) -> int | None:
    m = _NUM.match(path.name)
    return int(m.group(1)) if m else None


def _strip_comments(sql: str) -> str:
    return "\n".join(
        line for line in sql.splitlines() if not line.lstrip().startswith("--")
    )


def test_the_baseline_exists_and_sorts_first():
    """Sort order IS apply order — the runner pipes `find | sort` into mysql.

    A seed that sorts before the baseline runs against tables that do not exist
    yet, which is the same class of failure the reset was undertaken to fix.
    """
    track = _live_track()
    assert track, "empty MySQL track — nothing would install"
    assert track[0].name == _BASELINE, (
        f"{track[0].name} sorts before the baseline. Apply order is filename "
        f"order; the schema has to be created first."
    )


def test_the_baseline_really_does_carry_the_schema():
    """Guards a baseline emptied by a broken generator."""
    text = _strip_comments((_MYSQL / _BASELINE).read_text())
    assert len(_DDL.findall(text)) > 100, (
        "the baseline holds the entire starting schema; a near-empty one means "
        "the generator failed and fresh installs would come up with no tables"
    )


def test_ddl_lives_only_in_the_baseline_or_above_the_freeze_point():
    """The rule the 2026-08-06 breakage came down to."""
    offenders = {}
    for path in _live_track():
        if path.name == _BASELINE:
            continue
        if not _DDL.search(_strip_comments(path.read_text())):
            continue
        n = _number(path)
        if n is None or n <= _FREEZE_POINT:
            offenders[path.name] = n

    assert not offenders, (
        f"DDL in file(s) at or below the freeze point ({_FREEZE_POINT}): "
        f"{offenders}. The baseline already contains every schema change up to "
        f"PG {_FREEZE_POINT}, so such a file is a duplicate-column failure on the "
        f"next fresh install — and the runner exits on the first failure. Number "
        f"the migration after its PostgreSQL sibling (> {_FREEZE_POINT}) instead."
    )


def test_bootstrap_band_is_seeds_only():
    """000–003 is the bootstrap; nothing new belongs there."""
    for path in _live_track():
        n = _number(path)
        if path.name == _BASELINE or n is None or n > _BOOTSTRAP_MAX:
            continue
        assert "seed" in path.name, (
            f"{path.name} occupies the bootstrap band (000–{_BOOTSTRAP_MAX}), "
            f"which is reserved for the baseline and the seed rows ORM metadata "
            f"cannot carry. A new migration goes above {_FREEZE_POINT}."
        )


def test_numbered_migrations_match_a_real_postgres_sibling():
    """A MySQL migration numbered N claims to be the port of PG migration N.

    Catching a wrong number here is worth it: the two tracks are read side by
    side by anyone auditing whether a change reached both engines, and a number
    that matches nothing makes that audit silently incomplete.
    """
    pg_numbers = {
        int(m.group(1)) for p in _PG.glob("*.sql") if (m := _NUM.match(p.name))
    }
    for path in _live_track():
        n = _number(path)
        if n is None or n <= _FREEZE_POINT:
            continue
        assert n in pg_numbers, (
            f"{path.name} is numbered {n}, but migrations/postgresql/ has no "
            f"migration {n}. MySQL files above the freeze point share their "
            f"PostgreSQL sibling's number."
        )


def test_historical_files_are_out_of_the_runner_s_reach():
    """`find -maxdepth 1` is what keeps the retired DDL from being applied.

    Nesting is load-bearing, not cosmetic: move one back up a level and fresh
    installs break again.
    """
    historical = _MYSQL / "historical"
    if not historical.exists():
        return
    assert list(historical.glob("*.sql")), "empty historical/ — did a move go wrong?"
    live = {p.name for p in _live_track()}
    assert not (live & {p.name for p in historical.glob("*.sql")}), (
        "a file exists in both the live track and historical/"
    )
