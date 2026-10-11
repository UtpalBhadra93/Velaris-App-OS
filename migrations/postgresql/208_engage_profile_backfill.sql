-- 208 — rep desk: profile backfill that PROPOSES (Oct item 3).
--
-- WHY. The identity tables start empty on every install, by design, so a rep's
-- first call about a long-standing customer finds nobody. The history is already
-- in the database — portal customers, case submitter emails, the numbers that
-- wrote in on those cases. An operator runs a scan; the scan writes PROPOSALS
-- here; a person accepts or rejects each one. Nothing creates or merges a
-- profile on its own (design §11), and a number with no other evidence proposes
-- nothing — that would manufacture a person out of a wrong number.
--
-- A proposal has a stable `fingerprint`, so a re-run refreshes what is still
-- pending, never re-proposes what an operator rejected, and marks superseded
-- what the data no longer supports.
--
-- WHAT DOES NOT CHANGE. Additive: two new tables, read by nothing but the new
-- routes. No scan runs unless an operator starts one, and the rep desk is dark
-- unless released AND entitled.
--
-- RE-RUNNABLE. `IF NOT EXISTS` throughout.
--
-- Copyright (c) 2024-2026 Velaris Contributors
-- SPDX-License-Identifier: BSL-1.1

CREATE TABLE IF NOT EXISTS customer_backfill_runs (
    id           UUID PRIMARY KEY,
    tenant_id    UUID         NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    started_by   VARCHAR(255) NOT NULL,
    started_at   TIMESTAMPTZ  NOT NULL DEFAULT now(),
    finished_at  TIMESTAMPTZ  NULL,
    -- what was read, what was proposed, what was skipped and why
    counts       JSONB        NOT NULL DEFAULT '{}'::jsonb
);
CREATE INDEX IF NOT EXISTS ix_cbr_tenant ON customer_backfill_runs (tenant_id, started_at);

CREATE TABLE IF NOT EXISTS customer_backfill_proposals (
    id            UUID PRIMARY KEY,
    tenant_id     UUID         NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    run_id        UUID         NULL REFERENCES customer_backfill_runs(id) ON DELETE SET NULL,
    -- create_profile | enrich_profile | merge_profiles
    kind          VARCHAR(16)  NOT NULL,
    fingerprint   VARCHAR(64)  NOT NULL,
    payload       JSONB        NOT NULL DEFAULT '{}'::jsonb,
    evidence      JSONB        NOT NULL DEFAULT '{}'::jsonb,
    -- proposed | accepted | rejected | superseded
    status        VARCHAR(16)  NOT NULL DEFAULT 'proposed',
    created_at    TIMESTAMPTZ  NOT NULL DEFAULT now(),
    updated_at    TIMESTAMPTZ  NOT NULL DEFAULT now(),
    decided_by    VARCHAR(255) NULL,
    decided_at    TIMESTAMPTZ  NULL,
    reason        TEXT         NULL,
    result        JSONB        NULL,

    CONSTRAINT uq_cbp_fingerprint UNIQUE (tenant_id, fingerprint),
    CONSTRAINT ck_cbp_kind CHECK (kind IN ('create_profile', 'enrich_profile', 'merge_profiles')),
    CONSTRAINT ck_cbp_status CHECK (status IN ('proposed', 'accepted', 'rejected', 'superseded'))
);
CREATE INDEX IF NOT EXISTS ix_cbp_tenant_status ON customer_backfill_proposals (tenant_id, status, kind);
