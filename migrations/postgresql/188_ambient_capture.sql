-- F-06 (first slice) — ambient case capture.
--
-- Design: docs/Future/Done/AI-ERA-ROADMAP.md §F-06. A meeting transcript, a voice
-- note or an inbound email should turn into structured case work — a variable
-- update, a follow-up task, a note — presented as confirm-or-edit rather than
-- as a form somebody retypes from what they just heard.
--
-- What this migration supports:
--
--   BUILT — the capture RUN (one extraction pass over one source, with the
--   model that answered and a hash of exactly the text it read) and the
--   PROPOSAL (one suggested change, its evidence snippet, and the human's
--   answer). Extraction is explicit: a person asks for it on a source.
--
--   NOT BUILT — automatic extraction the moment an email arrives or a meeting
--   is analysed. That is a trigger, not a schema, and switching one on for an
--   install that has never seen this feature is exactly the kind of surprise
--   the upgrade rules forbid. The tables are the same either way.
--
-- NOTHING HERE CHANGES BEHAVIOUR ON UPGRADE. Two new tables, nothing altered.
-- Capture is tenant opt-in and OFF by default, so an install taking this update
-- has no runs, no proposals, and nothing new happening in the background.
--
-- **Nothing here ever writes case data.** An accepted proposal is applied
-- through the ordinary case-variable path as the person who confirmed it, with
-- the same grant check their own edit would face. These tables hold what was
-- suggested and what was decided — never a second way into the case.

-- ── one extraction pass over one source ─────────────────────────────────────
--
-- The run exists so a proposal can say what it came from. `source_sha256` is
-- the commitment: it is the hash of the exact text the model read, so a
-- proposal reviewed a week later can be checked against the transcript or
-- email it claims to summarise — and a re-run over changed text is visibly a
-- different run rather than an unexplained second opinion.
CREATE TABLE IF NOT EXISTS capture_runs (
    id             UUID         PRIMARY KEY,
    tenant_id      UUID         NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    case_id        UUID         NOT NULL REFERENCES case_instances(id) ON DELETE CASCADE,

    -- meeting | email | voice_note. A closed vocabulary: each has its own
    -- consent story, and a fourth source is a code change precisely because
    -- somebody has to answer that question for it.
    source_kind    VARCHAR(20)  NOT NULL,
    -- The session id, the email message row id, or 'adhoc' for a voice note
    -- dictated into the digest. Kept as text: the three sources live in three
    -- tables and a foreign key to "one of three" is not a foreign key.
    source_ref     VARCHAR(255) NOT NULL,
    source_sha256  VARCHAR(64)  NOT NULL,
    -- Where the reader can go and look: a transcript document id, a message-id,
    -- or NULL when the text was dictated and never stored anywhere.
    source_locator VARCHAR(255) NULL,

    -- running | complete | failed | refused.
    -- `refused` is NOT `failed`: consent absent, capture switched off, or a
    -- source with nothing in it are all the gate working, and reading them as
    -- faults would send somebody looking for a bug that is not there.
    status         VARCHAR(20)  NOT NULL DEFAULT 'running',
    refusal_reason VARCHAR(64)  NULL,
    error          TEXT         NULL,

    -- What actually answered, not what is configured.
    provider       VARCHAR(64)  NULL,
    model          VARCHAR(128) NULL,
    prompt_sha256  VARCHAR(64)  NULL,

    -- How many goes it took. A small local model asked for JSON while an
    -- email is busy telling it to do something else can return syntax that
    -- does not parse, and one repair round recovers it — but a run that only
    -- succeeded on the second attempt is worth a closer read, and a source
    -- that always needs two is worth investigating rather than tolerating.
    attempts       INTEGER      NOT NULL DEFAULT 1,

    -- Counted at the run, because "the model proposed nine things and six were
    -- outside what capture may touch" is the sentence an operator needs. A
    -- dropped proposal leaves no row by design — it is not a suggestion anybody
    -- may accept — so the count is the only trace it existed.
    proposed_count INTEGER      NOT NULL DEFAULT 0,
    dropped_count  INTEGER      NOT NULL DEFAULT 0,

    requested_by   VARCHAR(255) NOT NULL,
    created_at     TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    completed_at   TIMESTAMPTZ  NULL
);

CREATE INDEX IF NOT EXISTS ix_capture_runs_case   ON capture_runs (case_id, created_at DESC);
CREATE INDEX IF NOT EXISTS ix_capture_runs_source ON capture_runs (source_kind, source_ref);


-- ── one suggested change, awaiting a person ─────────────────────────────────
--
-- Proposal-only, always. Nothing in this table has any effect until somebody
-- accepts or edits it, and the row keeps both what was suggested and what was
-- decided — which is what makes the capture's real accuracy measurable later
-- instead of merely felt.
CREATE TABLE IF NOT EXISTS capture_proposals (
    id             UUID         PRIMARY KEY,
    run_id         UUID         NOT NULL REFERENCES capture_runs(id) ON DELETE CASCADE,
    tenant_id      UUID         NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    case_id        UUID         NOT NULL REFERENCES case_instances(id) ON DELETE CASCADE,

    -- variable_update | task | note. Closed, and closed STRUCTURALLY rather
    -- than by asking the model nicely: a proposal kind that does not exist here
    -- cannot be stored, so it cannot be accepted. This is the whitelist the
    -- design asks for — capture reaches case content and nothing else, and no
    -- prompt injection can widen it, because widening it would take a
    -- migration.
    kind           VARCHAR(20)  NOT NULL,
    -- 'namespace.name' for a variable update; NULL for a task or a note.
    target         VARCHAR(255) NULL,

    -- What the case says now, captured when the proposal was made. A reviewer
    -- needs the before as much as the after, and re-reading it at decision
    -- time would show a value that may have moved since.
    current_value  JSONB        NULL,
    proposed_value JSONB        NULL,

    -- The model's own confidence when it gave one. NULL means it did not, and
    -- is stored as NULL rather than 0 — "no confidence reported" and "confident
    -- it is wrong" are opposite readings of the same screen.
    confidence     NUMERIC(4,3) NULL,

    -- The evidence. A proposal a reviewer cannot trace back to a sentence is a
    -- proposal they can only guess about, so the snippet travels with it.
    snippet        TEXT         NULL,
    -- Transcript timestamp, message-id, or line reference.
    locator        VARCHAR(255) NULL,

    -- pending | accepted | edited | rejected | superseded.
    -- `edited` mirrors the ledger's own vocabulary rather than inventing a
    -- fourth word — "the human changed the answer" is the same fact here as
    -- everywhere else, and a synonym would make override rates incomparable.
    -- `superseded` is what a re-run does to the pending proposals it replaces:
    -- they are not rejected, nobody looked at them.
    status         VARCHAR(20)  NOT NULL DEFAULT 'pending',
    applied_value  JSONB        NULL,
    decided_by     VARCHAR(255) NULL,
    decided_at     TIMESTAMPTZ  NULL,

    -- The F-01 row this proposal is the capture half of. One record per
    -- proposal, not per run: the human answers each one separately, and a
    -- ledger verdict covers one decision.
    ledger_record_id UUID       NULL,

    created_at     TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS ix_capture_prop_case ON capture_proposals (case_id, status);
CREATE INDEX IF NOT EXISTS ix_capture_prop_run  ON capture_proposals (run_id);
