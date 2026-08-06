-- Rename helix_settings → velaris_settings (brand alignment).
--
-- Deliberately NOT touching `helix_processes` / `helix_process_instances`:
-- those belong to the ENGINE (`engine/helix_engine/db/models.py`), which keeps
-- the Helix name by design. Renaming them here would desynchronise the engine's
-- own models from the schema it owns.
--
-- No foreign keys reference this table; only its primary-key index needs a
-- matching name so upgraded and fresh installs do not drift apart.

ALTER TABLE IF EXISTS helix_settings RENAME TO velaris_settings;

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class WHERE relname = 'helix_settings_pkey') THEN
        ALTER INDEX helix_settings_pkey RENAME TO velaris_settings_pkey;
    END IF;
END $$;
