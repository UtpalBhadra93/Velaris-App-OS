-- RPA / bot runner — automating systems that have no API.
--
-- **No new runtime.** The Marketplace Layer-2 substrate is already digest-pinned,
-- registry-allowlisted, cosign-policed, on an internal network with no route out
-- by construction, non-root, read-only-rootfs, seccomp'd, cap-dropped and
-- resource-capped, with zero database credentials in the container. A bot is a
-- different kind of tenant of that substrate, not a different substrate. What is
-- new is a job queue, a result path, and credentials.
--
-- **Results come back without opening broker writes.** Adding write scopes to the
-- scoped broker would put mutation capability inside every container on the apps
-- network, permanently, to serve one feature. Instead the bot returns and the
-- platform writes: a per-job, single-use token whose SHA-256 lives on the job
-- row, and an output validated against the bot's declared `output_schema`.

CREATE TABLE IF NOT EXISTS bot_definitions (
    id                 UUID         PRIMARY KEY,
    tenant_id          VARCHAR(255) NOT NULL,
    name               VARCHAR(255) NOT NULL,
    description        TEXT         NOT NULL DEFAULT '',
    image              VARCHAR(512) NOT NULL,
    -- Pinned. The tag is display-only and is never what gets pulled.
    digest             VARCHAR(80)  NOT NULL,
    signature_verified BOOLEAN      NOT NULL DEFAULT FALSE,
    declared_command   JSONB        NOT NULL DEFAULT '[]'::jsonb,
    -- NON-SECRET ONLY. A password here would be static, identical for every
    -- job, and readable by anyone who can read this table. Secrets are
    -- `credential_refs` and are leased per job.
    declared_env       JSONB        NOT NULL DEFAULT '{}'::jsonb,
    credential_refs    JSONB        NOT NULL DEFAULT '[]'::jsonb,
    input_schema       JSONB        NOT NULL DEFAULT '{}'::jsonb,
    output_schema      JSONB        NOT NULL DEFAULT '{}'::jsonb,
    granted_domains    JSONB        NOT NULL DEFAULT '[]'::jsonb,
    timeout_seconds    INTEGER      NOT NULL DEFAULT 300,
    max_concurrency    INTEGER      NOT NULL DEFAULT 1,
    -- 'light' keeps the L2 pid cap; 'browser' raises it, because a Chromium
    -- process tree exceeds 64 immediately.
    resource_profile   VARCHAR(20)  NOT NULL DEFAULT 'light',
    -- 'draft' until an explicit approval by an admin who is not the creator.
    status             VARCHAR(20)  NOT NULL DEFAULT 'draft',
    created_by         VARCHAR(255) NOT NULL,
    approved_by        VARCHAR(255),
    approved_at        TIMESTAMPTZ,
    created_at         TIMESTAMP(6) NOT NULL DEFAULT now(),
    updated_at         TIMESTAMPTZ  NOT NULL DEFAULT now(),
    CONSTRAINT bot_definitions_name_uq UNIQUE (tenant_id, name)
);

CREATE TABLE IF NOT EXISTS bot_jobs (
    id                   UUID         PRIMARY KEY,
    tenant_id            VARCHAR(255) NOT NULL,
    bot_id               UUID         NOT NULL REFERENCES bot_definitions(id) ON DELETE CASCADE,
    case_id              UUID         REFERENCES case_instances(id) ON DELETE SET NULL,
    step_id              VARCHAR(255),
    process_instance_id  UUID,
    node_id              VARCHAR(255),
    trigger              VARCHAR(20)  NOT NULL,
    requested_by         VARCHAR(255),
    input                JSONB        NOT NULL DEFAULT '{}'::jsonb,
    output               JSONB,
    artifacts            JSONB        NOT NULL DEFAULT '[]'::jsonb,
    status               VARCHAR(20)  NOT NULL DEFAULT 'queued',
    attempt              INTEGER      NOT NULL DEFAULT 0,
    -- A crashed runner's lease expires and the job requeues; the lease is what
    -- makes two runners safe without a distributed lock.
    lease_expires_at     TIMESTAMPTZ,
    container_id         VARCHAR(80),
    result_token_hash    VARCHAR(64),
    -- Single use. A second POST with the same token finds this set and is
    -- refused, so a leaked token is a one-shot rather than a standing capability.
    result_token_used_at TIMESTAMPTZ,
    error                TEXT,
    queued_at            TIMESTAMP(6) NOT NULL DEFAULT now(),
    started_at           TIMESTAMPTZ,
    ended_at             TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS ix_botjobs_queue  ON bot_jobs(status, queued_at);
CREATE INDEX IF NOT EXISTS ix_botjobs_case   ON bot_jobs(case_id);
CREATE INDEX IF NOT EXISTS ix_botjobs_bot    ON bot_jobs(bot_id, status);
CREATE INDEX IF NOT EXISTS ix_botjobs_tenant ON bot_jobs(tenant_id, queued_at);
CREATE INDEX IF NOT EXISTS ix_botjobs_token  ON bot_jobs(result_token_hash);

CREATE TABLE IF NOT EXISTS bot_run_events (
    id         UUID         PRIMARY KEY,
    job_id     UUID         NOT NULL REFERENCES bot_jobs(id) ON DELETE CASCADE,
    kind       VARCHAR(30)  NOT NULL,
    detail     JSONB        NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMP(6) NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS ix_botevents_job ON bot_run_events(job_id, created_at);

-- P4 — the part that decides whether this feature is safe. A bot signing into a
-- supplier portal needs that portal's password; it gets a per-job lease with a
-- TTL, revoked at teardown, never a value stored on the definition.
CREATE TABLE IF NOT EXISTS bot_credential_leases (
    id         UUID         PRIMARY KEY,
    job_id     UUID         NOT NULL REFERENCES bot_jobs(id) ON DELETE CASCADE,
    vault_ref  VARCHAR(512) NOT NULL,
    leased_at  TIMESTAMP(6) NOT NULL DEFAULT now(),
    expires_at TIMESTAMPTZ  NOT NULL,
    revoked_at TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS ix_botleases_job ON bot_credential_leases(job_id);

-- The credential store the leases draw from.
--
-- The design assumed HxVault was a path-addressed secret store; it is envelope
-- encryption for data at rest (`hxvault.encrypt/decrypt` via a per-tenant DEK).
-- So the store is this table, and the HxVault property that matters is
-- preserved: the value is encrypted under the tenant's DEK, so crypto-shredding
-- that tenant destroys its bot credentials with everything else, and no secret
-- is readable from a database dump alone.
CREATE TABLE IF NOT EXISTS bot_credentials (
    id         UUID         PRIMARY KEY,
    tenant_id  VARCHAR(255) NOT NULL,
    ref        VARCHAR(512) NOT NULL,
    value_enc  JSONB        NOT NULL,
    created_by VARCHAR(255) NOT NULL,
    -- Rotating bumps this; a lease minted under an older version is dead, which
    -- is what makes "rotating the secret invalidates outstanding leases" true.
    version    INTEGER      NOT NULL DEFAULT 1,
    created_at TIMESTAMP(6) NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ  NOT NULL DEFAULT now(),
    CONSTRAINT bot_credentials_ref_uq UNIQUE (tenant_id, ref)
);
