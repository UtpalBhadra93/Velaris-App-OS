-- F-04 (first slice) — the simulation gate on promotion.
-- MySQL 8.4 / MariaDB 11.8 sibling of migrations/postgresql/186. See that file
-- for why each table and column exists, and for what is deliberately NOT built.
--
-- Dialect notes:
--  * UUID columns are CHAR(36); TIMESTAMPTZ becomes DATETIME(6) with no DEFAULT
--    (MySQL rejects DEFAULT now() there where MariaDB allows it).
--  * JSONB becomes JSON, which takes no literal DEFAULT — the writer supplies it.
--  * NUMERIC becomes DECIMAL.
--  * Indexes are inline KEYs so the file is re-runnable, and the ALTER at the
--    bottom uses a prepared statement for the same reason: MySQL 8.4 has no
--    `ADD COLUMN IF NOT EXISTS` where MariaDB does, so neither plain form is
--    replayable on both.

CREATE TABLE IF NOT EXISTS simulation_gates (
    id                     CHAR(36)     PRIMARY KEY,
    tenant_id              CHAR(36)     NULL,
    tenant_key             VARCHAR(36)  NOT NULL,
    enabled                TINYINT(1)   NOT NULL DEFAULT 0,
    max_divergent_pct      DECIMAL(6,3) NULL,
    max_cost_increase_pct  DECIMAL(6,3) NULL,
    min_cohort             INT          NULL,
    require_proof          TINYINT(1)   NOT NULL DEFAULT 0,
    created_by             CHAR(36)     NULL,
    created_at             DATETIME(6)  NOT NULL,
    updated_at             DATETIME(6)  NOT NULL,

    CONSTRAINT ck_simgate_tenant_key CHECK (
        (tenant_id IS NULL     AND tenant_key =  'platform') OR
        (tenant_id IS NOT NULL AND tenant_key <> 'platform')
    ),
    CONSTRAINT ux_simgate_scope UNIQUE (tenant_key),
    CONSTRAINT fk_simgate_tenant FOREIGN KEY (tenant_id)
        REFERENCES tenants (id) ON DELETE CASCADE
);


CREATE TABLE IF NOT EXISTS simulation_runs (
    id                CHAR(36)     PRIMARY KEY,
    tenant_id         CHAR(36)     NOT NULL,
    deployment_run_id CHAR(36)     NULL,
    package_checksum  VARCHAR(64)  NULL,
    status            VARCHAR(20)  NOT NULL,
    findings          JSON         NOT NULL,
    thresholds        JSON         NOT NULL,
    replay_run_id     CHAR(36)     NULL,
    proof_id          CHAR(36)     NULL,
    twin_agent_id     CHAR(36)     NULL,
    created_by        CHAR(36)     NULL,
    created_at        DATETIME(6)  NOT NULL,
    completed_at      DATETIME(6)  NULL,

    KEY ix_simrun_deploy (deployment_run_id, created_at),
    KEY ix_simrun_tenant (tenant_id, created_at),

    CONSTRAINT fk_simrun_tenant FOREIGN KEY (tenant_id)
        REFERENCES tenants (id) ON DELETE CASCADE,
    CONSTRAINT fk_simrun_deploy FOREIGN KEY (deployment_run_id)
        REFERENCES deployment_runs (id) ON DELETE CASCADE
);


-- Four additive, nullable columns on `deployment_runs`. Every existing run
-- reads "never gated", which is what every existing run is.
SET @cols := (SELECT COUNT(*) FROM information_schema.columns
              WHERE table_schema = DATABASE()
                AND table_name   = 'deployment_runs'
                AND column_name  = 'simulation_id');
SET @ddl := IF(@cols = 0,
    'ALTER TABLE deployment_runs
        ADD COLUMN simulation_id        CHAR(36)     NULL,
        ADD COLUMN gate_override_reason TEXT         NULL,
        ADD COLUMN gate_overridden_by   VARCHAR(255) NULL,
        ADD COLUMN gate_overridden_at   DATETIME(6)  NULL',
    'DO 0');
PREPARE _add_gate_cols FROM @ddl;
EXECUTE _add_gate_cols;
DEALLOCATE PREPARE _add_gate_cols;
