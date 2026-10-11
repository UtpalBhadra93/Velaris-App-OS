-- 214 — design content: defaults inherited, tenant forks copy-on-write
-- (Oct item 8e, part a). MySQL 8.4 / MariaDB 11.8 sibling of
-- migrations/postgresql/214; see that file for why and why it is live-safe.
--
-- Dialect notes:
--  * Unlike PostgreSQL (migration 007), the MySQL baseline was generated from
--    the ORM, which had no `tenant_id` on these tables — so it is added here
--    for all four.
--  * Plain `ADD COLUMN` (MySQL 8.4 has no IF NOT EXISTS): the runner's ledger
--    applies this once. UUID → CHAR(36); TIMESTAMPTZ → DATETIME(6).
--  * The old UNIQUE was unnamed, so both engines call its index `name`.
--
-- Copyright (c) 2024-2026 Velaris Contributors
-- SPDX-License-Identifier: BSL-1.1

ALTER TABLE form_definitions
    ADD COLUMN tenant_id      CHAR(36)    NULL,
    ADD COLUMN tenant_key     VARCHAR(36) NOT NULL DEFAULT 'platform',
    ADD COLUMN forked_from_id CHAR(36)    NULL,
    ADD COLUMN forked_at      DATETIME(6) NULL;
ALTER TABLE form_definitions DROP INDEX name;
CREATE UNIQUE INDEX ux_form_definitions_tenant_key ON form_definitions (tenant_key, name, version);
CREATE INDEX idx_form_definitions_tenant_id ON form_definitions (tenant_id);
CREATE INDEX idx_form_definitions_forked_from ON form_definitions (forked_from_id);

ALTER TABLE rule_definitions
    ADD COLUMN tenant_id      CHAR(36)    NULL,
    ADD COLUMN tenant_key     VARCHAR(36) NOT NULL DEFAULT 'platform',
    ADD COLUMN forked_from_id CHAR(36)    NULL,
    ADD COLUMN forked_at      DATETIME(6) NULL;
ALTER TABLE rule_definitions DROP INDEX name;
CREATE UNIQUE INDEX ux_rule_definitions_tenant_key ON rule_definitions (tenant_key, name, version);
CREATE INDEX idx_rule_definitions_tenant_id ON rule_definitions (tenant_id);
CREATE INDEX idx_rule_definitions_forked_from ON rule_definitions (forked_from_id);

ALTER TABLE data_models
    ADD COLUMN tenant_id      CHAR(36)    NULL,
    ADD COLUMN tenant_key     VARCHAR(36) NOT NULL DEFAULT 'platform',
    ADD COLUMN forked_from_id CHAR(36)    NULL,
    ADD COLUMN forked_at      DATETIME(6) NULL;
ALTER TABLE data_models DROP INDEX name;
CREATE UNIQUE INDEX ux_data_models_tenant_key ON data_models (tenant_key, name, version);
CREATE INDEX idx_data_models_tenant_id ON data_models (tenant_id);
CREATE INDEX idx_data_models_forked_from ON data_models (forked_from_id);

ALTER TABLE business_calendars
    ADD COLUMN tenant_id      CHAR(36)    NULL,
    ADD COLUMN tenant_key     VARCHAR(36) NOT NULL DEFAULT 'platform',
    ADD COLUMN forked_from_id CHAR(36)    NULL,
    ADD COLUMN forked_at      DATETIME(6) NULL;
ALTER TABLE business_calendars DROP INDEX name;
CREATE UNIQUE INDEX ux_business_calendars_tenant_key ON business_calendars (tenant_key, name);
CREATE INDEX idx_business_calendars_tenant_id ON business_calendars (tenant_id);
CREATE INDEX idx_business_calendars_forked_from ON business_calendars (forked_from_id);

