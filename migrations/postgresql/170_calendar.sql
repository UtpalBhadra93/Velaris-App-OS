-- Calendar — the platform's first record of when a *person* is available.
--
-- `business_calendars` has always described the ORGANISATION: work days, work
-- hours, a timezone and a list of holidays, which the SLA clock counts against.
-- Nothing anywhere described a person. So `assignment_router` — nine strategies,
-- round-robin, least-loaded, skill-based, manager-of — picked people who were on
-- holiday, and escalation trees escalated into empty inboxes, because the
-- question "is she in today?" had no answer to give.
--
-- Two tables, and the split matters:
--
--   `user_work_patterns` is the normal week when it differs from the org's. One
--   row per person, every field optional; no row means "the tenant's default
--   business calendar", which is what almost everyone will be.
--
--   `user_absences` is the exception, and it is the table every consumer reads.
--   Only an **approved** absence changes anything: a request that reroutes work
--   the moment it is typed would let anyone quietly stop their own queue.
--
-- `ends_at` is a timestamp rather than a date because half days exist, and a
-- date-only column makes every caller invent its own convention for them.
--
-- `delegate_user_id` sits on the ABSENCE, not on the user: cover is a property
-- of this absence, and a person may hand over to someone different in December
-- than in July.
CREATE TABLE IF NOT EXISTS user_work_patterns (
    id                   UUID         PRIMARY KEY,
    tenant_id            VARCHAR(255) NOT NULL,
    user_id              VARCHAR(255) NOT NULL,
    -- The org calendar this person follows. NULL = the tenant's default.
    business_calendar_id UUID         REFERENCES business_calendars(id) ON DELETE SET NULL,
    -- ISO weekday numbers, 1 = Monday. NULL = inherit from the calendar above.
    work_days            JSONB,
    work_start_hour      INTEGER,
    work_end_hour        INTEGER,
    timezone             VARCHAR(64),
    updated_by           VARCHAR(255) NOT NULL,
    created_at           TIMESTAMP(6) NOT NULL DEFAULT now(),
    updated_at           TIMESTAMPTZ  NOT NULL DEFAULT now(),
    CONSTRAINT user_work_patterns_uq UNIQUE (tenant_id, user_id)
);

CREATE TABLE IF NOT EXISTS user_absences (
    id                UUID         PRIMARY KEY,
    tenant_id         VARCHAR(255) NOT NULL,
    user_id           VARCHAR(255) NOT NULL,
    -- holiday | sick | training | other
    kind              VARCHAR(20)  NOT NULL DEFAULT 'holiday',
    starts_at         TIMESTAMPTZ  NOT NULL,
    ends_at           TIMESTAMPTZ  NOT NULL,
    all_day           BOOLEAN      NOT NULL DEFAULT TRUE,
    -- requested | approved | cancelled. Only `approved` reroutes work.
    status            VARCHAR(20)  NOT NULL DEFAULT 'requested',
    approved_by       VARCHAR(255),
    approved_at       TIMESTAMPTZ,
    -- Who covers. Named per absence, never per person.
    delegate_user_id  VARCHAR(255),
    -- manual | case | import. `case` means a Leave Request case type created it.
    source            VARCHAR(20)  NOT NULL DEFAULT 'manual',
    case_id           UUID,
    -- Personal. Visible to the person, their manager and an admin — never on a
    -- team view, because "sick" is health data.
    note              TEXT         NOT NULL DEFAULT '',
    created_by        VARCHAR(255) NOT NULL,
    created_at        TIMESTAMP(6) NOT NULL DEFAULT now(),
    updated_at        TIMESTAMPTZ  NOT NULL DEFAULT now(),
    CONSTRAINT user_absences_range_ck CHECK (ends_at > starts_at)
);

-- The lookup every consumer makes: "is this person away right now?"
CREATE INDEX IF NOT EXISTS ix_user_absences_lookup
    ON user_absences (tenant_id, user_id, status, starts_at, ends_at);
-- The team view, and the approvals queue.
CREATE INDEX IF NOT EXISTS ix_user_absences_window
    ON user_absences (tenant_id, starts_at, ends_at);
CREATE INDEX IF NOT EXISTS ix_user_absences_status
    ON user_absences (tenant_id, status);
