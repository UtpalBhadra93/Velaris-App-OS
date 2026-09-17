-- 205 — moving a case between tenants.
-- MySQL 8.4 / MariaDB 11.8 sibling of migrations/postgresql/205. See that file
-- for why: additive, one table, read only by new admin-only routes.
--
-- Dialect notes:
--  * UUID → CHAR(36); TIMESTAMPTZ → DATETIME(6) with NO DEFAULT (migration 176),
--    so the application writes requested_at; JSONB → JSON.
--  * `active_key` is the portable partial unique index, as in 203.
--
-- Copyright (c) 2024-2026 Velaris Contributors
-- SPDX-License-Identifier: BSL-1.1

CREATE TABLE IF NOT EXISTS case_tenant_transfers (
    id                    CHAR(36)     PRIMARY KEY,
    case_id               CHAR(36)     NOT NULL,
    case_number           VARCHAR(30)  NULL,
    case_type_name        VARCHAR(255) NULL,
    source_tenant_id      CHAR(36)     NOT NULL,
    destination_tenant_id CHAR(36)     NOT NULL,

    state                 VARCHAR(16)  NOT NULL DEFAULT 'requested',
    active_key            VARCHAR(36)  NULL,

    reason                TEXT         NOT NULL,
    requested_by          VARCHAR(255) NOT NULL,
    requested_at          DATETIME(6)  NOT NULL,
    approved_by           VARCHAR(255) NULL,
    approved_at           DATETIME(6)  NULL,
    decided_by            VARCHAR(255) NULL,
    decided_at            DATETIME(6)  NULL,
    decision_note         TEXT         NULL,
    cancelled_by          VARCHAR(255) NULL,

    manifest              JSON         NULL,
    completed_at          DATETIME(6)  NULL,

    UNIQUE KEY ux_case_tenant_transfers_active (active_key),
    KEY ix_case_tenant_transfers_source (source_tenant_id, state, requested_at),
    KEY ix_case_tenant_transfers_destination (destination_tenant_id, state, requested_at),
    KEY ix_case_tenant_transfers_case (case_id),
    CONSTRAINT ck_case_tenant_transfers_state CHECK (
        state IN ('requested', 'pending', 'completed', 'refused', 'cancelled')
    )
);
