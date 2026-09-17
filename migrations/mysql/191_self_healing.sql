-- F-08 (first slice) — self-healing processes.
-- MySQL 8.4 / MariaDB 11.8 sibling of migrations/postgresql/191. See that file
-- for why each column exists and for what is deliberately NOT built.
--
-- Dialect notes:
--  * UUID columns are CHAR(36); TIMESTAMPTZ becomes DATETIME(6) with no DEFAULT.
--  * JSONB becomes JSON, which takes no literal DEFAULT — the writer supplies it.
--  * NUMERIC becomes DECIMAL.
--  * Indexes are inline KEYs so the file is re-runnable on all three engines.

CREATE TABLE IF NOT EXISTS healing_episodes (
    id                     CHAR(36)     PRIMARY KEY,
    tenant_id              CHAR(36)     NOT NULL,
    case_type_id           CHAR(36)     NOT NULL,
    insight_id             CHAR(36)     NULL,

    diagnosis              JSON         NOT NULL,
    operator               VARCHAR(32)  NOT NULL,
    proposal               JSON         NOT NULL,

    ladder                 JSON         NOT NULL,
    stopped_at             VARCHAR(32)  NULL,
    stopped_reason         TEXT         NULL,

    proof_id               CHAR(36)     NULL,
    simulation_id          CHAR(36)     NULL,
    twin_run_id            CHAR(36)     NULL,
    replay_run_id          CHAR(36)     NULL,

    status                 VARCHAR(20)  NOT NULL DEFAULT 'pending',
    approved_by            VARCHAR(255) NULL,
    approved_at            DATETIME(6)  NULL,
    decided_reason         TEXT         NULL,

    branch_id              CHAR(36)     NULL,
    applied_at             DATETIME(6)  NULL,
    rollback_artifact_type VARCHAR(32)  NULL,
    rollback_artifact_id   VARCHAR(64)  NULL,
    rollback_patch         JSON         NULL,

    watch_until            DATETIME(6)  NULL,
    watch_metric           VARCHAR(64)  NULL,
    watch_baseline         JSON         NULL,
    watch_bound_pct        DECIMAL(6,3) NULL,
    outcome                JSON         NULL,
    outcome_at             DATETIME(6)  NULL,

    rolled_back_at         DATETIME(6)  NULL,
    rollback_reason        TEXT         NULL,
    incident_case_id       CHAR(36)     NULL,

    sequence               BIGINT       NOT NULL,
    prev_hash              VARCHAR(64)  NOT NULL,
    content_hash           VARCHAR(64)  NOT NULL,

    created_by             VARCHAR(255) NULL,
    created_at             DATETIME(6)  NOT NULL,
    updated_at             DATETIME(6)  NOT NULL,

    KEY ix_heal_tenant (tenant_id, status),
    KEY ix_heal_ct     (case_type_id, created_at),
    KEY ix_heal_watch  (status, watch_until),

    CONSTRAINT ux_heal_sequence UNIQUE (sequence),
    CONSTRAINT fk_heal_tenant FOREIGN KEY (tenant_id)
        REFERENCES tenants (id) ON DELETE CASCADE
);
