-- 202 — a variable namespace belongs to a tenant (residual gap 30).
--
-- WHAT WAS WRONG. `variable_namespaces.name` was globally UNIQUE with no tenant
-- column at all. On a box with more than one tenant, the first tenant to
-- register `crm` owned that name for everybody: the second could not create it,
-- and `namespace_grants` — which hangs off the namespace — could not be
-- tenant-scoped either, for `ai_action`, rules, modules or connectors alike.
-- Scoping `ai_action` alone would have been the misleading half-fix, because
-- the name collision is in the namespace table, not in any one consumer.
--
-- HOW THIS STAYS SAFE ON A LIVE BOX.
--
--  * **Every existing row becomes a PLATFORM row.** `tenant_id` is NULL and
--    `tenant_key` is 'platform'. Resolution reads the caller's tenant first and
--    falls back to the platform row, so every namespace that resolves today
--    resolves tomorrow, for every caller, whether or not it has a tenant.
--  * **The uniqueness rule is RELAXED, never tightened.** `UNIQUE (name)`
--    becomes `UNIQUE (tenant_key, name)`, which admits strictly more rows. No
--    existing row can violate it, and the previous version's code — still
--    running while this applies — reads by name and finds exactly what it found
--    before, because the only rows that exist are platform rows.
--  * **Nothing is renamed or removed.** The old column set is intact; two
--    columns are added.
--
-- WHAT IT DOES NOT DO. It does not move any existing namespace to a tenant.
-- That is a data decision per install — the same shape as the 185 NULL-tenant
-- cases — and an update must not make it on an operator's behalf.
--
-- Copyright (c) 2024-2026 Velaris Contributors
-- SPDX-License-Identifier: BSL-1.1

ALTER TABLE variable_namespaces ADD COLUMN IF NOT EXISTS tenant_id VARCHAR(255) NULL;

-- 'platform' rather than NULL, because NULL <> NULL: a UNIQUE over a nullable
-- tenant would not constrain the platform rows at all, and two platform `crm`
-- namespaces would then decide which one wins by insertion order. Written by
-- the application on both dialects — MariaDB refuses COALESCE in a generated
-- column (error 1901), which migration 177 discovered the expensive way.
ALTER TABLE variable_namespaces
    ADD COLUMN IF NOT EXISTS tenant_key VARCHAR(255) NOT NULL DEFAULT 'platform';

-- The old global rule goes, and the scoped one replaces it. Dropping first is
-- safe in either order here because the new index admits every row the old one
-- did.
ALTER TABLE variable_namespaces DROP CONSTRAINT IF EXISTS variable_namespaces_name_key;

CREATE UNIQUE INDEX IF NOT EXISTS ux_variable_namespaces_tenant_name
    ON variable_namespaces (tenant_key, name);

-- Resolution reads (tenant_key, name) on every variable read and write, which
-- is the hottest path this table has.
CREATE INDEX IF NOT EXISTS idx_variable_namespaces_lookup
    ON variable_namespaces (name, tenant_key);
