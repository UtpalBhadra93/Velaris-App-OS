-- 215 — legal holds (Oct item 13).
-- MySQL 8.4 / MariaDB 11.8 sibling of migrations/postgresql/215. See that file
-- for why; additive — one new table, and no rows means nothing is held.
--
-- Dialect notes: UUID → CHAR(36); TIMESTAMPTZ → DATETIME(6) with no default
-- (migration 176) — the application writes placed_at.
--
-- Copyright (c) 2024-2026 Velaris Contributors
-- SPDX-License-Identifier: BSL-1.1

CREATE TABLE IF NOT EXISTS legal_holds (
    id             CHAR(36)     PRIMARY KEY,
    tenant_id      CHAR(36)     NULL,
    case_id        CHAR(36)     NOT NULL,
    case_number    VARCHAR(30)  NULL,
    matter_ref     VARCHAR(120) NOT NULL,
    reason         TEXT         NOT NULL,
    status         VARCHAR(12)  NOT NULL DEFAULT 'active',
    active_key     VARCHAR(160) NULL,
    placed_by      VARCHAR(255) NOT NULL,
    placed_at      DATETIME(6)  NOT NULL,
    released_by    VARCHAR(255) NULL,
    released_at    DATETIME(6)  NULL,
    release_reason TEXT         NULL,

    UNIQUE KEY ux_legal_holds_active (active_key),
    KEY ix_legal_holds_case (case_id, status),
    KEY ix_legal_holds_tenant (tenant_id, status, placed_at),
    CONSTRAINT fk_legal_holds_tenant FOREIGN KEY (tenant_id)
        REFERENCES tenants(id) ON DELETE CASCADE,
    CONSTRAINT ck_legal_holds_status CHECK (status IN ('active', 'released'))
);
