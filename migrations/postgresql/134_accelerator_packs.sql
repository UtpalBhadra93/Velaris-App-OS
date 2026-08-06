-- Accelerator Packs P1 — provisioning record for installed vertical content packs.
--
-- A pack is CONTENT, never code: case types, forms, rules, SLAs and (optionally)
-- sample data. Provisioning delegates to the same `apply_bundle` importer that
-- HxDeploy uses to promote artifacts between environments, so a pack can never
-- do anything a promotion bundle could not.
--
-- Installing a pack from the marketplace and PROVISIONING it into a tenant are
-- deliberately separate: install records that the tenant is entitled to the
-- content, provisioning is the moment case types actually appear in their
-- Studio. This table records the second one.
--
-- P1 provisions ONCE. `apply_bundle` upserts, so re-provisioning would silently
-- overwrite whatever the tenant has since edited on those case types — which is
-- precisely the failure P2's delta/merge exists to prevent. Blocking is
-- reversible; an overwritten customer workflow is not. The unique index below
-- is what makes that guarantee real rather than a check someone can forget.

CREATE TABLE IF NOT EXISTS marketplace_pack_installs (
    id             UUID PRIMARY KEY,
    tenant_id      VARCHAR(255) NOT NULL,
    package_id     VARCHAR(255) NOT NULL,
    pack_version   VARCHAR(64)  NOT NULL,
    -- The vertical this pack targets (claims, onboarding, kyc, …). Free text:
    -- packs are content and new verticals must not need a schema change.
    vertical       VARCHAR(64),
    status         VARCHAR(16)  NOT NULL DEFAULT 'provisioned',   -- provisioned | failed
    -- What actually landed, from apply_bundle's own return value — not a
    -- prediction made before the import ran.
    imported_count INTEGER      NOT NULL DEFAULT 0,
    skipped_count  INTEGER      NOT NULL DEFAULT 0,
    -- Items the pack could not fill in itself (email accounts, connector
    -- credentials). Surfaced to the operator as the post-install checklist.
    needs_config   JSONB        NOT NULL DEFAULT '[]'::jsonb,
    errors_json    JSONB        NOT NULL DEFAULT '[]'::jsonb,
    -- Sample data is opt-in and off by default: quietly creating demo cases in
    -- a live tenant is not a reasonable default.
    sample_data    BOOLEAN      NOT NULL DEFAULT FALSE,
    sample_count   INTEGER      NOT NULL DEFAULT 0,
    provisioned_by VARCHAR(255) NOT NULL,
    provisioned_at TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

-- One SUCCESSFUL provision per (tenant, pack). A failed attempt is kept for
-- diagnosis and must NOT block a retry, so the rule is "one row with
-- status='provisioned'" rather than "one row".
--
-- That is a PARTIAL unique index, which MySQL/MariaDB do not support, so it is
-- enforced in code (`packs.already_provisioned`) exactly as the Layer-1
-- capability grants do for their one-active-grant rule. A partial index here
-- would pass on Postgres and silently do nothing on MySQL — worse than no
-- constraint, because it would look enforced.
CREATE INDEX IF NOT EXISTS ix_pack_installs_lookup
    ON marketplace_pack_installs (tenant_id, package_id, status);

CREATE INDEX IF NOT EXISTS ix_pack_installs_tenant
    ON marketplace_pack_installs (tenant_id, provisioned_at DESC);
