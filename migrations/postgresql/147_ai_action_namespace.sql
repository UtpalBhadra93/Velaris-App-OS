-- Provision the `ai` variable namespace and its grant (#33).
--
-- `ai_action` writes accepted values through `set_granted`, the same
-- grant-checked path rules and modules use — never a privileged bypass. That
-- is the right posture (a model writing case data should require an explicit,
-- revocable grant), but it also meant the step type could not write anything
-- at all: no `ai` namespace existed and no grant named `ai_action` as a
-- grantee, so every accepted proposal failed closed.
--
-- This seeds both. The grant is revocable exactly like any other — deleting
-- the row disarms every ai_action step platform-wide without touching a case
-- type, which is the operational lever an admin actually wants.
--
-- `owner_type = 'platform'` with a NULL owner_ref: the namespace belongs to
-- the platform rather than to a connector, so `_resolve_write_namespace`
-- never resolves it as a caller's own — writes must come through the grant.
--
-- Sensitivity `internal`: model output is not automatically PII, and marking
-- it `pii` wholesale would mask it from the very reviewers who have to judge
-- whether to accept it.

INSERT INTO variable_namespaces (id, name, owner_type, owner_ref, sensitivity, status, created_at)
SELECT gen_random_uuid(), 'ai', 'platform', NULL, 'internal', 'active', NOW()
WHERE NOT EXISTS (SELECT 1 FROM variable_namespaces WHERE name = 'ai');

INSERT INTO namespace_grants (id, namespace_id, grantee_type, grantee_ref, capability, granted_by, granted_at)
SELECT gen_random_uuid(), ns.id, 'ai_action', '*', 'write', 'migration-147', NOW()
FROM variable_namespaces ns
WHERE ns.name = 'ai'
  AND NOT EXISTS (
      SELECT 1 FROM namespace_grants g
      WHERE g.namespace_id = ns.id AND g.grantee_type = 'ai_action'
        AND g.capability = 'write'
  );
