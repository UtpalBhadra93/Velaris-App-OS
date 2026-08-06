-- Velaris Release Manifest
--
-- This file is the single source of truth for which features are active
-- on a Velaris instance. start-velaris.sh compares the INSERT count here
-- against the row count in scheduled_releases — if they differ, this file
-- is applied automatically.
--
-- Rules:
--   - One INSERT per feature. Never delete existing INSERTs.
--   - To release a new feature: append a new INSERT block at the bottom.
--   - ON CONFLICT DO NOTHING: existing rows are preserved, only new ones inserted.
--   - Do not use this file for schema changes — use migrations/ for that.
--
-- Format:
--   feature_key  must match the flag name used in is_feature_enabled() in code
--   version      matches the entry in releases.txt on the website server
--   enabled      same as version — activates the feature on install

-- ── v1.0.0 — no hidden feature flags; all core features ship by default ───────
-- (append future release INSERT blocks below when ready to ship)

-- ── Future releases: append below this line ───────────────────────────────────
-- Template:
--
-- INSERT INTO scheduled_releases
--   (id, feature_key, version, title, description, status, enabled, released_at, created_at, updated_at)
-- VALUES
--   (gen_random_uuid(), 'feature_key', 'v1.0.0',
--    'Feature Title', 'One-line description',
--    'released', 'v1.0.0', NOW(), NOW(), NOW())
-- ON CONFLICT (feature_key) DO NOTHING;

-- ══════════════════════════════════════════════════════════════════════════════
-- v2.0.0 — feature flags shipped with the Velaris 2.0.0 platform release.
-- Versions match releases.txt. Only is_feature_enabled() keys appear here;
-- the platform itself ("velaris") and marketplace apps ("hxtest", install-gated
-- via marketplace_installs) are NOT feature flags and are documented in
-- releases/notes/v2.0.0.md, not inserted below.
-- ══════════════════════════════════════════════════════════════════════════════

-- Marketplace
INSERT INTO scheduled_releases
  (id, feature_key, version, title, description, status, enabled, released_at, created_at, updated_at)
VALUES
  (gen_random_uuid(), 'marketplace', 'v1.3.0',
   'Marketplace', 'Velaris App Store — browse, sandbox, and install platform extensions (official + community tiers)',
   'released', 'v1.0.0', NOW(), NOW(), NOW())
ON CONFLICT (feature_key) DO NOTHING;

-- Customer Accounts
INSERT INTO scheduled_releases
  (id, feature_key, version, title, description, status, enabled, released_at, created_at, updated_at)
VALUES
  (gen_random_uuid(), 'customer_accounts', 'v1.0.2',
   'Customer Accounts', 'Persistent portal identity — OTP login, case history, GDPR erasure',
   'released', 'v1.0.1', NOW(), NOW(), NOW())
ON CONFLICT (feature_key) DO NOTHING;

-- HxDB Manager
INSERT INTO scheduled_releases
  (id, feature_key, version, title, description, status, enabled, released_at, created_at, updated_at)
VALUES
  (gen_random_uuid(), 'hxdbmanager', 'v1.0.0',
   'HxDB Manager', 'AI-powered database operations panel inside Studio',
   'released', 'v1.0.0', NOW(), NOW(), NOW())
ON CONFLICT (feature_key) DO NOTHING;

-- HxDB Manager Security
INSERT INTO scheduled_releases
  (id, feature_key, version, title, description, status, enabled, released_at, created_at, updated_at)
VALUES
  (gen_random_uuid(), 'hxdbmanager_security', 'v1.0.1',
   'HxDB Manager Security', 'Credential masking, breach detection, account auto-disable, DML before-image log',
   'released', 'v1.0.1', NOW(), NOW(), NOW())
ON CONFLICT (feature_key) DO NOTHING;

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
  (gen_random_uuid(), 'hxsentinel', 'v1.9.0',
   'HxSentinel', 'Risk engine — ground-truth capture, rules + verify gate, similarity oversight, signal packs, local scoring model',
   'released', 'v1.9.0', NOW(), NOW(), NOW())
ON CONFLICT (feature_key) DO NOTHING;

-- Voice Input
INSERT INTO scheduled_releases
  (id, feature_key, version, title, description, status, enabled, released_at, created_at, updated_at)
VALUES
  (gen_random_uuid(), 'voice_input', 'v1.1.0',
   'Voice Input', 'Sovereign speech-to-text — local Whisper dictation in Studio ask boxes, form fields, and the customer portal',
   'released', 'v1.1.0', NOW(), NOW(), NOW())
ON CONFLICT (feature_key) DO NOTHING;

-- Voice Form-Fill
INSERT INTO scheduled_releases
  (id, feature_key, version, title, description, status, enabled, released_at, created_at, updated_at)
VALUES
  (gen_random_uuid(), 'voice_fill', 'v1.1.0',
   'Voice Form-Fill', 'Spoken answers proposed as form values, every candidate quoting the transcript verbatim; never auto-committed',
   'released', 'v1.1.0', NOW(), NOW(), NOW())
ON CONFLICT (feature_key) DO NOTHING;

-- ══════════════════════════════════════════════════════════════════════════════
-- v2.3.0 — NO NEW ENTRIES, deliberately.
--
-- Recorded explicitly because this file was found three flags behind at v2.2.0,
-- and "did someone forget again?" is otherwise unanswerable without auditing
-- the whole release.
--
-- v2.3.0 shipped HxConsent, HxPrivacy, HxLedger, HxDR, HxStatus, event
-- sourcing, the CDC event bus, CRDT convergence, the Temporal interpreter,
-- LLMOps and `ai_action` — and **none of them is an is_feature_enabled() flag**,
-- which is the only thing this file governs. They are either always-on routes
-- gated by role, or switched by environment variable:
--
--   VELARIS_CASE_LEDGER_POSTING_ENABLED   (default ON)
--   VELARIS_CASE_HEALTH_SAMPLER_ENABLED   (default ON)
--   VELARIS_CASE_EVENT_SOURCING_ENABLED   (default OFF)
--   VELARIS_CASE_EVENT_BUS_ENABLED        (default OFF)
--   VELARIS_CASE_TEMPORAL_INTERPRETER_ENABLED (default OFF)
--   VELARIS_CASE_HXSANDBOX_ENABLED        (default OFF)
--
-- Env switches are per-deployment operator choices; a database row would make
-- them look centrally released, which they are not. Verify with:
--   grep -rho 'is_feature_enabled("[a-z_]*"' services/case-service | sort -u
-- ══════════════════════════════════════════════════════════════════════════════
