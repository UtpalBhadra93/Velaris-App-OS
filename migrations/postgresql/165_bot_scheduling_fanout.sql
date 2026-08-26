-- Bot queue: delayed retries, fan-out batches, and a per-tenant pool.
--
-- Three of these columns exist because the queue could not express things it
-- already claimed to do:
--
-- * `run_after` — `requeue_or_fail` computes the connector backoff
--   ([60, 300, 1800]) and writes it into a run *event*, but nothing held it and
--   `lease_next` had no time filter, so a failed job was re-leased on the next
--   five-second cycle. The backoff was documentation, not behaviour. This is
--   the column that makes it real, and the same column the scheduler needs to
--   queue work for later.
--
-- * `batch_id` / `batch_index` / `batch_size` — a fan-out run is N jobs from one
--   request. They are ordinary jobs on purpose: one queue, one lease path, one
--   set of limits. The batch is how they are reported together, not a second
--   execution mechanism.
--
-- * `bot_tenant_limits` — `max_concurrency` bounds one definition. Nothing
--   bounded a tenant, so twenty definitions at their individual limits could
--   fill the host between them. The per-tenant cap is checked in the same place
--   as the per-bot cap.
--
-- Fan-out lives on the DEFINITION, not on the run request: the approval is the
-- review, and a reviewer who approved a bot must be able to see that one call
-- can start `fanout_max` containers.
ALTER TABLE bot_jobs
    ADD COLUMN IF NOT EXISTS run_after   TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS batch_id    UUID,
    ADD COLUMN IF NOT EXISTS batch_index INTEGER,
    ADD COLUMN IF NOT EXISTS batch_size  INTEGER;

CREATE INDEX IF NOT EXISTS ix_botjobs_batch ON bot_jobs(batch_id);
-- The queue scan orders by queued_at and now filters on run_after.
CREATE INDEX IF NOT EXISTS ix_botjobs_runafter ON bot_jobs(status, run_after);

ALTER TABLE bot_definitions
    -- NULL = no fan-out. Names the key in the run input holding the collection.
    ADD COLUMN IF NOT EXISTS fanout_input_key VARCHAR(255),
    -- The bound a reviewer is approving. One request may never start more.
    ADD COLUMN IF NOT EXISTS fanout_max       INTEGER NOT NULL DEFAULT 25;

CREATE TABLE IF NOT EXISTS bot_tenant_limits (
    tenant_id           VARCHAR(255) PRIMARY KEY,
    max_concurrent_jobs INTEGER      NOT NULL,
    updated_by          VARCHAR(255) NOT NULL,
    updated_at          TIMESTAMPTZ  NOT NULL DEFAULT now()
);
