-- Calendar phase 4 — team events.
--
-- HxMeet already runs meetings, but every one hangs off a **case**
-- (`/meet/cases/{case_id}/sessions`). A team stand-up belongs to no case, so
-- there was nowhere to put it.
--
-- **Calendar owns when and who; HxMeet owns the room.** An event may point at
-- a meet session; it never becomes one, and nothing here starts video.
--
-- `attendee_user_ids` is a JSON array rather than a join table: attendance is
-- read whole every time. The consequence — no per-attendee response status — is
-- deliberate; an accept/decline flow is a case type's job, not a column.
CREATE TABLE IF NOT EXISTS calendar_events (
    id                CHAR(36)     NOT NULL,
    tenant_id         VARCHAR(255) NOT NULL,
    title             VARCHAR(255) NOT NULL,
    description       TEXT         NOT NULL,
    starts_at         DATETIME     NOT NULL,
    ends_at           DATETIME     NOT NULL,
    timezone          VARCHAR(64)  NOT NULL DEFAULT 'UTC',
    organiser_user_id VARCHAR(255) NOT NULL,
    attendee_user_ids JSON         NOT NULL,
    case_id           CHAR(36)     NULL,
    meet_session_id   CHAR(36)     NULL,
    visibility        VARCHAR(20)  NOT NULL DEFAULT 'team',
    status            VARCHAR(20)  NOT NULL DEFAULT 'scheduled',
    created_by        VARCHAR(255) NOT NULL,
    created_at        DATETIME(6)  NOT NULL,
    updated_at        DATETIME     NOT NULL,
    PRIMARY KEY (id),
    KEY ix_calendar_events_window (tenant_id, starts_at, ends_at),
    KEY ix_calendar_events_organiser (tenant_id, organiser_user_id),
    CONSTRAINT calendar_events_range_ck CHECK (ends_at > starts_at)
);
