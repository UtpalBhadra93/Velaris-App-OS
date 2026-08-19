-- ============================================================================
-- SAML SP key material — the service provider's own signing/decryption key.
--
-- Until now Velaris was a *passive* SAML SP: it verified the IdP's signature and
-- needed no key of its own. Three features require one, and they share exactly
-- this dependency, which is why they arrive together: signed AuthnRequests
-- (which several IdPs and most security reviews require), encrypted assertions
-- (sealed to our public key, unreadable without the private half), and
-- SP-initiated single logout (a LogoutRequest is only honoured if signed).
--
-- `sp_key_enc` is sealed with the tenant's HxVault DEK and mirrors
-- `client_secret_enc`: a column rather than a key inside `config`, because
-- `config` is returned wholesale to the admin console and key material must
-- never appear in a response body. `sp_cert` is the public certificate and is
-- deliberately NOT encrypted — it is published in SP metadata for the IdP to
-- import. `sp_key_created_at` lets an operator see the key's age without
-- decrypting it.
--
-- One key pair per provider row, and one enabled SAML row per tenant, so this is
-- effectively per tenant — matching the DEK it is sealed with.
--
-- Dialect notes: JSON is what `PortableJSON` emits; DATETIME(6) matches
-- `MicroDateTime`. No IF NOT EXISTS on ADD COLUMN — MySQL 8 rejects both
-- spellings, and the runner applies each file exactly once by filename.
-- ============================================================================

ALTER TABLE sso_providers ADD COLUMN sp_key_enc        JSON NULL;
ALTER TABLE sso_providers ADD COLUMN sp_cert           TEXT NULL;
ALTER TABLE sso_providers ADD COLUMN sp_key_created_at DATETIME(6) NULL;
