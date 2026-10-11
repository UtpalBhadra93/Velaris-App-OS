-- 209 — rep desk: industry action rails (Oct item 5).
-- MySQL 8.4 / MariaDB 11.8 sibling of migrations/postgresql/209. See that file
-- for why; additive — one new table.
--
-- Dialect notes: UUID → CHAR(36); JSONB → JSON with no default and
-- TIMESTAMPTZ → DATETIME(6) with no default (migration 176) — the application
-- writes config, created_at and updated_at.
--
-- Copyright (c) 2024-2026 Velaris Contributors
-- SPDX-License-Identifier: BSL-1.1

CREATE TABLE IF NOT EXISTS engage_rail_configs (
    id            CHAR(36)     PRIMARY KEY,
    tenant_id     CHAR(36)     NOT NULL,
    rail_key      VARCHAR(64)  NOT NULL,
    name          VARCHAR(120) NOT NULL,
    industry      VARCHAR(60)  NOT NULL DEFAULT 'general',
    source        VARCHAR(8)   NOT NULL,
    package_id    VARCHAR(255) NULL,
    pack_version  VARCHAR(32)  NULL,
    config        JSON         NOT NULL,
    enabled       BOOLEAN      NOT NULL DEFAULT TRUE,
    edited_at     DATETIME(6)  NULL,
    edited_by     VARCHAR(255) NULL,
    created_at    DATETIME(6)  NOT NULL,
    updated_at    DATETIME(6)  NOT NULL,

    UNIQUE KEY uq_erc_key (tenant_id, rail_key),
    KEY ix_erc_tenant (tenant_id, enabled),
    CONSTRAINT fk_erc_tenant FOREIGN KEY (tenant_id)
        REFERENCES tenants(id) ON DELETE CASCADE,
    CONSTRAINT ck_erc_source CHECK (source IN ('pack', 'tenant'))
);
