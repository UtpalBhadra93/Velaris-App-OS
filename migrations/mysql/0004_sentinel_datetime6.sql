-- ============================================================================
-- Velaris — MySQL 8 / MariaDB 10.6+ : HxSentinel latest-wins timestamp precision.
--
-- MySQL/MariaDB `DATETIME` defaults to SECOND precision, whereas PostgreSQL
-- `TIMESTAMPTZ` is microsecond. HxSentinel resolves the CURRENT outcome (and the
-- latest feedback) with `ORDER BY created_at DESC, id DESC` on these append-only
-- tables. When two rows are written in the same second the `created_at` values tie
-- and the tiebreak falls to a random UUID id — so "latest-wins" became
-- non-deterministic on MySQL/MariaDB (correct on Postgres). `DATETIME(6)` restores
-- sub-second ordering, matching Postgres.
--
-- Fresh installs already get DATETIME(6) from the regenerated 0001 baseline; this
-- migration upgrades EXISTING installs. `MODIFY ... DATETIME(6)` is idempotent — a
-- no-op when the column is already microsecond.
-- ============================================================================

ALTER TABLE case_outcomes      MODIFY created_at DATETIME(6) NOT NULL;
ALTER TABLE case_risk_feedback MODIFY created_at DATETIME(6) NOT NULL;
