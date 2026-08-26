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
--   set of limits.
--
-- * `bot_tenant_limits` — `max_concurrency` bounds one definition; nothing
--   bounded a tenant.
--
-- MySQL has no `ADD COLUMN IF NOT EXISTS` (MariaDB does), so these run once, as
-- migration 164 did.
ALTER TABLE bot_jobs
    ADD COLUMN run_after   DATETIME NULL,
    ADD COLUMN batch_id    CHAR(36) NULL,
    ADD COLUMN batch_index INT      NULL,
    ADD COLUMN batch_size  INT      NULL,
    ADD KEY ix_botjobs_batch (batch_id),
    ADD KEY ix_botjobs_runafter (status, run_after);

ALTER TABLE bot_definitions
    -- NULL = no fan-out. Names the key in the run input holding the collection.
    ADD COLUMN fanout_input_key VARCHAR(255) NULL,
    -- The bound a reviewer is approving. One request may never start more.
    ADD COLUMN fanout_max       INT NOT NULL DEFAULT 25;

CREATE TABLE IF NOT EXISTS bot_tenant_limits (
    tenant_id           VARCHAR(255) NOT NULL,
    max_concurrent_jobs INT          NOT NULL,
    updated_by          VARCHAR(255) NOT NULL,
    updated_at          DATETIME     NOT NULL,
    PRIMARY KEY (tenant_id)
);
