-- The two-person rule was defeatable in two calls. This is the column that fixes it.
--
-- Approval compared the approver against `created_by` — and **an edit never
-- changes `created_by`**. So the reviewer could approve a bot, then edit the
-- thing they had just approved (a different image digest, a wildcard egress
-- allowlist, a different credential set), and approve their own change: the
-- author's name was still on the row, so the "not the author" check passed.
--
-- Proven against a running service on 2026-08-20: author registers
-- `registry.test/good`, reviewer approves it, reviewer repoints it at
-- `registry.test/EVIL` with `granted_domains: ["*"]` and approves again —
-- active, approved by the same person who wrote the change.
--
-- `last_edited_by` records who last changed the definition, and approval now
-- refuses BOTH the author and the last editor.
--
-- MySQL has no `ADD COLUMN IF NOT EXISTS` (MariaDB does), so these run once.
ALTER TABLE bot_definitions
    ADD COLUMN last_edited_by VARCHAR(255) NULL;

ALTER TABLE schedules
    ADD COLUMN last_edited_by VARCHAR(255) NULL;
