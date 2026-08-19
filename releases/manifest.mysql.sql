-- Velaris Release Manifest — MySQL variant (DB SDK Phase 1).
--
-- MySQL-dialect sibling of releases/manifest.sql. start-velaris.sh applies THIS file
-- when database: mysql; the Postgres manifest.sql is used for Postgres. Keep the two
-- in lockstep: when you append a feature INSERT to manifest.sql, add the same row here.
--
-- Dialect differences vs manifest.sql:
--   gen_random_uuid()                  → UUID()
--   ON CONFLICT (feature_key) DO UPDATE → ON DUPLICATE KEY UPDATE <same columns>
--     (a precise no-op on the feature_key UNIQUE key — same "keep existing row" semantics,
--      and unlike INSERT IGNORE it does not also swallow unrelated errors).
--
-- DO NOT replace VALUES(col) with the row-alias form (`... VALUES (...) AS n ... n.col`).
-- MySQL 8.4 raises warning 1287 saying VALUES() is deprecated and suggesting the alias,
-- but start-velaris.sh routes BOTH mysql and mariadb here (DB_FAMILY=mysql), and
-- MariaDB 11.8 rejects the alias form outright with ERROR 1064. VALUES() is the only
-- spelling both engines accept today; taking the 8.4 warning at face value would break
-- every MariaDB deploy. If MySQL ever removes VALUES(), this file has to split by engine.
-- Execution-verified 2026-08-15 on MySQL 8.4.11 and MariaDB 11.8.8.
--
-- Rules (same as manifest.sql):
--   - One INSERT per feature. Never delete existing INSERTs.
--   - feature_key must match the is_feature_enabled() flag name in code.
--   - Schema changes go in migrations/, never here.

-- ── Future releases: append below this line ───────────────────────────────────
-- Template:
--
-- INSERT INTO scheduled_releases
--   (id, feature_key, version, title, description, status, enabled, released_at, created_at, updated_at)
-- VALUES
--   (UUID(), 'feature_key', 'v1.0.0',
--    'Feature Title', 'One-line description',
--    'released', 'v1.0.0', NOW(), NOW(), NOW())
-- ON DUPLICATE KEY UPDATE
--   version     = VALUES(version),
--   title       = VALUES(title),
--   description = VALUES(description),
--   enabled     = IF(enabled IS NOT NULL, VALUES(version), enabled),
--   updated_at  = NOW();

-- ══════════════════════════════════════════════════════════════════════════════
-- v2.0.0 — feature flags shipped with the Velaris 2.0.0 platform release.
-- ══════════════════════════════════════════════════════════════════════════════

-- Marketplace
INSERT INTO scheduled_releases
  (id, feature_key, version, title, description, status, enabled, released_at, created_at, updated_at)
VALUES
  (UUID(), 'marketplace', 'v1.3.0',
   'Marketplace', 'Velaris App Store — browse, sandbox, and install platform extensions (official + community tiers)',
   'released', 'v1.0.0', NOW(), NOW(), NOW())
ON DUPLICATE KEY UPDATE
  version     = VALUES(version),
  title       = VALUES(title),
  description = VALUES(description),
  -- See manifest.sql: refresh the shipped version, never re-enable a feature
  -- an operator turned off; status/released_at keep the original release moment.
  enabled     = IF(enabled IS NOT NULL, VALUES(version), enabled),
  updated_at  = NOW();

-- Customer Accounts
INSERT INTO scheduled_releases
  (id, feature_key, version, title, description, status, enabled, released_at, created_at, updated_at)
VALUES
  (UUID(), 'customer_accounts', 'v1.0.2',
   'Customer Accounts', 'Persistent portal identity — OTP login, case history, GDPR erasure',
   'released', 'v1.0.1', NOW(), NOW(), NOW())
ON DUPLICATE KEY UPDATE
  version     = VALUES(version),
  title       = VALUES(title),
  description = VALUES(description),
  -- See manifest.sql: refresh the shipped version, never re-enable a feature
  -- an operator turned off; status/released_at keep the original release moment.
  enabled     = IF(enabled IS NOT NULL, VALUES(version), enabled),
  updated_at  = NOW();

-- HxDB Manager
INSERT INTO scheduled_releases
  (id, feature_key, version, title, description, status, enabled, released_at, created_at, updated_at)
VALUES
  (UUID(), 'hxdbmanager', 'v1.0.0',
   'HxDB Manager', 'AI-powered database operations panel inside Studio',
   'released', 'v1.0.0', NOW(), NOW(), NOW())
ON DUPLICATE KEY UPDATE
  version     = VALUES(version),
  title       = VALUES(title),
  description = VALUES(description),
  -- See manifest.sql: refresh the shipped version, never re-enable a feature
  -- an operator turned off; status/released_at keep the original release moment.
  enabled     = IF(enabled IS NOT NULL, VALUES(version), enabled),
  updated_at  = NOW();

-- HxDB Manager Security
INSERT INTO scheduled_releases
  (id, feature_key, version, title, description, status, enabled, released_at, created_at, updated_at)
VALUES
  (UUID(), 'hxdbmanager_security', 'v1.0.1',
   'HxDB Manager Security', 'Credential masking, breach detection, account auto-disable, DML before-image log',
   'released', 'v1.0.1', NOW(), NOW(), NOW())
ON DUPLICATE KEY UPDATE
  version     = VALUES(version),
  title       = VALUES(title),
  description = VALUES(description),
  -- See manifest.sql: refresh the shipped version, never re-enable a feature
  -- an operator turned off; status/released_at keep the original release moment.
  enabled     = IF(enabled IS NOT NULL, VALUES(version), enabled),
  updated_at  = NOW();

-- ══════════════════════════════════════════════════════════════════════════════
-- v2.2.0 — flags shipped after v2.0.0. HxSentinel completed 2026-07-22; voice
-- input and voice form-fill 2026-07-25 → 2026-07-28. Versions match releases.txt.
-- Voice degrades honestly without the `intelligence` extra (faster-whisper):
-- the status endpoint reports no engine and the mic renders nothing at all.
-- ══════════════════════════════════════════════════════════════════════════════

-- HxSentinel
INSERT INTO scheduled_releases
  (id, feature_key, version, title, description, status, enabled, released_at, created_at, updated_at)
VALUES
  (UUID(), 'hxsentinel', 'v1.9.0',
   'HxSentinel', 'Risk engine — ground-truth capture, rules + verify gate, similarity oversight, signal packs, local scoring model',
   'released', 'v1.9.0', NOW(), NOW(), NOW())
ON DUPLICATE KEY UPDATE
  version     = VALUES(version),
  title       = VALUES(title),
  description = VALUES(description),
  -- See manifest.sql: refresh the shipped version, never re-enable a feature
  -- an operator turned off; status/released_at keep the original release moment.
  enabled     = IF(enabled IS NOT NULL, VALUES(version), enabled),
  updated_at  = NOW();

-- Voice Input
INSERT INTO scheduled_releases
  (id, feature_key, version, title, description, status, enabled, released_at, created_at, updated_at)
VALUES
  (UUID(), 'voice_input', 'v1.1.0',
   'Voice Input', 'Sovereign speech-to-text — local Whisper dictation in Studio ask boxes, form fields, and the customer portal',
   'released', 'v1.1.0', NOW(), NOW(), NOW())
ON DUPLICATE KEY UPDATE
  version     = VALUES(version),
  title       = VALUES(title),
  description = VALUES(description),
  -- See manifest.sql: refresh the shipped version, never re-enable a feature
  -- an operator turned off; status/released_at keep the original release moment.
  enabled     = IF(enabled IS NOT NULL, VALUES(version), enabled),
  updated_at  = NOW();

-- Voice Form-Fill
INSERT INTO scheduled_releases
  (id, feature_key, version, title, description, status, enabled, released_at, created_at, updated_at)
VALUES
  (UUID(), 'voice_fill', 'v1.1.0',
   'Voice Form-Fill', 'Spoken answers proposed as form values, every candidate quoting the transcript verbatim; never auto-committed',
   'released', 'v1.1.0', NOW(), NOW(), NOW())
ON DUPLICATE KEY UPDATE
  version     = VALUES(version),
  title       = VALUES(title),
  description = VALUES(description),
  -- See manifest.sql: refresh the shipped version, never re-enable a feature
  -- an operator turned off; status/released_at keep the original release moment.
  enabled     = IF(enabled IS NOT NULL, VALUES(version), enabled),
  updated_at  = NOW();

-- ══════════════════════════════════════════════════════════════════════════════
-- Enterprise identity (SAML 2.0 SP + SCIM 2.0 + the provisioning spine),
-- 2026-08-16 — NO ENTRY, deliberately. Mirrors the PostgreSQL manifest.
--
-- This file governs `is_feature_enabled()` flags and enterprise identity uses
-- none: SAML is live when a complete, enabled `sso_providers` row with
-- `provider='saml'` exists, SCIM when an unrevoked `scim_tokens` row does, and
-- the provisioning spine is a library the existing `bulk-sync` endpoint already
-- calls. A flag on top would be a second switch that can disagree with the
-- first.
-- ══════════════════════════════════════════════════════════════════════════════

-- ══════════════════════════════════════════════════════════════════════════════
-- Omnichannel inbound and the RPA bot runner, 2026-08-16 — NO ENTRIES.
-- Mirrors the PostgreSQL manifest.
--
-- Omnichannel is on when a `channel_endpoints` row with a signing secret
-- exists; the bot runner is the ENV switch VELARIS_CASE_BOT_RUNNER_ENABLED
-- (default off, because the loop starts containers on the host). A flag on top
-- of either would be a second switch that can disagree with the first.
-- ══════════════════════════════════════════════════════════════════════════════

-- ══════════════════════════════════════════════════════════════════════════════
-- v2.4.0, 2026-08-19 — enterprise identity, omnichannel inbound, RPA bots,
-- the finished authorization rebuild, and the mobile export.
--
-- ONE version bump, and NO new feature keys. Every capability in this release
-- is switched on by its own configuration, and this file's rule is one switch
-- per capability:
--
--   * **SAML / SCIM** — on when an `sso_providers` row exists and is enabled;
--     with no row there is no button and every SCIM route answers 401.
--   * **omnichannel** — on when a `channel_endpoints` row exists with a signing
--     secret. Without one there is no webhook to call and no number to text.
--   * **bot_runner** — the ENV switch `VELARIS_CASE_BOT_RUNNER_ENABLED`,
--     default off, because the loop starts containers on the host and that is a
--     per-deployment operator choice. Env switches do not appear in this file.
--   * **App Builder** and the **authorization** work are core platform, not
--     gated capabilities.
--
-- A row on top of any of those would be a second switch that can disagree with
-- the first, and the failure mode is an operator who has configured the feature
-- correctly and cannot see why it does nothing.
-- ══════════════════════════════════════════════════════════════════════════════

-- HxSentinel — v1.9.0 → v1.10.0.
-- Not a new capability: the gate shipped in v2.2.0 was near-inert. Four of its
-- five rules read the SUBJECT case's own signals, which an open case never had,
-- so the rules could not fire on the cases they exist to hold. Signals are now
-- extracted at evaluation time. The version advances so an operator can tell a
-- deployment that holds cases from one that only appeared to.
INSERT INTO scheduled_releases
  (id, feature_key, version, title, description, status, enabled, released_at, created_at, updated_at)
VALUES
  (UUID(), 'hxsentinel', 'v1.10.0',
   'HxSentinel', 'Risk engine — ground-truth capture, rules + verify gate, similarity oversight, signal packs, local scoring model',
   'released', 'v1.10.0', NOW(), NOW(), NOW())
ON DUPLICATE KEY UPDATE
  version     = VALUES(version),
  title       = VALUES(title),
  description = VALUES(description),
  -- See manifest.sql: refresh the shipped version, never re-enable a feature
  -- an operator turned off; status/released_at keep the original release moment.
  enabled     = IF(enabled IS NOT NULL, VALUES(version), enabled),
  updated_at  = NOW();
