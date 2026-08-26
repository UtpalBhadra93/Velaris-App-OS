-- The scheduler — one place that says "do this, at these times".
--
-- Cron did not belong on `bot_definitions`. The moment a second thing wants a
-- timer (a nightly email, a weekly warehouse push) that column set has to be
-- copied onto another table, and then "what is scheduled here?" has as many
-- answers as there are features. So the schedule is the row and the thing it
-- fires is `action_kind` + `action_config`.
--
-- **A schedule is `draft` until somebody who is not its author approves it**,
-- exactly as a bot definition is. An unattended action that runs at 2am while
-- nobody is watching — emailing out, driving a supplier's site, closing cases —
-- is precisely what a second pair of eyes exists for, and the approval is also
-- what stops the editor becoming a way around the bot approval gate: a draft
-- bot cannot be fired by an approved schedule either.
--
-- `next_run_at` is the claim: a due schedule is taken with a conditional UPDATE
-- whose WHERE still names the timestamp that was read, so two workers racing for
-- the same firing cannot both win. It is the same mechanism as a bot job lease,
-- for the same reason — and it is why a missed window is *skipped* rather than
-- replayed: the next occurrence is computed from now, so a service that was down
-- overnight does not send a night's worth of email at breakfast.
CREATE TABLE IF NOT EXISTS schedules (
    id            UUID         PRIMARY KEY,
    tenant_id     VARCHAR(255) NOT NULL,
    name          VARCHAR(255) NOT NULL,
    description   TEXT         NOT NULL DEFAULT '',
    -- Five fields, or a macro. Validated on write: an expression that can never
    -- fire is refused rather than stored.
    cron          VARCHAR(120) NOT NULL,
    timezone      VARCHAR(64)  NOT NULL DEFAULT 'UTC',
    -- bot | email | case | connector | case_bulk | report | sync
    action_kind   VARCHAR(30)  NOT NULL,
    action_config JSONB        NOT NULL DEFAULT '{}'::jsonb,
    -- draft until approved by somebody other than the author; disabled stops it.
    status        VARCHAR(20)  NOT NULL DEFAULT 'draft',
    created_by    VARCHAR(255) NOT NULL,
    approved_by   VARCHAR(255),
    approved_at   TIMESTAMPTZ,
    next_run_at   TIMESTAMPTZ,
    last_run_at   TIMESTAMPTZ,
    last_status   VARCHAR(20),
    last_error    TEXT,
    created_at    TIMESTAMP(6) NOT NULL DEFAULT now(),
    updated_at    TIMESTAMPTZ  NOT NULL DEFAULT now(),
    CONSTRAINT schedules_name_uq UNIQUE (tenant_id, name)
);

CREATE INDEX IF NOT EXISTS ix_schedules_due ON schedules(status, next_run_at);

-- Every firing, including the ones that did nothing. "Did the nightly job run?"
-- must be answerable from a row, not from a log file that has rotated.
CREATE TABLE IF NOT EXISTS schedule_runs (
    id          UUID         PRIMARY KEY,
    schedule_id UUID         NOT NULL REFERENCES schedules(id) ON DELETE CASCADE,
    fired_at    TIMESTAMP(6) NOT NULL DEFAULT now(),
    -- ok | error | skipped
    status      VARCHAR(20)  NOT NULL,
    detail      JSONB        NOT NULL DEFAULT '{}'::jsonb,
    duration_ms INTEGER,
    created_at  TIMESTAMP(6) NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS ix_schedule_runs_schedule
    ON schedule_runs(schedule_id, fired_at);
