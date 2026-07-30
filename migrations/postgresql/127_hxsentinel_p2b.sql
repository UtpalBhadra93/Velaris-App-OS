-- HxSentinel P2b — human score/note feedback on findings (RLHF-lite collection).
--
-- Every recommendation a human reviews can be scored 1 (bad analysis) .. 5
-- (best) with an optional note. The scores are the engine's future training
-- signal (P3); the notes are its explanations. Collection starts now so there
-- is a labelled feedback history to learn from later.
--
-- APPEND-ONLY, like case_outcomes: re-scoring inserts a new row (the newest is
-- current). The scoring history is itself evidence — who judged what, when —
-- and a supervisor's later dispute (P3) must not rewrite it.
--
-- Findings persist with stable ids and go 'stale' rather than delete, so a
-- feedback row never orphans.
CREATE TABLE IF NOT EXISTS case_risk_feedback (
    id          UUID PRIMARY KEY,
    tenant_id   VARCHAR(255),
    case_id     UUID         NOT NULL,      -- denormalised for tenant-scoped queries
    finding_id  UUID         NOT NULL,      -- the finding being judged
    reviewer    VARCHAR(255) NOT NULL,
    score       SMALLINT     NOT NULL,      -- 1 (bad analysis) .. 5 (best analysis)
    note        TEXT,
    created_at  TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);
-- The hot read: "latest (and full) feedback for this finding".
CREATE INDEX IF NOT EXISTS ix_risk_feedback_finding
    ON case_risk_feedback (finding_id, created_at DESC);
CREATE INDEX IF NOT EXISTS ix_risk_feedback_tenant
    ON case_risk_feedback (tenant_id, reviewer);
