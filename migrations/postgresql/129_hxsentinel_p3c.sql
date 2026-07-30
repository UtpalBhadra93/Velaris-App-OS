-- HxSentinel P3c-1 — score-driven rule tuning proposals.
--
-- The reliability-weighted P2b scores (P3b) are aggregated per rule. A rule the
-- reviewers persistently score LOW earns a proposal to deactivate it; one they
-- score HIGH earns a proposal to raise its severity. Proposals are NEVER applied
-- silently — a human approves each one (the HxEvolve discipline), and only then
-- does it change `case_risk_rules`.
--
-- Anti-gaming: scores are weighted by the reviewer's reliability (P3b), disputed
-- scores are excluded, and a floor of min-reviews is required — so one
-- miscalibrated or rubber-stamping reviewer cannot steer a rule.
CREATE TABLE IF NOT EXISTS case_risk_rule_proposals (
    id              UUID PRIMARY KEY,
    tenant_id       VARCHAR(255) NOT NULL,
    rule_key        VARCHAR(64)  NOT NULL,
    proposed_change JSONB        NOT NULL,      -- {"enabled": false} | {"severity": "high"}
    rationale       TEXT         NOT NULL,      -- human-readable why
    evidence        JSONB        NOT NULL,      -- {weighted_avg, reviews, ...}
    status          VARCHAR(16)  NOT NULL DEFAULT 'pending',   -- pending | approved | rejected
    created_at      TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    decided_by      VARCHAR(255),
    decided_at      TIMESTAMPTZ,
    decision_note   TEXT
);
-- "open proposals for this tenant" (dashboard) and the no-duplicate-pending guard.
CREATE INDEX IF NOT EXISTS ix_risk_proposals_tenant_status
    ON case_risk_rule_proposals (tenant_id, status);
CREATE INDEX IF NOT EXISTS ix_risk_proposals_rule
    ON case_risk_rule_proposals (tenant_id, rule_key, status);
