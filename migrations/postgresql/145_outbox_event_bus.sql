-- Durable-log fan-out for the transactional outbox (report §2.1).
--
-- The outbox has been atomic with domain writes since Group C, so an event
-- cannot be lost. What it lacked was fan-out: the only sink was HTTP webhook
-- delivery, so an event reached whoever had subscribed a URL and nobody else.
-- Analytics, HxShield detection, the lakehouse and event sourcing (#30a) all
-- need to read the same stream, and none of them are webhooks.
--
-- These two columns are a SEPARATE publish cursor from `delivered_at`,
-- deliberately: the event bus can be turned on, turned off, or replayed
-- without touching webhook delivery semantics, and a publishing backlog is
-- visible rather than inferred.
--
-- NULL for every existing row, so enabling the bus republishes history from
-- the beginning — which is the correct default for a log consumers may need
-- to build state from.

ALTER TABLE outbox
    ADD COLUMN IF NOT EXISTS published_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS publish_claimed_at TIMESTAMPTZ;

CREATE INDEX IF NOT EXISTS ix_outbox_unpublished
    ON outbox (created_at) WHERE published_at IS NULL;
