-- Calendar — ICS import (`source='import'` finally has a producer).
--
-- `user_absences.source` has carried `'import'` in its vocabulary since
-- migration 170 and nothing has ever written it: the design named an ICS/Graph
-- feed as a later, optional producer and it was never built. These columns are
-- what that producer needs.
--
-- `source` on `calendar_events` mirrors the column absences already have, so
-- both halves of the calendar answer "where did this row come from" the same
-- way. It defaults to `'manual'`, which is what every existing row is — an
-- upgrade changes nothing about them.
--
-- `external_uid` is the iCalendar UID. It is what makes a re-import an UPDATE
-- rather than a duplicate: a feed exported twice is the normal case, not the
-- exception. It is NULL for everything created here, and a NULL never matches
-- another NULL, so manually-created rows can never be adopted by an import.
--
-- MySQL has no `ADD COLUMN IF NOT EXISTS` or `CREATE INDEX IF NOT EXISTS`
-- (MariaDB does), so these run once — start-velaris.sh records applied files in
-- schema_migrations and never repeats one, which is what makes the plain form
-- safe here.
--
-- The uniqueness is deliberately NOT enforced by a constraint. Two different
-- people may legitimately import the same meeting from the same UID into their
-- own calendars, and a unique index would refuse the second one. The importer
-- matches on (tenant_id, source='import', external_uid, organiser_user_id).
ALTER TABLE calendar_events
    ADD COLUMN source       VARCHAR(20) NOT NULL DEFAULT 'manual';

ALTER TABLE calendar_events
    ADD COLUMN external_uid VARCHAR(255) NULL;

ALTER TABLE user_absences
    ADD COLUMN external_uid VARCHAR(255) NULL;

CREATE INDEX ix_calendar_events_external
    ON calendar_events (tenant_id, external_uid);

CREATE INDEX ix_user_absences_external
    ON user_absences (tenant_id, external_uid);
