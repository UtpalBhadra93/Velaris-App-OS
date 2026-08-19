-- Enterprise identity P0 — the shared provisioning spine.
--
-- A federated user is not one row. It fans out to four tables:
--
--     velaris_users          login identity, is_active, sso_subject
--     user_directory         profile, manager, skills, timezone
--     tenant_memberships     which tenant
--     operator_access_groups authorization
--
-- Today `sso_callback` writes only the first and `bulk-sync` only the second,
-- so a leaver deactivated through one path stays active on the other. That is
-- already happening — `user_directory.update_entry` carries a partial patch
-- (`_sync_auth_active`) that mirrors is_active one way only. Adding SAML or
-- SCIM before this would add a third and fourth divergent writer.
--
-- This migration adds what the spine needs and nothing else. The SAML replay
-- table and the SCIM token table belong to P1 and P2 and arrive with them.

-- ── The stable IdP key ────────────────────────────────────────────────────
-- NOT `userName`: that changes on marriage or rename, and an identity keyed on
-- it forks one person into two accounts. Nullable with no backfill — a row
-- without an external_id is a locally-created account, which is the norm.
ALTER TABLE velaris_users  ADD COLUMN IF NOT EXISTS external_id VARCHAR(255);
ALTER TABLE user_directory ADD COLUMN IF NOT EXISTS external_id VARCHAR(255);

-- Not unique: two providers may legitimately assert the same opaque id, and a
-- unique index would turn that collision into a failed login rather than a
-- lookup that falls through to username.
CREATE INDEX IF NOT EXISTS ix_velaris_users_external  ON velaris_users(external_id);
CREATE INDEX IF NOT EXISTS ix_user_directory_external ON user_directory(external_id);

COMMENT ON COLUMN velaris_users.external_id IS
    'Stable identifier asserted by the external IdP (SCIM externalId / SAML '
    'NameID). Never userName, which changes on rename.';

-- ── IdP group → access group ──────────────────────────────────────────────
-- An unmapped IdP group grants NOTHING. There is deliberately no fallback that
-- matches an access group by name: the IdP controls those strings, so a name
-- fallback would let whoever administers the directory mint Velaris
-- authorization by naming a group after one.
--
-- tenant_id is the resolved tenant UUID, not the caller's token. The rest of
-- the platform stores a slug in some columns and a UUID in others
-- (`tenancy/identity.resolve_tenant`), and a mapping table keyed on the
-- ambiguous form would resolve differently depending on how the row was
-- written.
CREATE TABLE IF NOT EXISTS identity_group_mappings (
    id              UUID         PRIMARY KEY,
    tenant_id       UUID         NOT NULL,
    source          VARCHAR(20)  NOT NULL,
    external_group  VARCHAR(255) NOT NULL,
    access_group_id UUID         NOT NULL REFERENCES access_groups(id) ON DELETE CASCADE,
    priority        INTEGER      NOT NULL DEFAULT 100,
    created_by      VARCHAR(255) NOT NULL DEFAULT 'system',
    created_at      TIMESTAMP(6) NOT NULL DEFAULT now(),
    CONSTRAINT identity_group_mappings_uq UNIQUE (tenant_id, source, external_group)
);

CREATE INDEX IF NOT EXISTS ix_igm_tenant_source
    ON identity_group_mappings(tenant_id, source);

COMMENT ON COLUMN identity_group_mappings.source IS
    'Which provisioning entry point may use this mapping: saml | scim | oidc | '
    'bulk_sync. A mapping is not shared across protocols by accident.';

-- ── Provisioning audit ────────────────────────────────────────────────────
-- Answers "why is this user in this group?" and, just as important, "the IdP
-- sent twelve groups and three of them are mapped to nothing".
CREATE TABLE IF NOT EXISTS identity_provision_log (
    id          UUID         PRIMARY KEY,
    tenant_id   VARCHAR(255),
    source      VARCHAR(20)  NOT NULL,
    external_id VARCHAR(255),
    username    VARCHAR(255),
    action      VARCHAR(30)  NOT NULL,
    result      VARCHAR(20)  NOT NULL,
    detail      JSONB        NOT NULL DEFAULT '{}'::jsonb,
    created_at  TIMESTAMP(6) NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS ix_ipl_tenant_time
    ON identity_provision_log(tenant_id, created_at);
CREATE INDEX IF NOT EXISTS ix_ipl_external
    ON identity_provision_log(external_id);

COMMENT ON COLUMN identity_provision_log.action IS
    'create | update | deactivate | group_add | group_remove | rejected';
COMMENT ON COLUMN identity_provision_log.result IS
    'ok | error. A rejection is result=error with action=rejected and a reason '
    'in detail — refusals are recorded, not dropped.';
