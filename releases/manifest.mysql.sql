-- Velaris Release Manifest — MySQL variant (DB SDK Phase 1).
--
-- MySQL-dialect sibling of releases/manifest.sql. start-velaris.sh applies THIS file
-- when database: mysql; the Postgres manifest.sql is used for Postgres. Keep the two
-- in lockstep: when you append a feature INSERT to manifest.sql, add the same row here.
--
-- Dialect differences vs manifest.sql:
--   gen_random_uuid()                  → UUID()
--   ON CONFLICT (feature_key) DO NOTHING → ON DUPLICATE KEY UPDATE feature_key = feature_key
--     (a precise no-op on the feature_key UNIQUE key — same "keep existing row" semantics,
--      and unlike INSERT IGNORE it does not also swallow unrelated errors).
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
-- ON DUPLICATE KEY UPDATE feature_key = feature_key;

-- ══════════════════════════════════════════════════════════════════════════════
-- v2.0.0 — feature flags shipped with the Velaris 2.0.0 platform release.
-- ══════════════════════════════════════════════════════════════════════════════

-- Marketplace
INSERT INTO scheduled_releases
  (id, feature_key, version, title, description, status, enabled, released_at, created_at, updated_at)
VALUES
  (UUID(), 'marketplace', 'v1.0.0',
   'Marketplace', 'Velaris App Store — browse, sandbox, and install platform extensions (official + community tiers)',
   'released', 'v1.0.0', NOW(), NOW(), NOW())
ON DUPLICATE KEY UPDATE feature_key = feature_key;

-- Customer Accounts
INSERT INTO scheduled_releases
  (id, feature_key, version, title, description, status, enabled, released_at, created_at, updated_at)
VALUES
  (UUID(), 'customer_accounts', 'v1.0.1',
   'Customer Accounts', 'Persistent portal identity — OTP login, case history, GDPR erasure',
   'released', 'v1.0.1', NOW(), NOW(), NOW())
ON DUPLICATE KEY UPDATE feature_key = feature_key;

-- HxDB Manager
INSERT INTO scheduled_releases
  (id, feature_key, version, title, description, status, enabled, released_at, created_at, updated_at)
VALUES
  (UUID(), 'hxdbmanager', 'v1.0.0',
   'HxDB Manager', 'AI-powered database operations panel inside Studio',
   'released', 'v1.0.0', NOW(), NOW(), NOW())
ON DUPLICATE KEY UPDATE feature_key = feature_key;

-- HxDB Manager Security
INSERT INTO scheduled_releases
  (id, feature_key, version, title, description, status, enabled, released_at, created_at, updated_at)
VALUES
  (UUID(), 'hxdbmanager_security', 'v1.0.1',
   'HxDB Manager Security', 'Credential masking, breach detection, account auto-disable, DML before-image log',
   'released', 'v1.0.1', NOW(), NOW(), NOW())
ON DUPLICATE KEY UPDATE feature_key = feature_key;

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
ON DUPLICATE KEY UPDATE feature_key = feature_key;

-- Voice Input
INSERT INTO scheduled_releases
  (id, feature_key, version, title, description, status, enabled, released_at, created_at, updated_at)
VALUES
  (UUID(), 'voice_input', 'v1.1.0',
   'Voice Input', 'Sovereign speech-to-text — local Whisper dictation in Studio ask boxes, form fields, and the customer portal',
   'released', 'v1.1.0', NOW(), NOW(), NOW())
ON DUPLICATE KEY UPDATE feature_key = feature_key;

-- Voice Form-Fill
INSERT INTO scheduled_releases
  (id, feature_key, version, title, description, status, enabled, released_at, created_at, updated_at)
VALUES
  (UUID(), 'voice_fill', 'v1.1.0',
   'Voice Form-Fill', 'Spoken answers proposed as form values, every candidate quoting the transcript verbatim; never auto-committed',
   'released', 'v1.1.0', NOW(), NOW(), NOW())
ON DUPLICATE KEY UPDATE feature_key = feature_key;
