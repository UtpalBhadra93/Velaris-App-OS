-- F-04 (first slice) — the simulation gate on promotion.
--
-- Design: docs/Future/Done/AI-ERA-ROADMAP.md §F-04. Every process change should be
-- rehearsed before HxDeploy promotes it, and promotion gated on the result.
--
-- What this migration supports, and what it does not, stated here because the
-- gap is the thing a reader most needs to know:
--
--   BUILT — the GATE and the REPORT. A promotion run can be simulated by
--   replaying the recorded caseload through the candidate rules (HxReplay
--   already does exactly this, including a cost delta), checked against
--   tenant thresholds, and blocked. An override is signed, reasoned and
--   audited.
--
--   NOT BUILT — the digital twin itself: candidate definitions executing on an
--   isolated Temporal task queue against scratch schemas, with synthetic
--   Locust load. That is infrastructure, not schema, and a table pretending to
--   hold its results would be worse than an honest absence.
--
-- THE UPGRADE-SAFETY PROPERTY THAT MATTERS MOST IS HERE: the gate is OFF. An
-- install taking this update has no `simulation_gates` row, and no row means
-- not enforced — deployments approve exactly as they did yesterday. Switching
-- it on is an operator's decision and never an update's.

-- ── the gate's thresholds ───────────────────────────────────────────────────
--
-- `tenant_id IS NULL` is the platform default, inherited by a tenant with no
-- row. `tenant_key` collapses that NULL to 'platform' so one UNIQUE constrains
-- both scopes — the mig 177 lesson, third time carried rather than relearned.
CREATE TABLE IF NOT EXISTS simulation_gates (
    id                     UUID          PRIMARY KEY,
    tenant_id              UUID          NULL REFERENCES tenants(id) ON DELETE CASCADE,
    tenant_key             VARCHAR(36)   NOT NULL,

    -- FALSE, and the default is the whole upgrade story: an existing install
    -- has no row at all, and a row that exists but is disabled behaves the same
    -- way. Nothing blocks until somebody chooses.
    enabled                BOOLEAN       NOT NULL DEFAULT FALSE,

    -- Thresholds. NULL means "not checked" rather than zero — a threshold of
    -- zero is the strictest possible setting and must never be what an operator
    -- gets by leaving a field blank.
    max_divergent_pct      NUMERIC(6,3)  NULL,
    max_cost_increase_pct  NUMERIC(6,3)  NULL,
    min_cohort             INTEGER       NULL,
    -- Require a compliance proof (F-03) when the package changes policy.
    require_proof          BOOLEAN       NOT NULL DEFAULT FALSE,

    created_by             UUID          NULL,
    created_at             TIMESTAMPTZ   NOT NULL DEFAULT NOW(),
    updated_at             TIMESTAMPTZ   NOT NULL DEFAULT NOW(),

    CONSTRAINT ck_simgate_tenant_key CHECK (
        (tenant_id IS NULL     AND tenant_key =  'platform') OR
        (tenant_id IS NOT NULL AND tenant_key <> 'platform')
    ),
    CONSTRAINT ux_simgate_scope UNIQUE (tenant_key)
);


-- ── one simulation of one promotion ─────────────────────────────────────────
--
-- Append-only. A re-simulation writes a new row; the run points at the newest.
-- Keeping the earlier ones is the point — "it failed twice and then passed" is
-- exactly what somebody reviewing a promotion afterwards needs to see, and an
-- UPDATE would erase it.
CREATE TABLE IF NOT EXISTS simulation_runs (
    id                UUID         PRIMARY KEY,
    tenant_id         UUID         NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    deployment_run_id UUID         NULL REFERENCES deployment_runs(id) ON DELETE CASCADE,

    -- What it was simulating, pinned. A simulation of a different package than
    -- the one being approved is not evidence about this promotion, and the
    -- checksum is how the gate refuses to accept it as such.
    package_checksum  VARCHAR(64)  NULL,

    -- passed | failed | refused. REFUSED is not FAILED: failed means the
    -- thresholds were exceeded, refused means the simulation could not be run
    -- honestly at all — and letting the second read as the first would block
    -- promotions for a reason nobody can act on.
    status            VARCHAR(20)  NOT NULL,
    findings          JSONB        NOT NULL DEFAULT '{}'::jsonb,
    -- The thresholds AS THEY WERE when this ran. Reading them live at review
    -- time would let a later edit change what a past simulation appears to have
    -- been judged against.
    thresholds        JSONB        NOT NULL DEFAULT '{}'::jsonb,

    -- Links to the evidence this reused rather than reinvented.
    replay_run_id     UUID         NULL,
    proof_id          UUID         NULL,
    -- The F-00 agent the twin work is attributed to. Its capability ceiling is
    -- empty by construction, which is what makes "the twin can reach nothing"
    -- a property rather than a promise.
    twin_agent_id     UUID         NULL,

    created_by        UUID         NULL,
    created_at        TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    completed_at      TIMESTAMPTZ  NULL
);

CREATE INDEX IF NOT EXISTS ix_simrun_deploy ON simulation_runs (deployment_run_id, created_at DESC);
CREATE INDEX IF NOT EXISTS ix_simrun_tenant ON simulation_runs (tenant_id, created_at DESC);


-- ── what the promotion run remembers about its gate ─────────────────────────
--
-- Additive and nullable: every existing run reads "never gated", which is what
-- every existing run is.
ALTER TABLE deployment_runs
    ADD COLUMN IF NOT EXISTS simulation_id        UUID         NULL,
    ADD COLUMN IF NOT EXISTS gate_override_reason TEXT         NULL,
    ADD COLUMN IF NOT EXISTS gate_overridden_by   VARCHAR(255) NULL,
    ADD COLUMN IF NOT EXISTS gate_overridden_at   TIMESTAMPTZ  NULL;
