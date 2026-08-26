-- The scheduler — one place that says "do this, at these times".
--
-- Cron did not belong on `bot_definitions`: the moment a second thing wants a
-- timer, that column set has to be copied onto another table and "what is
-- scheduled here?" has as many answers as there are features. So the schedule is
-- the row and the thing it fires is `action_kind` + `action_config`.
--
-- **A schedule is `draft` until somebody who is not its author approves it**,
-- exactly as a bot definition is — an unattended 2am action that emails out or
-- drives a supplier's site is what a second pair of eyes exists for.
--
-- `next_run_at` is the claim: a due schedule is taken with a conditional UPDATE
-- whose WHERE still names the timestamp that was read, so two workers racing for
-- one firing cannot both win. A missed window is skipped rather than replayed.
CREATE TABLE IF NOT EXISTS schedules (
    id            CHAR(36)     NOT NULL,
    tenant_id     VARCHAR(255) NOT NULL,
    name          VARCHAR(255) NOT NULL,
    description   TEXT         NOT NULL,
    cron          VARCHAR(120) NOT NULL,
    timezone      VARCHAR(64)  NOT NULL DEFAULT 'UTC',
    -- bot | email | case | connector | case_bulk | report | sync
    action_kind   VARCHAR(30)  NOT NULL,
    action_config JSON         NOT NULL,
    status        VARCHAR(20)  NOT NULL DEFAULT 'draft',
    created_by    VARCHAR(255) NOT NULL,
    approved_by   VARCHAR(255) NULL,
    approved_at   DATETIME     NULL,
    next_run_at   DATETIME     NULL,
    last_run_at   DATETIME     NULL,
    last_status   VARCHAR(20)  NULL,
    last_error    TEXT         NULL,
    created_at    DATETIME(6)  NOT NULL,
    updated_at    DATETIME     NOT NULL,
    PRIMARY KEY (id),
    -- (255 + 255) chars at utf8mb4 exceeds the 3072-byte InnoDB key limit, so
    -- the name half is prefixed — the same treatment `bot_credentials` needed.
    UNIQUE KEY schedules_name_uq (tenant_id, name(191)),
    KEY ix_schedules_due (status, next_run_at)
);

CREATE TABLE IF NOT EXISTS schedule_runs (
    id          CHAR(36)    NOT NULL,
    schedule_id CHAR(36)    NOT NULL,
    fired_at    DATETIME(6) NOT NULL,
    status      VARCHAR(20) NOT NULL,
    detail      JSON        NOT NULL,
    duration_ms INT         NULL,
    created_at  DATETIME(6) NOT NULL,
    PRIMARY KEY (id),
    KEY ix_schedule_runs_schedule (schedule_id, fired_at),
    CONSTRAINT schedule_runs_schedule_fk
        FOREIGN KEY (schedule_id) REFERENCES schedules (id) ON DELETE CASCADE
);
