-- Rename the users table helix_users → velaris_users (brand alignment).
--
-- Runs AFTER 065/069, which created and seeded `helix_users`. Those migrations
-- are deliberately left untouched: they are the historical record of what was
-- applied, and rewriting an already-applied migration makes a fresh install and
-- an upgraded one diverge in ways nothing would catch.
--
-- Foreign keys survive automatically — PostgreSQL constraints reference the
-- table by OID, not by name, so `auth_otp.auth_otp_user_id_fkey` follows the
-- rename without being touched.
--
-- Indexes do NOT rename themselves. Left alone they would keep `helix_` names
-- forever on an upgraded install while a fresh one (built from the renamed
-- table) gets `velaris_` names — the two populations would drift apart in a way
-- that only surfaces when someone compares schemas. Each is renamed explicitly,
-- guarded so a partially-applied run can be repeated.

ALTER TABLE IF EXISTS helix_users RENAME TO velaris_users;

DO $$
BEGIN
    -- Implicit constraint-backed indexes (primary key + uniques).
    IF EXISTS (SELECT 1 FROM pg_class WHERE relname = 'helix_users_pkey') THEN
        ALTER INDEX helix_users_pkey RENAME TO velaris_users_pkey;
    END IF;
    IF EXISTS (SELECT 1 FROM pg_class WHERE relname = 'helix_users_username_key') THEN
        ALTER INDEX helix_users_username_key RENAME TO velaris_users_username_key;
    END IF;
    IF EXISTS (SELECT 1 FROM pg_class WHERE relname = 'helix_users_email_key') THEN
        ALTER INDEX helix_users_email_key RENAME TO velaris_users_email_key;
    END IF;

    -- Explicit indexes declared on the model.
    IF EXISTS (SELECT 1 FROM pg_class WHERE relname = 'ix_helix_users_email') THEN
        ALTER INDEX ix_helix_users_email RENAME TO ix_velaris_users_email;
    END IF;
    IF EXISTS (SELECT 1 FROM pg_class WHERE relname = 'ix_helix_users_username') THEN
        ALTER INDEX ix_helix_users_username RENAME TO ix_velaris_users_username;
    END IF;
    IF EXISTS (SELECT 1 FROM pg_class WHERE relname = 'ix_helix_users_sso') THEN
        ALTER INDEX ix_helix_users_sso RENAME TO ix_velaris_users_sso;
    END IF;
END $$;
