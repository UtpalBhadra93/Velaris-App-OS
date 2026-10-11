-- 216 — audit chain prune records (Oct_TODO §11h).
-- MySQL 8.4 / MariaDB 11.8 sibling of migrations/postgresql/216. See that file
-- for why; additive — one new table, and no rows means nothing was pruned.
--
-- Dialect notes: UUID → CHAR(36); JSONB → JSON; TIMESTAMPTZ → DATETIME(6) with
-- no default (migration 176) — the application writes created_at.
--
-- Copyright (c) 2024-2026 Velaris Contributors
-- SPDX-License-Identifier: BSL-1.1

CREATE TABLE IF NOT EXISTS case_audit_log_chain_prunes (
    id            CHAR(36)     PRIMARY KEY,
    reason        VARCHAR(40)  NOT NULL,
    actor         VARCHAR(255) NOT NULL,
    cutoff        VARCHAR(40)  NULL,
    recorded_at   VARCHAR(40)  NOT NULL,
    pruned_count  INT          NOT NULL,
    ranges        JSON         NOT NULL,
    digest        VARCHAR(64)  NOT NULL,
    created_at    DATETIME(6)  NOT NULL
);
