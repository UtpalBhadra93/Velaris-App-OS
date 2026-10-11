-- 206 — background indexing for `POST /hxnexus/documents/upload` (Oct item 7).
--
-- WHY. Migration 203 queued indexing for documents that already exist in the
-- document store. The upload route stores nothing: it indexes the bytes inside
-- the request under a fresh id, so a scanned upload holds the request for the
-- OCR timeout. To queue it, the bytes must wait somewhere, and the job must say
-- where. These three columns are that "where".
--
-- WHAT DOES NOT CHANGE. Synchronous upload stays the default and never writes
-- these columns. A job queued by the case-wide route leaves them NULL and runs
-- exactly as before. The previous version's code, still running while this
-- applies, neither reads nor writes them.
--
-- RE-RUNNABLE. `ADD COLUMN IF NOT EXISTS`.
--
-- Copyright (c) 2024-2026 Velaris Contributors
-- SPDX-License-Identifier: BSL-1.1

ALTER TABLE document_index_jobs
    ADD COLUMN IF NOT EXISTS staged_storage_key  VARCHAR(512) NULL,
    ADD COLUMN IF NOT EXISTS staged_filename     VARCHAR(512) NULL,
    ADD COLUMN IF NOT EXISTS staged_content_type VARCHAR(255) NULL;
