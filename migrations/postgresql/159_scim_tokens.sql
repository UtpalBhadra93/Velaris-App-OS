-- Enterprise identity P2 — SCIM machine tokens.
--
-- SCIM callers are identity providers, not people: Entra and Okta hold a static
-- bearer token and post user lifecycle at us on their own schedule. A Velaris
-- JWT is the wrong credential for that — it expires, it belongs to a person who
-- may leave, and the roles it carries are a human's.
--
-- The shape follows the two machine-token systems already proven in this
-- codebase (`hxmcp/tokens.py`, `api/routers/broker.py`):
--
--   * stored as SHA-256 only. The plaintext is shown once, at mint, and cannot
--     be recovered — a database read must not yield a working credential;
--   * `token_hash` is UNIQUE, so lookup is a single indexed probe rather than a
--     scan comparing every row;
--   * **the tenant lives on the row, never in the request.** A SCIM client that
--     could name its own tenant could enumerate and rewrite every other one;
--   * `revoked_at` makes revocation instant and auditable, where deleting the
--     row would leave nothing to explain why the IdP started getting 401s;
--   * `expires_at` is optional because an IdP integration that stops working
--     silently at 90 days is worse than one an admin rotates deliberately.

CREATE TABLE IF NOT EXISTS scim_tokens (
    id           UUID         PRIMARY KEY,
    tenant_id    VARCHAR(255),
    name         VARCHAR(255) NOT NULL,
    token_hash   VARCHAR(64)  NOT NULL,
    scopes       JSONB        NOT NULL DEFAULT '[]'::jsonb,
    created_by   VARCHAR(255) NOT NULL,
    last_used_at TIMESTAMPTZ,
    expires_at   TIMESTAMPTZ,
    revoked_at   TIMESTAMPTZ,
    created_at   TIMESTAMP(6) NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX IF NOT EXISTS ix_scim_tokens_hash ON scim_tokens(token_hash);
CREATE INDEX IF NOT EXISTS ix_scim_tokens_tenant ON scim_tokens(tenant_id);

COMMENT ON COLUMN scim_tokens.token_hash IS
    'SHA-256 of the token. The plaintext is shown once at mint and never stored.';
COMMENT ON COLUMN scim_tokens.tenant_id IS
    'The tenant this token provisions into. Comes from here, NEVER from the '
    'request — a client that could name its own tenant could rewrite any other.';
