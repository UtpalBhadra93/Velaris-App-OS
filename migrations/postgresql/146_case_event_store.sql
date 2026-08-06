-- Event sourcing for the Case aggregate (roadmap #30a, report §2.2).
--
-- Case state becomes a PROJECTION of an append-only stream rather than being
-- replaced by one: `case_instances` keeps working exactly as it does today and
-- every existing read path is untouched. What this adds is a verifiable
-- history — the ability to replay a case and prove the current row is what the
-- recorded events say it should be.
--
-- UNIQUE (case_id, seq) is the load-bearing constraint. It does three jobs at
-- once: orders the stream, detects gaps (a hole means an event was deleted,
-- which nothing is supposed to be able to do), and acts as the optimistic
-- concurrency check — two writers computing the same next sequence will see
-- one succeed and one fail rather than silently interleaving.
--
-- CLEAN CUTOVER, NEVER A BACKFILL (§7.5). `case_instances.event_sourced` marks
-- cases created after the store was switched on. Earlier cases cannot have an
-- honest stream reconstructed: `case_step_completions` and `case_event_log`
-- record what happened but were never append-only, so a rebuilt history would
-- be a guess wearing the costume of a fact. Those cases keep working and
-- simply cannot be replayed. This is a forward-only feature gain.
--
-- Scope is the Case aggregate ONLY (§2.2 caveat) — forms, marketplace and
-- HxDocs stay relational and are not event-sourced.

CREATE TABLE IF NOT EXISTS case_events (
    id          UUID PRIMARY KEY,
    case_id     UUID        NOT NULL REFERENCES case_instances(id) ON DELETE CASCADE,
    seq         BIGINT      NOT NULL,
    event_type  VARCHAR(64) NOT NULL,
    data        JSONB       NOT NULL DEFAULT '{}'::jsonb,
    actor_id    VARCHAR(255),
    actor_type  VARCHAR(20) NOT NULL DEFAULT 'system',
    occurred_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_case_events_case_seq UNIQUE (case_id, seq)
);

CREATE INDEX IF NOT EXISTS ix_case_events_case_seq ON case_events (case_id, seq);
CREATE INDEX IF NOT EXISTS ix_case_events_type     ON case_events (event_type);

ALTER TABLE case_instances
    ADD COLUMN IF NOT EXISTS event_sourced BOOLEAN NOT NULL DEFAULT FALSE;
