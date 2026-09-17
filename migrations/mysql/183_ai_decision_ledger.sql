-- F-01 — the AI decision ledger.
-- MySQL 8.4 / MariaDB 11.8 sibling of migrations/postgresql/183. See that file
-- for why each table and column exists; only dialect differences are noted here.
--
-- Dialect notes:
--  * UUID columns are CHAR(36) — what the GUID() decorator emits off PostgreSQL.
--  * TIMESTAMPTZ becomes DATETIME(6) and carries NO DEFAULT: MySQL rejects
--    DEFAULT now() there where MariaDB accepts it, so every writer supplies it.
--  * JSONB becomes JSON, which takes no literal DEFAULT either.
--  * Indexes are declared INLINE as KEYs (MySQL has no CREATE INDEX IF NOT
--    EXISTS), so this file is re-runnable on all three engines — see the note
--    in 182 for why that is worth the small departure from its neighbours.
--  * `sequence` is a reserved word in neither engine, but is quoted throughout
--    for the same reason `window_start`/`window_end` are: these are all
--    borderline names and the backticks cost nothing.

CREATE TABLE IF NOT EXISTS ai_decision_records (
    id                  CHAR(36)      PRIMARY KEY,
    tenant_id           CHAR(36)      NOT NULL,
    agent_id            CHAR(36)      NULL,
    agent_slug          VARCHAR(128)  NULL,
    agent_kind          VARCHAR(20)   NULL,
    model_id            VARCHAR(255)  NOT NULL,
    provider_class      VARCHAR(16)   NOT NULL DEFAULT 'local',
    purpose             VARCHAR(64)   NOT NULL,
    case_id             CHAR(36)      NULL,
    step_id             VARCHAR(255)  NULL,
    input_sha256        CHAR(64)      NOT NULL,
    input_data_classes  JSON          NOT NULL,
    output_sha256       CHAR(64)      NULL,
    prompt_sealed       TEXT          NULL,
    output_sealed       TEXT          NULL,
    confidence          DECIMAL(5,4)  NULL,
    latency_ms          DECIMAL(10,2) NULL,
    arbiter_decision_id CHAR(36)      NULL,
    chain_sequence      BIGINT        NULL,
    recorded_at         DATETIME(6)   NOT NULL,

    KEY ix_aidr_tenant_time (tenant_id, recorded_at),
    KEY ix_aidr_case        (case_id, recorded_at),
    KEY ix_aidr_agent       (agent_id, recorded_at),
    KEY ix_aidr_model       (model_id),
    KEY ix_aidr_unsealed    (chain_sequence, recorded_at),

    CONSTRAINT fk_aidr_tenant FOREIGN KEY (tenant_id)
        REFERENCES tenants (id) ON DELETE CASCADE
);


CREATE TABLE IF NOT EXISTS ai_decision_verdicts (
    id             CHAR(36)     PRIMARY KEY,
    record_id      CHAR(36)     NOT NULL,
    tenant_id      CHAR(36)     NOT NULL,
    verdict        VARCHAR(20)  NOT NULL,
    diff_sha256    CHAR(64)     NULL,
    diff_sealed    TEXT         NULL,
    reason         TEXT         NOT NULL,
    decided_by     VARCHAR(255) NULL,
    decided_at     DATETIME(6)  NOT NULL,
    chain_sequence BIGINT       NULL,

    KEY ix_aidv_record   (record_id, decided_at),
    KEY ix_aidv_unsealed (chain_sequence, decided_at),

    CONSTRAINT fk_aidv_record FOREIGN KEY (record_id)
        REFERENCES ai_decision_records (id) ON DELETE CASCADE
);


CREATE TABLE IF NOT EXISTS ai_ledger_chain (
    id             CHAR(36)    PRIMARY KEY,
    `sequence`     BIGINT      NOT NULL UNIQUE,
    window_start   DATETIME(6) NOT NULL,
    window_end     DATETIME(6) NOT NULL,
    record_count   INT         NOT NULL DEFAULT 0,
    verdict_count  INT         NOT NULL DEFAULT 0,
    prev_hash      CHAR(64)    NOT NULL,
    content_hash   CHAR(64)    NOT NULL,
    sealed_at      DATETIME(6) NOT NULL,
    pruned_at      DATETIME(6) NULL,
    pruned_count   INT         NULL,

    KEY ix_ailc_pruned (pruned_at)
);


-- Linking the existing ai_action path to the ledger. Additive and nullable:
-- every existing proposal reads "not linked", which is what it is.
--
-- MySQL 8.4 has no `ADD COLUMN IF NOT EXISTS` and MariaDB does, so neither
-- plain form is re-runnable on both. The prepared statement below is — it asks
-- information_schema and no-ops when the column is already there. Six lines to
-- keep the file replayable on a half-migrated box, which is the state an
-- operator is in exactly when they most need to re-run it.
SET @col := (SELECT COUNT(*) FROM information_schema.columns
             WHERE table_schema = DATABASE()
               AND table_name   = 'ai_action_proposals'
               AND column_name  = 'ledger_record_id');
SET @ddl := IF(@col = 0,
    'ALTER TABLE ai_action_proposals ADD COLUMN ledger_record_id CHAR(36) NULL',
    'DO 0');
PREPARE _add_ledger_col FROM @ddl;
EXECUTE _add_ledger_col;
DEALLOCATE PREPARE _add_ledger_col;
