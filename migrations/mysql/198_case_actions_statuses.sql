-- 198 — case statuses become data; case actions get one dispatcher.
-- MySQL 8.4 / MariaDB 11.8 sibling of migrations/postgresql/198. See that file
-- for why each column exists and for the split between a status (a label the
-- customer owns) and an action (behaviour the platform owns).
--
-- Dialect notes:
--  * UUID columns are CHAR(36); TIMESTAMPTZ becomes DATETIME(6) with no DEFAULT.
--  * JSONB becomes JSON, which takes no literal DEFAULT — so `allowed_from` is
--    NULL-able here and NULL is read as the empty list, which is the same
--    permissive "from any status" the Postgres '[]' default means.
--  * BOOLEAN becomes TINYINT(1).
--  * `scope_key` is written by the application on BOTH dialects rather than
--    GENERATED, because MariaDB 11.8 refuses COALESCE in a GENERATED ALWAYS AS
--    clause (error 1901) where MySQL 8.4 accepts it — migration 177 met this
--    first. The CHECK keeps it honest without calling a function.
--  * Indexes are inline KEYs so the file is re-runnable on all three engines.

CREATE TABLE IF NOT EXISTS case_statuses (
    id            CHAR(36)     PRIMARY KEY,

    tenant_id     CHAR(36)     NULL,
    case_type_id  CHAR(36)     NULL,

    -- `<tenant_id|platform>:<case_type_id|any>`. Exists because NULL <> NULL:
    -- a plain UNIQUE over the two nullable ids would permit unlimited duplicate
    -- platform rows for `closed`, and then "is closed terminal" depends on
    -- which row was read first.
    scope_key     VARCHAR(80)  NOT NULL,

    name          VARCHAR(30)  NOT NULL,
    label         VARCHAR(100) NOT NULL,

    -- open | working | waiting | terminal
    category      VARCHAR(16)  NOT NULL DEFAULT 'open',

    -- First-class rather than derived from `category`: deriving it is the
    -- indirection that let the two hardcoded terminal tuples drift.
    is_terminal   TINYINT(1)   NOT NULL DEFAULT 0,
    is_default    TINYINT(1)   NOT NULL DEFAULT 0,

    -- NULL or [] both mean "reachable from any status" — the permissive
    -- default every seeded row ships with, so no transition that works today
    -- stops working.
    allowed_from  JSON         NULL,

    sort_order    INT          NOT NULL DEFAULT 0,
    enabled       TINYINT(1)   NOT NULL DEFAULT 1,

    created_at    DATETIME(6)  NOT NULL,
    updated_at    DATETIME(6)  NOT NULL,

    CONSTRAINT ck_case_statuses_scope_key CHECK (
        (tenant_id IS NULL     AND case_type_id IS NULL     AND scope_key = 'platform:any') OR
        (tenant_id IS NULL     AND case_type_id IS NOT NULL AND scope_key = CONCAT('platform:', case_type_id)) OR
        (tenant_id IS NOT NULL AND case_type_id IS NULL     AND scope_key = CONCAT(tenant_id, ':any')) OR
        (tenant_id IS NOT NULL AND case_type_id IS NOT NULL AND scope_key = CONCAT(tenant_id, ':', case_type_id))
    ),
    CONSTRAINT ux_case_statuses_scope UNIQUE (scope_key, name),
    KEY idx_case_statuses_lookup (tenant_id, case_type_id, enabled),

    CONSTRAINT fk_case_statuses_tenant FOREIGN KEY (tenant_id)
        REFERENCES tenants (id) ON DELETE CASCADE,
    CONSTRAINT fk_case_statuses_case_type FOREIGN KEY (case_type_id)
        REFERENCES case_types (id) ON DELETE CASCADE
);


CREATE TABLE IF NOT EXISTS case_action_config (
    id            CHAR(36)     PRIMARY KEY,

    tenant_id     CHAR(36)     NULL,
    case_type_id  CHAR(36)     NULL,
    scope_key     VARCHAR(80)  NOT NULL,

    -- Must be a name in the code registry. Validated on write, not ignored on
    -- read: a configuration row that silently does nothing is worse than a
    -- refusal.
    action        VARCHAR(40)  NOT NULL,

    label         VARCHAR(100) NULL,
    -- 0 makes the action unreachable through the DISPATCHER, not merely hidden
    -- in the UI. A switch that only hides is decoration.
    enabled       TINYINT(1)   NOT NULL DEFAULT 1,
    sort_order    INT          NOT NULL DEFAULT 0,

    privilege     VARCHAR(80)  NULL,
    allowed_from  JSON         NULL,

    created_at    DATETIME(6)  NOT NULL,
    updated_at    DATETIME(6)  NOT NULL,

    CONSTRAINT ck_case_action_config_scope_key CHECK (
        (tenant_id IS NULL     AND case_type_id IS NULL     AND scope_key = 'platform:any') OR
        (tenant_id IS NULL     AND case_type_id IS NOT NULL AND scope_key = CONCAT('platform:', case_type_id)) OR
        (tenant_id IS NOT NULL AND case_type_id IS NULL     AND scope_key = CONCAT(tenant_id, ':any')) OR
        (tenant_id IS NOT NULL AND case_type_id IS NOT NULL AND scope_key = CONCAT(tenant_id, ':', case_type_id))
    ),
    CONSTRAINT ux_case_action_config_scope UNIQUE (scope_key, action),
    KEY idx_case_action_config_lookup (tenant_id, case_type_id, enabled),

    CONSTRAINT fk_case_action_config_tenant FOREIGN KEY (tenant_id)
        REFERENCES tenants (id) ON DELETE CASCADE,
    CONSTRAINT fk_case_action_config_case_type FOREIGN KEY (case_type_id)
        REFERENCES case_types (id) ON DELETE CASCADE
);


-- ── seed: the platform default status set ───────────────────────────────────
-- Six are in use today; five are new and unused until something sets them.
-- `is_terminal` matches the two hardcoded tuples exactly, which is what makes
-- replacing them with a lookup change no answer.

INSERT IGNORE INTO case_statuses
    (id, tenant_id, case_type_id, scope_key, name, label, category, is_terminal, is_default, sort_order, created_at, updated_at)
VALUES
    ('a1000000-0000-4000-8000-000000000001', NULL, NULL, 'platform:any', 'new',         'New',         'open',     0, 1,  10, NOW(6), NOW(6)),
    ('a1000000-0000-4000-8000-000000000002', NULL, NULL, 'platform:any', 'open',        'Open',        'open',     0, 0,  20, NOW(6), NOW(6)),
    ('a1000000-0000-4000-8000-000000000003', NULL, NULL, 'platform:any', 'in_progress', 'In Progress', 'working',  0, 0,  30, NOW(6), NOW(6)),
    ('a1000000-0000-4000-8000-000000000004', NULL, NULL, 'platform:any', 'pending',     'Pending',     'waiting',  0, 0,  40, NOW(6), NOW(6)),
    ('a1000000-0000-4000-8000-000000000005', NULL, NULL, 'platform:any', 'on_hold',     'On Hold',     'waiting',  0, 0,  50, NOW(6), NOW(6)),
    ('a1000000-0000-4000-8000-000000000006', NULL, NULL, 'platform:any', 'escalated',   'Escalated',   'working',  0, 0,  60, NOW(6), NOW(6)),
    ('a1000000-0000-4000-8000-000000000007', NULL, NULL, 'platform:any', 'resolved',    'Resolved',    'terminal', 1, 0,  70, NOW(6), NOW(6)),
    ('a1000000-0000-4000-8000-000000000008', NULL, NULL, 'platform:any', 'closed',      'Closed',      'terminal', 1, 0,  80, NOW(6), NOW(6)),
    ('a1000000-0000-4000-8000-000000000009', NULL, NULL, 'platform:any', 'reopened',    'Reopened',    'open',     0, 0,  90, NOW(6), NOW(6)),
    ('a1000000-0000-4000-8000-00000000000a', NULL, NULL, 'platform:any', 'cancelled',   'Cancelled',   'terminal', 1, 0, 100, NOW(6), NOW(6)),
    ('a1000000-0000-4000-8000-00000000000b', NULL, NULL, 'platform:any', 'archived',    'Archived',    'terminal', 1, 0, 110, NOW(6), NOW(6)),
    -- `pending_external` and `pending_subcase` are NOT new. They are live
    -- platform statuses: `core/case_lifecycle.py` PAUSES a case's SLAs on
    -- reaching either and resumes them on leaving, `relationship_manager.py`
    -- moves a parent off `pending_subcase` when its child resolves, and
    -- analytics reports on both. No case on this box happens to be in either
    -- state, so neither showed up in the six statuses actually in use — and
    -- omitting them would have made the sub-case feature refuse its own
    -- transition the first time a parent waited on a child.
    ('a1000000-0000-4000-8000-00000000000c', NULL, NULL, 'platform:any', 'pending_external', 'Pending External', 'waiting', 0, 0, 42, NOW(6), NOW(6)),
    ('a1000000-0000-4000-8000-00000000000d', NULL, NULL, 'platform:any', 'pending_subcase',  'Pending Subcase',  'waiting', 0, 0, 44, NOW(6), NOW(6));


-- Statuses a case already holds that the seed does not define. Should be zero
-- here. A non-empty result is not an error: those rows keep working, because
-- validation applies to WRITES. It is a list an operator should add labels for.
SELECT ci.status AS undefined_status, COUNT(*) AS cases
  FROM case_instances ci
 WHERE NOT EXISTS (
       SELECT 1 FROM case_statuses cs
        WHERE cs.tenant_id IS NULL AND cs.case_type_id IS NULL
          AND cs.name = ci.status)
 GROUP BY ci.status;
