-- Outstanding AuthnRequests, so `InResponseTo` can be verified.
--
-- An assertion carries the ID of the request it answers. Without a record of
-- what we actually asked for, that field can only be read, never checked — and a
-- field nobody checks is decoration. Recording the ID at `/login` and consuming
-- it at the ACS closes a narrow but real gap: an assertion minted for one
-- browser's sign-in attempt cannot be posted into a different one, and an
-- assertion answering a request we never sent is refused outright.
--
-- **Only SP-initiated logins have one.** An IdP-initiated sign-in answers no
-- request and carries no `InResponseTo`, which is legitimate and common — Okta
-- and Entra both offer a tile that starts there. So an absent value is accepted
-- and a *present* one must match; requiring it would break the IdP-initiated
-- flow that the landing-path setting exists to serve.
--
-- Shape mirrors `saml_assertion_replay` (mig 158) and for the same reason: the
-- primary key is the control. Consumption is a DELETE, so a second assertion
-- quoting the same request finds nothing and is refused — a SELECT-then-DELETE
-- would race exactly where it matters.
--
-- Rows past `expires_at` are inert (the assertion fails its own time window by
-- then), so the retention runner may delete them freely.

CREATE TABLE IF NOT EXISTS saml_authn_requests (
    request_id  VARCHAR(255) PRIMARY KEY,
    provider_id UUID         NOT NULL,
    expires_at  TIMESTAMPTZ  NOT NULL,
    created_at  TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS ix_saml_authn_req_expiry ON saml_authn_requests (expires_at);
