-- 204 — rep desk: what a rep decided about a suggested next action (item 12, P6).
-- MySQL 8.4 / MariaDB 11.8 sibling of migrations/postgresql/204. See that file
-- for why; additive, one table, dark unless the rep desk is released AND the
-- tenant is entitled.
--
-- Dialect notes:
--  * UUID → CHAR(36); TIMESTAMPTZ → DATETIME(6) with NO DEFAULT (MySQL 8.4
--    rejects `DEFAULT now()` there — migration 176), so the application writes
--    decided_at.
--  * Indexes are inline KEYs, so the whole file is one re-runnable statement.
--
-- Copyright (c) 2024-2026 Velaris Contributors
-- SPDX-License-Identifier: BSL-1.1

CREATE TABLE IF NOT EXISTS customer_next_action_feedback (
    id             CHAR(36)     PRIMARY KEY,
    tenant_id      CHAR(36)     NOT NULL,
    customer_id    CHAR(36)     NOT NULL,
    interaction_id CHAR(36)     NULL,
    action_key     VARCHAR(64)  NOT NULL,
    verdict        VARCHAR(16)  NOT NULL,
    reason         TEXT         NULL,
    decided_by     VARCHAR(255) NOT NULL,
    decided_at     DATETIME(6)  NOT NULL,

    KEY ix_cnaf_tenant_key (tenant_id, action_key, decided_at),
    KEY ix_cnaf_customer (customer_id, decided_at),
    CONSTRAINT fk_cnaf_tenant FOREIGN KEY (tenant_id)
        REFERENCES tenants(id) ON DELETE CASCADE,
    CONSTRAINT fk_cnaf_customer FOREIGN KEY (customer_id)
        REFERENCES customer_profiles(id) ON DELETE CASCADE,
    CONSTRAINT fk_cnaf_interaction FOREIGN KEY (interaction_id)
        REFERENCES customer_interactions(id) ON DELETE SET NULL,
    CONSTRAINT ck_cnaf_verdict CHECK (verdict IN ('accepted', 'rejected'))
);
