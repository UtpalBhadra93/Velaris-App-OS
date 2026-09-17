-- F-04 (second slice) — the digital twin.
--
-- Design: docs/Future/Done/AI-ERA-ROADMAP.md §F-04. Migration 186 built the GATE:
-- thresholds, a verdict, and a promotion that cannot be approved past a
-- failure. It deliberately did NOT build the twin, and every run it produced
-- said so — `synthetic_load: false`, with a coverage note, because a reader who
-- takes a passing gate for a capacity guarantee has been misled.
--
-- This migration is the other half.
--
-- WHAT THE TWIN IS, CONCRETELY. It is not a mode of this process. It is a
-- SECOND case-service Temporal worker that the operator runs, with two things
-- of its own:
--
--   * its own task queue  (VELARIS_CASE_TWIN_TASK_QUEUE), so production workers
--     can never pick up rehearsal work, and
--   * its own database    (VELARIS_CASE_TWIN_DATABASE_URL), so a rehearsal
--     cannot write a synthetic case anywhere a person will ever read it.
--
-- Nothing in Velaris starts that process. A platform that spawned workers to
-- rehearse a promotion would be a platform whose blast radius is a bug in its
-- own process management, and it could not be made safe on an install that
-- takes this update unattended. **No twin configured means no twin run**, and
-- the simulation reports exactly what it reported yesterday.
--
-- WHAT IS STILL NOT BUILT, and named rather than implied: a load PROFILE —
-- ramped concurrency, think-time distributions, sustained soak, the Locust-shaped
-- half of the roadmap's sentence. What runs here is a bounded batch of synthetic
-- cases driven through the candidate definition. It answers "does this candidate
-- work, and how fast, on data nobody will ever see" — it does not answer "what
-- happens at 500 concurrent users", and `twin_runs.load_profile` records the
-- word `batch` so no reader has to guess which question was asked.
--
-- NOTHING HERE CHANGES BEHAVIOUR ON UPGRADE. One new table and four nullable
-- columns. The three new thresholds are NULL, which means NOT CHECKED — the
-- F-04 rule that a blank field must never produce the strictest possible
-- setting.


-- ── one execution of a candidate against the twin ───────────────────────────
CREATE TABLE IF NOT EXISTS twin_runs (
    id                 UUID         PRIMARY KEY,
    tenant_id          UUID         NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    deployment_run_id  UUID         NULL REFERENCES deployment_runs(id) ON DELETE CASCADE,
    -- Pinned for the same reason the simulation pins it: a rehearsal of a
    -- different package is not evidence about this promotion.
    package_checksum   VARCHAR(64)  NULL,

    -- ── where it ran ────────────────────────────────────────────────────────
    task_queue         VARCHAR(255) NULL,
    -- A HASH of the twin DSN, never the DSN. A connection string carries a
    -- password, and this row is read by anybody who may read a simulation.
    -- The fingerprint is enough to answer the only question a reader has:
    -- "was that the same database as production?"
    database_fingerprint VARCHAR(64) NULL,

    -- The isolation proof. Recorded rather than assumed, because "the twin was
    -- actually production" is the one failure of this feature that would be
    -- catastrophic and silent: synthetic cases written into real data, and a
    -- promotion cleared by a rehearsal that damaged the thing it was rehearsing.
    --
    -- Compared on (host, port, database) and on the queue name — NOT on the
    -- whole connection string. Two DSNs differing only by password point at the
    -- same database and are not isolation.
    isolation_verified BOOLEAN      NOT NULL DEFAULT FALSE,
    isolation_detail   JSONB        NULL,

    -- ── what was put there ──────────────────────────────────────────────────
    -- The candidate bundle is applied to the twin through the SAME import path
    -- a real promotion uses. A twin that provisioned itself some other way
    -- would be rehearsing a different deployment from the one being approved.
    provisioned        JSONB        NULL,

    -- ── what was run ────────────────────────────────────────────────────────
    -- `batch` today. The column exists so the day a ramped profile is built,
    -- an old row still says which question it answered.
    load_profile       VARCHAR(20)  NOT NULL DEFAULT 'batch',
    requested_cases    INTEGER      NOT NULL DEFAULT 0,
    started_cases      INTEGER      NOT NULL DEFAULT 0,
    -- Reached a terminal stage. Not the same as "succeeded" — see `stuck`.
    terminal_cases     INTEGER      NOT NULL DEFAULT 0,
    failed_cases       INTEGER      NOT NULL DEFAULT 0,
    -- Started, did not fail, and did not finish. A candidate definition that
    -- strands cases is the failure this whole feature exists to catch, and it
    -- is invisible if `terminal` and `failed` are the only two counters.
    stuck_cases        INTEGER      NOT NULL DEFAULT 0,

    -- ── how it went ─────────────────────────────────────────────────────────
    p50_ms             INTEGER      NULL,
    p95_ms             INTEGER      NULL,
    max_ms             INTEGER      NULL,
    -- Per-stage counts and the reasons cases stopped where they did. The
    -- aggregate says a candidate is slow; this says which stage is.
    stage_profile      JSONB        NULL,
    failures           JSONB        NULL,

    -- pending | running | complete | refused | failed.
    -- `refused` is NOT `failed`, exactly as in the gate: no twin configured,
    -- no worker polling, isolation unproven, or nothing in the package worth
    -- running are all reasons a rehearsal did not happen. Reading them as a
    -- failing candidate would send an author to fix code that is fine.
    status             VARCHAR(20)  NOT NULL DEFAULT 'pending',
    refusal_reason     VARCHAR(64)  NULL,
    error              TEXT         NULL,

    -- ── afterwards ──────────────────────────────────────────────────────────
    -- The twin is cleaned up by the run that dirtied it. A twin database that
    -- accumulates every rehearsal ever run stops being a rehearsal environment
    -- and becomes a second production nobody maintains.
    torn_down_at       TIMESTAMPTZ  NULL,
    torn_down_rows     INTEGER      NULL,
    -- Set when teardown could not finish. It is deliberately not an error on
    -- the run: the rehearsal's verdict is still valid, and somebody has to be
    -- told the twin needs sweeping.
    teardown_error     TEXT         NULL,

    created_by         VARCHAR(255) NULL,
    created_at         TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    completed_at       TIMESTAMPTZ  NULL
);

CREATE INDEX IF NOT EXISTS ix_twinrun_tenant ON twin_runs (tenant_id, created_at DESC);
CREATE INDEX IF NOT EXISTS ix_twinrun_deploy ON twin_runs (deployment_run_id, created_at DESC);


-- ── the simulation points at the twin run that informed it ──────────────────
ALTER TABLE simulation_runs ADD COLUMN IF NOT EXISTS twin_run_id UUID NULL;


-- ── three more thresholds, all NULL = NOT CHECKED ───────────────────────────
--
-- NULL rather than a default, and that is the whole upgrade story for the gate:
-- an install that already has a `simulation_gates` row keeps approving exactly
-- what it approved yesterday, because three unchecked thresholds cannot fail.
ALTER TABLE simulation_gates ADD COLUMN IF NOT EXISTS min_twin_cases       INTEGER      NULL;
ALTER TABLE simulation_gates ADD COLUMN IF NOT EXISTS max_twin_failure_pct NUMERIC(6,3) NULL;
ALTER TABLE simulation_gates ADD COLUMN IF NOT EXISTS max_twin_p95_ms      INTEGER      NULL;
