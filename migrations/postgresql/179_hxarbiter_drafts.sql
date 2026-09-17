-- HxArbiter ARB-F — natural-language policy drafts.
--
-- Deferred from ARB-A deliberately (§10.2): the table was known to be missing
-- columns until the authoring pipeline existed, and shipping an empty table you
-- will ALTER later is worse than shipping it once, correctly.
--
-- A draft is not a policy. It is the record of how a proposed policy came to be
-- proposed — which is the artefact an auditor asks for when the answer to "who
-- wrote this control" is "a language model".
CREATE TABLE IF NOT EXISTS arbiter_drafts (
    id              UUID         PRIMARY KEY,
    -- NULL means the draft proposes a NEW policy rather than a new version of
    -- an existing one.
    policy_id       UUID         NULL REFERENCES arbiter_policies(id) ON DELETE CASCADE,
    tenant_id       UUID         NULL REFERENCES tenants(id) ON DELETE CASCADE,
    namespace       VARCHAR(64)  NOT NULL,

    -- The operator's own words. Kept verbatim: the readback is compared against
    -- THIS, and paraphrasing it would destroy the comparison.
    intent          TEXT         NOT NULL,
    generated_rules JSONB        NULL,
    compile_report  JSONB        NULL,
    analysis_report JSONB        NULL,
    -- What the approver actually signed. Produced WITHOUT the intent in
    -- context (§7.1 P4), so it says what the rules do rather than agreeing
    -- with what the author meant.
    readback        TEXT         NULL,
    readback_source VARCHAR(20)  NULL,   -- model | deterministic
    test_seeds      JSONB        NULL,
    whatif_report   JSONB        NULL,

    -- PROVENANCE. §19.5 makes this mandatory rather than optional: "your
    -- compliance control was written by an LLM" is the first question in the
    -- room, and the answer has to name which model, running where, under which
    -- prompt. Without these three columns the honest answer is "we don't know".
    model_provider  VARCHAR(64)  NULL,   -- local | external
    model_name      VARCHAR(128) NULL,
    prompt_version  VARCHAR(32)  NULL,
    -- How many attempts the compiler needed. A draft that only compiled on the
    -- repair round is worth a closer read.
    attempts        INTEGER      NOT NULL DEFAULT 1,

    -- Which legal texts drove this, for regulatory change management (§19.6).
    -- A citation the operator can follow is the difference between a proposal
    -- and an assertion.
    corpus_refs     JSONB        NULL,

    state           VARCHAR(32)  NOT NULL DEFAULT 'drafted',
    created_by      UUID         NULL,
    created_at      TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS ix_arbiter_drafts_tenant_state
    ON arbiter_drafts (tenant_id, state, created_at DESC);
