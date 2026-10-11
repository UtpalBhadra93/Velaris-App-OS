-- 217 — repair: scheduled_releases.enabled left BOOLEAN by an early 080.
--
-- Migration 080 turns `enabled` from BOOLEAN into a version string. As first
-- shipped it failed on any install holding 076's seed rows (enabled = FALSE,
-- NOT NULL still in place), and the update script of that time recorded a failed
-- migration as applied — so 080, corrected since, never ran again there.
--
-- What that install sees: /releases/flags answers `false` instead of a version
-- or null, releases/manifest.sql fails on every start (a version string into a
-- boolean column), and publishing a feature answers 500. Seen 2026-10-11 on an
-- install whose Marketplace never appeared in the sidebar.
--
-- This converts the column ONLY where it is still boolean. On an install where
-- 080 worked it changes nothing. It is re-runnable.
--
-- Every existing row becomes ON (its version, or 'released') — FALSE rows too.
-- That is not this migration switching a feature on; it is keeping what such an
-- install has been doing all along. The backend's test is "enabled IS NOT NULL",
-- and a boolean FALSE is not NULL: on a broken install the server has treated
-- every listed feature — Marketplace, Customer Accounts — as enabled since the
-- day 080 failed, people have used them, and only Studio's sidebar (which tests
-- truthiness) hid the entry. Mapping FALSE to NULL, as 080 does on a healthy
-- install, would switch those features OFF on upgrade, under running portal
-- logins and installed packages. An upgrade must not do that.
--
-- What changes for such an install: the sidebar now shows what the server was
-- already serving. An operator who wants one off uses
-- POST /api/v1/releases/{id}/rollback.
--
-- PostgreSQL only: the MySQL/MariaDB baseline created the column as VARCHAR(20).
--
-- Copyright (c) 2024-2026 Velaris Contributors
-- SPDX-License-Identifier: BSL-1.1

DO $$
BEGIN
  IF EXISTS (
    SELECT 1 FROM information_schema.columns
     WHERE table_schema = current_schema()
       AND table_name   = 'scheduled_releases'
       AND column_name  = 'enabled'
       AND data_type    = 'boolean'
  ) THEN
    ALTER TABLE scheduled_releases ALTER COLUMN enabled DROP DEFAULT;
    ALTER TABLE scheduled_releases ALTER COLUMN enabled DROP NOT NULL;
    ALTER TABLE scheduled_releases
      ALTER COLUMN enabled TYPE VARCHAR(20)
      USING (COALESCE(version, 'released'));
  END IF;
END $$;
