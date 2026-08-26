-- A deployment is a frozen artifact, not a rebuild of whatever dev looks like
-- at the moment somebody clicks Deploy.
--
-- Three things were wrong at once, and one table fixes all of them:
--
-- 1. **The artifact selection in the Promote screen was cosmetic.** The push
--    called `build_bundle(session, run.tenant_id)` with no selection at all, so
--    picking three case types still shipped the entire environment. The manifest
--    the UI assembled reached the risk classifier and nothing else.
--
-- 2. **Nothing recorded what was approved.** `deployment_runs` had no manifest
--    column, so the approver approved a risk summary while the bundle was built
--    fresh at execute time — anything edited in dev in between shipped too, with
--    no record that it had.
--
-- 3. **`deployment_runs.package_id` referenced nothing.** There was no package
--    table anywhere; the column had been nullable and unwritten since P55.
--
-- So: a package is built once from an explicit selection, **sealed** (frozen and
-- checksummed), and it is those bytes that promote — to one environment or to
-- the next one after it. What QA tested and what production receives are the
-- same bundle, and the checksum is what proves it on arrival.
--
-- `status` is `draft` until sealed. A sealed package is immutable: a change is a
-- new version, which is why the unique key carries the version.
CREATE TABLE IF NOT EXISTS deployment_packages (
    id                    UUID         PRIMARY KEY,
    tenant_id             VARCHAR(255) NOT NULL,
    name                  VARCHAR(255) NOT NULL,
    version               VARCHAR(50)  NOT NULL,
    description           TEXT         NOT NULL DEFAULT '',
    -- What was asked for, kept beside what it resolved to. A package that turns
    -- out to hold the wrong thing has to be explainable after the fact.
    selection             JSONB        NOT NULL DEFAULT '{}'::jsonb,
    -- The resolved bundle. Frozen at seal time; never rebuilt.
    bundle                JSONB        NOT NULL DEFAULT '{}'::jsonb,
    -- Section counts, so the list view never has to load the bundle.
    summary               JSONB        NOT NULL DEFAULT '{}'::jsonb,
    -- SHA-256 over the canonical JSON of `bundle`. NULL until sealed.
    checksum              VARCHAR(64),
    bundle_schema_version VARCHAR(10)  NOT NULL DEFAULT '2',
    -- draft | sealed
    status                VARCHAR(20)  NOT NULL DEFAULT 'draft',
    source_env_id         UUID         REFERENCES environment_registry(id) ON DELETE SET NULL,
    created_by            VARCHAR(255) NOT NULL,
    created_at            TIMESTAMP(6) NOT NULL DEFAULT now(),
    updated_at            TIMESTAMPTZ  NOT NULL DEFAULT now(),
    sealed_by             VARCHAR(255),
    sealed_at             TIMESTAMPTZ,
    CONSTRAINT deployment_packages_name_uq UNIQUE (tenant_id, name, version)
);

CREATE INDEX IF NOT EXISTS ix_deployment_packages_tenant ON deployment_packages(tenant_id, status);
CREATE INDEX IF NOT EXISTS ix_deployment_packages_created ON deployment_packages(created_at);

-- The run now names the package it ships AND the checksum it was approved
-- against. Recording the checksum separately is the point: if the two ever
-- disagree, the package was replaced after approval, and that is a question the
-- run row can answer on its own.
ALTER TABLE deployment_runs
    ADD COLUMN IF NOT EXISTS package_checksum VARCHAR(64);

-- `package_id` existed but pointed at no table. Existing rows are all NULL
-- (nothing ever wrote it), so the key can be added without a backfill.
ALTER TABLE deployment_runs
    DROP CONSTRAINT IF EXISTS deployment_runs_package_fk;
ALTER TABLE deployment_runs
    ADD CONSTRAINT deployment_runs_package_fk
        FOREIGN KEY (package_id) REFERENCES deployment_packages(id) ON DELETE SET NULL;

CREATE INDEX IF NOT EXISTS ix_deployment_runs_package ON deployment_runs(package_id);
