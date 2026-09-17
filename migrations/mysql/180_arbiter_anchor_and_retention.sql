-- HxArbiter — anchoring the DECISION chain, and giving it a retention horizon.
--
-- Two additive columns and nothing else. Both default to the behaviour an
-- existing install already has, so an upgrade that changes nothing for an
-- operator breaks nothing for them.
--
-- ── 1. `audit_anchors.chain` ────────────────────────────────────────────────
--
-- `hxarbiter/__init__.py` has said "one anchored decision log" since ARB-A, and
-- it was not true: `audit_anchors` held RFC-3161 receipts over the CASE audit
-- chain only, and `arbiter_decision_chain` was hash-chained but never
-- externally timestamped. Hash-chaining alone is tamper-EVIDENT against a
-- writer; it is not evidence against someone who can rewrite the whole chain,
-- which is the party a sovereignty product is asked about.
--
-- A discriminator rather than a second table: the two chains are anchored the
-- same way, verified the same way, and an operator asking "when was this last
-- anchored" should not have to know which one they mean first.
--
-- DEFAULT 'audit' is load-bearing — every existing row IS an audit-chain
-- anchor, and backfilling them as anything else would misdescribe history.
-- MySQL 8.4 and MariaDB 11.8 have no `ADD COLUMN IF NOT EXISTS`, and a plain
-- ADD fails the whole migration on a re-run. The information_schema guard is
-- the portable way to keep this re-runnable, which the platform requires of
-- every migration.
SET @c := (SELECT COUNT(*) FROM information_schema.COLUMNS
           WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'audit_anchors'
             AND COLUMN_NAME = 'chain');
SET @s := IF(@c = 0,
    'ALTER TABLE audit_anchors ADD COLUMN chain VARCHAR(32) NOT NULL DEFAULT ''audit''',
    'DO 0');
PREPARE stmt FROM @s; EXECUTE stmt; DEALLOCATE PREPARE stmt;

-- The "has the tip moved since we last anchored" query is per chain now.
SET @i := (SELECT COUNT(*) FROM information_schema.STATISTICS
           WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'audit_anchors'
             AND INDEX_NAME = 'idx_audit_anchors_chain_time');
SET @s := IF(@i = 0,
    'CREATE INDEX idx_audit_anchors_chain_time ON audit_anchors (chain, anchored_at)',
    'DO 0');
PREPARE stmt FROM @s; EXECUTE stmt; DEALLOCATE PREPARE stmt;

-- ── 2. `arbiter_namespaces.decision_retention_days` ─────────────────────────
--
-- The decision log had no retention of any kind: nothing in `retention.py`
-- touched `arbiter_decisions`, so it grew without bound and no erasure request
-- could ever reach it.
--
-- NULL means **keep forever**, which is exactly what every install does today.
-- An operator opts a namespace in; an upgrade never does it for them. Purging
-- evidence is a decision with legal weight and is not an update's to make.
SET @c := (SELECT COUNT(*) FROM information_schema.COLUMNS
           WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'arbiter_namespaces'
             AND COLUMN_NAME = 'decision_retention_days');
SET @s := IF(@c = 0,
    'ALTER TABLE arbiter_namespaces ADD COLUMN decision_retention_days INT NULL',
    'DO 0');
PREPARE stmt FROM @s; EXECUTE stmt; DEALLOCATE PREPARE stmt;
