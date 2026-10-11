-- 210 — staged rollout of .hxapp content (Oct item 18a).
-- MySQL 8.4 / MariaDB 11.8 sibling of migrations/postgresql/210. See that file
-- for why; additive — three new tables. The lock is the UNIQUE index on
-- `open_key` (NULL once a rollout closes; NULLs never collide).
--
-- Dialect notes: UUID → CHAR(36); JSONB → JSON with no default and
-- TIMESTAMPTZ → DATETIME(6) with no default (migration 176) — the application
-- writes them.
--
-- Copyright (c) 2024-2026 Velaris Contributors
-- SPDX-License-Identifier: BSL-1.1

CREATE TABLE IF NOT EXISTS content_rollouts (
    id             CHAR(36)     PRIMARY KEY,
    tenant_id      CHAR(36)     NOT NULL,
    artifact_kind  VARCHAR(20)  NOT NULL,
    artifact_name  VARCHAR(255) NOT NULL,
    baseline_id    CHAR(36)     NOT NULL,
    candidate_id   CHAR(36)     NOT NULL,
    strategy       VARCHAR(20)  NOT NULL,
    percent        INTEGER      NOT NULL DEFAULT 0,
    state          VARCHAR(20)  NOT NULL,
    open_key       VARCHAR(400) NULL,
    source         VARCHAR(20)  NOT NULL DEFAULT 'local',
    request        JSON         NULL,
    requested_by   VARCHAR(255) NULL,
    approved_by    VARCHAR(255) NULL,
    closed_by      VARCHAR(255) NULL,
    superseded_by  CHAR(36)     NULL,
    note           TEXT         NULL,
    created_at     DATETIME(6)  NOT NULL,
    updated_at     DATETIME(6)  NOT NULL,
    approved_at    DATETIME(6)  NULL,
    closed_at      DATETIME(6)  NULL,

    UNIQUE KEY uq_cr_open (open_key),
    KEY ix_cr_baseline (tenant_id, baseline_id),
    KEY ix_cr_candidate (tenant_id, candidate_id),
    KEY ix_cr_state (tenant_id, state),
    CONSTRAINT fk_cr_tenant FOREIGN KEY (tenant_id) REFERENCES tenants(id) ON DELETE CASCADE,
    CONSTRAINT ck_cr_percent CHECK (percent BETWEEN 0 AND 100),
    CONSTRAINT ck_cr_strategy CHECK (strategy IN ('immediate', 'flag', 'percentage', 'blue_green')),
    CONSTRAINT ck_cr_state CHECK (state IN ('requested', 'dormant', 'rolling', 'promoted',
                                            'rolled_back', 'superseded'))
);

CREATE TABLE IF NOT EXISTS content_rollout_events (
    id            CHAR(36)     PRIMARY KEY,
    rollout_id    CHAR(36)     NOT NULL,
    tenant_id     CHAR(36)     NOT NULL,
    action        VARCHAR(30)  NOT NULL,
    from_state    VARCHAR(20)  NULL,
    to_state      VARCHAR(20)  NULL,
    from_percent  INTEGER      NULL,
    to_percent    INTEGER      NULL,
    actor         VARCHAR(255) NULL,
    reason        TEXT         NULL,
    at            DATETIME(6)  NOT NULL,
    KEY ix_cre_rollout (rollout_id, at),
    CONSTRAINT fk_cre_rollout FOREIGN KEY (rollout_id)
        REFERENCES content_rollouts(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS content_rollout_cases (
    case_id       CHAR(36)     PRIMARY KEY,
    rollout_id    CHAR(36)     NOT NULL,
    tenant_id     CHAR(36)     NOT NULL,
    arm           VARCHAR(10)  NOT NULL,
    bucket        INTEGER      NOT NULL,
    via           VARCHAR(40)  NULL,
    created_at    DATETIME(6)  NOT NULL,
    KEY ix_crc_rollout (rollout_id, arm),
    CONSTRAINT fk_crc_case FOREIGN KEY (case_id)
        REFERENCES case_instances(id) ON DELETE CASCADE,
    CONSTRAINT fk_crc_rollout FOREIGN KEY (rollout_id)
        REFERENCES content_rollouts(id) ON DELETE CASCADE,
    CONSTRAINT ck_crc_arm CHECK (arm IN ('baseline', 'candidate'))
);
