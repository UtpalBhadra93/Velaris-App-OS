-- 205 — moving a case between tenants (docs/Future/CASE-TENANT-TRANSFER-DESIGN.md).
--
-- WHY. `transfer` moves who works a case inside one tenant. This moves the case
-- OUT of one customer's data and INTO another's — the one act that is supposed
-- to cross the boundary September spent closing. So it is not an action on the
-- dispatcher: it is a request, a second person's approval in the source tenant,
-- and the destination's acceptance, and nothing moves until all three.
--
-- THIS TABLE IS ALSO THE SOURCE'S TOMBSTONE. After a completed transfer the
-- source can still answer "what happened to case 000123" from this row — case
-- number, type, destination, who authorised it, why, and a manifest of what
-- moved — without keeping any of the content it gave up.
--
-- ONE OPEN TRANSFER PER CASE, portably: `active_key` holds the case id while the
-- transfer is requested or pending and NULL once it is not (the migration-203
-- pattern; MySQL has no partial index).
--
-- WHAT DOES NOT CHANGE. Additive, one table. Nothing reads it but the new routes,
-- which need a new admin-only privilege. An install taking this update moves no
-- case and sees nothing.
--
-- RE-RUNNABLE. `IF NOT EXISTS` throughout.
--
-- Copyright (c) 2024-2026 Velaris Contributors
-- SPDX-License-Identifier: BSL-1.1

CREATE TABLE IF NOT EXISTS case_tenant_transfers (
    id                    UUID PRIMARY KEY,
    -- No FK: the row outlives the case's presence in the source tenant, and is
    -- the only thing the source keeps.
    case_id               UUID         NOT NULL,
    case_number           VARCHAR(30)  NULL,
    case_type_name        VARCHAR(255) NULL,
    source_tenant_id      UUID         NOT NULL,
    destination_tenant_id UUID         NOT NULL,

    -- requested → pending → completed | refused ; requested|pending → cancelled
    state                 VARCHAR(16)  NOT NULL DEFAULT 'requested',
    active_key            VARCHAR(36)  NULL,

    reason                TEXT         NOT NULL,
    requested_by          VARCHAR(255) NOT NULL,
    requested_at          TIMESTAMPTZ  NOT NULL DEFAULT now(),
    -- The source tenant's second person. Never the requester.
    approved_by           VARCHAR(255) NULL,
    approved_at           TIMESTAMPTZ  NULL,
    -- The destination's answer.
    decided_by            VARCHAR(255) NULL,
    decided_at            TIMESTAMPTZ  NULL,
    decision_note         TEXT         NULL,
    cancelled_by          VARCHAR(255) NULL,

    -- What moved, what was closed, what was severed — written at completion.
    manifest              JSONB        NULL,
    completed_at          TIMESTAMPTZ  NULL,

    CONSTRAINT ck_case_tenant_transfers_state CHECK (
        state IN ('requested', 'pending', 'completed', 'refused', 'cancelled')
    )
);

CREATE UNIQUE INDEX IF NOT EXISTS ux_case_tenant_transfers_active
    ON case_tenant_transfers (active_key);
CREATE INDEX IF NOT EXISTS ix_case_tenant_transfers_source
    ON case_tenant_transfers (source_tenant_id, state, requested_at);
CREATE INDEX IF NOT EXISTS ix_case_tenant_transfers_destination
    ON case_tenant_transfers (destination_tenant_id, state, requested_at);
CREATE INDEX IF NOT EXISTS ix_case_tenant_transfers_case
    ON case_tenant_transfers (case_id);
