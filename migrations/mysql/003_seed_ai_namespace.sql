-- ============================================================================
-- Velaris — MySQL 8 / MariaDB 10.6+ : `ai` namespace + ai_action grant
-- (PostgreSQL migration 147). Seeds the namespace and grant that let an
-- `ai_action` step write an accepted proposal at all — without them every
-- accepted value failed closed on the grant check.
--
-- Revoking the grant row disarms every ai_action step platform-wide without
-- editing a single case type. UUID() rather than gen_random_uuid().
-- ============================================================================

INSERT INTO variable_namespaces (id, name, owner_type, owner_ref, sensitivity, status, created_at)
SELECT UUID(), 'ai', 'platform', NULL, 'internal', 'active', NOW(6)
FROM DUAL
WHERE NOT EXISTS (SELECT 1 FROM variable_namespaces WHERE name = 'ai');

INSERT INTO namespace_grants (id, namespace_id, grantee_type, grantee_ref, capability, granted_by, granted_at)
SELECT UUID(), ns.id, 'ai_action', '*', 'write', 'migration-0020', NOW(6)
FROM variable_namespaces ns
WHERE ns.name = 'ai'
  AND NOT EXISTS (
      SELECT 1 FROM namespace_grants g
      WHERE g.namespace_id = ns.id AND g.grantee_type = 'ai_action'
        AND g.capability = 'write'
  );
