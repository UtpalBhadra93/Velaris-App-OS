-- Calendar — the platform's first record of when a *person* is available.
--
-- `business_calendars` has always described the ORGANISATION: work days, hours,
-- timezone and holidays, which the SLA clock counts against. Nothing described a
-- person, so `assignment_router` picked people who were on holiday and
-- escalation trees escalated into empty inboxes — "is she in today?" had no
-- answer to give.
--
--   `user_work_patterns` — the normal week when it differs from the org's. No
--   row means "the tenant's default business calendar".
--   `user_absences` — the exceptions, and the table every consumer reads. Only
--   an **approved** absence changes anything.
--
-- `ends_at` is a timestamp, not a date: half days exist. `delegate_user_id` is on
-- the absence rather than the user: cover belongs to this absence.
--
-- MySQL notes: `usage` and `groups` are reserved but nothing here uses them;
-- CHECK constraints are enforced from MySQL 8.0.16 and MariaDB 10.2, so the
-- range check is portable. (255 + 255) chars at utf8mb4 is 2040 bytes, inside
-- the 3072-byte InnoDB key limit, so the unique key needs no prefix.
CREATE TABLE IF NOT EXISTS user_work_patterns (
    id                   CHAR(36)     NOT NULL,
    tenant_id            VARCHAR(255) NOT NULL,
    user_id              VARCHAR(255) NOT NULL,
    business_calendar_id CHAR(36)     NULL,
    work_days            JSON         NULL,
    work_start_hour      INT          NULL,
    work_end_hour        INT          NULL,
    timezone             VARCHAR(64)  NULL,
    updated_by           VARCHAR(255) NOT NULL,
    created_at           DATETIME(6)  NOT NULL,
    updated_at           DATETIME     NOT NULL,
    PRIMARY KEY (id),
    UNIQUE KEY user_work_patterns_uq (tenant_id, user_id),
    KEY ix_user_work_patterns_cal (business_calendar_id),
    CONSTRAINT user_work_patterns_cal_fk
        FOREIGN KEY (business_calendar_id) REFERENCES business_calendars (id) ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS user_absences (
    id               CHAR(36)     NOT NULL,
    tenant_id        VARCHAR(255) NOT NULL,
    user_id          VARCHAR(255) NOT NULL,
    kind             VARCHAR(20)  NOT NULL DEFAULT 'holiday',
    starts_at        DATETIME     NOT NULL,
    ends_at          DATETIME     NOT NULL,
    all_day          TINYINT(1)   NOT NULL DEFAULT 1,
    status           VARCHAR(20)  NOT NULL DEFAULT 'requested',
    approved_by      VARCHAR(255) NULL,
    approved_at      DATETIME     NULL,
    delegate_user_id VARCHAR(255) NULL,
    source           VARCHAR(20)  NOT NULL DEFAULT 'manual',
    case_id          CHAR(36)     NULL,
    note             TEXT         NOT NULL,
    created_by       VARCHAR(255) NOT NULL,
    created_at       DATETIME(6)  NOT NULL,
    updated_at       DATETIME     NOT NULL,
    PRIMARY KEY (id),
    KEY ix_user_absences_lookup (tenant_id, user_id, status, starts_at, ends_at),
    KEY ix_user_absences_window (tenant_id, starts_at, ends_at),
    KEY ix_user_absences_status (tenant_id, status),
    CONSTRAINT user_absences_range_ck CHECK (ends_at > starts_at)
);
