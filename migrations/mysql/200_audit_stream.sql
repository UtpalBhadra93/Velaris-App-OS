-- 200 — audit streaming to a SIEM (market gap A3).
-- MySQL 8.4 / MariaDB 11.8 sibling of migrations/postgresql/200. See that file
-- for why each column exists, and above all for why the forwarder READS
-- `security_events` on a cursor rather than hooking the writer: a SIEM outage
-- must never become a login outage.
--
-- Dialect notes:
--  * UUID columns are CHAR(36); TIMESTAMPTZ becomes DATETIME(6) with no
--    DEFAULT — MySQL 8.4 rejects `DEFAULT now()` on DATETIME(6) where MariaDB
--    accepts it (migration 176 found that one), so the application writes both
--    timestamps.
--  * BOOLEAN becomes TINYINT(1); BIGINT is the same on both.
--  * `tenant_key` is written by the application on BOTH dialects rather than
--    GENERATED, because MariaDB 11.8 refuses COALESCE in a GENERATED ALWAYS AS
--    clause (error 1901). The CHECK keeps it honest without calling a function.
--  * Indexes are inline KEYs, so the CREATE TABLE is the whole file.
--
-- ON RE-RUNS. `CREATE TABLE IF NOT EXISTS` is re-runnable on both engines. The
-- index on `security_events` is a plain CREATE INDEX for the same reason 199's
-- was: MySQL 8.4 has no `IF NOT EXISTS` for it, and once-only application is
-- the migration runner's ledger.
--
-- Copyright (c) 2024-2026 Velaris Contributors
-- SPDX-License-Identifier: BSL-1.1

CREATE TABLE IF NOT EXISTS audit_stream_targets (
    id                CHAR(36)     PRIMARY KEY,

    tenant_id         VARCHAR(255) NULL,
    tenant_key        VARCHAR(255) NOT NULL,

    name              VARCHAR(255) NOT NULL,
    kind              VARCHAR(50)  NOT NULL DEFAULT 'splunk_hec',
    endpoint_url      TEXT         NOT NULL,
    token_encrypted   TEXT         NULL,

    verify_tls        TINYINT(1)   NOT NULL DEFAULT 1,
    enabled           TINYINT(1)   NOT NULL DEFAULT 0,

    min_severity      VARCHAR(20)  NULL,
    event_types       TEXT         NULL,

    cursor_timestamp  DATETIME(6)  NULL,
    cursor_event_id   CHAR(36)     NULL,

    last_success_at   DATETIME(6)  NULL,
    last_error        TEXT         NULL,
    last_error_at     DATETIME(6)  NULL,
    forwarded_count   BIGINT       NOT NULL DEFAULT 0,

    created_at        DATETIME(6)  NOT NULL,
    updated_at        DATETIME(6)  NOT NULL,
    created_by        VARCHAR(255) NULL,

    CONSTRAINT ck_audit_stream_tenant_key CHECK (
        (tenant_id IS NULL     AND tenant_key = 'platform') OR
        (tenant_id IS NOT NULL AND tenant_key = tenant_id)
    ),
    CONSTRAINT ux_audit_stream_targets_name UNIQUE (tenant_key, name),
    KEY idx_audit_stream_targets_enabled (enabled)
);

CREATE INDEX idx_security_events_cursor
    ON security_events (timestamp, id);
