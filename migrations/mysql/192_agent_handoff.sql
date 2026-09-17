-- F-09 (first slice) — the agent-to-agent case handoff protocol.
-- MySQL 8.4 / MariaDB 11.8 sibling of migrations/postgresql/192. See that file
-- for why each column exists and for what is deliberately NOT built.
--
-- Dialect notes:
--  * UUID columns are CHAR(36); TIMESTAMPTZ becomes DATETIME(6) with no DEFAULT.
--  * JSONB becomes JSON, which takes no literal DEFAULT — the writer supplies it.
--  * **There is no partial unique index.** PostgreSQL enforces "one open work
--    order per case" with `UNIQUE ... WHERE status IN ('issued','accepted')`;
--    MySQL and MariaDB have no such thing. The application enforces it on every
--    dialect (`service.issue()` refuses when one is open), so the constraint is
--    a belt on PostgreSQL rather than the only guard — a rule enforced ONLY by
--    a constraint one of three backends cannot express is a rule two of them do
--    not have.

CREATE TABLE IF NOT EXISTS handoff_counterparties (
    id            CHAR(36)     PRIMARY KEY,
    tenant_id     CHAR(36)     NOT NULL,
    slug          VARCHAR(64)  NOT NULL,
    display_name  VARCHAR(255) NOT NULL,

    public_key    TEXT         NOT NULL,
    key_id        VARCHAR(64)  NOT NULL,

    status        VARCHAR(16)  NOT NULL DEFAULT 'active',
    max_ceiling   VARCHAR(20)  NOT NULL DEFAULT 'internal',
    notes         TEXT         NULL,

    created_by    VARCHAR(255) NULL,
    created_at    DATETIME(6)  NOT NULL,
    updated_at    DATETIME(6)  NOT NULL,

    KEY ix_handoff_cp_key (key_id),

    CONSTRAINT ux_handoff_cp_slug UNIQUE (tenant_id, slug),
    CONSTRAINT fk_handoff_cp_tenant FOREIGN KEY (tenant_id)
        REFERENCES tenants (id) ON DELETE CASCADE
);


CREATE TABLE IF NOT EXISTS handoff_work_orders (
    id                   CHAR(36)     PRIMARY KEY,
    tenant_id            CHAR(36)     NOT NULL,
    case_id              CHAR(36)     NOT NULL,
    counterparty_id      CHAR(36)     NOT NULL,
    agent_id             CHAR(36)     NULL,

    ceiling              VARCHAR(20)  NOT NULL DEFAULT 'internal',
    allowed_actions      JSON         NOT NULL,
    obligations          JSON         NULL,

    status               VARCHAR(16)  NOT NULL DEFAULT 'issued',
    expires_at           DATETIME(6)  NOT NULL,

    token_grant_id       CHAR(36)     NULL,

    order_signature      TEXT         NULL,
    acceptance_signature TEXT         NULL,
    accepted_at          DATETIME(6)  NULL,
    returned_at          DATETIME(6)  NULL,
    return_summary       TEXT         NULL,
    revoked_at           DATETIME(6)  NULL,
    revoked_reason       TEXT         NULL,

    created_by           VARCHAR(255) NULL,
    created_at           DATETIME(6)  NOT NULL,

    KEY ix_handoff_wo_case   (case_id, created_at),
    KEY ix_handoff_wo_tenant (tenant_id, status),

    CONSTRAINT fk_handoff_wo_tenant FOREIGN KEY (tenant_id)
        REFERENCES tenants (id) ON DELETE CASCADE,
    CONSTRAINT fk_handoff_wo_case FOREIGN KEY (case_id)
        REFERENCES case_instances (id) ON DELETE CASCADE,
    CONSTRAINT fk_handoff_wo_cp FOREIGN KEY (counterparty_id)
        REFERENCES handoff_counterparties (id) ON DELETE CASCADE
);


CREATE TABLE IF NOT EXISTS handoff_actions (
    id               CHAR(36)     PRIMARY KEY,
    work_order_id    CHAR(36)     NOT NULL,
    kind             VARCHAR(16)  NOT NULL,
    decision         VARCHAR(10)  NOT NULL,
    reason           TEXT         NULL,

    projection       JSON         NULL,
    payload_sha256   VARCHAR(64)  NULL,
    proposal_id      CHAR(36)     NULL,
    ledger_record_id CHAR(36)     NULL,

    created_at       DATETIME(6)  NOT NULL,

    KEY ix_handoff_act_order (work_order_id, created_at),

    CONSTRAINT fk_handoff_act_wo FOREIGN KEY (work_order_id)
        REFERENCES handoff_work_orders (id) ON DELETE CASCADE
);
