-- HxSentinel P1 — ground truth + signal store.
--
-- The engine recommends, never decides (EU-AI-Act high-risk stance, same as
-- Video-KYC). P1 ships NO recommendations at all: it establishes the labelled
-- history that P2/P3 learn from, because a risk engine with no confirmed
-- outcomes has nothing to be right or wrong about.
--
-- Three tables:
--   case_outcome_labels — per-tenant taxonomy EXTENSIONS only
--   case_outcomes       — append-only labels on cases (the ground truth)
--   case_risk_signals   — extracted, matchable features per case
--
-- The CORE taxonomy (fraud / identity_theft / criminal_transaction /
-- chargeback / abuse / clean) is a CODE constant in case_service.sentinel.
-- taxonomy, not seeded rows — cross-tenant learning depends on a core label
-- meaning the same thing everywhere, and a code constant cannot drift between
-- the PostgreSQL migration track and the generated MySQL baseline the way a
-- seeded row can.

-- ── Taxonomy extensions ─────────────────────────────────────────────────────
-- Tenant-defined labels only. A tenant may extend the vocabulary but cannot
-- redefine a core key (enforced in code) — otherwise 'fraud' could quietly
-- mean something different per tenant.
CREATE TABLE IF NOT EXISTS case_outcome_labels (
    id          UUID PRIMARY KEY,
    tenant_id   VARCHAR(255) NOT NULL,
    key         VARCHAR(64)  NOT NULL,        -- machine name
    display     VARCHAR(128) NOT NULL,
    description TEXT,
    is_negative BOOLEAN      NOT NULL DEFAULT TRUE,   -- FALSE = a "good" outcome
    active      BOOLEAN      NOT NULL DEFAULT TRUE,
    created_at  TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_outcome_labels_tenant_key
    ON case_outcome_labels (tenant_id, key);

-- ── Ground truth ────────────────────────────────────────────────────────────
-- APPEND-ONLY. Re-labelling (including retroactive labelling of historical
-- cases) inserts a new row; the current label is the most recent one. The
-- label history is itself evidence — who believed what, when — and matches the
-- audit-chain discipline used elsewhere. Never UPDATE these rows.
CREATE TABLE IF NOT EXISTS case_outcomes (
    id           UUID PRIMARY KEY,
    tenant_id    VARCHAR(255),
    case_id      UUID         NOT NULL,
    outcome      VARCHAR(64)  NOT NULL,       -- core key, or a tenant label key
    note         TEXT,
    source       VARCHAR(20)  NOT NULL DEFAULT 'manual',   -- manual | bulk | import
    labelled_by  VARCHAR(255) NOT NULL,
    created_at   TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);
-- The hot read is "current label for this case": newest row per case.
CREATE INDEX IF NOT EXISTS ix_case_outcomes_case
    ON case_outcomes (case_id, created_at DESC);
CREATE INDEX IF NOT EXISTS ix_case_outcomes_tenant
    ON case_outcomes (tenant_id, outcome);

-- ── Signal store ────────────────────────────────────────────────────────────
-- One row per (case, signal). `signal_key` is what makes two cases match.
--
-- PRIVACY: identity-bearing values (email / phone / address / account) are
-- NEVER stored raw. signal_key holds a keyed BLAKE2b digest computed with the
-- tenant's own DEK, so:
--   * equal values inside a tenant produce equal digests -> matching works;
--   * the table is useless if it leaks -- there is no plaintext to read;
--   * digests cannot be correlated ACROSS tenants (different keys);
--   * shredding a tenant's DEK orphans its signal index -- crypto-shredding
--     that follows the tenant, which is the intended GDPR behaviour.
-- Non-identity signals (document sha256, velocity counters, amount z-scores)
-- carry no personal data and are stored as-is for explainability.
CREATE TABLE IF NOT EXISTS case_risk_signals (
    id          UUID PRIMARY KEY,
    tenant_id   VARCHAR(255),
    case_id     UUID         NOT NULL,
    signal_type VARCHAR(48)  NOT NULL,   -- doc_hash | identity_email | identity_phone | velocity | amount_z
    signal_key  VARCHAR(128) NOT NULL,   -- hashed for identity types, literal otherwise
    hashed      BOOLEAN      NOT NULL DEFAULT FALSE,
    value       JSONB,                   -- explainable detail; never the raw identity value
    computed_at TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);
-- THE lookup: "which other cases share this signal?" — tenant-scoped so a
-- match can never span tenants even if two digests somehow collided.
CREATE INDEX IF NOT EXISTS ix_risk_signals_lookup
    ON case_risk_signals (tenant_id, signal_type, signal_key);
CREATE INDEX IF NOT EXISTS ix_risk_signals_case
    ON case_risk_signals (case_id);
-- Re-extraction must not duplicate rows.
CREATE UNIQUE INDEX IF NOT EXISTS ux_risk_signals_unique
    ON case_risk_signals (case_id, signal_type, signal_key);
