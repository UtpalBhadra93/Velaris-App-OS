-- F-00 — the agent principal.
-- MySQL 8.4 / MariaDB 11.8 sibling of migrations/postgresql/182. See that file
-- for why each table and column exists; only dialect differences are noted here.
--
-- Dialect notes:
--  * UUID columns are CHAR(36) — what the GUID() decorator emits off PostgreSQL.
--  * TIMESTAMPTZ becomes DATETIME(6) and carries NO DEFAULT: MySQL rejects
--    DEFAULT now() there where MariaDB accepts it, so every writer supplies the
--    value. `_utcnow()` already does.
--  * JSONB becomes JSON, which takes no literal DEFAULT either — the writer
--    always sends the list, never relies on one.
--  * MySQL has no `CREATE INDEX IF NOT EXISTS` (MariaDB does), so every index
--    is declared INLINE as a KEY inside its CREATE TABLE. The neighbouring
--    files use a standalone CREATE INDEX and rely on start-velaris.sh never
--    replaying an applied file, which is true — but it makes those files
--    re-runnable on PostgreSQL and not on MySQL, and a migration you cannot
--    safely re-run by hand is one nobody can recover a half-applied box with.
--    Inline keys are covered by the CREATE TABLE IF NOT EXISTS above them, so
--    this file is idempotent on all three engines.
--  * Foreign keys are kept. They are not decoration here: `ON DELETE CASCADE`
--    from `agents` is what stops a deleted agent leaving live capability rows
--    and live token grants behind, and a token grant that outlives its
--    principal is exactly the failure this table exists to prevent.

CREATE TABLE IF NOT EXISTS agents (
    id             CHAR(36)     PRIMARY KEY,
    tenant_id      CHAR(36)     NOT NULL,
    kind           VARCHAR(20)  NOT NULL,
    slug           VARCHAR(128) NOT NULL,
    display_name   VARCHAR(255) NOT NULL DEFAULT '',
    model_binding  VARCHAR(255) NULL,
    package_id     VARCHAR(255) NULL,
    status         VARCHAR(20)  NOT NULL DEFAULT 'active',
    created_by     CHAR(36)     NULL,
    created_at     DATETIME(6)  NOT NULL,
    updated_at     DATETIME(6)  NOT NULL,

    KEY ix_agents_tenant_status (tenant_id, status),
    KEY ix_agents_package       (package_id),

    CONSTRAINT ck_agents_kind   CHECK (kind IN ('internal', 'marketplace', 'external')),
    CONSTRAINT ck_agents_status CHECK (status IN ('active', 'suspended', 'retired')),
    CONSTRAINT uq_agents_tenant_slug UNIQUE (tenant_id, slug),
    CONSTRAINT fk_agents_tenant FOREIGN KEY (tenant_id)
        REFERENCES tenants (id) ON DELETE CASCADE
);


CREATE TABLE IF NOT EXISTS agent_capabilities (
    id          CHAR(36)     PRIMARY KEY,
    agent_id    CHAR(36)     NOT NULL,
    capability  VARCHAR(128) NOT NULL,
    granted_by  CHAR(36)     NULL,
    granted_at  DATETIME(6)  NOT NULL,
    revoked_at  DATETIME(6)  NULL,
    revoked_by  CHAR(36)     NULL,

    KEY ix_agent_caps_agent (agent_id, revoked_at),

    CONSTRAINT fk_agent_caps_agent FOREIGN KEY (agent_id)
        REFERENCES agents (id) ON DELETE CASCADE
);


CREATE TABLE IF NOT EXISTS agent_token_grants (
    id               CHAR(36)     PRIMARY KEY,
    agent_id         CHAR(36)     NOT NULL,
    tenant_id        CHAR(36)     NOT NULL,
    capabilities     JSON         NOT NULL,
    audience_case_id CHAR(36)     NULL,
    label            VARCHAR(255) NULL,
    issued_by        CHAR(36)     NULL,
    expires_at       DATETIME(6)  NOT NULL,
    revoked          TINYINT(1)   NOT NULL DEFAULT 0,
    revoked_at       DATETIME(6)  NULL,
    revoked_by       CHAR(36)     NULL,
    created_at       DATETIME(6)  NOT NULL,

    KEY ix_agent_grants_agent   (agent_id, revoked),
    KEY ix_agent_grants_expires (expires_at),

    CONSTRAINT fk_agent_grants_agent FOREIGN KEY (agent_id)
        REFERENCES agents (id) ON DELETE CASCADE
);


CREATE TABLE IF NOT EXISTS agent_status_history (
    id          CHAR(36)     PRIMARY KEY,
    agent_id    CHAR(36)     NOT NULL,
    from_status VARCHAR(20)  NULL,
    to_status   VARCHAR(20)  NOT NULL,
    reason      TEXT         NOT NULL,
    changed_by  CHAR(36)     NULL,
    changed_at  DATETIME(6)  NOT NULL,

    KEY ix_agent_status_agent (agent_id, changed_at),

    CONSTRAINT fk_agent_status_agent FOREIGN KEY (agent_id)
        REFERENCES agents (id) ON DELETE CASCADE
);


INSERT IGNORE INTO arbiter_namespaces
    (namespace, default_effect, max_mode, description, updated_at) VALUES
    ('agent', 'deny', 'shadow',
     'Agent principals: what an AI agent may do, and on whose behalf.',
     UTC_TIMESTAMP(6));
