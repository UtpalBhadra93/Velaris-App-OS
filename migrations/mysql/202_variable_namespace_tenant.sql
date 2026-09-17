-- 202 — a variable namespace belongs to a tenant (residual gap 30).
-- MySQL 8.4 / MariaDB 11.8 sibling of migrations/postgresql/202. See that file
-- for why every existing row becomes a PLATFORM row and why relaxing the
-- uniqueness rule is safe against the previous version's code.
--
-- Dialect notes:
--  * `ADD COLUMN` is plain, matching 199, 176, 164 and 162: MySQL 8.4 has no
--    `ADD COLUMN IF NOT EXISTS` (MariaDB does, but one file serves both), so
--    once-only application is the migration runner's ledger.
--  * The old constraint was written `UNIQUE (name)` with no name, so both
--    engines call the index `name`. `DROP INDEX name` is how it goes.
--  * `tenant_key` carries a DEFAULT so the ALTER fills every existing row in
--    one statement; the application writes it explicitly from then on.
--
-- Copyright (c) 2024-2026 Velaris Contributors
-- SPDX-License-Identifier: BSL-1.1

ALTER TABLE variable_namespaces
    ADD COLUMN tenant_id  VARCHAR(255) NULL,
    ADD COLUMN tenant_key VARCHAR(255) NOT NULL DEFAULT 'platform';

ALTER TABLE variable_namespaces DROP INDEX name;

CREATE UNIQUE INDEX ux_variable_namespaces_tenant_name
    ON variable_namespaces (tenant_key, name);

CREATE INDEX idx_variable_namespaces_lookup
    ON variable_namespaces (name, tenant_key);
