-- A deployment is a frozen artifact, not a rebuild of whatever dev looks like
-- at the moment somebody clicks Deploy.
--
-- Three things were wrong at once, and one table fixes all of them:
--
-- 1. **The artifact selection in the Promote screen was cosmetic.** The push
--    called `build_bundle(session, run.tenant_id)` with no selection at all, so
--    picking three case types still shipped the entire environment.
--
-- 2. **Nothing recorded what was approved.** `deployment_runs` had no manifest
--    column, so the approver approved a risk summary while the bundle was built
--    fresh at execute time — anything edited in dev in between shipped too.
--
-- 3. **`deployment_runs.package_id` referenced nothing.** There was no package
--    table anywhere; the column had been nullable and unwritten since P55.
--
-- So: a package is built once from an explicit selection, **sealed** (frozen and
-- checksummed), and it is those bytes that promote. What QA tested and what
-- production receives are the same bundle, and the checksum proves it on arrival.
--
-- MySQL notes: no `ADD COLUMN IF NOT EXISTS` (MariaDB has it) and no
-- `CREATE INDEX IF NOT EXISTS`, so the ALTERs below run once. The unique key
-- prefixes `name` — (255 + 255 + 50) chars at utf8mb4 exceeds the 3072-byte
-- InnoDB key limit, the same treatment `schedules` and `bot_credentials` needed.
CREATE TABLE IF NOT EXISTS deployment_packages (
    id                    CHAR(36)     NOT NULL,
    tenant_id             VARCHAR(255) NOT NULL,
    name                  VARCHAR(255) NOT NULL,
    version               VARCHAR(50)  NOT NULL,
    description           TEXT         NOT NULL,
    selection             JSON         NOT NULL,
    bundle                JSON         NOT NULL,
    summary               JSON         NOT NULL,
    checksum              VARCHAR(64)  NULL,
    bundle_schema_version VARCHAR(10)  NOT NULL DEFAULT '2',
    status                VARCHAR(20)  NOT NULL DEFAULT 'draft',
    source_env_id         CHAR(36)     NULL,
    created_by            VARCHAR(255) NOT NULL,
    created_at            DATETIME(6)  NOT NULL,
    updated_at            DATETIME     NOT NULL,
    sealed_by             VARCHAR(255) NULL,
    sealed_at             DATETIME     NULL,
    PRIMARY KEY (id),
    UNIQUE KEY deployment_packages_name_uq (tenant_id, name(150), version),
    KEY ix_deployment_packages_tenant (tenant_id, status),
    KEY ix_deployment_packages_created (created_at),
    CONSTRAINT deployment_packages_env_fk
        FOREIGN KEY (source_env_id) REFERENCES environment_registry (id) ON DELETE SET NULL
);

-- The run now names the package it ships AND the checksum it was approved
-- against. If the two ever disagree, the package was replaced after approval.
ALTER TABLE deployment_runs
    ADD COLUMN package_checksum VARCHAR(64) NULL;

-- `package_id` existed but pointed at no table. Existing rows are all NULL
-- (nothing ever wrote it), so the key can be added without a backfill.
ALTER TABLE deployment_runs
    ADD KEY ix_deployment_runs_package (package_id),
    ADD CONSTRAINT deployment_runs_package_fk
        FOREIGN KEY (package_id) REFERENCES deployment_packages (id) ON DELETE SET NULL;
