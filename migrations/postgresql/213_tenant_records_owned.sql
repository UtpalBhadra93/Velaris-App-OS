-- 213 — tenant records get an owner (Oct item 8e, part b).
--
-- WHAT WAS WRONG. Eight tables recorded no tenant at all, so no check could
-- know whose a row was: 8d's path guard protects every row that names an
-- owner and could say nothing about these. Artifact branches were readable AND
-- writable across tenants by id; the rest were listed to every tenant's admin.
--
-- DECIDED 2026-10-01 (with the user): these are TENANT RECORDS — work one
-- tenant did — and become tenant-owned. Existing rows are the default tenant's.
-- (Migration tasks and branch reviews follow their parent; they get no column.)
--
-- HOW THIS STAYS SAFE ON A LIVE BOX.
--  * Additive: one nullable column and one index per table. Nothing renamed.
--  * The backfill names the DEFAULT tenant, which on a single-tenant install is
--    every user's tenant (no claim = default), so nobody loses a row. Where no
--    `default` tenant exists the rows stay NULL — platform-shared, as today.
--  * The previous version's code, still running while this applies, writes no
--    tenant: a row it creates in that window is NULL, i.e. visible as before.
--    Re-running this file claims such rows for the default tenant too.
--  * Re-runnable: IF NOT EXISTS everywhere; the UPDATE touches NULLs only.
--
-- Copyright (c) 2024-2026 Velaris Contributors
-- SPDX-License-Identifier: BSL-1.1

ALTER TABLE artifact_branches ADD COLUMN IF NOT EXISTS tenant_id UUID NULL REFERENCES tenants(id);
CREATE INDEX IF NOT EXISTS idx_artifact_branches_tenant ON artifact_branches (tenant_id);
UPDATE artifact_branches SET tenant_id = (SELECT id FROM tenants WHERE slug = 'default')
 WHERE tenant_id IS NULL;

ALTER TABLE component_commits ADD COLUMN IF NOT EXISTS tenant_id UUID NULL REFERENCES tenants(id);
CREATE INDEX IF NOT EXISTS idx_component_commits_tenant ON component_commits (tenant_id);
UPDATE component_commits SET tenant_id = (SELECT id FROM tenants WHERE slug = 'default')
 WHERE tenant_id IS NULL;

ALTER TABLE app_packages ADD COLUMN IF NOT EXISTS tenant_id UUID NULL REFERENCES tenants(id);
CREATE INDEX IF NOT EXISTS idx_app_packages_tenant ON app_packages (tenant_id);
UPDATE app_packages SET tenant_id = (SELECT id FROM tenants WHERE slug = 'default')
 WHERE tenant_id IS NULL;

ALTER TABLE gdpr_requests ADD COLUMN IF NOT EXISTS tenant_id UUID NULL REFERENCES tenants(id);
CREATE INDEX IF NOT EXISTS idx_gdpr_requests_tenant ON gdpr_requests (tenant_id);
UPDATE gdpr_requests SET tenant_id = (SELECT id FROM tenants WHERE slug = 'default')
 WHERE tenant_id IS NULL;

ALTER TABLE notification_logs ADD COLUMN IF NOT EXISTS tenant_id UUID NULL REFERENCES tenants(id);
CREATE INDEX IF NOT EXISTS idx_notification_logs_tenant ON notification_logs (tenant_id);
UPDATE notification_logs SET tenant_id = (SELECT id FROM tenants WHERE slug = 'default')
 WHERE tenant_id IS NULL;

ALTER TABLE import_jobs ADD COLUMN IF NOT EXISTS tenant_id UUID NULL REFERENCES tenants(id);
CREATE INDEX IF NOT EXISTS idx_import_jobs_tenant ON import_jobs (tenant_id);
UPDATE import_jobs SET tenant_id = (SELECT id FROM tenants WHERE slug = 'default')
 WHERE tenant_id IS NULL;

ALTER TABLE migration_scans ADD COLUMN IF NOT EXISTS tenant_id UUID NULL REFERENCES tenants(id);
CREATE INDEX IF NOT EXISTS idx_migration_scans_tenant ON migration_scans (tenant_id);
UPDATE migration_scans SET tenant_id = (SELECT id FROM tenants WHERE slug = 'default')
 WHERE tenant_id IS NULL;

ALTER TABLE migration_projects ADD COLUMN IF NOT EXISTS tenant_id UUID NULL REFERENCES tenants(id);
CREATE INDEX IF NOT EXISTS idx_migration_projects_tenant ON migration_projects (tenant_id);
UPDATE migration_projects SET tenant_id = (SELECT id FROM tenants WHERE slug = 'default')
 WHERE tenant_id IS NULL;

