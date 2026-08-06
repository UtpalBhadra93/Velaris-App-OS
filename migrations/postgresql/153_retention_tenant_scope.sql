-- Retention policies become per-tenant.
--
-- `retention_policies.resource_type` carried a bare UNIQUE constraint, so the
-- table could hold exactly one policy per resource type for the entire
-- platform. On a multi-tenant deployment that is not a limitation, it is a
-- correctness problem: retention periods are a contractual and jurisdictional
-- commitment made per customer, and one tenant's 3-year policy silently became
-- every tenant's.
--
-- NULL tenant_id means "platform default" — the policy applies to any tenant
-- with no policy of its own. Existing rows keep NULL, so behaviour on a
-- single-tenant install is unchanged.
--
-- The unique constraint moves to (tenant_id, resource_type). Postgres treats
-- NULLs as distinct in a plain UNIQUE, which would allow several platform
-- defaults for the same resource type — so the default row is pinned by a
-- separate partial index instead.

ALTER TABLE retention_policies
    ADD COLUMN IF NOT EXISTS tenant_id VARCHAR(255) NULL;

ALTER TABLE retention_policies
    DROP CONSTRAINT IF EXISTS retention_policies_resource_type_key;

CREATE UNIQUE INDEX IF NOT EXISTS ux_retention_tenant_resource
    ON retention_policies (tenant_id, resource_type)
    WHERE tenant_id IS NOT NULL;

CREATE UNIQUE INDEX IF NOT EXISTS ux_retention_default_resource
    ON retention_policies (resource_type)
    WHERE tenant_id IS NULL;

CREATE INDEX IF NOT EXISTS ix_retention_tenant
    ON retention_policies (tenant_id);
