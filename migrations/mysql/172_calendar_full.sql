-- Calendar phase 5 — a repeating meeting, an editable one, a joinable one, guests.
--
-- Recurrence is a small JSON object rather than an RRULE string —
-- `{"freq":"weekly","interval":1,"until":"..."}` covers what a team calendar
-- uses and stays readable in a database client. Occurrences are expanded on
-- READ inside the window asked for, so an endless daily meeting costs one row.
-- `excluded_dates` cancels one occurrence without destroying the series.
--
-- `external_emails` are guests with no account here. Deliberately not mixed into
-- `attendee_user_ids`: every availability lookup would then try to find a
-- working pattern for an email address.
--
-- `case_sessions.case_id` was NOT NULL, so HxMeet could only hold a meeting
-- attached to a case and a team event could never carry a join link. The column
-- becomes nullable and gains a second anchor — exactly one of the two.
--
-- MySQL notes: no `ADD COLUMN IF NOT EXISTS` (MariaDB has it), so these run once.
-- CHECK is enforced from MySQL 8.0.16 and MariaDB 10.2.
ALTER TABLE calendar_events
    ADD COLUMN recurrence      JSON         NULL,
    ADD COLUMN excluded_dates  JSON         NULL,
    ADD COLUMN external_emails JSON         NULL,
    ADD COLUMN meeting_url     TEXT         NULL,
    ADD COLUMN updated_by      VARCHAR(255) NULL;

ALTER TABLE case_sessions
    MODIFY COLUMN case_id CHAR(36) NULL;

ALTER TABLE case_sessions
    ADD COLUMN calendar_event_id CHAR(36) NULL,
    ADD KEY ix_case_sessions_event (calendar_event_id),
    ADD CONSTRAINT case_sessions_event_fk
        FOREIGN KEY (calendar_event_id) REFERENCES calendar_events (id) ON DELETE CASCADE,
    ADD CONSTRAINT case_sessions_anchor_ck CHECK (
        (case_id IS NOT NULL AND calendar_event_id IS NULL)
        OR (case_id IS NULL AND calendar_event_id IS NOT NULL)
    );
