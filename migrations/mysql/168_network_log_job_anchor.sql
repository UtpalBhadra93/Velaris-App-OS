-- A bot's egress records were written and then silently thrown away.
--
-- `marketplace_network_log.grant_id` is a foreign key to
-- `marketplace_capability_grants` — a MARKETPLACE grant. The bot runner mints a
-- per-job egress identity and stamps it with the **job** id, so every line the
-- gateway wrote for a bot violated that key on insert. The commit raised inside
-- the audit view's `except Exception`, which exists so a deployment without a
-- gateway answers with an empty list rather than a traceback, and the exception
-- it swallowed was this one. Worse, `ingest_access_log` had already renamed the
-- proxy's access log and deleted it: the rows were unsaveable AND the evidence
-- was gone.
--
-- Proven live on 2026-08-21: a bot reached `example.com` (200) and was refused
-- `wikipedia.org` (403 at the tunnel); the service logged "ingested 2 entries";
-- `marketplace_network_log` held none, and the Bot Console Audit view's network
-- section was empty — which is exactly what it would show if the bot had made
-- no requests at all.
--
-- A bot job is a third anchor beside the workspace and the grant, so it gets its
-- own column and its own key rather than borrowing one that means something
-- else. Any of the three may be NULL; never all three.
--
-- MySQL has neither `ADD COLUMN IF NOT EXISTS` (MariaDB does) nor
-- `CREATE INDEX IF NOT EXISTS`, so these run once.
ALTER TABLE marketplace_network_log
    ADD COLUMN job_id CHAR(36) NULL,
    ADD CONSTRAINT marketplace_network_log_job_fk
        FOREIGN KEY (job_id) REFERENCES bot_jobs (id) ON DELETE CASCADE;

CREATE INDEX ix_mnl_job ON marketplace_network_log (job_id);
