-- Item 12 (P0) — Customer Engagement: the identity spine.
-- MySQL 8.4 / MariaDB 11.8 sibling of migrations/postgresql/196. See that file
-- for why each column exists and for the four decisions taken with the user.
--
-- Dialect notes:
--  * UUID columns are CHAR(36); TIMESTAMPTZ becomes DATETIME(6) with no DEFAULT.
--  * JSONB becomes JSON, which takes no literal DEFAULT.
--  * BOOLEAN becomes TINYINT(1).
--  * Indexes are inline KEYs so the file is re-runnable on all three engines.

CREATE TABLE IF NOT EXISTS customer_profiles (
    id               CHAR(36)     PRIMARY KEY,
    tenant_id        CHAR(36)     NOT NULL,

    party_kind       VARCHAR(16)  NOT NULL DEFAULT 'person',
    display_name     VARCHAR(255) NOT NULL,

    status           VARCHAR(16)  NOT NULL DEFAULT 'active',
    merged_into_id   CHAR(36)     NULL,

    source_of_truth  VARCHAR(16)  NOT NULL DEFAULT 'velaris',
    external_system  VARCHAR(64)  NULL,
    attribute_source JSON         NULL,
    refreshed_at     DATETIME(6)  NULL,

    source           VARCHAR(16)  NOT NULL DEFAULT 'agent',
    notes            TEXT         NULL,

    created_by       VARCHAR(255) NULL,
    created_at       DATETIME(6)  NOT NULL,
    updated_at       DATETIME(6)  NOT NULL,

    KEY ix_cprofile_tenant (tenant_id, status),
    KEY ix_cprofile_merged (merged_into_id),

    CONSTRAINT fk_cprofile_tenant FOREIGN KEY (tenant_id)
        REFERENCES tenants (id) ON DELETE CASCADE,
    CONSTRAINT fk_cprofile_merged FOREIGN KEY (merged_into_id)
        REFERENCES customer_profiles (id) ON DELETE SET NULL
);


CREATE TABLE IF NOT EXISTS customer_identifiers (
    id               CHAR(36)     PRIMARY KEY,
    tenant_id        CHAR(36)     NOT NULL,
    customer_id      CHAR(36)     NOT NULL,

    kind             VARCHAR(24)  NOT NULL,
    value_normalised VARCHAR(255) NOT NULL,
    value_display    VARCHAR(255) NOT NULL,

    verified         TINYINT(1)   NOT NULL DEFAULT 0,
    verified_at      DATETIME(6)  NULL,
    verified_by      VARCHAR(16)  NULL,

    is_primary       TINYINT(1)   NOT NULL DEFAULT 0,
    source           VARCHAR(16)  NOT NULL DEFAULT 'agent',
    created_at       DATETIME(6)  NOT NULL,

    KEY ix_cident_customer (customer_id, kind),

    CONSTRAINT ux_cident_value UNIQUE (tenant_id, kind, value_normalised),
    CONSTRAINT fk_cident_tenant FOREIGN KEY (tenant_id)
        REFERENCES tenants (id) ON DELETE CASCADE,
    CONSTRAINT fk_cident_customer FOREIGN KEY (customer_id)
        REFERENCES customer_profiles (id) ON DELETE CASCADE
);


CREATE TABLE IF NOT EXISTS customer_relations (
    id               CHAR(36)     PRIMARY KEY,
    tenant_id        CHAR(36)     NOT NULL,
    from_customer_id CHAR(36)     NOT NULL,
    to_customer_id   CHAR(36)     NOT NULL,
    relation         VARCHAR(24)  NOT NULL,
    may_act_for      TINYINT(1)   NOT NULL DEFAULT 0,
    note             TEXT         NULL,
    created_by       VARCHAR(255) NULL,
    created_at       DATETIME(6)  NOT NULL,

    KEY ix_crel_to (to_customer_id),

    CONSTRAINT ux_crel_edge UNIQUE (from_customer_id, to_customer_id, relation),
    CONSTRAINT fk_crel_tenant FOREIGN KEY (tenant_id)
        REFERENCES tenants (id) ON DELETE CASCADE,
    CONSTRAINT fk_crel_from FOREIGN KEY (from_customer_id)
        REFERENCES customer_profiles (id) ON DELETE CASCADE,
    CONSTRAINT fk_crel_to FOREIGN KEY (to_customer_id)
        REFERENCES customer_profiles (id) ON DELETE CASCADE
);


CREATE TABLE IF NOT EXISTS customer_links (
    id           CHAR(36)     PRIMARY KEY,
    tenant_id    CHAR(36)     NOT NULL,
    customer_id  CHAR(36)     NOT NULL,
    object_kind  VARCHAR(24)  NOT NULL,
    object_id    CHAR(36)     NOT NULL,
    link_reason  VARCHAR(16)  NOT NULL DEFAULT 'resolved',
    linked_by    VARCHAR(255) NULL,
    linked_at    DATETIME(6)  NOT NULL,

    KEY ix_clink_lookup (tenant_id, object_kind, object_id),

    CONSTRAINT ux_clink_object UNIQUE (customer_id, object_kind, object_id),
    CONSTRAINT fk_clink_tenant FOREIGN KEY (tenant_id)
        REFERENCES tenants (id) ON DELETE CASCADE,
    CONSTRAINT fk_clink_customer FOREIGN KEY (customer_id)
        REFERENCES customer_profiles (id) ON DELETE CASCADE
);


CREATE TABLE IF NOT EXISTS customer_merges (
    id         CHAR(36)     PRIMARY KEY,
    tenant_id  CHAR(36)     NOT NULL,
    winner_id  CHAR(36)     NOT NULL,
    loser_id   CHAR(36)     NOT NULL,
    moved      JSON         NULL,
    reason     TEXT         NOT NULL,
    merged_by  VARCHAR(255) NOT NULL,
    merged_at  DATETIME(6)  NOT NULL,
    undone_at  DATETIME(6)  NULL,
    undone_by  VARCHAR(255) NULL,

    KEY ix_cmerge_winner (winner_id, merged_at),

    CONSTRAINT fk_cmerge_tenant FOREIGN KEY (tenant_id)
        REFERENCES tenants (id) ON DELETE CASCADE,
    CONSTRAINT fk_cmerge_winner FOREIGN KEY (winner_id)
        REFERENCES customer_profiles (id) ON DELETE CASCADE,
    CONSTRAINT fk_cmerge_loser FOREIGN KEY (loser_id)
        REFERENCES customer_profiles (id) ON DELETE CASCADE
);


CREATE TABLE IF NOT EXISTS customer_interactions (
    id                 CHAR(36)     PRIMARY KEY,
    tenant_id          CHAR(36)     NOT NULL,
    customer_id        CHAR(36)     NULL,

    channel            VARCHAR(16)  NOT NULL,
    direction          VARCHAR(8)   NOT NULL DEFAULT 'inbound',
    started_at         DATETIME(6)  NOT NULL,
    ended_at           DATETIME(6)  NULL,
    agent_user_id      VARCHAR(255) NULL,

    external_call_id   VARCHAR(255) NULL,
    ani                VARCHAR(64)  NULL,
    dnis               VARCHAR(64)  NULL,
    `queue`            VARCHAR(128) NULL,
    transferred_from   CHAR(36)     NULL,

    verification_level VARCHAR(8)   NOT NULL DEFAULT 'none',
    verified_by        VARCHAR(16)  NULL,
    verified_at        DATETIME(6)  NULL,

    disposition_code   VARCHAR(64)  NULL,
    wrap_up_notes      TEXT         NULL,
    status             VARCHAR(16)  NOT NULL DEFAULT 'open',
    outcome            VARCHAR(64)  NULL,

    created_at         DATETIME(6)  NOT NULL,

    KEY ix_cint_customer (customer_id, started_at),
    KEY ix_cint_tenant   (tenant_id, status, started_at),
    KEY ix_cint_agent    (agent_user_id, started_at),

    CONSTRAINT fk_cint_tenant FOREIGN KEY (tenant_id)
        REFERENCES tenants (id) ON DELETE CASCADE,
    CONSTRAINT fk_cint_customer FOREIGN KEY (customer_id)
        REFERENCES customer_profiles (id) ON DELETE SET NULL,
    CONSTRAINT fk_cint_transfer FOREIGN KEY (transferred_from)
        REFERENCES customer_interactions (id) ON DELETE SET NULL
);


CREATE TABLE IF NOT EXISTS customer_disclosures (
    id              CHAR(36)     PRIMARY KEY,
    tenant_id       CHAR(36)     NOT NULL,
    customer_id     CHAR(36)     NOT NULL,
    interaction_id  CHAR(36)     NULL,
    field_path      VARCHAR(128) NOT NULL,
    reason          TEXT         NOT NULL,
    at_verification VARCHAR(8)   NOT NULL,
    revealed_by     VARCHAR(255) NOT NULL,
    revealed_at     DATETIME(6)  NOT NULL,

    KEY ix_cdisc_customer (customer_id, revealed_at),
    KEY ix_cdisc_agent    (revealed_by, revealed_at),

    CONSTRAINT fk_cdisc_tenant FOREIGN KEY (tenant_id)
        REFERENCES tenants (id) ON DELETE CASCADE,
    CONSTRAINT fk_cdisc_customer FOREIGN KEY (customer_id)
        REFERENCES customer_profiles (id) ON DELETE CASCADE,
    CONSTRAINT fk_cdisc_interaction FOREIGN KEY (interaction_id)
        REFERENCES customer_interactions (id) ON DELETE SET NULL
);
