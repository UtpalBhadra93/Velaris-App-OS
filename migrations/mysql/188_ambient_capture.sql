-- F-06 (first slice) — ambient case capture.
-- MySQL 8.4 / MariaDB 11.8 sibling of migrations/postgresql/188. See that file
-- for why each column exists, and for what is deliberately NOT built.
--
-- Dialect notes:
--  * UUID columns are CHAR(36); TIMESTAMPTZ becomes DATETIME(6) with no DEFAULT
--    (MySQL rejects DEFAULT now() there where MariaDB allows it).
--  * JSONB becomes JSON, which takes no literal DEFAULT — the writer supplies it.
--  * NUMERIC becomes DECIMAL.
--  * Indexes are inline KEYs so the file is re-runnable on all three engines.

CREATE TABLE IF NOT EXISTS capture_runs (
    id             CHAR(36)     PRIMARY KEY,
    tenant_id      CHAR(36)     NOT NULL,
    case_id        CHAR(36)     NOT NULL,
    source_kind    VARCHAR(20)  NOT NULL,
    source_ref     VARCHAR(255) NOT NULL,
    source_sha256  VARCHAR(64)  NOT NULL,
    source_locator VARCHAR(255) NULL,
    status         VARCHAR(20)  NOT NULL DEFAULT 'running',
    refusal_reason VARCHAR(64)  NULL,
    error          TEXT         NULL,
    provider       VARCHAR(64)  NULL,
    model          VARCHAR(128) NULL,
    prompt_sha256  VARCHAR(64)  NULL,
    attempts       INT          NOT NULL DEFAULT 1,
    proposed_count INT          NOT NULL DEFAULT 0,
    dropped_count  INT          NOT NULL DEFAULT 0,
    requested_by   VARCHAR(255) NOT NULL,
    created_at     DATETIME(6)  NOT NULL,
    completed_at   DATETIME(6)  NULL,

    KEY ix_capture_runs_case   (case_id, created_at),
    KEY ix_capture_runs_source (source_kind, source_ref),

    CONSTRAINT fk_capture_run_tenant FOREIGN KEY (tenant_id)
        REFERENCES tenants (id) ON DELETE CASCADE,
    CONSTRAINT fk_capture_run_case FOREIGN KEY (case_id)
        REFERENCES case_instances (id) ON DELETE CASCADE
);


CREATE TABLE IF NOT EXISTS capture_proposals (
    id               CHAR(36)     PRIMARY KEY,
    run_id           CHAR(36)     NOT NULL,
    tenant_id        CHAR(36)     NOT NULL,
    case_id          CHAR(36)     NOT NULL,
    kind             VARCHAR(20)  NOT NULL,
    target           VARCHAR(255) NULL,
    current_value    JSON         NULL,
    proposed_value   JSON         NULL,
    confidence       DECIMAL(4,3) NULL,
    snippet          TEXT         NULL,
    locator          VARCHAR(255) NULL,
    status           VARCHAR(20)  NOT NULL DEFAULT 'pending',
    applied_value    JSON         NULL,
    decided_by       VARCHAR(255) NULL,
    decided_at       DATETIME(6)  NULL,
    ledger_record_id CHAR(36)     NULL,
    created_at       DATETIME(6)  NOT NULL,

    KEY ix_capture_prop_case (case_id, status),
    KEY ix_capture_prop_run  (run_id),

    CONSTRAINT fk_capture_prop_run FOREIGN KEY (run_id)
        REFERENCES capture_runs (id) ON DELETE CASCADE,
    CONSTRAINT fk_capture_prop_tenant FOREIGN KEY (tenant_id)
        REFERENCES tenants (id) ON DELETE CASCADE,
    CONSTRAINT fk_capture_prop_case FOREIGN KEY (case_id)
        REFERENCES case_instances (id) ON DELETE CASCADE
);
