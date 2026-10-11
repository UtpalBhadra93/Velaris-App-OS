-- 213 — tenant records get an owner (Oct item 8e, part b).
-- MySQL 8.4 / MariaDB 11.8 sibling of migrations/postgresql/213. See that file
-- for why and why it is safe on a live box.
--
-- Dialect notes: plain `ADD COLUMN` (MySQL 8.4 has no IF NOT EXISTS), so
-- once-only application is the runner's ledger; UUID → CHAR(36). No FK to
-- `tenants`, matching the other CHAR(36) tenant columns added since 176.
--
-- Copyright (c) 2024-2026 Velaris Contributors
-- SPDX-License-Identifier: BSL-1.1

ALTER TABLE artifact_branches ADD COLUMN tenant_id CHAR(36) NULL;
CREATE INDEX idx_artifact_branches_tenant ON artifact_branches (tenant_id);
UPDATE artifact_branches SET tenant_id = (SELECT id FROM tenants WHERE slug = 'default')
 WHERE tenant_id IS NULL;

ALTER TABLE component_commits ADD COLUMN tenant_id CHAR(36) NULL;
CREATE INDEX idx_component_commits_tenant ON component_commits (tenant_id);
UPDATE component_commits SET tenant_id = (SELECT id FROM tenants WHERE slug = 'default')
 WHERE tenant_id IS NULL;

ALTER TABLE app_packages ADD COLUMN tenant_id CHAR(36) NULL;
CREATE INDEX idx_app_packages_tenant ON app_packages (tenant_id);
UPDATE app_packages SET tenant_id = (SELECT id FROM tenants WHERE slug = 'default')
 WHERE tenant_id IS NULL;

ALTER TABLE gdpr_requests ADD COLUMN tenant_id CHAR(36) NULL;
CREATE INDEX idx_gdpr_requests_tenant ON gdpr_requests (tenant_id);
UPDATE gdpr_requests SET tenant_id = (SELECT id FROM tenants WHERE slug = 'default')
 WHERE tenant_id IS NULL;

ALTER TABLE notification_logs ADD COLUMN tenant_id CHAR(36) NULL;
CREATE INDEX idx_notification_logs_tenant ON notification_logs (tenant_id);
UPDATE notification_logs SET tenant_id = (SELECT id FROM tenants WHERE slug = 'default')
 WHERE tenant_id IS NULL;

ALTER TABLE import_jobs ADD COLUMN tenant_id CHAR(36) NULL;
CREATE INDEX idx_import_jobs_tenant ON import_jobs (tenant_id);
UPDATE import_jobs SET tenant_id = (SELECT id FROM tenants WHERE slug = 'default')
 WHERE tenant_id IS NULL;

ALTER TABLE migration_scans ADD COLUMN tenant_id CHAR(36) NULL;
CREATE INDEX idx_migration_scans_tenant ON migration_scans (tenant_id);
UPDATE migration_scans SET tenant_id = (SELECT id FROM tenants WHERE slug = 'default')
 WHERE tenant_id IS NULL;

ALTER TABLE migration_projects ADD COLUMN tenant_id CHAR(36) NULL;
CREATE INDEX idx_migration_projects_tenant ON migration_projects (tenant_id);
UPDATE migration_projects SET tenant_id = (SELECT id FROM tenants WHERE slug = 'default')
 WHERE tenant_id IS NULL;

