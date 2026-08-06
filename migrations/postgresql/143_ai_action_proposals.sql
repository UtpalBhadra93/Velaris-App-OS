-- `ai_action` step type (#33, report §5.1) — AI as a workflow participant.
--
-- The model PROPOSES; a human accepts, edits or rejects; only then does the
-- value reach case data. That default is the human-in-the-loop requirement in
-- §5.1 — a workflow that lets a model write case variables unattended is a
-- materially riskier product than one that does not.
--
-- A step may opt out with `require_human_approval: false`. Even then a row is
-- written (status `auto_accepted`), because an unattended AI write that leaves
-- no trace is the one outcome nobody could defend afterwards.
--
-- `model` / `prompt_sha256` mirror the §5.5 Article-22 provenance kept for
-- DecisionPoints. The prompt itself is never stored — it carries case context.
-- Keeping `proposed` alongside `final_output` is what makes a model's real
-- accuracy measurable: how often did a human override it, and to what.

CREATE TABLE IF NOT EXISTS ai_action_proposals (
    id            UUID PRIMARY KEY,
    case_id       UUID NOT NULL REFERENCES case_instances(id) ON DELETE CASCADE,
    stage_id      VARCHAR(255),
    step_id       VARCHAR(255) NOT NULL,
    status        VARCHAR(20)  NOT NULL DEFAULT 'pending',
    proposed      JSONB        NOT NULL DEFAULT '{}'::jsonb,
    final_output  JSONB        NOT NULL DEFAULT '{}'::jsonb,
    provider      VARCHAR(64),
    model         VARCHAR(128),
    prompt_sha256 VARCHAR(64),
    error         TEXT,
    decided_by    VARCHAR(255),
    decided_at    TIMESTAMPTZ,
    created_at    TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS ix_ai_proposals_case ON ai_action_proposals (case_id, status);
