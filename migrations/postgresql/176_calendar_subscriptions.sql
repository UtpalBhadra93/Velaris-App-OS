-- Calendar subscriptions — "add a calendar from somewhere else".
--
-- Migration 175 gave the calendar a reader for an uploaded `.ics`. A snapshot
-- goes stale the moment somebody books leave in Outlook, so this is the same
-- reader on a URL that is fetched again on a timetable.
--
-- **Read-only, deliberately.** This is what Google and Outlook actually offer
-- when you "add a calendar by URL", and it is why that feature is cheap: with
-- no write-back there is no question of who wins when both sides change, and
-- no way for a bug here to alter somebody's real calendar.
--
-- `url` is not a secret but it is often a **capability** — a Google or Outlook
-- private address embeds a token, so anyone holding the link can read the
-- calendar. It is therefore treated like one: only the owner, their manager and
-- an admin ever see it back.
--
-- `etag` / `last_modified` are the polite half of polling. A feed that has not
-- changed answers 304 and costs both sides nothing, which is what makes a short
-- interval reasonable.
--
-- `last_status` / `last_error` exist so a subscription that has been failing for
-- a fortnight says so on the screen. A sync that silently stops is worse than
-- one that never started: the calendar still looks full.
CREATE TABLE IF NOT EXISTS calendar_subscriptions (
    id                 UUID         PRIMARY KEY,
    tenant_id          VARCHAR(255) NOT NULL,
    -- Whose calendar the feed lands in. Not necessarily who created it: a
    -- manager may subscribe on behalf of somebody they manage.
    user_id            VARCHAR(255) NOT NULL,
    name               VARCHAR(255) NOT NULL,
    url                TEXT         NOT NULL,
    enabled            BOOLEAN      NOT NULL DEFAULT TRUE,
    -- Minutes between polls. Bounded by the API, not by this column.
    interval_minutes   INTEGER      NOT NULL DEFAULT 60,
    etag               VARCHAR(255),
    last_modified      VARCHAR(255),
    last_fetched_at    TIMESTAMPTZ,
    last_success_at    TIMESTAMPTZ,
    -- ok | unchanged | error | never
    last_status        VARCHAR(20)  NOT NULL DEFAULT 'never',
    last_error         TEXT         NOT NULL DEFAULT '',
    last_event_count   INTEGER      NOT NULL DEFAULT 0,
    consecutive_errors INTEGER      NOT NULL DEFAULT 0,
    created_by         VARCHAR(255) NOT NULL,
    created_at         TIMESTAMP(6) NOT NULL DEFAULT now(),
    updated_at         TIMESTAMPTZ  NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS ix_calendar_subscriptions_user
    ON calendar_subscriptions (tenant_id, user_id);
CREATE INDEX IF NOT EXISTS ix_calendar_subscriptions_due
    ON calendar_subscriptions (tenant_id, enabled, last_fetched_at);

-- Which subscription put a row here. Two feeds can legitimately carry the same
-- iCalendar UID — a meeting both a person and their team are invited to — so
-- the UID alone cannot identify a row. NULL means an uploaded file (mig 175) or
-- something typed, and those are never touched by a feed refresh.
ALTER TABLE calendar_events
    ADD COLUMN IF NOT EXISTS subscription_id UUID;

ALTER TABLE user_absences
    ADD COLUMN IF NOT EXISTS subscription_id UUID;

CREATE INDEX IF NOT EXISTS ix_calendar_events_subscription
    ON calendar_events (subscription_id);
CREATE INDEX IF NOT EXISTS ix_user_absences_subscription
    ON user_absences (subscription_id);
