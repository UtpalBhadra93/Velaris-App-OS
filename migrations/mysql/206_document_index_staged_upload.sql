-- 206 — background indexing for `POST /hxnexus/documents/upload` (Oct item 7).
--
-- See postgresql/206 for the why. Three nullable columns; the synchronous path
-- and every job queued by the case-wide route leave them NULL.
--
-- Dialect note: `ADD COLUMN` is plain, matching 202/199/176: MySQL 8.4 has no
-- `ADD COLUMN IF NOT EXISTS` (MariaDB does, but one file serves both), so
-- once-only application is the migration runner's ledger.
--
-- Copyright (c) 2024-2026 Velaris Contributors
-- SPDX-License-Identifier: BSL-1.1

ALTER TABLE document_index_jobs
    ADD COLUMN staged_storage_key  VARCHAR(512) NULL,
    ADD COLUMN staged_filename     VARCHAR(512) NULL,
    ADD COLUMN staged_content_type VARCHAR(255) NULL;
