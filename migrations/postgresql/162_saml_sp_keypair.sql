-- SAML SP key material — the service provider's own signing/decryption key.
--
-- Until now Velaris was a *passive* SAML SP: it verified the IdP's signature and
-- needed no key of its own. Three things require one, and they share exactly this
-- dependency, which is why they arrive together:
--
--   * **signed AuthnRequests** — several IdPs (and most enterprise security
--     reviews) require the request itself to be signed;
--   * **encrypted assertions** — an `EncryptedAssertion` is sealed to our public
--     key and unreadable without the private half;
--   * **SP-initiated single logout** — a `LogoutRequest` we send is only
--     honoured if it is signed.
--
-- Shape, and why:
--
--   * `sp_key_enc` is **sealed with the tenant's HxVault DEK**, never stored in
--     the clear, and mirrors `client_secret_enc`: a column rather than a key in
--     `config`, because `config` is returned wholesale to the admin console and
--     key material must never be in a response body. `_provider_admin_dict`
--     returns neither.
--   * `sp_cert` is the public certificate and is deliberately *not* encrypted —
--     it is published in SP metadata for the IdP to import, so treating it as a
--     secret would only make it awkward to hand out.
--   * `sp_key_created_at` exists so an operator can see the age of the key
--     without decrypting it. Rotation is replacing the pair and re-importing the
--     metadata at the IdP; there is no automatic rotation, because a rotation the
--     IdP has not been told about is an outage.
--
-- One key pair per provider row, and there is one enabled SAML row per tenant, so
-- this is effectively per tenant — matching the DEK it is sealed with.

ALTER TABLE sso_providers ADD COLUMN IF NOT EXISTS sp_key_enc        JSONB;
ALTER TABLE sso_providers ADD COLUMN IF NOT EXISTS sp_cert           TEXT;
ALTER TABLE sso_providers ADD COLUMN IF NOT EXISTS sp_key_created_at TIMESTAMPTZ;
