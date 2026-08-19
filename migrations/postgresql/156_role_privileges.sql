-- Privilege authz Phase 6: role -> privilege, in DATA.
--
-- The model has always been "actions in code, privileges in data, roles are
-- bundles". The first two shipped in Phases 1-5; the third never did. A platform
-- role's privileges come from `SEEDED_ROLE_PRIVILEGES`, which is DERIVED IN CODE
-- from each action's `legacy_roles` — and `legacy_roles` is the parity oracle
-- recording what `require_role` used to answer, not a policy knob. Editing it to
-- change who may do what corrupts the proof that the migration preserved
-- behaviour. So until this table exists there is no way to grant a platform role
-- an action without a code change and a deploy.
--
-- ADDITIVE TO THE SEED, NEVER A REPLACEMENT. A row here adds entries to whatever
-- the seed already grants that role; it does not define the role's privileges
-- outright. That is deliberate and it is the whole safety property:
--
--   * the cache that serves this table is in-process, and an unloaded or failed
--     cache is therefore EMPTY. Under "replace" semantics an empty cache means
--     every role holds nothing — a total, silent, platform-wide lockout during
--     the first seconds of boot or after any refresh failure. Under "additive"
--     it means exactly today's behaviour, which is the only acceptable way for
--     an authorization cache to fail;
--   * revocation is still expressible, as an explicit `{"effect": "deny"}`
--     entry. The evaluator has supported deny-wins since Phase 1 and no seeded
--     role uses it, so this costs nothing and makes every revocation a row
--     somebody wrote on purpose rather than an absence.
--
-- `role` is a platform role name as it appears in `velaris_users.roles`
-- (admin, manager, developer, ...), NOT an `access_roles` row. Those are a
-- different, case-level concept and keep their own privilege column.
--
-- Global, not tenant-scoped: platform roles are global today (they live in a
-- JSON column on the account, with no tenant dimension), and inventing a tenant
-- key here would imply a scoping the rest of the model does not honour.

CREATE TABLE IF NOT EXISTS role_privileges (
    role        VARCHAR(64)  PRIMARY KEY,
    privileges  JSONB        NOT NULL DEFAULT '[]'::jsonb,
    updated_by  VARCHAR(255) NOT NULL,
    updated_at  TIMESTAMPTZ  NOT NULL DEFAULT now()
);

COMMENT ON TABLE role_privileges IS
    'Phase 6: privilege entries ADDED to a platform role on top of the code-'
    'derived seed. Never a replacement — an empty or unloaded cache must mean '
    '"today''s behaviour", never "nobody holds anything".';

COMMENT ON COLUMN role_privileges.role IS
    'Platform role name as it appears in velaris_users.roles. Not an access_roles id.';

COMMENT ON COLUMN role_privileges.privileges IS
    'JSON array of {"resource", "actions", "effect"?} entries. effect defaults '
    'to allow; "deny" wins over any allow, which is how a seeded grant is revoked.';
