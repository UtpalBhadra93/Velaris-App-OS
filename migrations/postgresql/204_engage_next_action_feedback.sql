-- 204 — rep desk: what a rep decided about a suggested next action (item 12, P6).
--
-- WHY. The rep desk now suggests a next action from named rules over the
-- customer's own record, and says why. A suggestion a rep cannot disagree with
-- ON THE RECORD is an instruction, and one whose disagreements are not kept can
-- never be tuned — nobody can tell a rule that is ignored nine times in ten from
-- one that is followed. This is that record: one row per verdict, appended,
-- never updated, the same shape as `ai_decision_verdicts` and
-- `case_risk_feedback`.
--
-- WHAT DOES NOT CHANGE. Additive, one new table, nothing reads it but the new
-- routes, and the rep desk is dark unless the platform released it AND the
-- tenant installed the listing. An install taking this update sees nothing.
--
-- RE-RUNNABLE. `IF NOT EXISTS` throughout.
--
-- Copyright (c) 2024-2026 Velaris Contributors
-- SPDX-License-Identifier: BSL-1.1

CREATE TABLE IF NOT EXISTS customer_next_action_feedback (
    id             UUID PRIMARY KEY,
    tenant_id      UUID         NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    customer_id    UUID         NOT NULL REFERENCES customer_profiles(id) ON DELETE CASCADE,
    interaction_id UUID         NULL REFERENCES customer_interactions(id) ON DELETE SET NULL,
    -- The rule's stable key (`repeat_contact`, `sla_breached_case`, …). A rule
    -- is code, so the key is the join to what it said.
    action_key     VARCHAR(64)  NOT NULL,
    -- accepted | rejected
    verdict        VARCHAR(16)  NOT NULL,
    reason         TEXT         NULL,
    decided_by     VARCHAR(255) NOT NULL,
    decided_at     TIMESTAMPTZ  NOT NULL DEFAULT now(),

    CONSTRAINT ck_cnaf_verdict CHECK (verdict IN ('accepted', 'rejected'))
);

CREATE INDEX IF NOT EXISTS ix_cnaf_tenant_key
    ON customer_next_action_feedback (tenant_id, action_key, decided_at);
CREATE INDEX IF NOT EXISTS ix_cnaf_customer
    ON customer_next_action_feedback (customer_id, decided_at);
