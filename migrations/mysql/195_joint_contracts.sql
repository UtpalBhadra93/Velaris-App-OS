-- F-12 (first slice) — process-as-negotiation. The capstone.
-- MySQL 8.4 / MariaDB 11.8 sibling of migrations/postgresql/195. See that file
-- for why each column exists, and for what is deliberately NOT built.
--
-- Dialect notes:
--  * UUID columns are CHAR(36); TIMESTAMPTZ becomes DATETIME(6) with no DEFAULT.
--  * JSONB becomes JSON, which takes no literal DEFAULT — the writer supplies it.
--  * Indexes are inline KEYs so the file is re-runnable on all three engines.

CREATE TABLE IF NOT EXISTS joint_contracts (
    id                 CHAR(36)     PRIMARY KEY,
    tenant_id          CHAR(36)     NOT NULL,
    counterparty_id    CHAR(36)     NOT NULL,

    name               VARCHAR(255) NOT NULL,
    version            INT          NOT NULL DEFAULT 1,
    supersedes         CHAR(36)     NULL,

    our_offer          JSON         NOT NULL,
    their_offer        JSON         NOT NULL,
    our_requirements   JSON         NOT NULL,
    their_requirements JSON         NOT NULL,

    composition        JSON         NULL,
    resolution         JSON         NULL,
    effective_ceiling  VARCHAR(20)  NULL,

    contract_sha256    VARCHAR(64)  NULL,
    our_signature      TEXT         NULL,
    their_signature    TEXT         NULL,

    status             VARCHAR(16)  NOT NULL DEFAULT 'draft',
    unmatched_reason   TEXT         NULL,

    approved_by        VARCHAR(255) NULL,
    approved_at        DATETIME(6)  NULL,
    enforced_at        DATETIME(6)  NULL,
    suspended_at       DATETIME(6)  NULL,
    suspended_by       VARCHAR(255) NULL,
    suspended_reason   TEXT         NULL,

    created_by         VARCHAR(255) NULL,
    created_at         DATETIME(6)  NOT NULL,
    updated_at         DATETIME(6)  NOT NULL,

    KEY ix_contract_tenant (tenant_id, status),
    KEY ix_contract_party  (counterparty_id, version),

    CONSTRAINT fk_contract_tenant FOREIGN KEY (tenant_id)
        REFERENCES tenants (id) ON DELETE CASCADE,
    CONSTRAINT fk_contract_party FOREIGN KEY (counterparty_id)
        REFERENCES handoff_counterparties (id) ON DELETE CASCADE,
    CONSTRAINT fk_contract_supersedes FOREIGN KEY (supersedes)
        REFERENCES joint_contracts (id) ON DELETE SET NULL
);


CREATE TABLE IF NOT EXISTS contract_anchors (
    id          CHAR(36)     PRIMARY KEY,
    contract_id CHAR(36)     NOT NULL,
    tenant_id   CHAR(36)     NOT NULL,

    direction   VARCHAR(8)   NOT NULL,
    seq         INT          NOT NULL,
    chain_head  VARCHAR(64)  NOT NULL,
    signature   TEXT         NULL,
    divergence  TEXT         NULL,

    anchored_at DATETIME(6)  NOT NULL,

    KEY ix_anchor_contract (contract_id, direction, seq),

    CONSTRAINT fk_anchor_contract FOREIGN KEY (contract_id)
        REFERENCES joint_contracts (id) ON DELETE CASCADE,
    CONSTRAINT fk_anchor_tenant FOREIGN KEY (tenant_id)
        REFERENCES tenants (id) ON DELETE CASCADE
);
