-- 203 — document indexing off the request (the queue half).
--
-- WHY. `hxnexus/service.py::index_document` is awaited INSIDE the request, and
-- `POST /hxnexus/cases/{id}/documents/index` loops every document on the case.
-- With OCR switched on (migration-free, shipped 2026-09-09) a scanned page can
-- take `OCR_TIMEOUT_SECONDS` — and that number is multiplied by the number of
-- scans in the case, not paid once. It was halved from 60 to 30 to bound the
-- damage; bounding it is not fixing it. A case with twelve scans is a request
-- that either times out at the gateway or holds a worker for minutes.
--
-- WHAT THIS IS. One queue table. A caller may ask for the work to be done
-- LATER, and a scheduler action drains it — the same shape `calendar_feed` and
-- `audit_stream` already use, for the same reason: a second always-on loop is a
-- second thing to configure, a second thing to forget, and a second place for a
-- stall to hide.
--
-- WHAT DOES NOT CHANGE. The synchronous path is untouched and stays the
-- default. An install that takes this update and creates no schedule indexes
-- exactly as it does today, at exactly the same moment in the request. Nothing
-- here turns anything on: the queue is only written when a caller passes
-- `background: true`, and it is only drained when an operator has created a
-- `document_index` schedule. A row that is never drained is a row, not a
-- silently lost document — `GET /hxnexus/index-jobs` says so, which is why the
-- outcome columns are on the row rather than in a log.
--
-- ONE ACTIVE JOB PER DOCUMENT, portably. `active_key` holds the document id
-- while the job is pending or running and is set to NULL the moment it is not.
-- NULL <> NULL on both engines, so the unique index constrains exactly the
-- active rows and lets a document be re-indexed any number of times. A partial
-- index would have been the PostgreSQL spelling and MySQL 8.4 has no such
-- thing; the same table has to exist on three engines.
--
-- RE-RUNNABLE. `IF NOT EXISTS` throughout.
--
-- Copyright (c) 2024-2026 Velaris Contributors
-- SPDX-License-Identifier: BSL-1.1

CREATE TABLE IF NOT EXISTS document_index_jobs (
    id             UUID PRIMARY KEY,
    -- The tenant the CASE belongs to, captured at enqueue. The runner has no
    -- caller to ask, so the boundary has to be carried on the row.
    tenant_id      VARCHAR(255) NULL,
    case_id        UUID         NULL,
    document_id    UUID         NOT NULL,

    -- pending → running → done | failed. `cancelled` is an operator's act.
    status         VARCHAR(20)  NOT NULL DEFAULT 'pending',
    -- See the header. Document id while active, NULL once it is not.
    active_key     VARCHAR(36)  NULL,

    attempts       INTEGER      NOT NULL DEFAULT 0,
    -- A document that cannot be read will never become readable by being
    -- retried forever, and each attempt costs an OCR timeout.
    max_attempts   INTEGER      NOT NULL DEFAULT 3,
    -- Whether existing chunks for this document are dropped first.
    reindex        BOOLEAN      NOT NULL DEFAULT FALSE,

    chunks_indexed INTEGER      NULL,
    last_error     TEXT         NULL,
    -- Who asked. The runner acts on their behalf, so the audit answer to "why
    -- was this document read" is on the row that caused it.
    requested_by   VARCHAR(255) NULL,

    created_at     TIMESTAMPTZ  NOT NULL DEFAULT now(),
    started_at     TIMESTAMPTZ  NULL,
    finished_at    TIMESTAMPTZ  NULL,

    CONSTRAINT ck_document_index_jobs_status CHECK (
        status IN ('pending', 'running', 'done', 'failed', 'cancelled')
    )
);

-- One active job per document; unlimited finished ones.
CREATE UNIQUE INDEX IF NOT EXISTS ux_document_index_jobs_active
    ON document_index_jobs (active_key);

-- The drain reads oldest-pending-first.
CREATE INDEX IF NOT EXISTS idx_document_index_jobs_status
    ON document_index_jobs (status, created_at);

-- The screen and the API list a case's jobs.
CREATE INDEX IF NOT EXISTS idx_document_index_jobs_case
    ON document_index_jobs (case_id);
