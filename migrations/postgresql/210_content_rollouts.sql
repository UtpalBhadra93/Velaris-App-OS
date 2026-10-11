-- 210 — staged rollout of .hxapp content (Oct item 18a).
--
-- WHY. A client ships a new version of a case type and chooses how it reaches
-- their customers: at once (as today), dormant behind a switch, to N% of
-- customers (sticky), or as a blue-green switch — and then promotes it or rolls
-- it back. A rollout only chooses the version for NEW cases; a case keeps the
-- case type it was created on.
--
-- THE LOCK. One open rollout per (tenant, kind, name), so a customer only ever
-- sees baseline or candidate, never three versions. `open_key` holds
-- "<tenant>:<kind>:<name>" while the rollout is open and NULL once it closes; a
-- UNIQUE index on it is the lock. NULLs never collide, so this is the same
-- guarantee on PostgreSQL, MySQL and MariaDB without a partial index.
--
-- A CLOSED rollout still means something: a promoted one sends requests for its
-- baseline to its candidate, a rolled-back or superseded one sends requests for
-- its candidate back to the baseline. That is how a channel, portal or schedule
-- configured with the old id follows the promotion.
--
-- WHAT DOES NOT CHANGE. Additive: three new tables. No rollout rows means every
-- case type resolves to itself — exactly as before.
--
-- RE-RUNNABLE. `IF NOT EXISTS` throughout.
--
-- Copyright (c) 2024-2026 Velaris Contributors
-- SPDX-License-Identifier: BSL-1.1

CREATE TABLE IF NOT EXISTS content_rollouts (
    id             UUID PRIMARY KEY,
    tenant_id      UUID         NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    artifact_kind  VARCHAR(20)  NOT NULL,
    artifact_name  VARCHAR(255) NOT NULL,
    baseline_id    UUID         NOT NULL,
    candidate_id   UUID         NOT NULL,
    -- immediate | flag | percentage | blue_green
    strategy       VARCHAR(20)  NOT NULL,
    percent        INTEGER      NOT NULL DEFAULT 0,
    -- requested | dormant | rolling | promoted | rolled_back | superseded
    state          VARCHAR(20)  NOT NULL,
    open_key       VARCHAR(400) NULL,
    source         VARCHAR(20)  NOT NULL DEFAULT 'local',
    request        JSONB        NULL,
    requested_by   VARCHAR(255) NULL,
    approved_by    VARCHAR(255) NULL,
    closed_by      VARCHAR(255) NULL,
    superseded_by  UUID         NULL,
    note           TEXT         NULL,
    created_at     TIMESTAMPTZ  NOT NULL DEFAULT now(),
    updated_at     TIMESTAMPTZ  NOT NULL DEFAULT now(),
    approved_at    TIMESTAMPTZ  NULL,
    closed_at      TIMESTAMPTZ  NULL,

    CONSTRAINT uq_cr_open UNIQUE (open_key),
    CONSTRAINT ck_cr_percent CHECK (percent BETWEEN 0 AND 100),
    CONSTRAINT ck_cr_strategy CHECK (strategy IN ('immediate', 'flag', 'percentage', 'blue_green')),
    CONSTRAINT ck_cr_state CHECK (state IN ('requested', 'dormant', 'rolling', 'promoted',
                                            'rolled_back', 'superseded'))
);
CREATE INDEX IF NOT EXISTS ix_cr_baseline  ON content_rollouts (tenant_id, baseline_id);
CREATE INDEX IF NOT EXISTS ix_cr_candidate ON content_rollouts (tenant_id, candidate_id);
CREATE INDEX IF NOT EXISTS ix_cr_state     ON content_rollouts (tenant_id, state);

-- Every state and percent change, and who made it.
CREATE TABLE IF NOT EXISTS content_rollout_events (
    id            UUID PRIMARY KEY,
    rollout_id    UUID         NOT NULL REFERENCES content_rollouts(id) ON DELETE CASCADE,
    tenant_id     UUID         NOT NULL,
    action        VARCHAR(30)  NOT NULL,
    from_state    VARCHAR(20)  NULL,
    to_state      VARCHAR(20)  NULL,
    from_percent  INTEGER      NULL,
    to_percent    INTEGER      NULL,
    actor         VARCHAR(255) NULL,
    reason        TEXT         NULL,
    at            TIMESTAMPTZ  NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_cre_rollout ON content_rollout_events (rollout_id, at);

-- Which arm each new case landed in, for audit and the arm counts.
CREATE TABLE IF NOT EXISTS content_rollout_cases (
    case_id       UUID PRIMARY KEY REFERENCES case_instances(id) ON DELETE CASCADE,
    rollout_id    UUID         NOT NULL REFERENCES content_rollouts(id) ON DELETE CASCADE,
    tenant_id     UUID         NOT NULL,
    arm           VARCHAR(10)  NOT NULL,
    bucket        INTEGER      NOT NULL,
    via           VARCHAR(40)  NULL,
    created_at    TIMESTAMPTZ  NOT NULL DEFAULT now(),
    CONSTRAINT ck_crc_arm CHECK (arm IN ('baseline', 'candidate'))
);
CREATE INDEX IF NOT EXISTS ix_crc_rollout ON content_rollout_cases (rollout_id, arm);
