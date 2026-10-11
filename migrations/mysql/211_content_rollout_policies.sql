-- 211 — staged rollout, phases 2 and 3 (Oct item 18a).
-- MySQL 8.4 / MariaDB 11.8 sibling of migrations/postgresql/211. See that file
-- for why; additive — two new tables, no ALTER, so re-runnable here too.
--
-- Dialect notes: UUID → CHAR(36); JSONB → JSON with no default and
-- TIMESTAMPTZ → DATETIME(6) with no default (migration 176) — the application
-- writes them.
--
-- Copyright (c) 2024-2026 Velaris Contributors
-- SPDX-License-Identifier: BSL-1.1

CREATE TABLE IF NOT EXISTS content_rollout_policies (
    rollout_id     CHAR(36)     PRIMARY KEY,
    tenant_id      CHAR(36)     NOT NULL,
    shadow         BOOLEAN      NOT NULL DEFAULT FALSE,
    targeting      JSON         NULL,
    ramp           JSON         NULL,
    guard          JSON         NULL,
    next_step_at   DATETIME(6)  NULL,
    guard_state    VARCHAR(10)  NOT NULL DEFAULT 'ok',
    guard_detail   JSON         NULL,
    updated_by     VARCHAR(255) NULL,
    updated_at     DATETIME(6)  NOT NULL,
    KEY ix_crp_next (next_step_at),
    CONSTRAINT fk_crp_rollout FOREIGN KEY (rollout_id)
        REFERENCES content_rollouts(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS content_rollout_shadow (
    case_id        CHAR(36)     PRIMARY KEY,
    rollout_id     CHAR(36)     NOT NULL,
    tenant_id      CHAR(36)     NOT NULL,
    determinacy    VARCHAR(20)  NOT NULL,
    diverged       BOOLEAN      NOT NULL DEFAULT FALSE,
    detail         JSON         NULL,
    evaluated_at   DATETIME(6)  NOT NULL,
    KEY ix_crs_rollout (rollout_id, diverged),
    CONSTRAINT fk_crs_case FOREIGN KEY (case_id)
        REFERENCES case_instances(id) ON DELETE CASCADE,
    CONSTRAINT fk_crs_rollout FOREIGN KEY (rollout_id)
        REFERENCES content_rollouts(id) ON DELETE CASCADE
);
