-- Calendar phase 5 — the parts that make it a calendar people can actually run
-- a team on: a repeating meeting, an editable one, a joinable one, and guests.
--
-- ── Recurrence ────────────────────────────────────────────────────────────
-- A stand-up is one row that describes a rule, not fifty rows a year. Stored as
-- a small JSON object rather than a full RRULE string: `{"freq":"weekly",
-- "interval":1,"until":"2027-01-01T00:00:00Z"}` covers what a team calendar
-- actually uses, and it is readable in a database client, which an RRULE is not.
-- Occurrences are expanded on READ, inside the window being asked for — so a
-- daily meeting with no end date costs nothing to store and cannot fill a table.
--
-- `excluded_dates` is how one occurrence is cancelled without cancelling the
-- series. Without it, "we skip next Monday" means deleting the series and
-- rebuilding it, which loses the history of what was agreed.
--
-- ── Guests ────────────────────────────────────────────────────────────────
-- `external_emails` are people with no account here — a supplier, a customer's
-- solicitor. They are invited by email and never resolve to a user, so they are
-- deliberately NOT in `attendee_user_ids`: mixing the two would mean every
-- availability lookup tries to find a working pattern for an address.
--
-- ── A meeting room for something that is not a case ───────────────────────
-- `case_sessions.case_id` was NOT NULL, so HxMeet could only hold a meeting
-- attached to a case. A team stand-up has no case, which is why a calendar event
-- could never carry a join link. The column becomes nullable and gains a second
-- anchor, with exactly one of the two required: a session belongs to a case OR
-- to a calendar event, never to both and never to neither.
ALTER TABLE calendar_events
    ADD COLUMN IF NOT EXISTS recurrence      JSONB,
    ADD COLUMN IF NOT EXISTS excluded_dates  JSONB NOT NULL DEFAULT '[]'::jsonb,
    ADD COLUMN IF NOT EXISTS external_emails JSONB NOT NULL DEFAULT '[]'::jsonb,
    -- A link somebody pasted (Teams, Zoom, a phone bridge). `meet_session_id`
    -- stays the anchor for a room this platform created.
    ADD COLUMN IF NOT EXISTS meeting_url     TEXT,
    ADD COLUMN IF NOT EXISTS updated_by      VARCHAR(255);

ALTER TABLE case_sessions
    ALTER COLUMN case_id DROP NOT NULL;

ALTER TABLE case_sessions
    ADD COLUMN IF NOT EXISTS calendar_event_id UUID
        REFERENCES calendar_events(id) ON DELETE CASCADE;

CREATE INDEX IF NOT EXISTS ix_case_sessions_event ON case_sessions (calendar_event_id);

-- Exactly one anchor. Every existing row has a case, so this holds on the way in.
ALTER TABLE case_sessions
    DROP CONSTRAINT IF EXISTS case_sessions_anchor_ck;
ALTER TABLE case_sessions
    ADD CONSTRAINT case_sessions_anchor_ck CHECK (
        (case_id IS NOT NULL AND calendar_event_id IS NULL)
        OR (case_id IS NULL AND calendar_event_id IS NOT NULL)
    );
