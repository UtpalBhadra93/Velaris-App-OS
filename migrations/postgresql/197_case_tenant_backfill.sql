-- 197 — stamp the tenant on cases that never got one.
--
-- WHY. `case_instances.tenant_id` is nullable, and a case created by a user
-- whose `user_directory` row carries no tenant is stored with it NULL. The list
-- filter is `tenant_id = mine OR tenant_id IS NULL`, and the single-case read
-- had no tenant clause at all — so a NULL-tenant case is readable by EVERY
-- tenant on the platform. Proven live on 2026-09-05: tenant `dev` read tenant
-- `default`'s case and its private field.
--
-- The exposure cannot be closed by tightening the read alone. Dropping the
-- `IS NULL` branch while rows are still NULL would hide those cases from
-- everybody, including their rightful owner — a data-loss-shaped outage on
-- upgrade. The rows have to be labelled FIRST. That is this migration; the read
-- is tightened separately, behind an operator switch, and only once this has
-- run and reported nothing left over.
--
-- WHAT IT WILL NOT DO. It never guesses. A row is stamped only where ownership
-- is *derivable*, in two cases, and is otherwise left exactly as it is:
--
--   1. **The install has exactly one tenant.** Then every unowned row belongs to
--      it by construction — there is no second party it could belong to, and no
--      reader whose view can change. This is the single-tenant install, which is
--      most of them, and for it this migration is a no-op in effect.
--   2. **The case's TYPE is owned.** A case of a tenant-owned case type belongs
--      to that tenant; the case type is the stronger statement and it was
--      already being used as the fallback at creation time.
--
-- Anything else — a multi-tenant install with cases of unowned types — is
-- genuinely ambiguous, and a wrong guess would hand one tenant's case to
-- another. Those rows keep `tenant_id IS NULL` and stay visible exactly as they
-- are today. The final SELECT reports how many, because that number is what
-- tells an operator whether it is safe to switch strict reads on.
--
-- SAFE AGAINST THE RUNNING VERSION. No schema change: this is UPDATE only, on a
-- nullable column that already exists, so the previous release's code reads and
-- writes these rows throughout. A stamped row still matches its owner's
-- `tenant_id = mine` branch under the OLD filter, so the owner never loses
-- sight of it mid-upgrade. Re-runnable: every statement is guarded by
-- `tenant_id IS NULL`, so a second run matches nothing.

-- ── 1. single-tenant install: every unowned case belongs to the one tenant ───
UPDATE case_instances
   SET tenant_id = (SELECT id FROM tenants LIMIT 1)
 WHERE tenant_id IS NULL
   AND (SELECT COUNT(*) FROM tenants) = 1;

-- ── 2. otherwise inherit from an owned case type ─────────────────────────────
UPDATE case_instances ci
   SET tenant_id = ct.tenant_id
  FROM case_types ct
 WHERE ci.case_type_id = ct.id
   AND ci.tenant_id IS NULL
   AND ct.tenant_id IS NOT NULL;

-- ── 3. the number the operator needs ────────────────────────────────────────
-- Anything still NULL is a case this migration could not prove ownership of.
-- While that count is above zero, tightening the read filter would hide those
-- rows from everyone — so strict reads must stay off until it is zero, or until
-- somebody labels the remainder by hand.
SELECT COUNT(*) AS cases_still_without_a_tenant
  FROM case_instances
 WHERE tenant_id IS NULL;
