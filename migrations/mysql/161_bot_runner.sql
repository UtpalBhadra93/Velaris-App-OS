-- ============================================================================
-- Velaris — MySQL 8 / MariaDB 10.6+ : RPA / bot runner
-- (PostgreSQL migration 161).
--
-- No new runtime: the Marketplace Layer-2 substrate is already digest-pinned,
-- registry-allowlisted, cosign-policed, on an internal network with no route
-- out, non-root, read-only-rootfs, seccomp'd and resource-capped, with zero
-- database credentials in the container. A bot is a different tenant of it.
--
-- Results come back WITHOUT opening broker writes: a per-job, single-use token
-- (SHA-256 on the job row) and an output validated against `output_schema`.
-- Adding write scopes to the scoped broker would put mutation capability inside
-- every container on the apps network, permanently, to serve one feature.
--
-- Dialect notes: CHAR(36) is what `GUID` emits outside PostgreSQL, JSON what
-- `PortableJSON` emits, DATETIME(6) matches `MicroDateTime`. `description` is
-- TEXT so it cannot carry a DEFAULT on MySQL 8 — the ORM supplies ''.
-- ============================================================================

CREATE TABLE IF NOT EXISTS bot_definitions (
    id                 CHAR(36)     NOT NULL,
    tenant_id          VARCHAR(255) NOT NULL,
    name               VARCHAR(255) NOT NULL,
    description        TEXT         NULL,
    image              VARCHAR(512) NOT NULL,
    digest             VARCHAR(80)  NOT NULL,
    signature_verified TINYINT(1)   NOT NULL DEFAULT 0,
    declared_command   JSON         NOT NULL,
    -- NON-SECRET ONLY. Secrets are `credential_refs`, leased per job.
    declared_env       JSON         NOT NULL,
    credential_refs    JSON         NOT NULL,
    input_schema       JSON         NOT NULL,
    output_schema      JSON         NOT NULL,
    granted_domains    JSON         NOT NULL,
    timeout_seconds    INT          NOT NULL DEFAULT 300,
    max_concurrency    INT          NOT NULL DEFAULT 1,
    resource_profile   VARCHAR(20)  NOT NULL DEFAULT 'light',
    status             VARCHAR(20)  NOT NULL DEFAULT 'draft',
    created_by         VARCHAR(255) NOT NULL,
    approved_by        VARCHAR(255) NULL,
    approved_at        DATETIME     NULL,
    created_at         DATETIME(6)  NOT NULL,
    updated_at         DATETIME     NOT NULL,
    PRIMARY KEY (id),
    UNIQUE KEY bot_definitions_name_uq (tenant_id, name)
);

CREATE TABLE IF NOT EXISTS bot_jobs (
    id                   CHAR(36)     NOT NULL,
    tenant_id            VARCHAR(255) NOT NULL,
    bot_id               CHAR(36)     NOT NULL,
    case_id              CHAR(36)     NULL,
    step_id              VARCHAR(255) NULL,
    process_instance_id  CHAR(36)     NULL,
    node_id              VARCHAR(255) NULL,
    -- `trigger` is a reserved word in MySQL and MariaDB (it is not in
    -- PostgreSQL), so it must be quoted here and only here.
    `trigger`            VARCHAR(20)  NOT NULL,
    requested_by         VARCHAR(255) NULL,
    input                JSON         NOT NULL,
    output               JSON         NULL,
    artifacts            JSON         NOT NULL,
    status               VARCHAR(20)  NOT NULL DEFAULT 'queued',
    attempt              INT          NOT NULL DEFAULT 0,
    lease_expires_at     DATETIME     NULL,
    container_id         VARCHAR(80)  NULL,
    result_token_hash    VARCHAR(64)  NULL,
    result_token_used_at DATETIME     NULL,
    error                TEXT         NULL,
    queued_at            DATETIME(6)  NOT NULL,
    started_at           DATETIME     NULL,
    ended_at             DATETIME     NULL,
    PRIMARY KEY (id),
    KEY ix_botjobs_queue (status, queued_at),
    KEY ix_botjobs_case (case_id),
    KEY ix_botjobs_bot (bot_id, status),
    KEY ix_botjobs_tenant (tenant_id, queued_at),
    KEY ix_botjobs_token (result_token_hash),
    CONSTRAINT bot_jobs_bot_fk
        FOREIGN KEY (bot_id) REFERENCES bot_definitions (id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS bot_run_events (
    id         CHAR(36)    NOT NULL,
    job_id     CHAR(36)    NOT NULL,
    kind       VARCHAR(30) NOT NULL,
    detail     JSON        NOT NULL,
    created_at DATETIME(6) NOT NULL,
    PRIMARY KEY (id),
    KEY ix_botevents_job (job_id, created_at),
    CONSTRAINT bot_run_events_job_fk
        FOREIGN KEY (job_id) REFERENCES bot_jobs (id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS bot_credential_leases (
    id         CHAR(36)     NOT NULL,
    job_id     CHAR(36)     NOT NULL,
    vault_ref  VARCHAR(512) NOT NULL,
    leased_at  DATETIME(6)  NOT NULL,
    expires_at DATETIME     NOT NULL,
    revoked_at DATETIME     NULL,
    PRIMARY KEY (id),
    KEY ix_botleases_job (job_id),
    CONSTRAINT bot_credential_leases_job_fk
        FOREIGN KEY (job_id) REFERENCES bot_jobs (id) ON DELETE CASCADE
);

-- The credential store the leases draw from. HxVault is envelope encryption
-- rather than a path-addressed secret store, so the store is this table and the
-- value is encrypted under the tenant's DEK — crypto-shredding a tenant
-- destroys its bot credentials with everything else.
CREATE TABLE IF NOT EXISTS bot_credentials (
    id         CHAR(36)     NOT NULL,
    tenant_id  VARCHAR(255) NOT NULL,
    ref        VARCHAR(512) NOT NULL,
    value_enc  JSON         NOT NULL,
    created_by VARCHAR(255) NOT NULL,
    version    INT          NOT NULL DEFAULT 1,
    created_at DATETIME(6)  NOT NULL,
    updated_at DATETIME     NOT NULL,
    PRIMARY KEY (id),
    -- (255 + 512) chars at utf8mb4 exceeds the 3072-byte InnoDB key limit, so
    -- the unique key is prefixed on `ref`. 191 chars is ample for a vault path
    -- and keeps the index inside the limit.
    UNIQUE KEY bot_credentials_ref_uq (tenant_id, ref(191))
);
