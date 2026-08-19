-- ============================================================================
-- Velaris — MySQL 8 / MariaDB 10.6+ : role → privilege store
-- (PostgreSQL migration 156).
--
-- Numbered file, not a baseline regeneration: `000_baseline_pg154.sql` is a
-- frozen snapshot through PG 154, and a regenerated baseline is silently SKIPPED
-- on any install that already recorded it — the change would simply never
-- arrive. See the header of `scripts/gen_mysql_baseline.py`.
--
-- Phase 6 of privilege-based authorization. Until this table exists, a platform
-- role's privileges come only from `SEEDED_ROLE_PRIVILEGES`, derived in code
-- from each action's `legacy_roles` — the parity oracle recording what
-- `require_role` used to answer, not a policy knob. So changing who may do what
-- required a code change and a deploy.
--
-- ADDITIVE TO THE SEED, NEVER A REPLACEMENT. The cache serving this table is
-- in-process, so an unloaded or failed cache is empty. Under "replace" that
-- would mean every role holds nothing — a silent platform-wide lockout for the
-- first seconds of every boot and for the whole of any refresh failure. Additive
-- makes an empty cache mean "exactly the pre-Phase-6 behaviour", the only
-- acceptable way for an authorization cache to fail. Revocation stays
-- expressible as an explicit {"effect": "deny"} entry (deny-wins since Phase 1,
-- and no seeded role uses it).
--
-- `role` is a platform role name from `velaris_users.roles`, NOT an
-- `access_roles` row — those are the case-level concept with their own column.
-- Global rather than tenant-scoped, because platform roles have no tenant
-- dimension today and inventing one here would imply a scoping nothing honours.
--
-- JSON, not JSONB: MySQL/MariaDB spell it JSON, which is what `PortableJSON`
-- emits outside PostgreSQL. VARCHAR(64) PK = 256 bytes at utf8mb4, far inside
-- the 3072-byte InnoDB key limit.
-- ============================================================================

CREATE TABLE IF NOT EXISTS role_privileges (
    role        VARCHAR(64)  NOT NULL,
    privileges  JSON         NOT NULL,
    updated_by  VARCHAR(255) NOT NULL,
    updated_at  DATETIME     NOT NULL,
    PRIMARY KEY (role)
);
