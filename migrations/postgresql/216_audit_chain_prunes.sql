-- 216 — audit chain prune records (Oct_TODO §11h).
--
-- Retention may delete old `case_audit_log` rows. The chain rows that sealed
-- them stay, and `verify_chain` reported every one as `missing_audit_row` — an
-- install that enforced an audit retention period was told its audit log had
-- been tampered with, for good.
--
-- A row here says "these sealed rows were removed on purpose". It is sealed
-- into the chain itself (a `case_audit_log_chain` row points at it), names the
-- sequences it covers and carries a digest of their hashes. Verification then
-- reports those rows as pruned; a row deleted without one is still a break.
--
-- Additive: one new table. No rows means nothing was pruned, exactly as
-- before. `cutoff` / `recorded_at` are text because they are hashed.
--
-- Copyright (c) 2024-2026 Velaris Contributors
-- SPDX-License-Identifier: BSL-1.1

CREATE TABLE IF NOT EXISTS case_audit_log_chain_prunes (
    id            UUID         PRIMARY KEY,
    reason        VARCHAR(40)  NOT NULL,
    actor         VARCHAR(255) NOT NULL,
    cutoff        VARCHAR(40)  NULL,
    recorded_at   VARCHAR(40)  NOT NULL,
    pruned_count  INTEGER      NOT NULL,
    ranges        JSONB        NOT NULL,
    digest        VARCHAR(64)  NOT NULL,
    created_at    TIMESTAMPTZ  NOT NULL DEFAULT now()
);
