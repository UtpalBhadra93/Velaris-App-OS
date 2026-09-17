-- F-03 — counterfactual compliance proofs.
-- MySQL 8.4 / MariaDB 11.8 sibling of migrations/postgresql/185. See that file
-- for why each column exists; only dialect differences are noted here.
--
-- Dialect notes:
--  * UUID columns are CHAR(36); TIMESTAMPTZ becomes DATETIME(6) with no DEFAULT
--    (MySQL rejects DEFAULT now() there where MariaDB allows it).
--  * JSONB becomes JSON, which takes no literal DEFAULT either — the writer
--    always supplies the value.
--  * Indexes are inline KEYs so the file is re-runnable on all three engines.
--  * `sequence` is quoted: it is not reserved in either engine today, but it
--    reads like one and the backticks cost nothing.

CREATE TABLE IF NOT EXISTS compliance_proofs (
    id              CHAR(36)     PRIMARY KEY,
    tenant_id       CHAR(36)     NULL,
    subject_kind    VARCHAR(32)  NOT NULL DEFAULT 'policy_version',
    policy_id       CHAR(36)     NULL,
    policy_version  INT          NULL,
    namespace       VARCHAR(64)  NOT NULL,
    status          VARCHAR(20)  NOT NULL DEFAULT 'complete',
    refusal_reason  TEXT         NULL,
    cohort          JSON         NOT NULL,
    inputs_sha256   CHAR(64)     NOT NULL,
    rules_sha256    CHAR(64)     NOT NULL,
    result          JSON         NOT NULL,
    divergent       JSON         NOT NULL,
    aggregate_only  TINYINT(1)   NOT NULL DEFAULT 0,
    prev_hash       CHAR(64)     NOT NULL,
    content_sha256  CHAR(64)     NOT NULL,
    `sequence`      BIGINT       NOT NULL,
    generated_by    CHAR(36)     NULL,
    created_at      DATETIME(6)  NOT NULL,

    KEY ix_proof_tenant  (tenant_id, created_at),
    KEY ix_proof_policy  (policy_id, policy_version),
    KEY ix_proof_content (content_sha256),

    CONSTRAINT ux_proof_sequence UNIQUE (`sequence`),
    CONSTRAINT fk_proof_tenant FOREIGN KEY (tenant_id)
        REFERENCES tenants (id) ON DELETE CASCADE
);
