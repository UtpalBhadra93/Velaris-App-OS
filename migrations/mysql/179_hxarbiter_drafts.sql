-- HxArbiter ARB-F — natural-language policy drafts.
-- MySQL 8.4 / MariaDB 11.8 sibling of migrations/postgresql/179.
-- See that file for why each column exists.
--
-- Dialect notes: DATETIME(6) carries no DEFAULT (MySQL rejects DEFAULT now()
-- there where MariaDB accepts it); JSON columns take no literal DEFAULT.
CREATE TABLE IF NOT EXISTS arbiter_drafts (
    id              CHAR(36)     PRIMARY KEY,
    policy_id       CHAR(36)     NULL,
    tenant_id       CHAR(36)     NULL,
    namespace       VARCHAR(64)  NOT NULL,
    intent          TEXT         NOT NULL,
    generated_rules JSON         NULL,
    compile_report  JSON         NULL,
    analysis_report JSON         NULL,
    readback        TEXT         NULL,
    readback_source VARCHAR(20)  NULL,
    test_seeds      JSON         NULL,
    whatif_report   JSON         NULL,
    -- Provenance: which model, running where, under which prompt.
    model_provider  VARCHAR(64)  NULL,
    model_name      VARCHAR(128) NULL,
    prompt_version  VARCHAR(32)  NULL,
    attempts        INT          NOT NULL DEFAULT 1,
    corpus_refs     JSON         NULL,
    state           VARCHAR(32)  NOT NULL DEFAULT 'drafted',
    created_by      CHAR(36)     NULL,
    created_at      DATETIME(6)  NOT NULL,
    updated_at      DATETIME(6)  NOT NULL
);

CREATE INDEX ix_arbiter_drafts_tenant_state
    ON arbiter_drafts (tenant_id, state, created_at);
