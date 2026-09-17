-- F-04 (second slice) — the digital twin.
-- MySQL 8.4 / MariaDB 11.8 sibling of migrations/postgresql/190. See that file
-- for why each column exists, and for what is deliberately NOT built.
--
-- Dialect notes:
--  * UUID columns are CHAR(36); TIMESTAMPTZ becomes DATETIME(6) with no DEFAULT.
--  * JSONB becomes JSON, which takes no literal DEFAULT.
--  * BOOLEAN becomes TINYINT(1); NUMERIC becomes DECIMAL.
--  * `ADD COLUMN IF NOT EXISTS` is MariaDB-only, so each ALTER is wrapped in a
--    prepared statement guarded on information_schema — the same idiom the
--    earlier ALTER migrations in this directory use, and the reason this file
--    is re-runnable on both engines.

CREATE TABLE IF NOT EXISTS twin_runs (
    id                   CHAR(36)     PRIMARY KEY,
    tenant_id            CHAR(36)     NOT NULL,
    deployment_run_id    CHAR(36)     NULL,
    package_checksum     VARCHAR(64)  NULL,

    task_queue           VARCHAR(255) NULL,
    database_fingerprint VARCHAR(64)  NULL,

    isolation_verified   TINYINT(1)   NOT NULL DEFAULT 0,
    isolation_detail     JSON         NULL,

    provisioned          JSON         NULL,

    load_profile         VARCHAR(20)  NOT NULL DEFAULT 'batch',
    requested_cases      INT          NOT NULL DEFAULT 0,
    started_cases        INT          NOT NULL DEFAULT 0,
    terminal_cases       INT          NOT NULL DEFAULT 0,
    failed_cases         INT          NOT NULL DEFAULT 0,
    stuck_cases          INT          NOT NULL DEFAULT 0,

    p50_ms               INT          NULL,
    p95_ms               INT          NULL,
    max_ms               INT          NULL,
    stage_profile        JSON         NULL,
    failures             JSON         NULL,

    status               VARCHAR(20)  NOT NULL DEFAULT 'pending',
    refusal_reason       VARCHAR(64)  NULL,
    error                TEXT         NULL,

    torn_down_at         DATETIME(6)  NULL,
    torn_down_rows       INT          NULL,
    teardown_error       TEXT         NULL,

    created_by           VARCHAR(255) NULL,
    created_at           DATETIME(6)  NOT NULL,
    completed_at         DATETIME(6)  NULL,

    KEY ix_twinrun_tenant (tenant_id, created_at),
    KEY ix_twinrun_deploy (deployment_run_id, created_at),

    CONSTRAINT fk_twinrun_tenant FOREIGN KEY (tenant_id)
        REFERENCES tenants (id) ON DELETE CASCADE,
    CONSTRAINT fk_twinrun_deploy FOREIGN KEY (deployment_run_id)
        REFERENCES deployment_runs (id) ON DELETE CASCADE
);


-- ── the four nullable columns, added re-runnably ────────────────────────────

DROP PROCEDURE IF EXISTS velaris_add_col_190;
DELIMITER //
CREATE PROCEDURE velaris_add_col_190(
    IN p_table VARCHAR(64), IN p_column VARCHAR(64), IN p_type VARCHAR(64))
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.COLUMNS
        WHERE TABLE_SCHEMA = DATABASE()
          AND TABLE_NAME = p_table AND COLUMN_NAME = p_column
    ) THEN
        SET @ddl = CONCAT('ALTER TABLE `', p_table, '` ADD COLUMN `',
                          p_column, '` ', p_type, ' NULL');
        PREPARE stmt FROM @ddl;
        EXECUTE stmt;
        DEALLOCATE PREPARE stmt;
    END IF;
END //
DELIMITER ;

CALL velaris_add_col_190('simulation_runs',  'twin_run_id',          'CHAR(36)');
CALL velaris_add_col_190('simulation_gates', 'min_twin_cases',       'INT');
CALL velaris_add_col_190('simulation_gates', 'max_twin_failure_pct', 'DECIMAL(6,3)');
CALL velaris_add_col_190('simulation_gates', 'max_twin_p95_ms',      'INT');

DROP PROCEDURE IF EXISTS velaris_add_col_190;
