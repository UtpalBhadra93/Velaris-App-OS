-- F-02 — explainable cost per decision.
--
-- Design: docs/Future/Done/AI-ERA-ROADMAP.md §F-02. Velaris already prices a case's
-- HUMAN time: `rate_cards` (HxReplay P4) holds an hourly rate per tenant and
-- role, and `case_event_log.duration_seconds` is recorded per activity, so
-- labour cost needs no new capture. What it cannot price is the AI half — and
-- "explainable" is the operative word: a single number per case is a bill
-- nobody can argue with, which is the opposite of what an operator asking
-- "why did this case cost that" needs.
--
-- So costs are stored as LINE ITEMS — quantity, unit, unit rate, amount — and
-- a case total is the sum of its lines. That is the whole feature: not a total,
-- a total you can take apart.
--
-- NOTHING HERE CHANGES BEHAVIOUR ON UPGRADE. Two new tables, nothing added to
-- an existing one, and an install that configures no model rate prices nothing
-- and says so rather than reporting zero.

-- ── model rate cards ────────────────────────────────────────────────────────
--
-- Per MILLION tokens, matching how every provider publishes its prices and how
-- `llmops.MODEL_RATES` already estimates. That platform dict stays where it is
-- — it prices the `llm_calls` metric, which is a different consumer with
-- different needs — but it is hardcoded and platform-wide, and a tenant on a
-- negotiated contract cannot be priced from it.
--
-- `tenant_id IS NULL` means the PLATFORM default, inherited by any tenant with
-- no row of its own.
CREATE TABLE IF NOT EXISTS model_rate_cards (
    id              UUID          PRIMARY KEY,
    -- Cascades: a rate card is commercially sensitive tenant data and belongs
    -- in the tenant's crypto-shred scope. Note the neighbouring `rate_cards`
    -- keys on a tenant SLUG (VARCHAR); this one keys on the id, because it is
    -- read beside the ledger, which cascades on the same column.
    tenant_id       UUID          NULL REFERENCES tenants(id) ON DELETE CASCADE,

    -- The NULL-uniqueness fix, carried over from migration 177 rather than
    -- rediscovered: `UNIQUE (tenant_id, model_id)` does NOT constrain platform
    -- rows, because NULL <> NULL in SQL, so unlimited duplicate platform rates
    -- would be permitted — the scope where a duplicate does the most damage,
    -- since every tenant inherits it. `tenant_key` collapses NULL to the
    -- literal 'platform' so one ordinary UNIQUE covers both scopes identically
    -- on all three engines. Written by the application; the CHECK keeps it
    -- honest without using a function, which MariaDB refuses in a generated
    -- column.
    tenant_key      VARCHAR(36)   NOT NULL,

    -- `provider/model`, the same string the ledger records — so a price and a
    -- decision join without a mapping table nobody maintains.
    model_id        VARCHAR(255)  NOT NULL,
    input_per_mtok  NUMERIC(14,6) NOT NULL DEFAULT 0,
    output_per_mtok NUMERIC(14,6) NOT NULL DEFAULT 0,
    currency        VARCHAR(8)    NOT NULL DEFAULT 'USD',
    created_by      UUID          NULL,
    created_at      TIMESTAMPTZ   NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ   NOT NULL DEFAULT NOW(),

    CONSTRAINT ck_model_rate_tenant_key CHECK (
        (tenant_id IS NULL     AND tenant_key = 'platform') OR
        (tenant_id IS NOT NULL AND tenant_key <> 'platform')
    ),
    CONSTRAINT ux_model_rate_scope_model UNIQUE (tenant_key, model_id)
);

CREATE INDEX IF NOT EXISTS ix_model_rate_model ON model_rate_cards (model_id);


-- ── decision costs ──────────────────────────────────────────────────────────
--
-- One row per LINE ITEM, not per decision. An AI decision priced at a rate with
-- separate input and output prices produces two lines, and that is the point:
-- an operator asking why a case cost what it did gets the arithmetic, not an
-- assertion.
--
-- Append-only in practice and by intent. Re-pricing a decision after a rate
-- changes must write new lines rather than update old ones, or last month's
-- invoice silently changes when this month's contract does.
CREATE TABLE IF NOT EXISTS decision_costs (
    id           UUID          PRIMARY KEY,
    tenant_id    UUID          NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,

    -- The F-01 ledger row this prices, when it prices one. NULL for a human
    -- step, which has no AI decision behind it — the two halves of a case's
    -- cost live in one table because reconciling them is the whole report.
    record_id    UUID          NULL REFERENCES ai_decision_records(id) ON DELETE CASCADE,
    case_id      UUID          NULL,
    step_id      VARCHAR(255)  NULL,

    -- 'ai' today, and only 'ai'. Two kinds are deliberately NOT stored here:
    --
    --   * LABOUR is derived on read from `case_event_log.duration_seconds` and
    --     the tenant's `rate_cards` row. Both already exist and are already the
    --     source of truth, so persisting a copy would create a reconciliation
    --     problem — two answers to one question, diverging the moment a rate
    --     changes — for no gain. AI cost must be stored because the token
    --     counts are ephemeral: measured once, at the chokepoint, and gone.
    --   * COMPUTE, because nothing in this platform emits per-decision compute
    --     duration yet, and a column nothing writes reports zero cost as though
    --     it were a measurement.
    --
    -- The column stays because the distinction is real and a second producer
    -- would need it; it is not a taxonomy waiting to be filled in.
    kind         VARCHAR(16)   NOT NULL,
    model_id     VARCHAR(255)  NULL,

    -- The arithmetic, kept as three fields so the answer can be checked rather
    -- than believed: quantity × unit_rate = amount.
    quantity     NUMERIC(18,6) NOT NULL DEFAULT 0,
    unit         VARCHAR(16)   NOT NULL,          -- mtok_in | mtok_out | hour
    unit_rate    NUMERIC(14,6) NOT NULL DEFAULT 0,
    amount       NUMERIC(18,6) NOT NULL DEFAULT 0,
    currency     VARCHAR(8)    NOT NULL DEFAULT 'USD',

    -- Where the rate came from — 'tenant', 'platform', or 'unpriced'. An
    -- unpriced line is recorded at zero AND SAYS SO. Reporting a zero that
    -- means "we have no rate for this model" as though it meant "this was
    -- free" is how a cost report becomes a lie an operator acts on.
    rate_source  VARCHAR(16)   NOT NULL DEFAULT 'unpriced',
    -- Token counts here are character-length estimates unless a backend reports
    -- real provider usage — none here does today. Carried per row rather than
    -- inferred, so a future backend that reports true counts is a data change,
    -- not a schema change, and old rows stay honest about what they were.
    estimated    BOOLEAN       NOT NULL DEFAULT TRUE,
    priced_at    TIMESTAMPTZ   NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS ix_dcost_case   ON decision_costs (case_id, priced_at);
CREATE INDEX IF NOT EXISTS ix_dcost_record ON decision_costs (record_id);
CREATE INDEX IF NOT EXISTS ix_dcost_tenant ON decision_costs (tenant_id, priced_at);
