-- HxArbiter — the decision log, and the chain that makes it evidence.
-- MySQL 8.4 / MariaDB 11.8 sibling of migrations/postgresql/178.
--
-- Dialect notes:
--  * `evaluated_at DATETIME(6)` carries NO DEFAULT: MySQL rejects DEFAULT
--    now() on a DATETIME(6) where MariaDB accepts it, and running both is the
--    only reason that was ever found. Every writer supplies the value.
--  * MySQL has no partial indexes, so the PostgreSQL sibling's filtered
--    disagreement and unsealed indexes become plain composite ones. They are
--    larger and still answer the same two queries.
--  * JSON columns cannot take a literal DEFAULT in MySQL; the writer always
--    supplies `[]`.

CREATE TABLE IF NOT EXISTS arbiter_decisions (
    id              CHAR(36)     PRIMARY KEY,
    tenant_id       CHAR(36)     NOT NULL,
    namespace       VARCHAR(64)  NOT NULL,
    action          VARCHAR(128) NOT NULL,
    subject_kind    VARCHAR(20)  NOT NULL,
    subject_id      VARCHAR(255) NOT NULL,
    subject         JSON         NOT NULL,
    resource        JSON         NOT NULL,
    context         JSON         NOT NULL,
    effect          VARCHAR(10)  NOT NULL,
    mode            VARCHAR(20)  NOT NULL,
    enforced        TINYINT(1)   NOT NULL,
    default_applied TINYINT(1)   NOT NULL,
    matched         JSON         NOT NULL,
    obligations     JSON         NOT NULL,
    legacy_effect   VARCHAR(10)  NULL,
    policy_set_hash CHAR(64)     NULL,
    error           VARCHAR(64)  NULL,
    request_id      VARCHAR(64)  NULL,
    -- The authenticated caller that asserted `subject`. See the
    -- PostgreSQL sibling: a subject nobody signed for is unattributable.
    claimed_by      VARCHAR(255) NULL,
    simulated       TINYINT(1)   NOT NULL DEFAULT 0,
    latency_us      INT          NOT NULL DEFAULT 0,
    chain_sequence  INT          NULL,
    evaluated_at    DATETIME(6)  NOT NULL
);

CREATE INDEX ix_arb_dec_tenant_ns_time
    ON arbiter_decisions (tenant_id, namespace, evaluated_at);
CREATE INDEX ix_arb_dec_subject
    ON arbiter_decisions (tenant_id, subject_id, evaluated_at);
-- No partial index here: (legacy_effect, ...) leads with the column the parity
-- report filters on, so the rows with a legacy answer sit together and the
-- NULLs — the overwhelming majority once parity is done — are skipped.
CREATE INDEX ix_arb_dec_disagreement
    ON arbiter_decisions (legacy_effect, tenant_id, namespace, evaluated_at);
CREATE INDEX ix_arb_dec_unsealed
    ON arbiter_decisions (chain_sequence, evaluated_at);

CREATE TABLE IF NOT EXISTS arbiter_decision_chain (
    id             CHAR(36)    PRIMARY KEY,
    sequence       INT         NOT NULL UNIQUE,
    window_start   DATETIME(6) NOT NULL,
    window_end     DATETIME(6) NOT NULL,
    decision_count INT         NOT NULL,
    prev_hash      CHAR(64)    NOT NULL,
    content_hash   CHAR(64)    NOT NULL,
    sealed_at      DATETIME(6) NOT NULL
);

CREATE INDEX ix_arb_chain_window
    ON arbiter_decision_chain (window_start, window_end);
