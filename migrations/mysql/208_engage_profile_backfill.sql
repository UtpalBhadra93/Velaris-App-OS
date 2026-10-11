-- 208 — rep desk: profile backfill that PROPOSES (Oct item 3).
-- MySQL 8.4 / MariaDB 11.8 sibling of migrations/postgresql/208. See that file
-- for why; additive — two new tables.
--
-- Dialect notes:
--  * UUID → CHAR(36); JSONB → JSON with NO default (MySQL 8.4 accepts an
--    expression default for JSON, MariaDB 11.8 behaves differently, so the
--    application always writes it); TIMESTAMPTZ → DATETIME(6) with NO DEFAULT
--    (migration 176) — the application writes started_at/created_at/updated_at.
--  * Indexes are inline KEYs, so each table is one re-runnable statement.
--
-- Copyright (c) 2024-2026 Velaris Contributors
-- SPDX-License-Identifier: BSL-1.1

CREATE TABLE IF NOT EXISTS customer_backfill_runs (
    id           CHAR(36)     PRIMARY KEY,
    tenant_id    CHAR(36)     NOT NULL,
    started_by   VARCHAR(255) NOT NULL,
    started_at   DATETIME(6)  NOT NULL,
    finished_at  DATETIME(6)  NULL,
    counts       JSON         NOT NULL,

    KEY ix_cbr_tenant (tenant_id, started_at),
    CONSTRAINT fk_cbr_tenant FOREIGN KEY (tenant_id)
        REFERENCES tenants(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS customer_backfill_proposals (
    id            CHAR(36)     PRIMARY KEY,
    tenant_id     CHAR(36)     NOT NULL,
    run_id        CHAR(36)     NULL,
    kind          VARCHAR(16)  NOT NULL,
    fingerprint   VARCHAR(64)  NOT NULL,
    payload       JSON         NOT NULL,
    evidence      JSON         NOT NULL,
    status        VARCHAR(16)  NOT NULL,
    created_at    DATETIME(6)  NOT NULL,
    updated_at    DATETIME(6)  NOT NULL,
    decided_by    VARCHAR(255) NULL,
    decided_at    DATETIME(6)  NULL,
    reason        TEXT         NULL,
    result        JSON         NULL,

    UNIQUE KEY uq_cbp_fingerprint (tenant_id, fingerprint),
    KEY ix_cbp_tenant_status (tenant_id, status, kind),
    CONSTRAINT fk_cbp_tenant FOREIGN KEY (tenant_id)
        REFERENCES tenants(id) ON DELETE CASCADE,
    CONSTRAINT fk_cbp_run FOREIGN KEY (run_id)
        REFERENCES customer_backfill_runs(id) ON DELETE SET NULL,
    CONSTRAINT ck_cbp_kind CHECK (kind IN ('create_profile', 'enrich_profile', 'merge_profiles')),
    CONSTRAINT ck_cbp_status CHECK (status IN ('proposed', 'accepted', 'rejected', 'superseded'))
);
