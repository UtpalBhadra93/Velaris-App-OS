-- HxSentinel P3b — supervisor oversight of the P2b score signal.
--
-- Scores must be honest to be useful: a reviewer who bulk-5s to look diligent,
-- or dismisses correct findings, poisons the training signal. P3b lets a
-- supervisor SEE that (dashboard) and mark a specific score DISPUTED, which
-- excludes it from reliability and from future training.
--
-- A dispute is a distinct supervisor judgment about someone else's score, so it
-- gets its own append-only table rather than mutating the (append-only) feedback
-- row — the reviewer's original score/note stay immutable, and the dispute
-- carries its own provenance.
CREATE TABLE IF NOT EXISTS case_risk_score_disputes (
    id           UUID PRIMARY KEY,
    tenant_id    VARCHAR(255),
    feedback_id  UUID         NOT NULL,      -- the score being disputed
    disputed_by  VARCHAR(255) NOT NULL,      -- the supervisor
    note         TEXT         NOT NULL,      -- why (required — the dispute must be justified)
    created_at   TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);
-- "is this score disputed?" and per-tenant dashboard scans.
CREATE INDEX IF NOT EXISTS ix_risk_disputes_feedback
    ON case_risk_score_disputes (feedback_id);
CREATE INDEX IF NOT EXISTS ix_risk_disputes_tenant
    ON case_risk_score_disputes (tenant_id);
