-- HxSandbox (#17) — storage for customer-authored WASM modules.
--
-- The WASM execution host has existed since Phase 2 but nothing called it:
-- `runtime: wasm` packages were refused at install because there was no
-- runtime to run them. This table is half of what unblocks that — the bytes
-- have to live somewhere before a sync pipeline can call a transform per row.
--
-- Stored at INSTALL time, deliberately:
--   * a pipeline must not depend on the publisher still being reachable;
--   * `validate_module()` is meant to reject a malformed guest before first
--     run, not halfway through a sync.
--
-- Tenant-scoped so revoking one tenant's install cannot disarm another's.
-- `sha256` is the digest of the stored bytes, so a module can be re-verified
-- without re-downloading it.

CREATE TABLE IF NOT EXISTS hxsandbox_modules (
    id           UUID PRIMARY KEY,
    tenant_id    VARCHAR(255) NOT NULL,
    package_id   VARCHAR(255) NOT NULL,
    version      VARCHAR(64)  NOT NULL,
    wasm_bytes   BYTEA        NOT NULL,
    sha256       VARCHAR(64)  NOT NULL,
    installed_at TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_hxsandbox_module_tenant_pkg UNIQUE (tenant_id, package_id)
);
