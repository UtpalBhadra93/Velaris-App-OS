-- 215 — legal holds (Oct item 13).
--
-- WHY. The platform had no legal hold. Retention deleted or anonymized a case
-- on its clock, a data subject's erasure went through, a case could be hidden
-- or moved to another tenant — and nothing could say "not this one, it is
-- under dispute". Moving a case between tenants borrowed an open HxSentinel
-- risk hold as the stand-in.
--
-- A case is held while it has an `active` row, and takes one active hold at a
-- time. Rows are
-- never deleted — a released hold is the record that the freeze existed.
--
-- `case_id` is NOT a foreign key on purpose: the record must outlive the case
-- once the hold is released and retention finally removes it. `active_key` is
-- the portable partial unique index (the case id, NULL once released).
--
-- WHAT DOES NOT CHANGE. Additive: one new table. An install with no rows in it
-- behaves exactly as before — nothing is held until an operator places a hold.
--
-- RE-RUNNABLE. `IF NOT EXISTS` throughout.
--
-- Copyright (c) 2024-2026 Velaris Contributors
-- SPDX-License-Identifier: BSL-1.1

CREATE TABLE IF NOT EXISTS legal_holds (
    id             UUID PRIMARY KEY,
    tenant_id      UUID         NULL REFERENCES tenants(id) ON DELETE CASCADE,
    case_id        UUID         NOT NULL,
    case_number    VARCHAR(30)  NULL,
    matter_ref     VARCHAR(120) NOT NULL,
    reason         TEXT         NOT NULL,
    -- active | released
    status         VARCHAR(12)  NOT NULL DEFAULT 'active',
    active_key     VARCHAR(160) NULL,
    placed_by      VARCHAR(255) NOT NULL,
    placed_at      TIMESTAMPTZ  NOT NULL DEFAULT now(),
    released_by    VARCHAR(255) NULL,
    released_at    TIMESTAMPTZ  NULL,
    release_reason TEXT         NULL,

    CONSTRAINT ck_legal_holds_status CHECK (status IN ('active', 'released'))
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_legal_holds_active ON legal_holds (active_key);
CREATE INDEX IF NOT EXISTS ix_legal_holds_case ON legal_holds (case_id, status);
CREATE INDEX IF NOT EXISTS ix_legal_holds_tenant ON legal_holds (tenant_id, status, placed_at);
