-- 197 — stamp the tenant on cases that never got one.
-- MySQL 8.4 / MariaDB 11.8 sibling of migrations/postgresql/197. See that file
-- for why each statement exists and for what it deliberately will not do.
--
-- Dialect notes:
--  * MySQL refuses `UPDATE t ... WHERE (SELECT ... FROM t)` — you cannot select
--    from the table you are updating in a subquery. The single-tenant case is
--    therefore written as a JOIN against a derived table, which MySQL evaluates
--    once (materialised) rather than re-reading the target.
--  * There is no `UPDATE ... FROM`; the multi-table `UPDATE a JOIN b SET ...`
--    form is used instead.
--  * `tenants.id` and `case_instances.tenant_id` are CHAR(36) here, not UUID,
--    so no cast is needed.
--
-- Re-runnable and schema-free, exactly as the PostgreSQL file: UPDATE only,
-- every statement guarded by `tenant_id IS NULL`.

-- ── 1. single-tenant install: every unowned case belongs to the one tenant ───
UPDATE case_instances ci
  JOIN (SELECT MIN(id) AS only_tenant, COUNT(*) AS n FROM tenants) t
    ON t.n = 1
   SET ci.tenant_id = t.only_tenant
 WHERE ci.tenant_id IS NULL;

-- ── 2. otherwise inherit from an owned case type ─────────────────────────────
UPDATE case_instances ci
  JOIN case_types ct ON ci.case_type_id = ct.id
   SET ci.tenant_id = ct.tenant_id
 WHERE ci.tenant_id IS NULL
   AND ct.tenant_id IS NOT NULL;

-- ── 3. the number the operator needs ────────────────────────────────────────
-- While this is above zero, strict reads must stay off: tightening the filter
-- would hide these rows from everyone, their owner included.
SELECT COUNT(*) AS cases_still_without_a_tenant
  FROM case_instances
 WHERE tenant_id IS NULL;
