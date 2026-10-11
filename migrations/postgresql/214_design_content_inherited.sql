-- 214 — design content: defaults inherited, tenant forks copy-on-write
-- (Oct item 8e, part a).
--
-- DECIDED 2026-10-01 (with the user): forms, rules, data models and business
-- calendars follow case types (8i, migration 212). A row with no tenant is a
-- DEFAULT every tenant reads; a tenant that edits one gets its own fork, which
-- wins for that tenant; a tenant's own rows are invisible to other tenants.
--
-- WHAT THIS ADDS, per table:
--  * `tenant_id` — already on forms, rules and data models on PostgreSQL since
--    migration 007 (IF NOT EXISTS makes that a no-op); new for calendars.
--  * `tenant_key` — the tenant id as text, 'platform' for a default — the
--    UNIQUE target, because NULL <> NULL (as 202 and 212).
--  * `forked_from_id` / `forked_at` — which default a fork came from, and when.
--  * UNIQUE (name, version) → (tenant_key, name, version); calendars UNIQUE
--    (name) → (tenant_key, name).
--
-- HOW THIS STAYS SAFE ON A LIVE BOX.
--  * Nothing moves: a NULL row stays a default, a tenant row stays its
--    tenant's. 007 gave forms and rules created before it to the default
--    tenant; they stay there.
--  * Uniqueness is RELAXED, never tightened. Old code writes no `tenant_key`
--    and gets 'platform'; for a tenant-owned row that is a collision only with
--    a default of the same name + version, which the old rule forbade anyway.
--  * Re-runnable: IF [NOT] EXISTS throughout; backfill touches 'platform'
--    rows that have a tenant only.
--
-- Copyright (c) 2024-2026 Velaris Contributors
-- SPDX-License-Identifier: BSL-1.1

ALTER TABLE form_definitions ADD COLUMN IF NOT EXISTS tenant_id UUID NULL REFERENCES tenants(id);
ALTER TABLE form_definitions ADD COLUMN IF NOT EXISTS tenant_key VARCHAR(36) NOT NULL DEFAULT 'platform';
ALTER TABLE form_definitions ADD COLUMN IF NOT EXISTS forked_from_id UUID NULL;
ALTER TABLE form_definitions ADD COLUMN IF NOT EXISTS forked_at TIMESTAMPTZ NULL;
UPDATE form_definitions SET tenant_key = tenant_id::text
 WHERE tenant_id IS NOT NULL AND tenant_key = 'platform';
ALTER TABLE form_definitions DROP CONSTRAINT IF EXISTS form_definitions_name_version_key;
CREATE UNIQUE INDEX IF NOT EXISTS ux_form_definitions_tenant_key ON form_definitions (tenant_key, name, version);
-- (tenant_id is already indexed by migration 007.)
CREATE INDEX IF NOT EXISTS idx_form_definitions_forked_from ON form_definitions (forked_from_id)
    WHERE forked_from_id IS NOT NULL;

ALTER TABLE rule_definitions ADD COLUMN IF NOT EXISTS tenant_id UUID NULL REFERENCES tenants(id);
ALTER TABLE rule_definitions ADD COLUMN IF NOT EXISTS tenant_key VARCHAR(36) NOT NULL DEFAULT 'platform';
ALTER TABLE rule_definitions ADD COLUMN IF NOT EXISTS forked_from_id UUID NULL;
ALTER TABLE rule_definitions ADD COLUMN IF NOT EXISTS forked_at TIMESTAMPTZ NULL;
UPDATE rule_definitions SET tenant_key = tenant_id::text
 WHERE tenant_id IS NOT NULL AND tenant_key = 'platform';
ALTER TABLE rule_definitions DROP CONSTRAINT IF EXISTS rule_definitions_name_version_key;
CREATE UNIQUE INDEX IF NOT EXISTS ux_rule_definitions_tenant_key ON rule_definitions (tenant_key, name, version);
-- (tenant_id is already indexed by migration 007.)
CREATE INDEX IF NOT EXISTS idx_rule_definitions_forked_from ON rule_definitions (forked_from_id)
    WHERE forked_from_id IS NOT NULL;

ALTER TABLE data_models ADD COLUMN IF NOT EXISTS tenant_id UUID NULL REFERENCES tenants(id);
ALTER TABLE data_models ADD COLUMN IF NOT EXISTS tenant_key VARCHAR(36) NOT NULL DEFAULT 'platform';
ALTER TABLE data_models ADD COLUMN IF NOT EXISTS forked_from_id UUID NULL;
ALTER TABLE data_models ADD COLUMN IF NOT EXISTS forked_at TIMESTAMPTZ NULL;
UPDATE data_models SET tenant_key = tenant_id::text
 WHERE tenant_id IS NOT NULL AND tenant_key = 'platform';
ALTER TABLE data_models DROP CONSTRAINT IF EXISTS data_models_name_version_key;
CREATE UNIQUE INDEX IF NOT EXISTS ux_data_models_tenant_key ON data_models (tenant_key, name, version);
-- (tenant_id is already indexed by migration 007.)
CREATE INDEX IF NOT EXISTS idx_data_models_forked_from ON data_models (forked_from_id)
    WHERE forked_from_id IS NOT NULL;

ALTER TABLE business_calendars ADD COLUMN IF NOT EXISTS tenant_id UUID NULL REFERENCES tenants(id);
ALTER TABLE business_calendars ADD COLUMN IF NOT EXISTS tenant_key VARCHAR(36) NOT NULL DEFAULT 'platform';
ALTER TABLE business_calendars ADD COLUMN IF NOT EXISTS forked_from_id UUID NULL;
ALTER TABLE business_calendars ADD COLUMN IF NOT EXISTS forked_at TIMESTAMPTZ NULL;
UPDATE business_calendars SET tenant_key = tenant_id::text
 WHERE tenant_id IS NOT NULL AND tenant_key = 'platform';
ALTER TABLE business_calendars DROP CONSTRAINT IF EXISTS business_calendars_name_key;
CREATE UNIQUE INDEX IF NOT EXISTS ux_business_calendars_tenant_key ON business_calendars (tenant_key, name);
CREATE INDEX IF NOT EXISTS idx_business_calendars_tenant_id ON business_calendars (tenant_id);
CREATE INDEX IF NOT EXISTS idx_business_calendars_forked_from ON business_calendars (forked_from_id)
    WHERE forked_from_id IS NOT NULL;

