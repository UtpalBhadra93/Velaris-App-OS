-- HxDR: measure the RTO instead of only declaring it.
--
-- `posture` reported `rto_hours` straight from a constant and never compared it
-- to anything. A restore drill recorded pass or fail, so a backup that took
-- twelve hours to restore against a four-hour objective reported exactly the
-- same healthy green as one that took ten minutes. The posture answered "can we
-- recover?" and never "in time?" — and the second question is the one an RTO
-- exists to ask.
--
-- Nullable, because every drill recorded before today has no measurement and
-- inventing one would be worse than admitting the gap: a region whose only
-- drill predates this column reports its RTO as unmeasured, not as met.

ALTER TABLE backup_records
    ADD COLUMN IF NOT EXISTS restore_test_duration_seconds INTEGER NULL;

COMMENT ON COLUMN backup_records.restore_test_duration_seconds IS
    'Wall-clock seconds the restore drill took. NULL = never measured, which '
    'the posture reports as its own state rather than as meeting the RTO.';
