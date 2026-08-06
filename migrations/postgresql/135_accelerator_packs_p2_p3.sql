-- Accelerator Packs P2 (versioning + upgrade) and P3 (community packs + fork).
--
-- P2 upgrades a pack WITHOUT clobbering tenant edits. That needs a three-way
-- merge, and a three-way merge needs a base: what the pack shipped at the time
-- it was provisioned. Without that snapshot the platform can only see "pack
-- says X, database says Y" and cannot tell a tenant customisation from a change
-- the pack itself made — so it would have to either overwrite the customer's
-- work or refuse every upgrade.
ALTER TABLE marketplace_pack_installs
    ADD COLUMN IF NOT EXISTS snapshot_json JSONB NOT NULL DEFAULT '{}'::jsonb;

-- Upgrade history. Separate from the install row because an upgrade is an
-- event with an outcome (what applied, what was skipped as a conflict), and
-- flattening it into the install row would lose every prior attempt.
CREATE TABLE IF NOT EXISTS marketplace_pack_upgrades (
    id            UUID PRIMARY KEY,
    tenant_id     VARCHAR(255) NOT NULL,
    package_id    VARCHAR(255) NOT NULL,
    from_version  VARCHAR(64)  NOT NULL,
    to_version    VARCHAR(64)  NOT NULL,
    status        VARCHAR(16)  NOT NULL DEFAULT 'applied',   -- applied | blocked | failed
    applied_count INTEGER      NOT NULL DEFAULT 0,
    skipped_count INTEGER      NOT NULL DEFAULT 0,
    -- Artifacts the pack changed AND the tenant changed. Never auto-resolved:
    -- the whole point is that silently picking a side loses someone's work.
    conflicts_json JSONB       NOT NULL DEFAULT '[]'::jsonb,
    errors_json   JSONB        NOT NULL DEFAULT '[]'::jsonb,
    upgraded_by   VARCHAR(255) NOT NULL,
    upgraded_at   TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS ix_pack_upgrades_lookup
    ON marketplace_pack_upgrades (tenant_id, package_id, upgraded_at DESC);
