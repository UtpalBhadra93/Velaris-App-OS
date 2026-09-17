-- F-01 — the AI decision ledger.
--
-- Design: docs/Future/Done/AI-ERA-ROADMAP.md §F-01. Velaris already records that an
-- AI call happened (`llm_calls`, mig 142: which model, how many tokens, how
-- long) and, for the `ai_action` step type only, what a human did about it
-- (`ai_action_proposals`). Neither is evidence:
--
--   * `llm_calls` is a METRIC. No case, no agent, no input commitment, no
--     verdict — it answers "what is AI costing us", which is a different
--     question from "on what basis was this decision made".
--   * `ai_action_proposals` covers one step type. HxNexus answering a question
--     about a case, and HxSentinel scoring one, produce nothing at all.
--
-- This is the third thing: a per-decision, hash-chained, human-override-
-- inclusive record that an Article 22 dossier can be built from.
--
-- NOTHING HERE CHANGES BEHAVIOUR ON UPGRADE. Three new tables, no column added
-- to anything an existing path reads, no raw prompt stored unless a policy
-- obligation turns it on, and an install whose AI is switched off records
-- nothing.

-- ── the records ─────────────────────────────────────────────────────────────
--
-- IMMUTABLE. Every column here is written once and never updated, which is what
-- lets the chain below hash the row. `chain_sequence` is the single exception
-- and is excluded from the hash for exactly that reason.
--
-- The human verdict is deliberately NOT a column. A verdict arrives minutes or
-- days after the decision, and a mutable field inside a hashed row would make
-- the seal fail the moment somebody reviewed something — reporting an ordinary
-- human action as tampering. Verdicts are their own append-only table below.
CREATE TABLE IF NOT EXISTS ai_decision_records (
    id                  UUID         PRIMARY KEY,
    -- Cascades with the tenant: the ledger is in the crypto-shred scope, and a
    -- deleted tenant's AI history must go with it.
    tenant_id           UUID         NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,

    -- Who decided (F-00). No foreign key on purpose — the LEDGER OUTLIVES THE
    -- AGENT. An agent retired last March must still be nameable in a record
    -- from February, and a FK with any ON DELETE rule either forbids the
    -- deletion or quietly rewrites history. The slug and kind are snapshots
    -- taken at write time, so the row still reads if the registry row is gone.
    agent_id            UUID         NULL,
    agent_slug          VARCHAR(128) NULL,
    agent_kind          VARCHAR(20)  NULL,

    -- What answered, resolved AFTER the call — never the configured model.
    -- `provider_class` is the sovereignty-relevant half: local or external.
    model_id            VARCHAR(255) NOT NULL,
    provider_class      VARCHAR(16)  NOT NULL DEFAULT 'local',

    -- Why the call was made, and what it was about.
    purpose             VARCHAR(64)  NOT NULL,
    case_id             UUID         NULL,
    step_id             VARCHAR(255) NULL,

    -- The input COMMITMENT, not the input. A hash proves "this input produced
    -- this output" without the ledger becoming a second copy of every prompt —
    -- and prompts carry case content, which is the personal data the rest of
    -- the platform works to minimise. It is a plain SHA-256 and therefore a
    -- commitment rather than a secret: someone who can already guess the exact
    -- prompt can confirm the guess. That is the intended property.
    input_sha256        CHAR(64)     NOT NULL,
    -- HxPrivacy classes the input carried, so an auditor can ask "did any
    -- decision on special-category data run externally" without reading one.
    input_data_classes  JSONB        NOT NULL DEFAULT '[]'::jsonb,
    output_sha256       CHAR(64)     NULL,

    -- Raw prompt + output, and the reason this is a column rather than a
    -- setting: it is written ONLY when an HxArbiter policy attaches the
    -- `record_prompt` obligation, which the writer must honour explicitly.
    -- Encrypted under the tenant DEK, so a crypto-shred takes it.
    prompt_sealed       TEXT         NULL,
    output_sealed       TEXT         NULL,

    confidence          NUMERIC(5,4) NULL,
    latency_ms          NUMERIC(10,2) NULL,

    -- The HxArbiter decision that gated this call, when there was one. This is
    -- the join that answers "show me the rule, and prove it was active" for an
    -- AI action specifically — the sentence the whole engine exists for.
    arbiter_decision_id UUID         NULL,

    chain_sequence      BIGINT       NULL,
    recorded_at         TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS ix_aidr_tenant_time ON ai_decision_records (tenant_id, recorded_at DESC);
CREATE INDEX IF NOT EXISTS ix_aidr_case        ON ai_decision_records (case_id, recorded_at DESC);
CREATE INDEX IF NOT EXISTS ix_aidr_agent       ON ai_decision_records (agent_id, recorded_at DESC);
CREATE INDEX IF NOT EXISTS ix_aidr_model       ON ai_decision_records (model_id);
CREATE INDEX IF NOT EXISTS ix_aidr_unsealed    ON ai_decision_records (chain_sequence, recorded_at);


-- ── the verdicts ────────────────────────────────────────────────────────────
--
-- APPEND-ONLY, and separate from the record for the reason given above. A
-- second review of the same decision inserts a second row; the current verdict
-- is the newest one, and the earlier one stays visible. "It was accepted, then
-- three days later somebody reversed that" is precisely the sequence an
-- override-rate report exists to show, and an UPDATE would erase it.
CREATE TABLE IF NOT EXISTS ai_decision_verdicts (
    id          UUID         PRIMARY KEY,
    record_id   UUID         NOT NULL REFERENCES ai_decision_records(id) ON DELETE CASCADE,
    tenant_id   UUID         NOT NULL,
    -- accepted | edited | rejected. There is no 'unreviewed' value: an absent
    -- row IS unreviewed, and storing it would make "nobody has looked at this"
    -- indistinguishable from "somebody looked and recorded nothing".
    verdict     VARCHAR(20)  NOT NULL,
    -- What the human changed, when they edited rather than accepted. Hashes
    -- only by default — the same minimisation as the input above.
    diff_sha256 CHAR(64)     NULL,
    diff_sealed TEXT         NULL,
    reason      TEXT         NOT NULL DEFAULT '',
    decided_by  VARCHAR(255) NULL,
    decided_at  TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    chain_sequence BIGINT    NULL
);

CREATE INDEX IF NOT EXISTS ix_aidv_record   ON ai_decision_verdicts (record_id, decided_at);
CREATE INDEX IF NOT EXISTS ix_aidv_unsealed ON ai_decision_verdicts (chain_sequence, decided_at);


-- ── the chain ───────────────────────────────────────────────────────────────
--
-- The same shape as `arbiter_decision_chain` (migs 178 + 181) rather than a
-- second invention: batched windows, `content_hash = sha256(prev || batch)`,
-- contiguous by construction so a gap means rows were removed, and a tombstone
-- pair so retention and tamper-evidence can coexist. That last part is not
-- theoretical — migration 180 shipped a retention horizon that destroyed the
-- arbiter chain on first use, and the fix is carried here from the start
-- rather than learned again.
--
-- One chain covers BOTH tables above. A verdict is part of the evidence, not a
-- footnote to it, and sealing it separately would let one be rewritten while
-- the other still verified.
CREATE TABLE IF NOT EXISTS ai_ledger_chain (
    id             UUID        PRIMARY KEY,
    sequence       BIGINT      NOT NULL UNIQUE,
    window_start   TIMESTAMPTZ NOT NULL,
    window_end     TIMESTAMPTZ NOT NULL,
    record_count   INTEGER     NOT NULL DEFAULT 0,
    verdict_count  INTEGER     NOT NULL DEFAULT 0,
    prev_hash      CHAR(64)    NOT NULL,
    content_hash   CHAR(64)    NOT NULL,
    sealed_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    -- Tombstone: a batch retention emptied keeps its hash, so the chain still
    -- links and still proves nothing was edited, while saying honestly that
    -- its content can no longer be re-derived.
    pruned_at      TIMESTAMPTZ NULL,
    pruned_count   INTEGER     NULL
);

CREATE INDEX IF NOT EXISTS ix_ailc_pruned ON ai_ledger_chain (pruned_at);


-- ── linking the existing ai_action path to the ledger ───────────────────────
--
-- `ai_action_proposals` (mig 133) already records what the model proposed and
-- what a human then did about it — for that one step type. Rather than have two
-- half-records of the same event, the proposal points at its ledger row, and
-- `decide()` writes the human's answer into the ledger's append-only verdict
-- table as well as its own status column.
--
-- Additive and nullable: every existing proposal reads "not linked", which is
-- exactly what every existing proposal is. Nothing reads this column unless the
-- ledger wrote it.
ALTER TABLE ai_action_proposals
    ADD COLUMN IF NOT EXISTS ledger_record_id UUID NULL;
