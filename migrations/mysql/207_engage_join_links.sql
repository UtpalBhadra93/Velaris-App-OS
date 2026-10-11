-- 207 — rep desk: a join link for the next rep on a live contact (Oct item 1).
-- MySQL 8.4 / MariaDB 11.8 sibling of migrations/postgresql/207. See that file
-- for why; additive — one table and one nullable column.
--
-- Dialect notes:
--  * UUID → CHAR(36); TIMESTAMPTZ → DATETIME(6) with NO DEFAULT (MySQL 8.4
--    rejects `DEFAULT now()` there — migration 176), so the application writes
--    created_at.
--  * `ADD COLUMN` is plain, matching 206/202/199: MySQL 8.4 has no
--    `ADD COLUMN IF NOT EXISTS`, so once-only application is the runner's ledger.
--
-- Copyright (c) 2024-2026 Velaris Contributors
-- SPDX-License-Identifier: BSL-1.1

CREATE TABLE IF NOT EXISTS customer_interaction_joins (
    id                    CHAR(36)     PRIMARY KEY,
    tenant_id             CHAR(36)     NOT NULL,
    interaction_id        CHAR(36)     NOT NULL,
    token_hash            VARCHAR(64)  NOT NULL,
    mode                  VARCHAR(8)   NOT NULL,
    for_user_id           VARCHAR(255) NULL,
    for_queue             VARCHAR(128) NULL,
    created_by            VARCHAR(255) NOT NULL,
    created_at            DATETIME(6)  NOT NULL,
    expires_at            DATETIME(6)  NOT NULL,
    redeemed_at           DATETIME(6)  NULL,
    redeemed_by           VARCHAR(255) NULL,
    joined_interaction_id CHAR(36)     NULL,
    revoked_at            DATETIME(6)  NULL,
    revoked_by            VARCHAR(255) NULL,

    UNIQUE KEY uq_cij_token (token_hash),
    KEY ix_cij_interaction (interaction_id, created_at),
    KEY ix_cij_tenant (tenant_id, created_at),
    CONSTRAINT fk_cij_tenant FOREIGN KEY (tenant_id)
        REFERENCES tenants(id) ON DELETE CASCADE,
    CONSTRAINT fk_cij_interaction FOREIGN KEY (interaction_id)
        REFERENCES customer_interactions(id) ON DELETE CASCADE,
    CONSTRAINT fk_cij_joined FOREIGN KEY (joined_interaction_id)
        REFERENCES customer_interactions(id) ON DELETE SET NULL,
    CONSTRAINT ck_cij_mode CHECK (mode IN ('warm', 'cold')),
    CONSTRAINT ck_cij_target CHECK (for_user_id IS NOT NULL OR for_queue IS NOT NULL)
);

ALTER TABLE customer_interactions
    ADD COLUMN handoff_kind VARCHAR(8) NULL;
