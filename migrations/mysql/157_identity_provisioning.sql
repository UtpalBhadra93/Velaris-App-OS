-- ============================================================================
-- Velaris — MySQL 8 / MariaDB 10.6+ : enterprise identity P0, the shared
-- provisioning spine (PostgreSQL migration 157).
--
-- Numbered file, not a baseline regeneration: `000_baseline_pg154.sql` is a
-- frozen snapshot and a regenerated baseline is silently SKIPPED on any install
-- that already recorded its filename — the change would never arrive.
--
-- A federated user is not one row. It fans out to velaris_users (login
-- identity), user_directory (profile), tenant_memberships (which tenant) and
-- operator_access_groups (authorization). Today `sso_callback` writes only the
-- first and `bulk-sync` only the second, so a leaver deactivated through one
-- path stays active on the other. SAML and SCIM would be a third and fourth
-- divergent writer; this migration backs the single spine they both call.
--
-- Dialect notes: CHAR(36) is what `GUID` emits outside PostgreSQL; JSON is what
-- `PortableJSON` emits; DATETIME(6) matches `MicroDateTime`. No IF NOT EXISTS
-- on ADD COLUMN or CREATE INDEX — MySQL 8 rejects both spellings, and the
-- runner already applies each file exactly once by filename.
-- ============================================================================

-- ── The stable IdP key ────────────────────────────────────────────────────
-- NOT `userName`: that changes on marriage or rename, and an identity keyed on
-- it forks one person into two accounts. Nullable, no backfill — a row without
-- an external_id is a locally-created account, which is the norm.
ALTER TABLE velaris_users  ADD COLUMN external_id VARCHAR(255) NULL;
ALTER TABLE user_directory ADD COLUMN external_id VARCHAR(255) NULL;

-- Not unique: two providers may legitimately assert the same opaque id, and a
-- unique index would turn that collision into a failed login rather than a
-- lookup that falls through to username.
CREATE INDEX ix_velaris_users_external  ON velaris_users (external_id);
CREATE INDEX ix_user_directory_external ON user_directory (external_id);

-- ── IdP group → access group ──────────────────────────────────────────────
-- An unmapped IdP group grants NOTHING. There is deliberately no fallback that
-- matches an access group by name: the IdP controls those strings, so a name
-- fallback would let whoever administers the directory mint Velaris
-- authorization by naming a group after one.
--
-- tenant_id holds the RESOLVED tenant UUID, not the caller's token. The
-- platform stores a slug in some tenant columns and a UUID in others
-- (`tenancy/identity.resolve_tenant`), and a mapping keyed on the ambiguous
-- form would resolve differently depending on how the row was written.
--
-- (tenant_id, source, external_group) = 36 + 20 + 255 chars; at utf8mb4 that is
-- 1,244 bytes, inside the 3072-byte InnoDB key limit.
CREATE TABLE IF NOT EXISTS identity_group_mappings (
    id              CHAR(36)     NOT NULL,
    tenant_id       CHAR(36)     NOT NULL,
    source          VARCHAR(20)  NOT NULL,
    external_group  VARCHAR(255) NOT NULL,
    access_group_id CHAR(36)     NOT NULL,
    priority        INT          NOT NULL DEFAULT 100,
    created_by      VARCHAR(255) NOT NULL DEFAULT 'system',
    created_at      DATETIME(6)  NOT NULL,
    PRIMARY KEY (id),
    UNIQUE KEY identity_group_mappings_uq (tenant_id, source, external_group),
    KEY ix_igm_tenant_source (tenant_id, source),
    CONSTRAINT identity_group_mappings_group_fk
        FOREIGN KEY (access_group_id) REFERENCES access_groups (id) ON DELETE CASCADE
);

-- ── Provisioning audit ────────────────────────────────────────────────────
-- Answers "why is this user in this group?" and, just as important, "the IdP
-- sent twelve groups and three of them are mapped to nothing".
--
-- action:  create | update | deactivate | group_add | group_remove | rejected
-- result:  ok | error — a refusal is recorded as action=rejected with the
--          reason in detail, never dropped.
CREATE TABLE IF NOT EXISTS identity_provision_log (
    id          CHAR(36)     NOT NULL,
    tenant_id   VARCHAR(255) NULL,
    source      VARCHAR(20)  NOT NULL,
    external_id VARCHAR(255) NULL,
    username    VARCHAR(255) NULL,
    action      VARCHAR(30)  NOT NULL,
    result      VARCHAR(20)  NOT NULL,
    detail      JSON         NOT NULL,
    created_at  DATETIME(6)  NOT NULL,
    PRIMARY KEY (id),
    KEY ix_ipl_tenant_time (tenant_id, created_at),
    KEY ix_ipl_external (external_id)
);
