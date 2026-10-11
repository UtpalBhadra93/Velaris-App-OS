-- 209 — rep desk: industry action rails (Oct item 5).
--
-- WHY. The rail's seven actions (item 2) are the same for every trade, but a
-- card-dispute desk, a telco port-out desk and a clinic's booking desk word,
-- order and default them differently. A rail config says how; a marketplace
-- pack can ship one (`engage_rails.json`), and a tenant can edit or author its
-- own. A config orders, hides and relabels actions, adds guidance and closed
-- defaults, and may RAISE a tier — never lower one, never add an action.
--
-- `edited_at` marks a pack rail the tenant has changed: a pack upgrade keeps it
-- rather than overwriting it, the same promise the pack upgrade makes for case
-- types.
--
-- WHAT DOES NOT CHANGE. Additive: one new table, read only by the rail route. A
-- tenant with no config sees exactly the rail it saw before.
--
-- RE-RUNNABLE. `IF NOT EXISTS` throughout.
--
-- Copyright (c) 2024-2026 Velaris Contributors
-- SPDX-License-Identifier: BSL-1.1

CREATE TABLE IF NOT EXISTS engage_rail_configs (
    id            UUID PRIMARY KEY,
    tenant_id     UUID         NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    rail_key      VARCHAR(64)  NOT NULL,
    name          VARCHAR(120) NOT NULL,
    industry      VARCHAR(60)  NOT NULL DEFAULT 'general',
    -- pack | tenant
    source        VARCHAR(8)   NOT NULL,
    package_id    VARCHAR(255) NULL,
    pack_version  VARCHAR(32)  NULL,
    config        JSONB        NOT NULL DEFAULT '{}'::jsonb,
    enabled       BOOLEAN      NOT NULL DEFAULT TRUE,
    edited_at     TIMESTAMPTZ  NULL,
    edited_by     VARCHAR(255) NULL,
    created_at    TIMESTAMPTZ  NOT NULL DEFAULT now(),
    updated_at    TIMESTAMPTZ  NOT NULL DEFAULT now(),

    CONSTRAINT uq_erc_key UNIQUE (tenant_id, rail_key),
    CONSTRAINT ck_erc_source CHECK (source IN ('pack', 'tenant'))
);
CREATE INDEX IF NOT EXISTS ix_erc_tenant ON engage_rail_configs (tenant_id, enabled);
