-- HxLedger — double-entry ledger & reconciliation (§8.4, roadmap #39).
--
-- The platform recorded money movement (payment_requests,
-- payment_disbursements, invoice_records) but never BALANCED it. That is an
-- audit gap rather than a reporting one: without a reconciling ledger there
-- is no mechanical way to show that what was collected equals what was
-- recorded.
--
-- Three structural decisions:
--
-- 1. A TRANSACTION is its own row. Idempotency lives there, not on the lines:
--    a balanced transaction has at least two lines sharing the same source, so
--    a UNIQUE on the entries would reject the second half of every posting.
--    UNIQUE (source_type, source_id, purpose) is what makes a payment webhook
--    that fires twice post exactly once.
--
-- 2. APPEND-ONLY. A mistake is corrected with a reversing transaction
--    (`reverses_id`), never an edit or delete. An edited ledger cannot answer
--    "what did the books say on the 3rd", which is what an auditor asks.
--
-- 3. INTEGER CENTS. BIGINT throughout, matching the existing payment models.
--    Floats accumulate rounding error that reconciliation then reports as a
--    real discrepancy.
--
-- Balance itself (debits = credits) is enforced in `compliance/ledger.post()`
-- at write time — an unbalanced transaction raises and stores nothing, rather
-- than being detected after the books have been relied on.

CREATE TABLE IF NOT EXISTS ledger_accounts (
    id           UUID PRIMARY KEY,
    code         VARCHAR(32)  NOT NULL,
    name         VARCHAR(255) NOT NULL,
    account_type VARCHAR(16)  NOT NULL,
    currency     VARCHAR(10)  NOT NULL DEFAULT 'usd',
    tenant_id    VARCHAR(64),
    created_at   TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_ledger_account_tenant_code UNIQUE (tenant_id, code)
);

CREATE TABLE IF NOT EXISTS ledger_transactions (
    id          UUID PRIMARY KEY,
    description TEXT,
    currency    VARCHAR(10) NOT NULL DEFAULT 'usd',
    source_type VARCHAR(32),
    source_id   UUID,
    purpose     VARCHAR(32),
    case_id     UUID,
    tenant_id   VARCHAR(64),
    reverses_id UUID,
    posted_by   VARCHAR(255),
    posted_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_ledger_txn_source_purpose UNIQUE (source_type, source_id, purpose)
);

CREATE INDEX IF NOT EXISTS ix_ledger_txn_tenant
    ON ledger_transactions (tenant_id, posted_at);

CREATE TABLE IF NOT EXISTS ledger_entries (
    id             UUID PRIMARY KEY,
    transaction_id UUID        NOT NULL,
    account_id     UUID        NOT NULL REFERENCES ledger_accounts(id) ON DELETE RESTRICT,
    direction      VARCHAR(6)  NOT NULL,
    amount_cents   BIGINT      NOT NULL,
    currency       VARCHAR(10) NOT NULL DEFAULT 'usd',
    description    TEXT,
    source_type    VARCHAR(32),
    source_id      UUID,
    purpose        VARCHAR(32),
    case_id        UUID,
    tenant_id      VARCHAR(64),
    posted_by      VARCHAR(255),
    posted_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS ix_ledger_entries_txn     ON ledger_entries (transaction_id);
CREATE INDEX IF NOT EXISTS ix_ledger_entries_account ON ledger_entries (account_id, posted_at);
