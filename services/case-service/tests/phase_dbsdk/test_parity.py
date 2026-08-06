"""DB SDK Phase 1b — Postgres/MySQL parity gates.

These run on the default (SQLite) harness — no DB needed, they compare generated
artifacts against committed files — so they are always-on CI guards against the
recurring "I changed the Postgres side and forgot the MySQL side" drift that has
bitten this project repeatedly (velaris_settings missing from the baseline, a unique
constraint present in the model but not the shipped DDL, a feature added to one
manifest but not the other).
"""
from __future__ import annotations

import hashlib
import re
import subprocess
import sys
from pathlib import Path

# services/case-service/tests/phase_dbsdk/test_parity.py → repo root is parents[4]
_ROOT = Path(__file__).resolve().parents[4]

#: SHA-256 of the frozen MySQL baseline, cut 2026-08-06 at PostgreSQL 154.
#: Changing this constant is how you declare a deliberate re-cut; see
#: `test_mysql_baseline_is_frozen` for what else that entails.
_FROZEN_BASELINE_SHA256 = (
    "61153aa32f9d015dc55d725aeccde837cb914f3afebbfedca1e86a39700c6476"
)


def test_mysql_baseline_is_frozen():
    """The baseline must NOT track the ORM models.

    This test asserted the opposite until 2026-08-06 — that the baseline always
    equalled current metadata — and that assertion is what made MySQL installs
    unupgradable. `start-velaris.sh` records applied filenames in
    `schema_migrations`, so on an existing install the baseline is already
    recorded and a regenerated one is **silently skipped**: the change reaches
    nobody, with no error to notice. Only a new numbered file reaches an install
    that already exists.

    So drift between the baseline and the models is now **expected** — it is
    exactly the set of changes carried by the numbered migrations beside it.
    What is pinned instead is the baseline's content, by hash: regenerating it
    is a deliberate release-engineering act (re-cut at a major version, delete
    the migrations it subsumes, rename to the new high-water mark), and it must
    never happen as a side effect of a model change.

    That baseline + migrations still equal the ORM models is proven where it can
    actually be proven — `ci-mysql.yml` / `ci-mariadb.yml` apply the whole track
    to a real server and diff the result against the metadata. A file comparison
    cannot establish it.
    """
    baseline = _ROOT / "migrations" / "mysql" / "000_baseline_pg154.sql"
    assert baseline.exists(), f"frozen baseline missing at {baseline}"

    digest = hashlib.sha256(baseline.read_bytes()).hexdigest()
    assert digest == _FROZEN_BASELINE_SHA256, (
        "the frozen MySQL baseline changed.\n"
        "If a model change did this, that is the bug: add a numbered migration "
        "(sharing its PostgreSQL sibling's number) instead — regenerating the "
        "baseline delivers nothing to any existing install.\n"
        "If you re-cut the baseline deliberately, delete the numbered files it "
        "now subsumes, rename it to the new PG high-water mark, and update "
        f"_FROZEN_BASELINE_SHA256 to:\n  {digest}"
    )


def test_mysql_bpm_seed_in_sync_with_pg_migration():
    """migrations/mysql/0003_seed_bpm_concepts.sql must equal what the generator emits
    from the Postgres migration 040. Fails if a bpm_concept was added/changed on the PG
    side without regenerating the MySQL parity seed."""
    script = _ROOT / "scripts" / "gen_mysql_bpm_seed.py"
    assert script.exists(), f"bpm seed generator not found at {script}"
    r = subprocess.run([sys.executable, str(script), "--check"],
                       capture_output=True, text=True)
    assert r.returncode == 0, (
        "MySQL bpm_concepts parity seed has drifted from Postgres migration 040 — "
        "regenerate with `python scripts/gen_mysql_bpm_seed.py`.\n"
        f"stdout: {r.stdout}\nstderr: {r.stderr}"
    )


def _manifest_features(path: Path) -> set[tuple[str, str]]:
    """Extract (feature_key, version) pairs from a release-manifest seed file. The first
    VALUES column is the dialect's UUID function (gen_random_uuid()/UUID()), the 2nd is
    feature_key, the 3rd is version."""
    # Strip SQL line-comments first — both manifests carry a commented-out template
    # row (`-- (gen_random_uuid(), 'feature_key', ...)`) that must not be parsed as a
    # real feature.
    sql = "\n".join(l for l in path.read_text().splitlines() if not l.lstrip().startswith("--"))
    pairs = re.findall(
        r"(?:gen_random_uuid\(\)|UUID\(\))\s*,\s*'([a-z_]+)'\s*,\s*'([^']+)'",
        sql,
    )
    return set(pairs)


def test_release_manifests_in_lockstep():
    """releases/manifest.sql (Postgres) and manifest.mysql.sql must seed the SAME set of
    features at the SAME versions. The runner compares INSERT counts for parity, but a
    swapped key/version would pass that check — so assert the actual (key, version) sets
    match, guarding the 'added a feature to one manifest only' drift."""
    pg = _manifest_features(_ROOT / "releases" / "manifest.sql")
    my = _manifest_features(_ROOT / "releases" / "manifest.mysql.sql")
    assert pg, "no features parsed from manifest.sql — extraction regex may be stale"
    assert pg == my, (
        "Postgres and MySQL release manifests are out of lockstep.\n"
        f"  only in manifest.sql:       {sorted(pg - my)}\n"
        f"  only in manifest.mysql.sql: {sorted(my - pg)}"
    )
