-- F-05 (first slice) — agents as workers.
--
-- Design: docs/Future/Done/AI-ERA-ROADMAP.md §F-05. An AI agent should sit in a
-- queue next to a human: receive assignments from the same engine, carry a
-- workload cap, and get a performance review computed from the same evidence
-- everything else uses.
--
-- What this migration supports:
--
--   BUILT — the ROSTER (skills, capacity, escalation target, bench state,
--   routing weight), the SCORECARD (acceptance and override from F-01, cost
--   per step from F-02), autonomy as a POLICY rather than a config flag, and a
--   `workforce` assignment strategy that can return an agent.
--
--   NOT BUILT — the Studio Workforce view, and the nightly job that recomputes
--   scorecards on a schedule. The scorecard is computed on demand; a scheduled
--   job is a cron entry, not a schema.
--
-- NOTHING HERE CHANGES BEHAVIOUR ON UPGRADE. Two new tables, nothing altered,
-- and the new assignment strategy is opt-in per rule: an install whose case
-- types do not name it never reaches any of this. `case_assignments.
-- assignee_type` is already a free string, so an agent assignment needs no
-- schema change at all.

-- ── the roster row ──────────────────────────────────────────────────────────
--
-- One per agent that is allowed to hold work. An F-00 agent WITHOUT one of
-- these is not a worker — it can be attributed and can hold a token, and it is
-- simply not in the roster. That separation is deliberate: HxNexus's case-Q&A
-- agent should never be handed a case step.
CREATE TABLE IF NOT EXISTS agent_workforce (
    id                    UUID         PRIMARY KEY,
    agent_id              UUID         NOT NULL REFERENCES agents(id) ON DELETE CASCADE,
    tenant_id             UUID         NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,

    -- The SAME taxonomy humans use: `user_directory.skills`, a JSON array of
    -- names matched case-insensitively. Sharing the vocabulary is the whole
    -- point — a skill that means one thing for a person and another for an
    -- agent makes a mixed roster unreadable.
    skills                JSONB        NOT NULL DEFAULT '[]'::jsonb,

    -- Capacity. `max_concurrent` is the same idea as a human's workload cap;
    -- `token_budget_per_hour` is the one humans have no equivalent of, and it
    -- is what stops a looping agent spending a month's budget in an afternoon.
    -- NULL means uncapped, and is written that way rather than as a large
    -- number so "no cap" is legible as a decision.
    max_concurrent        INTEGER      NULL,
    token_budget_per_hour BIGINT       NULL,

    -- MANDATORY, and NOT NULL for that reason: every agent must have a human
    -- queue its work can fall to. An agent whose escalation target is missing
    -- is auto-benched — fail-closed staffing, because the alternative is work
    -- silently stuck on something that cannot ask for help.
    escalation_queue      VARCHAR(255) NOT NULL,

    -- Routing weight, applied to the agent's score. Changed through a PROPOSAL
    -- a supervisor approves, never by the scorecard job directly: a system that
    -- adjusts its own routing from its own performance data is a feedback loop
    -- nobody is steering.
    routing_weight        NUMERIC(5,3) NOT NULL DEFAULT 1.000,

    -- Benched agents hold no new work. The reason is kept because "why is this
    -- agent not receiving anything" is the first question asked about one.
    benched               BOOLEAN      NOT NULL DEFAULT FALSE,
    bench_reason          TEXT         NULL,
    benched_at            TIMESTAMPTZ  NULL,

    created_by            UUID         NULL,
    created_at            TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    updated_at            TIMESTAMPTZ  NOT NULL DEFAULT NOW(),

    CONSTRAINT ux_workforce_agent UNIQUE (agent_id)
);

CREATE INDEX IF NOT EXISTS ix_workforce_tenant ON agent_workforce (tenant_id, benched);


-- ── the scorecard ───────────────────────────────────────────────────────────
--
-- A snapshot, append-only. Every number here is DERIVED from F-01 and F-02 and
-- could be recomputed — it is stored so that a routing-weight proposal can name
-- the evidence it was based on, and so a supervisor reviewing that proposal
-- three days later sees what the proposer saw rather than what is true now.
CREATE TABLE IF NOT EXISTS agent_scorecards (
    id                UUID          PRIMARY KEY,
    agent_id          UUID          NOT NULL REFERENCES agents(id) ON DELETE CASCADE,
    tenant_id         UUID          NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,

    window_start      TIMESTAMPTZ   NOT NULL,
    window_end        TIMESTAMPTZ   NOT NULL,

    decisions         INTEGER       NOT NULL DEFAULT 0,
    reviewed          INTEGER       NOT NULL DEFAULT 0,
    accepted          INTEGER       NOT NULL DEFAULT 0,
    overridden        INTEGER       NOT NULL DEFAULT 0,
    -- NULL, never 0.0, when nothing was reviewed. A 0% override rate over zero
    -- reviews reads as "never wrong" and means "nobody looked" — the same rule
    -- the F-01 report follows, and for the same reason.
    override_rate     NUMERIC(6,4)  NULL,
    cost_total        NUMERIC(18,6) NOT NULL DEFAULT 0,
    cost_per_decision NUMERIC(18,6) NULL,
    currency          VARCHAR(8)    NULL,

    -- The proposal this scorecard supports, if any. A weight change with no
    -- scorecard behind it is somebody's opinion; this column is what makes the
    -- difference visible.
    proposed_weight   NUMERIC(5,3)  NULL,
    proposal_status   VARCHAR(20)   NULL,     -- proposed | approved | rejected
    proposal_reason   TEXT          NULL,
    decided_by        VARCHAR(255)  NULL,
    decided_at        TIMESTAMPTZ   NULL,

    created_at        TIMESTAMPTZ   NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS ix_scorecard_agent ON agent_scorecards (agent_id, created_at DESC);
CREATE INDEX IF NOT EXISTS ix_scorecard_open  ON agent_scorecards (tenant_id, proposal_status);
