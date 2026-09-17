-- HxArbiter — policies, immutable versions, and the namespace registry.
-- MySQL 8.4 / MariaDB 11.8 sibling of migrations/postgresql/177. See that file
-- for why each column exists; only the dialect differences are noted here.
--
-- Dialect notes:
--  * UUID columns are CHAR(36) — the GUID() decorator emits that off PostgreSQL.
--  * MySQL has no partial indexes, so the platform-scope name uniqueness that
--    PostgreSQL gets from `WHERE tenant_id IS NULL` is carried by a generated
--    column instead (see below). It is not optional: without it, duplicate
--    PLATFORM policy names are permitted, which is the scope where a duplicate
--    does the most damage.
--  * No `CREATE INDEX IF NOT EXISTS` on MySQL (MariaDB has it). These files run
--    exactly once — start-velaris.sh records applied files in schema_migrations
--    and never repeats one — so the plain form is safe.
--  * TIMESTAMPTZ becomes DATETIME(6); MySQL rejects a DEFAULT on DATETIME(6)
--    where MariaDB allows it, so every writer supplies the value.

CREATE TABLE IF NOT EXISTS arbiter_namespaces (
    namespace           VARCHAR(64)  PRIMARY KEY,
    default_effect      VARCHAR(10)  NOT NULL,
    max_mode            VARCHAR(20)  NOT NULL DEFAULT 'shadow',
    description         TEXT         NOT NULL,
    sample_allow_rate   DECIMAL(4,3) NOT NULL DEFAULT 1.000,
    sample_default_rate DECIMAL(4,3) NOT NULL DEFAULT 1.000,
    promoted_at         DATETIME(6)  NULL,
    updated_at          DATETIME(6)  NOT NULL
);

INSERT IGNORE INTO arbiter_namespaces
    (namespace, default_effect, max_mode, description, updated_at) VALUES
    ('ai',     'deny',  'shadow', 'AI inference and provider egress. Supersedes HxNexus local_only.', UTC_TIMESTAMP(6)),
    ('egress', 'deny',  'shadow', 'Outbound network calls. Generalises marketplace outbound_domains.', UTC_TIMESTAMP(6)),
    ('export', 'deny',  'shadow', 'Data leaving the platform in any form.', UTC_TIMESTAMP(6)),
    ('vault',  'deny',  'shadow', 'HxVault key operations.', UTC_TIMESTAMP(6)),
    ('case',   'allow', 'shadow', 'Case stage transitions and case-scoped actions.', UTC_TIMESTAMP(6)),
    ('auth',   'allow', 'shadow', 'Session elevation and step-up requirements.', UTC_TIMESTAMP(6));

CREATE TABLE IF NOT EXISTS arbiter_policies (
    id             CHAR(36)     PRIMARY KEY,
    tenant_id      CHAR(36)     NULL,
    name           VARCHAR(255) NOT NULL,
    description    TEXT         NOT NULL,
    namespace      VARCHAR(64)  NOT NULL,
    status         VARCHAR(20)  NOT NULL DEFAULT 'draft',
    active_version INT          NULL,
    origin         VARCHAR(20)  NOT NULL DEFAULT 'user',
    created_by     CHAR(36)     NULL,
    created_at     DATETIME(6)  NOT NULL,
    updated_at     DATETIME(6)  NOT NULL,
    retired_at     DATETIME(6)  NULL,
    -- A NULL tenant_id collapses to the literal 'platform', so one ordinary
    -- UNIQUE constrains platform names among themselves AND tenant names within
    -- their tenant. Without it, NULL <> NULL means a plain
    -- UNIQUE (tenant_id, name) permits unlimited duplicate platform policies.
    --
    -- Written by the application rather than GENERATED: MariaDB 11.8 refuses
    -- COALESCE in a GENERATED ALWAYS AS clause (error 1901) where MySQL 8.4
    -- accepts it — found only by running both, which is the whole reason both
    -- are run. The CHECK keeps the column honest without using any function,
    -- so it behaves identically on every engine.
    tenant_key     VARCHAR(36)  NOT NULL,
    CONSTRAINT ck_arbiter_policies_tenant_key CHECK (
        (tenant_id IS NULL     AND tenant_key = 'platform') OR
        (tenant_id IS NOT NULL AND tenant_key = tenant_id)
    ),
    CONSTRAINT ux_arbiter_policies_scope_name UNIQUE (tenant_key, name)
);

CREATE INDEX ix_arbiter_policies_lookup
    ON arbiter_policies (namespace, status, tenant_id);

CREATE TABLE IF NOT EXISTS arbiter_policy_versions (
    policy_id      CHAR(36)     NOT NULL,
    version        INT          NOT NULL,
    rules          JSON         NOT NULL,
    mode           VARCHAR(20)  NOT NULL DEFAULT 'shadow',
    nl_source      TEXT         NULL,
    nl_readback    TEXT         NULL,
    created_by     CHAR(36)     NULL,
    approved_by    CHAR(36)     NULL,
    approved_at    DATETIME(6)  NULL,
    signature      CHAR(64)     NOT NULL,
    created_at     DATETIME(6)  NOT NULL,
    -- The activation interval. "Prove this rule was active on 3 March" has no
    -- answer without these two columns; see the PostgreSQL sibling.
    activated_at   DATETIME(6)  NULL,
    deactivated_at DATETIME(6)  NULL,
    PRIMARY KEY (policy_id, version)
);

CREATE INDEX ix_arbiter_versions_active_window
    ON arbiter_policy_versions (policy_id, activated_at, deactivated_at);
