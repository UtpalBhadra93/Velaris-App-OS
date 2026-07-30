-- HxSentinel P4a — marketplace signal packs (vertical rule bundles as CONTENT).
--
-- A signal pack is CONTENT, never code (Layer-3 stays forbidden): a manifest of
-- declarative rules whose conditions are safe-expression strings the in-core
-- engine evaluates against a fixed, flat feature context. The expressions are
-- validated CONFORMING (the strict grammar in core.safe_expression) at install
-- time, so a pack can express new rule logic without shipping executable code.
--
-- Packs are tenant-scoped: installing one activates its rules for that tenant
-- only. The manifest is stored verbatim so the rules stay inspectable.
CREATE TABLE IF NOT EXISTS case_risk_signal_packs (
    id           UUID PRIMARY KEY,
    tenant_id    VARCHAR(255) NOT NULL,
    pack_id      VARCHAR(128) NOT NULL,      -- stable id from the manifest
    name         VARCHAR(200) NOT NULL,
    vertical     VARCHAR(64),
    version      VARCHAR(32)  NOT NULL,
    manifest     JSONB        NOT NULL,      -- the full validated manifest (rules + taxonomy)
    enabled      BOOLEAN      NOT NULL DEFAULT TRUE,
    installed_by VARCHAR(255) NOT NULL,
    installed_at TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);
-- One install of a given pack per tenant; "packs enabled for this tenant" scan.
CREATE UNIQUE INDEX IF NOT EXISTS ux_risk_packs_tenant_pack
    ON case_risk_signal_packs (tenant_id, pack_id);
CREATE INDEX IF NOT EXISTS ix_risk_packs_tenant_enabled
    ON case_risk_signal_packs (tenant_id, enabled);
