-- 212 — case types become tenant-owned, defaults are inherited copy-on-write
-- (Oct item 8i).
--
-- WHAT WAS WRONG. `case_types` was UNIQUE (name, version) across the whole
-- install, so two tenants could not both have an "Onboarding 1.0.0", and a
-- tenant that wanted to change a platform default had nowhere to put its
-- version but over the default itself — changing it for every tenant.
--
-- WHAT THIS ADDS.
--  * `tenant_key` — the tenant id as text, or 'platform' for a default (NULL
--    tenant). NULL <> NULL, so a UNIQUE over the nullable `tenant_id` would not
--    constrain the defaults at all; the same device as migration 202.
--  * `forked_from_id` / `forked_at` — a tenant's edited copy of a default
--    records which default it came from and when, so "an update to the default
--    is available" can be shown instead of overwriting the tenant's edits.
--
-- HOW THIS STAYS SAFE ON A LIVE BOX.
--  * Nothing moves. Every NULL-tenant row stays a default every tenant reads;
--    every tenant-owned row stays its tenant's. Moving the 17 legacy defaults
--    to the tenant that uses them is an OPERATOR action
--    (`/case-types/ownership/backfill`), never this migration.
--  * The uniqueness rule is RELAXED, never tightened: (name, version) becomes
--    (tenant_key, name, version), which admits every row the old rule did. The
--    previous version's code, still running while this applies, writes no
--    `tenant_key` and gets 'platform' — the value it would have had for a
--    default, and still unique, because the old code also checks name+version
--    before it inserts.
--  * Re-runnable: every statement is IF NOT EXISTS / IF EXISTS, and the
--    backfill only touches rows still carrying the column default.
--
-- Copyright (c) 2024-2026 Velaris Contributors
-- SPDX-License-Identifier: BSL-1.1

ALTER TABLE case_types
    ADD COLUMN IF NOT EXISTS tenant_key VARCHAR(36) NOT NULL DEFAULT 'platform';
ALTER TABLE case_types ADD COLUMN IF NOT EXISTS forked_from_id UUID NULL;
ALTER TABLE case_types ADD COLUMN IF NOT EXISTS forked_at TIMESTAMPTZ NULL;

UPDATE case_types SET tenant_key = tenant_id::text
 WHERE tenant_id IS NOT NULL AND tenant_key = 'platform';

ALTER TABLE case_types DROP CONSTRAINT IF EXISTS case_types_name_version_key;

CREATE UNIQUE INDEX IF NOT EXISTS ux_case_types_tenant_name_version
    ON case_types (tenant_key, name, version);

-- "Does this tenant have its own copy of that default?" — asked on every list.
CREATE INDEX IF NOT EXISTS idx_case_types_forked_from
    ON case_types (forked_from_id) WHERE forked_from_id IS NOT NULL;
