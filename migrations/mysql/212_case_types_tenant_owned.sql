-- 212 — case types become tenant-owned, defaults inherited copy-on-write
-- (Oct item 8i). MySQL 8.4 / MariaDB 11.8 sibling of
-- migrations/postgresql/212. See that file for why nothing moves and why
-- relaxing the uniqueness rule is safe against the previous version's code.
--
-- Dialect notes (as 202):
--  * `ADD COLUMN` is plain: MySQL 8.4 has no `ADD COLUMN IF NOT EXISTS`, so
--    once-only application is the migration runner's ledger.
--  * The old constraint was written `UNIQUE (name, version)` with no name, so
--    both engines call the index `name`. `DROP INDEX name` is how it goes.
--  * UUID → CHAR(36); TIMESTAMPTZ → DATETIME(6), written by the application.
--  * No partial index on MySQL; a plain one on `forked_from_id`.
--
-- Copyright (c) 2024-2026 Velaris Contributors
-- SPDX-License-Identifier: BSL-1.1

ALTER TABLE case_types
    ADD COLUMN tenant_key     VARCHAR(36) NOT NULL DEFAULT 'platform',
    ADD COLUMN forked_from_id CHAR(36)    NULL,
    ADD COLUMN forked_at      DATETIME(6) NULL;

UPDATE case_types SET tenant_key = tenant_id
 WHERE tenant_id IS NOT NULL AND tenant_key = 'platform';

ALTER TABLE case_types DROP INDEX name;

CREATE UNIQUE INDEX ux_case_types_tenant_name_version
    ON case_types (tenant_key, name, version);

CREATE INDEX idx_case_types_forked_from ON case_types (forked_from_id);
