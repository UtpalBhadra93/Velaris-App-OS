-- F-08 (first slice) — self-healing processes.
--
-- Design: docs/Future/Done/AI-ERA-ROADMAP.md §F-08. HxEvolve already mines
-- deviations and proposes replay-proven changes, and then **stops**: an insight
-- is surfaced, a human reads it, and a human stages it. This closes that loop —
-- but only as far as a loop may honestly be closed, which is one approval, not
-- none.
--
-- WHAT AN EPISODE IS. One row per healing attempt, holding the whole story:
-- the diagnosis that started it, the candidate change, every rung of the
-- validation ladder and where it stopped, the human's answer, what was applied,
-- what happened afterwards, and whether it had to be undone. The roadmap asks
-- for "one chained evidence record — the platform can show an auditor *why it
-- changed itself*", and a story split across five tables is not that.
--
-- THE THREE THINGS THAT MAKE THIS SAFE, and they are all structural:
--
--   1. **The operator catalog is the boundary.** A healing change can only ever
--      be one of HxEvolve's five signed-off kinds (rule_add, rule_adjust,
--      sla_duration, routing, reorder). Auth config, HxArbiter policy, egress,
--      credentials and agent autonomy are not *forbidden* — they are
--      inexpressible, because no operator in the catalog can name them. That is
--      a unit-provable property, not a policy check somebody can misconfigure.
--   2. **Nothing applies without a person.** `approved_by` is not nullable at
--      the point of application, and the apply path is HxBranch's existing
--      audited merge — not a second way into config.
--   3. **The rollback is pre-signed.** The exact inverse patch is computed and
--      stored AT APPROVAL TIME, so undoing a healing change at 3 a.m. replays a
--      snapshot a named human already approved rather than needing a new
--      privileged path at the worst possible moment.
--
-- NOTHING HERE CHANGES BEHAVIOUR ON UPGRADE. One new table. Healing is off
-- until an operator switches it on AND a tenant opts in — neither implies the
-- other — so an install taking this update produces no episodes, applies
-- nothing, and watches nothing.

CREATE TABLE IF NOT EXISTS healing_episodes (
    id                UUID         PRIMARY KEY,
    tenant_id         UUID         NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    case_type_id      UUID         NOT NULL,
    -- The HxEvolve insight this episode is about. An episode never invents a
    -- change: it takes one the existing miner already proposed and already
    -- gate-validated, then asks harder questions of it.
    insight_id        UUID         NULL,

    -- ── what was wrong ──────────────────────────────────────────────────────
    -- The mining signal, verbatim. Kept rather than referenced because an
    -- auditor asking "why did it change itself" needs the reason as it stood,
    -- and a later scan will have moved on.
    diagnosis         JSONB        NOT NULL,
    -- The operator, from the closed catalog. A value outside it cannot be
    -- written by any code path here, which is the whole security argument.
    operator          VARCHAR(32)  NOT NULL,
    proposal          JSONB        NOT NULL,

    -- ── the validation ladder ───────────────────────────────────────────────
    -- Every rung, in order, with its verdict and what it was measured against:
    -- [{"rung": "catalog", "ok": true, ...}, {"rung": "gate", ...},
    --  {"rung": "proof", ...}, {"rung": "twin", ...}, {"rung": "thresholds", ...}]
    --
    -- The ladder STOPS at the first refusal rather than collecting every
    -- failure: a change that cannot be expressed safely should not then be
    -- replayed across the whole caseload to find out whether it would also have
    -- been slow.
    ladder            JSONB        NOT NULL DEFAULT '[]'::jsonb,
    stopped_at        VARCHAR(32)  NULL,
    stopped_reason    TEXT         NULL,

    -- The evidence the ladder produced, by id, so a reviewer can open each one.
    proof_id          UUID         NULL,
    simulation_id     UUID         NULL,
    twin_run_id       UUID         NULL,
    replay_run_id     UUID         NULL,

    -- ── the human ───────────────────────────────────────────────────────────
    -- pending | ready | refused | rejected | applied | watching | healed
    -- | rolled_back | failed
    --
    -- `refused` is the ladder declining to put this in front of anybody;
    -- `rejected` is a person saying no. Collapsing them would lose the
    -- difference between "we could not evidence this" and "we could, and the
    -- answer was no".
    status            VARCHAR(20)  NOT NULL DEFAULT 'pending',
    approved_by       VARCHAR(255) NULL,
    approved_at       TIMESTAMPTZ  NULL,
    decided_reason    TEXT         NULL,

    -- ── what was applied, and how to undo it ────────────────────────────────
    branch_id         UUID         NULL,
    applied_at        TIMESTAMPTZ  NULL,
    -- The artifact the change lands on, and the EXACT bytes to write back.
    -- Computed and stored at approval; a rollback re-applies this through the
    -- same merge primitive the change used, so there is one write path, not two.
    rollback_artifact_type VARCHAR(32) NULL,
    rollback_artifact_id   VARCHAR(64) NULL,
    rollback_patch    JSONB        NULL,

    -- ── the watch window ────────────────────────────────────────────────────
    -- A change is not healed because it deployed. It is healed because the
    -- thing that was wrong stopped being wrong, and stayed that way.
    watch_until       TIMESTAMPTZ  NULL,
    watch_metric      VARCHAR(64)  NULL,
    -- What the metric read BEFORE, and how far it may move the wrong way
    -- before this is treated as a regression rather than noise.
    watch_baseline    JSONB        NULL,
    watch_bound_pct   NUMERIC(6,3) NULL,
    outcome           JSONB        NULL,
    outcome_at        TIMESTAMPTZ  NULL,

    rolled_back_at    TIMESTAMPTZ  NULL,
    rollback_reason   TEXT         NULL,
    -- Opened when a healing change is rolled back automatically. Somebody has
    -- to look at a process that healed itself and got worse.
    incident_case_id  UUID         NULL,

    -- ── the chain ───────────────────────────────────────────────────────────
    -- Same construction as the F-01 ledger and the F-03 proofs: an episode's
    -- address includes its predecessor, so a rewritten history breaks a hash
    -- somebody else already holds.
    sequence          BIGINT       NOT NULL,
    prev_hash         VARCHAR(64)  NOT NULL,
    content_hash      VARCHAR(64)  NOT NULL,

    created_by        VARCHAR(255) NULL,
    created_at        TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    updated_at        TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS ix_heal_tenant  ON healing_episodes (tenant_id, status);
CREATE INDEX IF NOT EXISTS ix_heal_ct      ON healing_episodes (case_type_id, created_at DESC);
-- The sweep's query: what is still being watched, and is its window up.
CREATE INDEX IF NOT EXISTS ix_heal_watch   ON healing_episodes (status, watch_until);
CREATE UNIQUE INDEX IF NOT EXISTS ux_heal_sequence ON healing_episodes (sequence);
