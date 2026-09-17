-- HxArbiter — the decision log, and the chain that makes it evidence.
--
-- Design §10 described `arbiter_decisions` as "audit-anchored" and §18.7 names
-- a tamper-evident decision log as one of the two things no competing engine
-- ships. The sketched table could be neither: it had no hash, no chain, no
-- anchor reference, and there was no chain table anywhere in the design. As
-- drawn, the log was ordinary rows a database administrator could edit with
-- nothing to show for it — which is the exact opposite of the claim being sold.
--
-- The shape below is not invented: it mirrors `case_audit_log_chain`
-- (migration 027) and reuses `audit_anchors`, which already obtains RFC-3161
-- receipts over a chain tip. That is the proven pattern in this platform, and a
-- second, different one would be a second thing to get wrong.


-- ── decisions ───────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS arbiter_decisions (
    id              UUID         PRIMARY KEY,
    -- Always resolved to the tenant UUID before the write, never the slug the
    -- caller's token happened to carry. A log that mixes both forms cannot be
    -- filtered, and "every decision for this tenant" is the first question
    -- anybody asks it.
    tenant_id       UUID         NOT NULL,
    namespace       VARCHAR(64)  NOT NULL,
    action          VARCHAR(128) NOT NULL,

    -- Denormalised out of the subject JSON, and indexed. "Every decision about
    -- this principal" is a core audit query; served from a JSON extraction it
    -- would need a different expression on each of the three backends and an
    -- index on none of them.
    subject_kind    VARCHAR(20)  NOT NULL,
    subject_id      VARCHAR(255) NOT NULL,

    subject         JSONB        NOT NULL,
    resource        JSONB        NOT NULL,
    -- Redacted per HxPrivacy classifications before it is written. A decision
    -- log that faithfully records the personal data it was asked about becomes
    -- a second copy of that data, governed by nothing.
    context         JSONB        NOT NULL,

    effect          VARCHAR(10)  NOT NULL,
    -- `enforced` alone cannot distinguish shadow from dry_run, and §5.5 needs
    -- exactly that distinction: dry_run must surface "would have been denied"
    -- to Studio, while shadow must not appear there at all. A boolean collapses
    -- two states the product treats differently.
    mode            VARCHAR(20)  NOT NULL,
    enforced        BOOLEAN      NOT NULL,
    default_applied BOOLEAN      NOT NULL,
    matched         JSONB        NOT NULL DEFAULT '[]'::jsonb,
    obligations     JSONB        NOT NULL DEFAULT '[]'::jsonb,

    -- What the legacy control said, during shadow and parity (§8). A
    -- disagreement between this and `effect` is the whole output of a parity
    -- report, and is never sampled away.
    legacy_effect   VARCHAR(10)  NULL,

    -- The policy set that produced this answer, as a digest over the active
    -- version signatures. Needed because a `default_applied` row names no
    -- policy at all: without it, a decision where nothing matched cannot be
    -- re-derived later, and "nothing matched" is the answer most worth
    -- questioning.
    policy_set_hash CHAR(64)     NULL,

    -- Why the evaluator could not answer, when it could not. §6 requires a PIP
    -- timeout to be logged as `pip_timeout` and governed namespaces treated as
    -- deny; the sketch specified the behaviour and gave the reason nowhere to
    -- go, which would have made a fail-closed deny indistinguishable from a
    -- policy deny on the screen.
    error           VARCHAR(64)  NULL,

    -- Ties a decision to the HTTP request and to the audit event beside it.
    request_id      VARCHAR(64)  NULL,

    -- WHO CLAIMED THIS SUBJECT. `subject` describes the principal the action is
    -- on behalf of, and a PEP legitimately asks about somebody else — the
    -- engine, a worker and a connector all do. That makes `subject` an
    -- assertion, not an identity, and an assertion with nobody's name on it is
    -- unattributable: an operator calling POST /decide could write any
    -- principal into the log and the row would read exactly like one the
    -- platform produced. This column is the authenticated caller, stamped by
    -- the router and never taken from the body.
    claimed_by      VARCHAR(255) NULL,

    simulated       BOOLEAN      NOT NULL DEFAULT FALSE,
    latency_us      INTEGER      NOT NULL DEFAULT 0,
    -- Microsecond precision, so decisions written inside the same second still
    -- order deterministically. MySQL's sibling must NOT carry a DEFAULT here:
    -- it rejects DEFAULT now() on DATETIME(6) where MariaDB accepts it.
    evaluated_at    TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS ix_arb_dec_tenant_ns_time
    ON arbiter_decisions (tenant_id, namespace, evaluated_at DESC);
CREATE INDEX IF NOT EXISTS ix_arb_dec_subject
    ON arbiter_decisions (tenant_id, subject_id, evaluated_at DESC);
-- Parity reporting reads exactly this: the rows where the engine and the
-- control it is replacing did not agree.
CREATE INDEX IF NOT EXISTS ix_arb_dec_disagreement
    ON arbiter_decisions (tenant_id, namespace, evaluated_at DESC)
    WHERE legacy_effect IS NOT NULL AND legacy_effect <> effect;


-- ── the chain ───────────────────────────────────────────────────────────────
--
-- One row per batched interval, not per decision. §10 puts the batch at 60
-- seconds, and the reason is throughput: chaining every decision individually
-- would serialise the hot path behind a single sequence.
--
-- `content_hash = sha256(prev_hash || canonical(batch))`, identical in form to
-- case_audit_log_chain. The tip is what `audit_anchors` gets an RFC-3161
-- receipt over, so a DB administrator who rewrites both the decisions and this
-- chain still cannot forge anything older than the newest receipt.
CREATE TABLE IF NOT EXISTS arbiter_decision_chain (
    id            UUID         PRIMARY KEY,
    sequence      INTEGER      NOT NULL UNIQUE,
    -- The window this batch covers, and how many decisions it sealed. A gap in
    -- the windows is itself evidence: it says rows were removed.
    window_start  TIMESTAMPTZ  NOT NULL,
    window_end    TIMESTAMPTZ  NOT NULL,
    decision_count INTEGER     NOT NULL,
    prev_hash     CHAR(64)     NOT NULL,
    content_hash  CHAR(64)     NOT NULL,
    sealed_at     TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS ix_arb_chain_window
    ON arbiter_decision_chain (window_start, window_end);

-- Which batch sealed a given decision. Nullable because a decision is written
-- immediately and sealed up to 60 seconds later — a NULL here means "not yet
-- sealed", never "unsealable".
ALTER TABLE arbiter_decisions
    ADD COLUMN IF NOT EXISTS chain_sequence INTEGER NULL;
CREATE INDEX IF NOT EXISTS ix_arb_dec_unsealed
    ON arbiter_decisions (evaluated_at) WHERE chain_sequence IS NULL;
