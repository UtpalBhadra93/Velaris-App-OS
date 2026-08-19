-- ============================================================================
-- Outstanding AuthnRequests, so `InResponseTo` can be verified.
--
-- An assertion carries the ID of the request it answers. Without a record of
-- what we asked for, that field can only be read, never checked — and a field
-- nobody checks is decoration. Recording the ID at `/login` and consuming it at
-- the ACS means an assertion answering a request we never sent is refused, and
-- one minted for a different sign-in attempt cannot be posted into this one.
--
-- **Only SP-initiated logins have one.** An IdP-initiated sign-in answers no
-- request and carries no `InResponseTo` — legitimate and common, since both
-- Okta and Entra offer a tile that starts there. An absent value is accepted; a
-- present one must match.
--
-- Shape mirrors `saml_assertion_replay` (mig 158): the primary key is the
-- control, and consumption is a DELETE so a second assertion quoting the same
-- request finds nothing.
--
-- Dialect notes: CHAR(36) is what `GUID` emits outside PostgreSQL; DATETIME(6)
-- matches `MicroDateTime`. VARCHAR(255) primary key = 1020 bytes at utf8mb4,
-- inside the 3072-byte InnoDB limit. No IF NOT EXISTS on CREATE INDEX — MySQL 8
-- rejects it, and the runner applies each file exactly once by filename.
-- ============================================================================

CREATE TABLE IF NOT EXISTS saml_authn_requests (
    request_id  VARCHAR(255) NOT NULL,
    provider_id CHAR(36)     NOT NULL,
    expires_at  DATETIME(6)  NOT NULL,
    created_at  DATETIME(6)  NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    PRIMARY KEY (request_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE INDEX ix_saml_authn_req_expiry ON saml_authn_requests (expires_at);
