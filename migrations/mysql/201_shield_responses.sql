-- 201 — HxShield gets a response layer (roadmap #25, market gap C2).
-- MySQL 8.4 / MariaDB 11.8 sibling of migrations/postgresql/201. See that file
-- for what was missing, and for the two safety properties this table holds:
-- shadow first, and no destructive response without a person.
--
-- Dialect notes:
--  * UUID → CHAR(36); TIMESTAMPTZ → DATETIME(6) with no DEFAULT, because MySQL
--    8.4 rejects `DEFAULT now()` on DATETIME(6) where MariaDB accepts it. The
--    application writes `created_at`.
--  * BOOLEAN → TINYINT(1); DOUBLE PRECISION → DOUBLE.
--  * Indexes are inline KEYs, so `CREATE TABLE IF NOT EXISTS` is the whole file
--    and re-running it changes nothing on either engine.
--
-- Copyright (c) 2024-2026 Velaris Contributors
-- SPDX-License-Identifier: BSL-1.1

CREATE TABLE IF NOT EXISTS shield_responses (
    id             CHAR(36)     PRIMARY KEY,
    tenant_id      VARCHAR(255) NULL,

    incident_id    CHAR(36)     NULL,

    response_type  VARCHAR(40)  NOT NULL,
    target_kind    VARCHAR(40)  NOT NULL,
    target_id      VARCHAR(255) NOT NULL,

    state          VARCHAR(20)  NOT NULL DEFAULT 'proposed',
    destructive    TINYINT(1)   NOT NULL DEFAULT 0,

    reason         TEXT         NULL,
    patterns       TEXT         NULL,
    score          DOUBLE       NULL,

    approved_by    VARCHAR(255) NULL,
    approved_at    DATETIME(6)  NULL,
    declined_by    VARCHAR(255) NULL,
    declined_at    DATETIME(6)  NULL,
    executed_at    DATETIME(6)  NULL,
    result         TEXT         NULL,

    created_at     DATETIME(6)  NOT NULL,

    KEY idx_shield_responses_state (state, created_at),
    KEY idx_shield_responses_incident (incident_id),
    KEY idx_shield_responses_target (target_kind, target_id, created_at)
);
