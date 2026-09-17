-- 203 — document indexing off the request (the queue half).
-- MySQL 8.4 / MariaDB 11.8 sibling of migrations/postgresql/203. See that file
-- for why this table exists and, above all, for why the SYNCHRONOUS path stays
-- the default: an install that takes this update and creates no schedule
-- indexes exactly as it does today.
--
-- Dialect notes:
--  * UUID → CHAR(36); TIMESTAMPTZ → DATETIME(6) with NO DEFAULT. MySQL 8.4
--    rejects `DEFAULT now()` on DATETIME(6) where MariaDB accepts it
--    (migration 176 paid for that one), so the application writes created_at.
--  * BOOLEAN → TINYINT(1).
--  * `active_key` is the portable form of PostgreSQL's partial unique index —
--    MySQL has no partial index, and NULL <> NULL on both engines, so a unique
--    key over a column that is NULL once the job finishes constrains exactly
--    the active rows.
--  * Indexes are inline KEYs, so CREATE TABLE is the whole file and the whole
--    file is re-runnable via IF NOT EXISTS.
--
-- Copyright (c) 2024-2026 Velaris Contributors
-- SPDX-License-Identifier: BSL-1.1

CREATE TABLE IF NOT EXISTS document_index_jobs (
    id             CHAR(36)     PRIMARY KEY,

    tenant_id      VARCHAR(255) NULL,
    case_id        CHAR(36)     NULL,
    document_id    CHAR(36)     NOT NULL,

    status         VARCHAR(20)  NOT NULL DEFAULT 'pending',
    active_key     VARCHAR(36)  NULL,

    attempts       INT          NOT NULL DEFAULT 0,
    max_attempts   INT          NOT NULL DEFAULT 3,
    reindex        TINYINT(1)   NOT NULL DEFAULT 0,

    chunks_indexed INT          NULL,
    last_error     TEXT         NULL,
    requested_by   VARCHAR(255) NULL,

    created_at     DATETIME(6)  NOT NULL,
    started_at     DATETIME(6)  NULL,
    finished_at    DATETIME(6)  NULL,

    CONSTRAINT ck_document_index_jobs_status CHECK (
        status IN ('pending', 'running', 'done', 'failed', 'cancelled')
    ),

    UNIQUE KEY ux_document_index_jobs_active (active_key),
    KEY idx_document_index_jobs_status (status, created_at),
    KEY idx_document_index_jobs_case (case_id)
);
