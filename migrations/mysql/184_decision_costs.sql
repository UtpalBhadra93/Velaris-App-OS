-- F-02 — explainable cost per decision.
-- MySQL 8.4 / MariaDB 11.8 sibling of migrations/postgresql/184. See that file
-- for why each table and column exists; only dialect differences are noted here.
--
-- Dialect notes:
--  * UUID columns are CHAR(36); TIMESTAMPTZ becomes DATETIME(6) with no DEFAULT
--    (MySQL rejects DEFAULT now() there where MariaDB allows it), so every
--    writer supplies the value.
--  * NUMERIC becomes DECIMAL, which is the same type under another name.
--  * Indexes are declared INLINE as KEYs so the file is re-runnable on all
--    three engines — MySQL has no CREATE INDEX IF NOT EXISTS.
--  * `tenant_key` and its CHECK are carried across verbatim: the constraint
--    uses no function, so it behaves identically on every engine. MariaDB 11.8
--    refuses COALESCE in a GENERATED ALWAYS AS clause (error 1901) where MySQL
--    8.4 accepts it, which is why the application writes the column.

CREATE TABLE IF NOT EXISTS model_rate_cards (
    id              CHAR(36)      PRIMARY KEY,
    tenant_id       CHAR(36)      NULL,
    tenant_key      VARCHAR(36)   NOT NULL,
    model_id        VARCHAR(255)  NOT NULL,
    input_per_mtok  DECIMAL(14,6) NOT NULL DEFAULT 0,
    output_per_mtok DECIMAL(14,6) NOT NULL DEFAULT 0,
    currency        VARCHAR(8)    NOT NULL DEFAULT 'USD',
    created_by      CHAR(36)      NULL,
    created_at      DATETIME(6)   NOT NULL,
    updated_at      DATETIME(6)   NOT NULL,

    KEY ix_model_rate_model (model_id),

    CONSTRAINT ck_model_rate_tenant_key CHECK (
        (tenant_id IS NULL     AND tenant_key =  'platform') OR
        (tenant_id IS NOT NULL AND tenant_key <> 'platform')
    ),
    CONSTRAINT ux_model_rate_scope_model UNIQUE (tenant_key, model_id),
    CONSTRAINT fk_model_rate_tenant FOREIGN KEY (tenant_id)
        REFERENCES tenants (id) ON DELETE CASCADE
);


CREATE TABLE IF NOT EXISTS decision_costs (
    id           CHAR(36)      PRIMARY KEY,
    tenant_id    CHAR(36)      NOT NULL,
    record_id    CHAR(36)      NULL,
    case_id      CHAR(36)      NULL,
    step_id      VARCHAR(255)  NULL,
    kind         VARCHAR(16)   NOT NULL,
    model_id     VARCHAR(255)  NULL,
    quantity     DECIMAL(18,6) NOT NULL DEFAULT 0,
    unit         VARCHAR(16)   NOT NULL,
    unit_rate    DECIMAL(14,6) NOT NULL DEFAULT 0,
    amount       DECIMAL(18,6) NOT NULL DEFAULT 0,
    currency     VARCHAR(8)    NOT NULL DEFAULT 'USD',
    rate_source  VARCHAR(16)   NOT NULL DEFAULT 'unpriced',
    estimated    TINYINT(1)    NOT NULL DEFAULT 1,
    priced_at    DATETIME(6)   NOT NULL,

    KEY ix_dcost_case   (case_id, priced_at),
    KEY ix_dcost_record (record_id),
    KEY ix_dcost_tenant (tenant_id, priced_at),

    CONSTRAINT fk_dcost_tenant FOREIGN KEY (tenant_id)
        REFERENCES tenants (id) ON DELETE CASCADE,
    CONSTRAINT fk_dcost_record FOREIGN KEY (record_id)
        REFERENCES ai_decision_records (id) ON DELETE CASCADE
);
