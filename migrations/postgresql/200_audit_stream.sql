-- 200 — audit streaming to a SIEM (market gap A3).
--
-- WHY. The audit trail has always been good and has never had a way out.
-- `security_events` holds every authentication, authorization decision, data
-- access and configuration change — 51 call sites funnel through one writer —
-- and it all stayed in this platform's own database. To an enterprise SOC that
-- is not an answer: "we keep it in our own UI" is written up as a finding,
-- because an attacker who reaches the platform reaches its logs too.
--
-- WHAT THIS IS NOT. It is not a second audit store, and it is not a hook on
-- the write path. Events are read from `security_events` AFTER they are
-- durable, by a cursor, on a schedule. A forwarder that ran inside
-- `log_security_event` would put a network call inside login and inside every
-- authorization denial — a SIEM outage would then be a platform outage, which
-- is precisely the failure this is supposed to help detect.
--
-- ADDITIVE AND OFF. One new table. No install has a row in it, so no install
-- forwards anything until an administrator creates a target AND enables it —
-- two separate acts, because a target that starts shipping the moment it is
-- created ships to an endpoint nobody has confirmed yet.
--
-- RE-RUNNABLE. `IF NOT EXISTS` throughout; running it twice changes nothing.
--
-- Copyright (c) 2024-2026 Velaris Contributors
-- SPDX-License-Identifier: BSL-1.1

CREATE TABLE IF NOT EXISTS audit_stream_targets (
    id                UUID PRIMARY KEY,
    -- NULL = platform-wide, matching every other configuration table here.
    -- A tenant's target receives only that tenant's events; the platform
    -- target receives everything, which is what a single-tenant install wants.
    tenant_id         VARCHAR(255) NULL,
    -- `<tenant_id|platform>`. Written by the APPLICATION on both dialects, not
    -- GENERATED: MariaDB 11.8 refuses COALESCE in a GENERATED ALWAYS AS clause
    -- (error 1901) where MySQL 8.4 accepts it, and migration 177 paid for that
    -- lesson by creating three tables of five before anybody counted. It exists
    -- because NULL <> NULL — a plain UNIQUE over a nullable tenant_id does not
    -- constrain the platform rows at all.
    tenant_key        VARCHAR(255) NOT NULL,
    name              VARCHAR(255) NOT NULL,
    -- The transport. `splunk_hec` is the one that is built; the column exists
    -- so a second one does not need a migration, not because more are shipped.
    kind              VARCHAR(50)  NOT NULL DEFAULT 'splunk_hec',
    endpoint_url      TEXT         NOT NULL,
    -- Encrypted at rest through the same envelope every other stored
    -- credential uses (per-tenant DEK where one can be had). NEVER read back
    -- to a screen.
    token_encrypted   TEXT         NULL,
    -- A SIEM is usually on the customer's own network with an internal CA, so
    -- an operator can turn verification off. Default TRUE: the insecure choice
    -- must be one somebody made.
    verify_tls        BOOLEAN      NOT NULL DEFAULT TRUE,
    -- OFF until an operator says otherwise.
    enabled           BOOLEAN      NOT NULL DEFAULT FALSE,
    -- Filters. NULL/empty means "everything", which is the safe default for an
    -- audit stream: a SOC that receives too much can filter, one that silently
    -- receives too little cannot know.
    min_severity      VARCHAR(20)  NULL,
    event_types       TEXT         NULL,
    -- The cursor. Advanced ONLY after a batch is accepted, so a failed push is
    -- retried rather than skipped — at-least-once, which is the right side to
    -- err on when the reader is a security team.
    cursor_timestamp  TIMESTAMPTZ  NULL,
    cursor_event_id   UUID         NULL,
    -- Operational truth, on the row, so the screen can say why it is quiet.
    last_success_at   TIMESTAMPTZ  NULL,
    last_error        TEXT         NULL,
    last_error_at     TIMESTAMPTZ  NULL,
    forwarded_count   BIGINT       NOT NULL DEFAULT 0,
    created_at        TIMESTAMPTZ  NOT NULL DEFAULT now(),
    updated_at        TIMESTAMPTZ  NOT NULL DEFAULT now(),
    created_by        VARCHAR(255) NULL,

    -- Function-free, so every engine accepts it and the column cannot drift
    -- away from the id it summarises.
    CONSTRAINT ck_audit_stream_tenant_key CHECK (
        (tenant_id IS NULL     AND tenant_key = 'platform') OR
        (tenant_id IS NOT NULL AND tenant_key = tenant_id)
    )
);

-- One name per tenant.
CREATE UNIQUE INDEX IF NOT EXISTS ux_audit_stream_targets_name
    ON audit_stream_targets (tenant_key, name);

-- The forwarder reads enabled targets, then reads events after each cursor.
CREATE INDEX IF NOT EXISTS idx_audit_stream_targets_enabled
    ON audit_stream_targets (enabled);

-- The cursor walks (timestamp, id). Without this index that walk is a scan of
-- the whole security_events table every run, on the one table that grows
-- fastest.
CREATE INDEX IF NOT EXISTS idx_security_events_cursor
    ON security_events (timestamp, id);
