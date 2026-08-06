-- ============================================================================
-- Velaris — MySQL 8 / MariaDB 10.6+ : HxDR measured RTO
-- (PostgreSQL migration 155).
--
-- **The first migration under the post-freeze scheme.** The baseline
-- (`000_baseline_pg154.sql`) is a frozen snapshot through PG 154; everything
-- after it ships as a numbered file sharing its PostgreSQL sibling's number.
-- That is what makes an existing MySQL install upgradable: the runner records
-- applied filenames in schema_migrations, skips the baseline it already has,
-- and applies only this.
--
-- A restore drill recorded pass or fail and no duration, so a twelve-hour
-- restore against a four-hour objective reported the same green as a ten-minute
-- one. Nullable: drills recorded before this column exist and have no
-- measurement — reported as unmeasured, never as meeting the RTO.
-- ============================================================================

ALTER TABLE backup_records
    ADD COLUMN restore_test_duration_seconds INT NULL;
