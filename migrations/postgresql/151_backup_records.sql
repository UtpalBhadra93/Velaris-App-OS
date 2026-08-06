-- HxDR — disaster recovery posture (§8.9, roadmap #39).
--
-- The platform already TAKES backups: `update-velaris.sh` dumps the database
-- before every update and aborts if the dump fails. What was missing is the
-- RECORD. Without one, "when did we last have a good backup" and "does that
-- meet our RPO" cannot be answered, which is the entire substance of a DR
-- posture.
--
-- Deliberately a REGISTRY, not an executor. Taking a dump belongs to ops
-- tooling that holds the credentials and can block for minutes; an API request
-- path must not shell out to pg_dump.
--
-- `restore_tested_at` is the load-bearing column. An untested backup is a hope,
-- not a recovery plan, and the posture report treats "untested" as a state
-- distinct from "ok" — collapsing them is how organisations discover at the
-- worst possible moment that their backups never worked. A FAILED drill is
-- kept too: knowing a backup does not restore is worth more than any number of
-- untested ones.
--
-- `stored_region_id` exists because residency follows the bytes: a backup of an
-- EU-governed region parked in another jurisdiction is a violation even though
-- the primary data never moved. The application refuses to record one.

CREATE TABLE IF NOT EXISTS backup_records (
    id                  UUID PRIMARY KEY,
    region_id           UUID REFERENCES region_registry(id) ON DELETE SET NULL,
    scope               VARCHAR(32)  NOT NULL DEFAULT 'database',
    status              VARCHAR(16)  NOT NULL DEFAULT 'succeeded',
    location            VARCHAR(512) NOT NULL,
    stored_region_id    UUID,
    size_bytes          BIGINT,
    checksum            VARCHAR(128),
    encrypted           BOOLEAN      NOT NULL DEFAULT FALSE,
    error               TEXT,
    tenant_id           VARCHAR(64),
    taken_at            TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    restore_tested_at   TIMESTAMPTZ,
    restore_test_result VARCHAR(16),
    restore_test_notes  TEXT,
    recorded_by         VARCHAR(255)
);

CREATE INDEX IF NOT EXISTS ix_backup_records_region ON backup_records (region_id, taken_at);
CREATE INDEX IF NOT EXISTS ix_backup_records_status ON backup_records (status);
