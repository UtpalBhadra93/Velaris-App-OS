-- F-10 (first slice) — offline-sovereign field operations, the HUB half.
-- MySQL 8.4 / MariaDB 11.8 sibling of migrations/postgresql/193. See that file
-- for why each column exists, and for what is deliberately NOT built.
--
-- Dialect notes:
--  * UUID columns are CHAR(36); TIMESTAMPTZ becomes DATETIME(6) with no DEFAULT.
--  * JSONB becomes JSON, which takes no literal DEFAULT — the writer supplies it.
--  * Indexes are inline KEYs so the file is re-runnable on all three engines.

CREATE TABLE IF NOT EXISTS field_nodes (
    id                     CHAR(36)     PRIMARY KEY,
    tenant_id              CHAR(36)     NOT NULL,
    slug                   VARCHAR(64)  NOT NULL,
    display_name           VARCHAR(255) NOT NULL,

    public_key             TEXT         NOT NULL,
    key_id                 VARCHAR(64)  NOT NULL,

    status                 VARCHAR(20)  NOT NULL DEFAULT 'active',

    charter                JSON         NOT NULL,
    ceiling                VARCHAR(20)  NOT NULL DEFAULT 'internal',

    snapshot_max_age_hours INT          NOT NULL DEFAULT 72,

    subchain_head          VARCHAR(64)  NULL,
    last_contact_at        DATETIME(6)  NULL,
    shred_ordered_at       DATETIME(6)  NULL,
    shred_reason           TEXT         NULL,
    shredded_at            DATETIME(6)  NULL,

    created_by             VARCHAR(255) NULL,
    created_at             DATETIME(6)  NOT NULL,
    updated_at             DATETIME(6)  NOT NULL,

    CONSTRAINT ux_field_node_slug UNIQUE (tenant_id, slug),
    CONSTRAINT fk_field_node_tenant FOREIGN KEY (tenant_id)
        REFERENCES tenants (id) ON DELETE CASCADE
);


CREATE TABLE IF NOT EXISTS field_dispatches (
    id                   CHAR(36)     PRIMARY KEY,
    node_id              CHAR(36)     NOT NULL,
    tenant_id            CHAR(36)     NOT NULL,

    case_ids             JSON         NOT NULL,
    projection           JSON         NULL,

    policy_snapshot      JSON         NULL,
    policy_snapshot_hash VARCHAR(64)  NULL,
    offline_deny         JSON         NOT NULL,

    subchain_genesis     VARCHAR(64)  NOT NULL,

    status               VARCHAR(16)  NOT NULL DEFAULT 'issued',
    issued_at            DATETIME(6)  NOT NULL,
    stale_after          DATETIME(6)  NOT NULL,
    synced_at            DATETIME(6)  NULL,

    created_by           VARCHAR(255) NULL,

    KEY ix_field_disp_node (node_id, issued_at),

    CONSTRAINT fk_field_disp_node FOREIGN KEY (node_id)
        REFERENCES field_nodes (id) ON DELETE CASCADE,
    CONSTRAINT fk_field_disp_tenant FOREIGN KEY (tenant_id)
        REFERENCES tenants (id) ON DELETE CASCADE
);


CREATE TABLE IF NOT EXISTS field_sync_events (
    id              CHAR(36)     PRIMARY KEY,
    node_id         CHAR(36)     NOT NULL,
    dispatch_id     CHAR(36)     NULL,
    tenant_id       CHAR(36)     NOT NULL,
    case_id         CHAR(36)     NULL,

    seq             INT          NOT NULL,
    kind            VARCHAR(24)  NOT NULL,
    target          VARCHAR(255) NULL,
    payload         JSON         NULL,
    payload_sha256  VARCHAR(64)  NOT NULL,
    node_time       DATETIME(6)  NULL,

    prev_hash       VARCHAR(64)  NOT NULL,
    content_hash    VARCHAR(64)  NOT NULL,

    merge_state     VARCHAR(16)  NOT NULL DEFAULT 'applied',
    conflict_reason TEXT         NULL,
    resolution      VARCHAR(16)  NULL,
    resolved_by     VARCHAR(255) NULL,
    resolved_at     DATETIME(6)  NULL,

    received_at     DATETIME(6)  NOT NULL,

    KEY ix_field_ev_node  (node_id, seq),
    KEY ix_field_ev_queue (tenant_id, merge_state),
    KEY ix_field_ev_case  (case_id, received_at),

    CONSTRAINT fk_field_ev_node FOREIGN KEY (node_id)
        REFERENCES field_nodes (id) ON DELETE CASCADE,
    CONSTRAINT fk_field_ev_disp FOREIGN KEY (dispatch_id)
        REFERENCES field_dispatches (id) ON DELETE SET NULL,
    CONSTRAINT fk_field_ev_tenant FOREIGN KEY (tenant_id)
        REFERENCES tenants (id) ON DELETE CASCADE
);
