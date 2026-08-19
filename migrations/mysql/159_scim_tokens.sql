-- ============================================================================
-- Velaris — MySQL 8 / MariaDB 10.6+ : SCIM machine tokens
-- (PostgreSQL migration 159).
--
-- SCIM callers are identity providers, not people: Entra and Okta hold a static
-- bearer token and post user lifecycle at us on their own schedule. A Velaris
-- JWT is the wrong credential — it expires, it belongs to a person who may
-- leave, and its roles are a human's.
--
-- The shape follows the machine-token systems already proven here
-- (`hxmcp/tokens.py`, `api/routers/broker.py`): SHA-256 at rest so a database
-- read yields no working credential, a UNIQUE hash so lookup is one indexed
-- probe, `revoked_at` for instant and auditable revocation, and an optional
-- `expires_at` because an integration that dies silently at 90 days is worse
-- than one an admin rotates on purpose.
--
-- **The tenant lives on the row, never in the request.** A SCIM client that
-- could name its own tenant could enumerate and rewrite every other one.
--
-- VARCHAR(64) unique key = 256 bytes at utf8mb4, well inside the 3072-byte
-- InnoDB limit. CHAR(36) is what `GUID` emits outside PostgreSQL; DATETIME(6)
-- matches `MicroDateTime`.
-- ============================================================================

CREATE TABLE IF NOT EXISTS scim_tokens (
    id           CHAR(36)     NOT NULL,
    tenant_id    VARCHAR(255) NULL,
    name         VARCHAR(255) NOT NULL,
    token_hash   VARCHAR(64)  NOT NULL,
    scopes       JSON         NOT NULL,
    created_by   VARCHAR(255) NOT NULL,
    last_used_at DATETIME     NULL,
    expires_at   DATETIME     NULL,
    revoked_at   DATETIME     NULL,
    created_at   DATETIME(6)  NOT NULL,
    PRIMARY KEY (id),
    UNIQUE KEY ix_scim_tokens_hash (token_hash),
    KEY ix_scim_tokens_tenant (tenant_id)
);
