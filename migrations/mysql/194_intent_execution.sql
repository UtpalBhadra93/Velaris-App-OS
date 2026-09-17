-- F-11 (first slice) — intent-based process execution.
-- MySQL 8.4 / MariaDB 11.8 sibling of migrations/postgresql/194. See that file
-- for why each column exists, and for what is deliberately NOT built.
--
-- Dialect notes:
--  * UUID columns are CHAR(36); TIMESTAMPTZ becomes DATETIME(6) with no DEFAULT.
--  * JSONB becomes JSON, which takes no literal DEFAULT — the writer supplies it.
--  * Indexes are inline KEYs so the file is re-runnable on all three engines.

CREATE TABLE IF NOT EXISTS process_fragments (
    id             CHAR(36)     PRIMARY KEY,
    tenant_id      CHAR(36)     NOT NULL,
    slug           VARCHAR(64)  NOT NULL,
    display_name   VARCHAR(255) NOT NULL,
    description    TEXT         NOT NULL,

    requires       JSON         NOT NULL,
    provides       JSON         NOT NULL,
    data_classes   JSON         NOT NULL,
    capabilities   JSON         NOT NULL,

    stages         JSON         NOT NULL,
    step_count     INT          NOT NULL DEFAULT 0,

    content_sha256 VARCHAR(64)  NOT NULL,
    signature      TEXT         NULL,
    signing_key_id VARCHAR(64)  NULL,

    status         VARCHAR(16)  NOT NULL DEFAULT 'draft',
    conformance    JSON         NULL,
    verified_at    DATETIME(6)  NULL,
    verified_by    VARCHAR(255) NULL,

    created_by     VARCHAR(255) NULL,
    created_at     DATETIME(6)  NOT NULL,
    updated_at     DATETIME(6)  NOT NULL,

    KEY ix_fragment_status (tenant_id, status),

    CONSTRAINT ux_fragment_slug UNIQUE (tenant_id, slug),
    CONSTRAINT fk_fragment_tenant FOREIGN KEY (tenant_id)
        REFERENCES tenants (id) ON DELETE CASCADE
);


CREATE TABLE IF NOT EXISTS intent_plans (
    id               CHAR(36)     PRIMARY KEY,
    tenant_id        CHAR(36)     NOT NULL,
    case_id          CHAR(36)     NULL,

    intent           TEXT         NOT NULL,
    goal_spec        JSON         NOT NULL,
    spec_source      VARCHAR(16)  NOT NULL DEFAULT 'model',

    fragment_slugs   JSON         NOT NULL,
    definition       JSON         NULL,
    capabilities     JSON         NOT NULL,
    data_classes     JSON         NOT NULL,
    step_count       INT          NOT NULL DEFAULT 0,

    plan_sha256      VARCHAR(64)  NULL,

    ladder           JSON         NOT NULL,
    stopped_at       VARCHAR(32)  NULL,
    stopped_reason   TEXT         NULL,
    twin_run_id      CHAR(36)     NULL,

    status           VARCHAR(16)  NOT NULL DEFAULT 'planned',
    confirmed_by     VARCHAR(255) NULL,
    confirmed_at     DATETIME(6)  NULL,
    confirmed_sha256 VARCHAR(64)  NULL,
    executed_at      DATETIME(6)  NULL,

    created_by       VARCHAR(255) NULL,
    created_at       DATETIME(6)  NOT NULL,

    KEY ix_intent_tenant (tenant_id, status),
    KEY ix_intent_case   (case_id, created_at),

    CONSTRAINT fk_intent_tenant FOREIGN KEY (tenant_id)
        REFERENCES tenants (id) ON DELETE CASCADE,
    CONSTRAINT fk_intent_case FOREIGN KEY (case_id)
        REFERENCES case_instances (id) ON DELETE SET NULL
);
