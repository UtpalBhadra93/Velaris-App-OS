-- 207 — rep desk: a join link for the next rep on a live contact (Oct item 1).
--
-- WHY. A warm transfer moved the RECORD — a new interaction for the receiving
-- rep, carrying the caller's verification tier — but nothing let that rep open
-- it. Rep A now creates a link for a named rep (or a queue); rep B opens it into
-- the same contact and 360. The link is a credential, so only its SHA-256 is
-- stored: single-use, short-lived, bound to a tenant and to a staff identity.
--
-- `customer_interactions.handoff_kind` records how the receiving interaction
-- came to be — `transfer`, `warm` (both reps on the contact) or `cold` (the
-- first rep left when the second joined) — so analytics can say that two reps
-- handled one contact instead of guessing from `transferred_from` alone.
--
-- WHAT DOES NOT CHANGE. Additive: one new table and one NULLABLE column that
-- existing rows leave NULL. Nothing reads either but the new routes and the
-- floor analytics, and the rep desk is dark unless released AND entitled.
--
-- RE-RUNNABLE. `IF NOT EXISTS` throughout.
--
-- Copyright (c) 2024-2026 Velaris Contributors
-- SPDX-License-Identifier: BSL-1.1

CREATE TABLE IF NOT EXISTS customer_interaction_joins (
    id                    UUID PRIMARY KEY,
    tenant_id             UUID         NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    interaction_id        UUID         NOT NULL REFERENCES customer_interactions(id) ON DELETE CASCADE,
    -- sha256 hex of the token; the token itself is shown once and never stored.
    token_hash            VARCHAR(64)  NOT NULL UNIQUE,
    -- warm | cold
    mode                  VARCHAR(8)   NOT NULL,
    for_user_id           VARCHAR(255) NULL,
    for_queue             VARCHAR(128) NULL,
    created_by            VARCHAR(255) NOT NULL,
    created_at            TIMESTAMPTZ  NOT NULL DEFAULT now(),
    expires_at            TIMESTAMPTZ  NOT NULL,
    redeemed_at           TIMESTAMPTZ  NULL,
    redeemed_by           VARCHAR(255) NULL,
    joined_interaction_id UUID         NULL REFERENCES customer_interactions(id) ON DELETE SET NULL,
    revoked_at            TIMESTAMPTZ  NULL,
    revoked_by            VARCHAR(255) NULL,

    CONSTRAINT ck_cij_mode CHECK (mode IN ('warm', 'cold')),
    CONSTRAINT ck_cij_target CHECK (for_user_id IS NOT NULL OR for_queue IS NOT NULL)
);

CREATE INDEX IF NOT EXISTS ix_cij_interaction ON customer_interaction_joins (interaction_id, created_at);
CREATE INDEX IF NOT EXISTS ix_cij_tenant ON customer_interaction_joins (tenant_id, created_at);

ALTER TABLE customer_interactions
    ADD COLUMN IF NOT EXISTS handoff_kind VARCHAR(8) NULL;
