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
-- **active, approved_by the same person who wrote the change.** One admin, two
-- ordinary calls, and a container of their choosing running on the host.
--
-- `last_edited_by` records who last changed the definition, and approval now
-- refuses BOTH the author and the last editor. Nothing else fixes this: the
-- edit-returns-to-draft rule already worked, and it is exactly what put the
-- reviewer in a position to re-approve.
ALTER TABLE bot_definitions
    ADD COLUMN IF NOT EXISTS last_edited_by VARCHAR(255);

ALTER TABLE schedules
    ADD COLUMN IF NOT EXISTS last_edited_by VARCHAR(255);
