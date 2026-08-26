-- Calendar phase 4 — team events.
--
-- HxMeet already runs meetings, but every one of them hangs off a **case**:
-- `case_sessions` is created at `/meet/cases/{case_id}/sessions`. A team
-- stand-up belongs to no case, so there was nowhere to put it, and the only
-- alternative was a third-party calendar nobody else on the platform can read.
--
-- The division of labour is deliberate: **Calendar owns when and who, HxMeet
-- owns the room.** An event may point at a meet session; it never becomes one,
-- and nothing here starts video. Building a second room would mean two places
-- to look for a recording, two retention rules, and two things to secure.
--
-- `attendee_user_ids` is a JSON array rather than a join table: attendance is
-- read whole, every time, and a join table would buy nothing but a migration.
-- The consequence — no per-attendee response status — is deliberate for now; an
-- accept/decline flow is a case type's job, not a column.
CREATE TABLE IF NOT EXISTS calendar_events (
    id                 UUID         PRIMARY KEY,
    tenant_id          VARCHAR(255) NOT NULL,
    title              VARCHAR(255) NOT NULL,
    description        TEXT         NOT NULL DEFAULT '',
    starts_at          TIMESTAMPTZ  NOT NULL,
    ends_at            TIMESTAMPTZ  NOT NULL,
    timezone           VARCHAR(64)  NOT NULL DEFAULT 'UTC',
    organiser_user_id  VARCHAR(255) NOT NULL,
    attendee_user_ids  JSONB        NOT NULL DEFAULT '[]'::jsonb,
    -- Optional anchors. A case-linked event is how a review meeting stays with
    -- the work it is about; a meet session is HxMeet's room, referenced not owned.
    case_id            UUID,
    meet_session_id    UUID,
    -- team = everybody in the tenant may see it; private = organiser and
    -- attendees only. The title is the whole payload, so this is the only
    -- privacy control the object needs.
    visibility         VARCHAR(20)  NOT NULL DEFAULT 'team',
    status             VARCHAR(20)  NOT NULL DEFAULT 'scheduled',
    created_by         VARCHAR(255) NOT NULL,
    created_at         TIMESTAMP(6) NOT NULL DEFAULT now(),
    updated_at         TIMESTAMPTZ  NOT NULL DEFAULT now(),
    CONSTRAINT calendar_events_range_ck CHECK (ends_at > starts_at)
);

CREATE INDEX IF NOT EXISTS ix_calendar_events_window
    ON calendar_events (tenant_id, starts_at, ends_at);
CREATE INDEX IF NOT EXISTS ix_calendar_events_organiser
    ON calendar_events (tenant_id, organiser_user_id);
