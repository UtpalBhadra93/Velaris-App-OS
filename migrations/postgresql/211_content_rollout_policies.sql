-- 211 — staged rollout, phases 2 and 3 (Oct item 18a).
--
-- WHY. Phase 1 (210) chose a version per new case. This adds, per rollout and
-- only when somebody sets it:
--   * SHADOW — the candidate's rules replayed over baseline cases with no effect
--     (HxReplay's read-only engine); results in content_rollout_shadow;
--   * TARGETING — a pilot list, an exclude list, a field match;
--   * a scheduled RAMP (10 → 25 → 50 …) and a GUARD that holds the ramp or rolls
--     the candidate back when it does measurably worse than the baseline.
-- The A/B outcome comparison needs no table: it is computed from the cases.
--
-- WHAT DOES NOT CHANGE. Additive: two new tables. A rollout with no policy row
-- behaves exactly as in 210; the sweep that reads this table does nothing when
-- it is empty.
--
-- RE-RUNNABLE. `IF NOT EXISTS` throughout; no ALTER, so the same holds on MySQL.
--
-- Copyright (c) 2024-2026 Velaris Contributors
-- SPDX-License-Identifier: BSL-1.1

CREATE TABLE IF NOT EXISTS content_rollout_policies (
    rollout_id     UUID PRIMARY KEY REFERENCES content_rollouts(id) ON DELETE CASCADE,
    tenant_id      UUID         NOT NULL,
    shadow         BOOLEAN      NOT NULL DEFAULT FALSE,
    targeting      JSONB        NULL,
    ramp           JSONB        NULL,
    guard          JSONB        NULL,
    next_step_at   TIMESTAMPTZ  NULL,
    -- ok | held | tripped
    guard_state    VARCHAR(10)  NOT NULL DEFAULT 'ok',
    guard_detail   JSONB        NULL,
    updated_by     VARCHAR(255) NULL,
    updated_at     TIMESTAMPTZ  NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_crp_next ON content_rollout_policies (next_step_at);

-- One shadow evaluation per baseline case: what the candidate's rules would
-- have done differently. Never written anywhere else.
CREATE TABLE IF NOT EXISTS content_rollout_shadow (
    case_id        UUID PRIMARY KEY REFERENCES case_instances(id) ON DELETE CASCADE,
    rollout_id     UUID         NOT NULL REFERENCES content_rollouts(id) ON DELETE CASCADE,
    tenant_id      UUID         NOT NULL,
    -- determinate | indeterminate | estimated | error
    determinacy    VARCHAR(20)  NOT NULL,
    diverged       BOOLEAN      NOT NULL DEFAULT FALSE,
    detail         JSONB        NULL,
    evaluated_at   TIMESTAMPTZ  NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_crs_rollout ON content_rollout_shadow (rollout_id, diverged);
