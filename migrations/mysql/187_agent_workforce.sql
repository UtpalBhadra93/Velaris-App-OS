-- F-05 (first slice) — agents as workers.
-- MySQL 8.4 / MariaDB 11.8 sibling of migrations/postgresql/187. See that file
-- for why each column exists, and for what is deliberately NOT built.
--
-- Dialect notes:
--  * UUID columns are CHAR(36); TIMESTAMPTZ becomes DATETIME(6) with no DEFAULT
--    (MySQL rejects DEFAULT now() there where MariaDB allows it).
--  * JSONB becomes JSON, which takes no literal DEFAULT — the writer supplies it.
--  * NUMERIC becomes DECIMAL.
--  * Indexes are inline KEYs so the file is re-runnable on all three engines.

CREATE TABLE IF NOT EXISTS agent_workforce (
    id                    CHAR(36)     PRIMARY KEY,
    agent_id              CHAR(36)     NOT NULL,
    tenant_id             CHAR(36)     NOT NULL,
    skills                JSON         NOT NULL,
    max_concurrent        INT          NULL,
    token_budget_per_hour BIGINT       NULL,
    escalation_queue      VARCHAR(255) NOT NULL,
    routing_weight        DECIMAL(5,3) NOT NULL DEFAULT 1.000,
    benched               TINYINT(1)   NOT NULL DEFAULT 0,
    bench_reason          TEXT         NULL,
    benched_at            DATETIME(6)  NULL,
    created_by            CHAR(36)     NULL,
    created_at            DATETIME(6)  NOT NULL,
    updated_at            DATETIME(6)  NOT NULL,

    KEY ix_workforce_tenant (tenant_id, benched),

    CONSTRAINT ux_workforce_agent UNIQUE (agent_id),
    CONSTRAINT fk_workforce_agent FOREIGN KEY (agent_id)
        REFERENCES agents (id) ON DELETE CASCADE,
    CONSTRAINT fk_workforce_tenant FOREIGN KEY (tenant_id)
        REFERENCES tenants (id) ON DELETE CASCADE
);


CREATE TABLE IF NOT EXISTS agent_scorecards (
    id                CHAR(36)      PRIMARY KEY,
    agent_id          CHAR(36)      NOT NULL,
    tenant_id         CHAR(36)      NOT NULL,
    window_start      DATETIME(6)   NOT NULL,
    window_end        DATETIME(6)   NOT NULL,
    decisions         INT           NOT NULL DEFAULT 0,
    reviewed          INT           NOT NULL DEFAULT 0,
    accepted          INT           NOT NULL DEFAULT 0,
    overridden        INT           NOT NULL DEFAULT 0,
    override_rate     DECIMAL(6,4)  NULL,
    cost_total        DECIMAL(18,6) NOT NULL DEFAULT 0,
    cost_per_decision DECIMAL(18,6) NULL,
    currency          VARCHAR(8)    NULL,
    proposed_weight   DECIMAL(5,3)  NULL,
    proposal_status   VARCHAR(20)   NULL,
    proposal_reason   TEXT          NULL,
    decided_by        VARCHAR(255)  NULL,
    decided_at        DATETIME(6)   NULL,
    created_at        DATETIME(6)   NOT NULL,

    KEY ix_scorecard_agent (agent_id, created_at),
    KEY ix_scorecard_open  (tenant_id, proposal_status),

    CONSTRAINT fk_scorecard_agent FOREIGN KEY (agent_id)
        REFERENCES agents (id) ON DELETE CASCADE,
    CONSTRAINT fk_scorecard_tenant FOREIGN KEY (tenant_id)
        REFERENCES tenants (id) ON DELETE CASCADE
);
