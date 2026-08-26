/**
 * Error Catalog — single source of truth for the in-app Error Documentation modal.
 *
 * Keyed by component key (derived from the nav path, e.g. "/documents" → "documents").
 * The same data drives both the per-page default view and the global error search.
 *
 * Seeded from docs/Future/error-catalog.md. Add entries here as errors are defined.
 * Keep code/HTTP/root-cause aligned with what the backend actually returns.
 */

export interface ErrorEntry {
  /** Stable code: VEL-<SUBSYSTEM>-<SLUG>-<HTTP>. Also shown in responses/logs (future). */
  code:        string;
  /** HTTP status returned, or "—" for non-HTTP / startup errors. */
  http:        string;
  /** What the user/operator actually sees. */
  symptom:     string;
  /** Why it happens. */
  rootCause:   string;
  /** How to fix it (operator and/or user). */
  resolution:  string;
  /** Related error codes worth checking. */
  related?:    string[];
}

/** A component's section: a friendly label + its error entries. */
export interface ComponentErrors {
  /** Human label shown in the modal title and the Component column. */
  label:   string;
  entries: ErrorEntry[];
}

/**
 * The catalog. Key = component key (nav path minus leading slash).
 * Pages with no own errors can be omitted — the modal still opens for global search.
 */
export const ERROR_CATALOG: Record<string, ComponentErrors> = {
  hxvault: {
    label: "Vault",
    entries: [
      {
        code: "VEL-VAULT-SHREDDED-410",
        http: "410",
        symptom: "410 — the data was encrypted with a key that has been destroyed.",
        rootCause:
          "Crypto-shredding destroyed the tenant's data-encryption key, which is how GDPR " +
          "erasure is honoured for encrypted-at-rest data. The ciphertext remains and is now " +
          "permanently undecryptable. 410 rather than 500 or 404: 500 invites a retry loop, " +
          "404 implies it never existed.",
        resolution:
          "Operator: there is NO recovery path — restoring a database backup does not help, " +
          "since the backup holds the same ciphertext and the key is gone from the vault too. " +
          "Confirm the shred was intended; if not, this is an incident. User: do not retry.",
        related: ["VEL-VAULT-DEK-503"],
      },
      {
        code: "VEL-VAULT-DEK-503",
        http: "503",
        symptom: "503 — encryption keys are unavailable.",
        rootCause:
          "OpenBao is sealed or unreachable, or the KEK is unset. Transient — the data is " +
          "intact and will decrypt once the vault is back. That is why it is a different code " +
          "from a shredded key.",
        resolution:
          "Operator: unseal OpenBao, restore connectivity, confirm the KEK. Check DEK cache " +
          "resync if only some workers are affected. User: retry shortly, nothing is lost.",
        related: ["VEL-VAULT-SHREDDED-410", "VEL-DOC-ENCRYPT-503"],
      },
    ],
  },

  database: {
    label: "Database",
    entries: [
      {
        code: "VEL-DB-UNAVAILABLE-503",
        http: "503",
        symptom: "503 — the database is unavailable.",
        rootCause:
          "Connection refused, a pool exhausted, a restart, or a network partition. It used to " +
          "surface as a bare 500, which reads as an application bug and sends people debugging " +
          "the wrong layer.",
        resolution:
          "Operator: check the database is reachable and whether a pool is exhausted — " +
          "operations, auth and analytics are separate pools, so one can starve unnoticed. " +
          "The driver message (host, port, user) is logged, never returned. User: retry shortly.",
        related: ["VEL-SYS-STORAGE-PRECHECK-000"],
      },
      {
        code: "VEL-CONSENT-REFUSED-403",
        http: "403",
        symptom:
          "403 — 'Refused: no recipient has a lawful basis for <purpose>. N address(es) were dropped.'",
        rootCause:
          "Every recipient of a consent-gated send either withdrew consent for that purpose " +
          "or has no record at all. The HxConsent ledger fails closed, so an address it has " +
          "never seen is dropped exactly like one that withdrew.",
        resolution:
          "Operator: this is the gate working, not a fault. Check the subject's ledger under " +
          "Compliance → Consent; if consent genuinely exists it was recorded under a different " +
          "address spelling (the ledger lowercases but does not alias). Case correspondence is " +
          "never gated — a case email that hit this was sent with the wrong purpose. " +
          "User: N/A, the recipient asked not to be contacted for this purpose.",
        related: ["VEL-CASE-GUARD-403"],
      },
      {
        code: "VEL-SYS-GATEWAY-REQUIRED-403",
        http: "403",
        symptom:
          "403 — 'This service is reachable only through the Velaris API gateway.'",
        rootCause:
          "The request reached a service port (8201-8204) without the credential the gateway " +
          "injects on every request it proxies. Either something addressed the service " +
          "directly instead of the gateway on 8200, or the gateway and the service disagree " +
          "about VELARIS_GATEWAY_IDENTITY_SECRET — usually because one was restarted after " +
          "the value changed and the other was not.",
        resolution:
          "Operator: send the request to the gateway on 8200. If it already was, the two " +
          "halves are out of step — confirm the secret matches in .env and in the gateway " +
          "container's environment, then restart the gateway and the services. Health, " +
          "readiness and metrics are exempt, so a green /health on a service whose API " +
          "returns this is the expected symptom, not a contradiction. " +
          "User: nothing to do — this is a deployment setting, not a permission on the account.",
        related: ["VEL-CASE-GUARD-403"],
      },
      {
        code: "VEL-SYS-UNHANDLED-500",
        http: "500",
        symptom:
          "500 — 'The server hit an unexpected error. It has been logged with a request id; " +
          "your request was not applied.'",
        rootCause:
          "Unknown by definition — this is the floor under every other handler, and it exists " +
          "for the SHAPE of the response rather than the diagnosis. Without it the server " +
          "returns text/plain 'Internal Server Error', which this app cannot parse, and the " +
          "browser reports 'JSON.parse: unexpected character at line 1 column 1' — a message " +
          "about the content type that tells you nothing about the actual fault.",
        resolution:
          "Operator: the response carries no detail on purpose (an unclassified error is the " +
          "most likely to leak a connection string or token). Read the traceback in the " +
          "service log, keyed by the x-request-id header on the failed response. A code that " +
          "recurs deserves its own handler — this one is a safety net, not a destination. " +
          "User: retry, and quote the request id if it persists.",
        related: ["VEL-DB-UNAVAILABLE-503"],
      },
    ],
  },

  hxnexus: {
    label: "HxNexus",
    entries: [
      {
        code: "VEL-LLM-NOT-ASSIGNED-403",
        http: "403",
        symptom: "403 on case-scoped Q&A.",
        rootCause: "Case-scoped AI is restricted to assignees. Broad AI access to case content would defeat case-level authorization by routing around it.",
        resolution: "Operator: Assign the user, or widen the case's access group. User: Ask to be assigned.",
      },
      {
        code: "VEL-LLM-NOTFOUND-404",
        http: "404",
        symptom: "404 on an HxNexus request.",
        rootCause: "The case or conversation does not exist or is outside the caller's tenant.",
        resolution: "Operator: Confirm the id and tenant. User: Reopen the case from the list.",
      },

      // ── HxDraft — drafting components from a description, inside this chat ──
      // Apply re-validates server-side; the client's copy is never trusted. So a
      // draft that was valid when generated can still be refused, and the two
      // reasons need different fixes.
      {
        code: "VEL-DRAFT-VALIDATION-400",
        http: "400",
        symptom: "\"Draft failed validation: …\" when applying or staging a draft card.",
        rootCause:
          "Apply re-normalises the submitted draft and re-runs the validator — the client copy " +
          "is never trusted. The specific faults are listed after the colon; the code says which " +
          "stage refused, not what was wrong.",
        resolution:
          "User: fix the flagged fields on the card, or re-draft. " +
          "Operator: the same failure recurring straight from the generator is a prompt or model " +
          "problem, not a user error.",
        related: ["VEL-DRAFT-GENERATE-400"],
      },
      {
        code: "VEL-DRAFT-STALE-409",
        http: "409",
        symptom: "\"<artifact> changed since this draft was generated — re-draft…\"",
        rootCause:
          "Every modify draft carries a checksum of what it was drafted against, and Apply " +
          "re-fetches to compare. Deliberate, not a retryable race: applying anyway would " +
          "silently overwrite whatever changed in between.",
        resolution:
          "User: re-draft against the current version and review the new diff. " +
          "Operator: none — frequent hits mean two people are editing the same artifact.",
        related: ["VEL-DRAFT-VALIDATION-400"],
      },
      {
        code: "VEL-DRAFT-KIND-400",
        http: "400",
        symptom: "\"kind must be one of […]\", \"Only WHEN rules can be modified by draft\", \"Only rule and escalation drafts can be staged\".",
        rootCause:
          "Either the kind does not exist, or it exists but not for this operation. Not every " +
          "kind is draftable in every direction by design — connectors are modify-only because " +
          "creating one needs credentials, which are never draftable.",
        resolution: "User: use one of the kinds named in the message. Operator: none — the supported sets are deliberate.",
        related: ["VEL-DRAFT-NOTFOUND-404"],
      },
      {
        code: "VEL-DRAFT-ROLE-403",
        http: "403",
        symptom: "\"Applying a drafted <thing> requires …\"",
        rootCause:
          "Anyone may generate a draft; applying one runs the same creation path a human would, " +
          "so it carries the same role requirement. The required role differs per kind and is " +
          "named in the message.",
        resolution:
          "User: use Stage instead — it submits for review and needs no role, because nothing " +
          "goes live without a reviewer. Operator: grant the named role if direct apply is intended.",
        related: ["VEL-AUTH-DISABLED-403"],
      },
      {
        code: "VEL-DRAFT-NOTFOUND-404",
        http: "404",
        symptom: "\"Rule not found\", \"Form not found\", \"Case type not found\", …",
        rootCause: "The artifact the draft targets no longer exists — usually deleted between drafting and applying.",
        resolution: "User: re-draft against something that still exists. Operator: check whether another operator deleted it.",
        related: ["VEL-DRAFT-BADID-400", "VEL-DRAFT-STALE-409"],
      },
      {
        code: "VEL-DRAFT-BADID-400",
        http: "400",
        symptom: "\"Invalid rule id '…'\", \"Invalid case_type_id '…'\", …",
        rootCause:
          "The id is not a UUID, so no lookup happened. Kept separate from the 404 on purpose: a " +
          "malformed id reveals nothing about what exists, so splitting them is not an oracle.",
        resolution: "User: open the draft from the artifact rather than pasting an id. Operator: a client sending non-UUIDs is a caller bug.",
        related: ["VEL-DRAFT-NOTFOUND-404"],
      },
      {
        code: "VEL-DRAFT-EXISTS-409",
        http: "409",
        symptom: "\"A rule named '…' v… already exists\", \"Case type '…' already exists\", \"Username or email already in use.\"",
        rootCause:
          "The identity the draft would claim is taken. Caught before the write so it surfaces as " +
          "a clean 409 rather than a database error reaching the user as a 500.",
        resolution: "User: re-draft with a different name — or modify the existing artifact, which is usually what was meant.",
        related: ["VEL-AUTH-EMAIL-TAKEN-409"],
      },
      {
        code: "VEL-DRAFT-RATE-429",
        http: "429",
        symptom: "\"Rate limit reached. Try again in Ns.\"",
        rootCause: "Drafting and staging share the HxNexus chat rate limit, per user. Both spend model time; staging also writes two rows.",
        resolution: "User: wait for Retry-After, then try once. Operator: check for a client retry loop before raising the limit.",
        related: ["VEL-AUTH-RATE-LIMIT-429"],
      },
      {
        code: "VEL-DRAFT-AI-503",
        http: "503",
        symptom: "A generation failure naming the backend as unavailable.",
        rootCause: "The model backend could not be reached or returned nothing usable. Nothing the user writes will fix it.",
        resolution:
          "User: wait and retry — do not rephrase. " +
          "Operator: check GET /hxnexus/status; a `misconfigured` block (provider set without a key) presents as an outage.",
        related: ["VEL-DRAFT-GENERATE-400"],
      },
      {
        code: "VEL-DRAFT-GENERATE-400",
        http: "400",
        symptom: "A builder error describing what could not be drafted from the description.",
        rootCause:
          "The description could not be turned into a valid component — too vague, or asking for " +
          "something the builder cannot express. This shared one raise site with the 503 until " +
          "2026-08-13: same message, opposite fixes.",
        resolution: "User: rephrase with specifics — name the fields, conditions or stages. Operator: a pattern on reasonable descriptions is a builder gap.",
        related: ["VEL-DRAFT-AI-503"],
      },
    ],
  },

  authentication: {
    label: "Authentication",
    entries: [
      {
        code: "VEL-AUTH-CREDENTIALS-401",
        http: "401",
        symptom: "Login is rejected with \"Invalid credentials\".",
        rootCause:
          "The username/email does not exist, or the password did not verify. These are " +
          "deliberately indistinguishable — a code that told them apart would be a " +
          "user-enumeration oracle, since it appears in the response body.",
        resolution:
          "Operator: confirm the account exists and is enabled in User Directory. Repeated " +
          "failures from one IP may be an attack, not a forgetful user. " +
          "User: re-enter your details, or reset your password.",
        related: ["VEL-AUTH-DISABLED-403", "VEL-AUTH-SSO-REQUIRED-400"],
      },
      {
        code: "VEL-AUTH-DISABLED-403",
        http: "403",
        symptom: "\"Account is disabled. Contact an administrator.\"",
        rootCause:
          "The account was disabled by an administrator, or auto-disabled by a security " +
          "control such as HxDBManager breach detection.",
        resolution:
          "Operator: establish WHY it was disabled before re-enabling — especially if a " +
          "security control did it. User: contact your administrator.",
        related: ["VEL-AUTH-CREDENTIALS-401"],
      },
      {
        code: "VEL-AUTH-SSO-REQUIRED-400",
        http: "400",
        symptom: "\"This account uses SSO login. Use the SSO button instead.\"",
        rootCause: "The account is provisioned against an SSO provider and has no local password.",
        resolution: "User: sign in with the SSO button for your provider. Operator: none — correct behaviour.",
        related: ["VEL-AUTH-SSO-UNCONFIGURED-404"],
      },
      {
        code: "VEL-AUTH-OTP-INVALID-400",
        http: "400",
        symptom: "\"Invalid or expired OTP.\"",
        rootCause:
          "The code was mistyped, already used, or past its expiry. One code for all three — " +
          "telling the user which narrows an attacker's search.",
        resolution:
          "Operator: confirm mail delivery works; an expired OTP is often a slow-mail symptom. " +
          "User: request a new code and enter it promptly.",
        related: ["VEL-AUTH-RATE-LIMIT-429"],
      },
      {
        code: "VEL-AUTH-RATE-LIMIT-429",
        http: "429",
        symptom: "\"Too many attempts. Please wait before retrying.\"",
        rootCause: "The sliding-window limiter tripped for this identity or IP.",
        resolution:
          "Operator: check for a client retry loop before raising the limit. " +
          "User: wait, then try once — repeated retries extend the window.",
        related: ["VEL-AUTH-OTP-INVALID-400"],
      },
      {
        code: "VEL-AUTH-DEVICE-MISMATCH-401",
        http: "401",
        symptom: "\"Session device check failed. Please log in again.\"",
        rootCause:
          "The refresh token was presented from a different device fingerprint than it was " +
          "issued to — the control that makes a stolen refresh token unusable elsewhere.",
        resolution:
          "Operator: expected after a device or browser change, but repeated occurrences for " +
          "one user from differing IPs are worth investigating as token theft. " +
          "User: log in again.",
        related: ["VEL-AUTH-CREDENTIALS-401"],
      },
      {
        code: "VEL-AUTH-SSO-UNCONFIGURED-404",
        http: "404",
        symptom: "\"SSO provider not configured\".",
        rootCause: "The provider is offered in the UI but its client id/secret or discovery URL is missing.",
        resolution:
          "Operator: complete the configuration, or stop offering the provider — an SSO " +
          "button that always fails is worse than no button. User: use another sign-in method.",
        related: ["VEL-AUTH-SSO-REQUIRED-400"],
      },
      {
        code: "VEL-AUTH-SSO-STATE-400",
        http: "400",
        symptom: "\"Sign-in could not be verified. Start again from the sign-in page.\"",
        rootCause:
          "The `state` returned by the identity provider was missing, expired (10 minutes), " +
          "signed for a different provider or redirect URI, or did not match the value this " +
          "browser tab stored when the sign-in began. Usually a stale or bookmarked callback " +
          "URL, or a sign-in finished in a different tab than it started in.",
        resolution:
          "User: go back to the sign-in page and click the SSO button again — do not reuse an " +
          "old callback link. Operator: a burst of these from one IP is a login-CSRF attempt, " +
          "not a user error; the state check is what stops it landing someone in another " +
          "person's account.",
        related: ["VEL-AUTH-SSO-UNCONFIGURED-404"],
      },
      {
        code: "VEL-AUTH-SSO-UNVERIFIED-403",
        http: "403",
        symptom: "\"<provider> has not verified this email address.\"",
        rootCause:
          "The provider returned an address it does not vouch for. GitHub's profile email in " +
          "particular is free text the account holder types in. Since the email decides which " +
          "Velaris account an SSO identity binds to, accepting an unverified one would let " +
          "anyone claim another user's account.",
        resolution:
          "User: verify the address with your identity provider, then sign in again. " +
          "Operator: none — this is the control working. Investigate if it repeats for one " +
          "address that belongs to an existing privileged account.",
        related: ["VEL-AUTH-SSO-PROVISION-403"],
      },
      {
        code: "VEL-AUTH-SSO-PROVISION-403",
        http: "403",
        symptom: "\"This account is not provisioned in Velaris.\"",
        rootCause:
          "SSO succeeded but no Velaris account matched, and the email's domain is not in the " +
          "provider's `allowed_domains` config. An empty or absent allowlist means no " +
          "auto-provisioning at all, which is the default.",
        resolution:
          "Operator: create the account, or add the domain to the SSO provider's " +
          "`allowed_domains` (and set `default_roles` if `viewer` is wrong). Do not add a " +
          "public domain such as gmail.com — that grants an account to anyone with a mailbox. " +
          "User: ask your administrator for access.",
        related: ["VEL-AUTH-SSO-UNVERIFIED-403"],
      },
      {
        code: "VEL-AUTH-SAML-ASSERTION-400",
        http: "400",
        symptom:
          "\"Sign-in could not be verified.\" — or, for a rejected landing target, " +
          "\"That sign-in destination is not allowed.\"",
        rootCause:
          "The assertion did not survive verification: signature invalid or absent, issuer or " +
          "audience mismatch, outside the permitted clock skew, an InResponseTo matching no " +
          "request we made, or already consumed (replay). The same code covers a RelayState " +
          "landing path that is not same-origin, refused before anything is signed. One code " +
          "and one message on purpose — which check failed must not be probeable.",
        resolution:
          "Operator: the reason is in the security event, never in the response. Check the IdP " +
          "certificate against the stored one first (an IdP rotation with no metadata re-import " +
          "is the usual cause), then issuer, audience and clock_skew_seconds. Repeated replay " +
          "refusals from one address are an attack, not a misconfiguration. " +
          "User: start the sign-in again — an assertion is single-use and short-lived.",
        related: ["VEL-AUTH-SAML-PROVISION-403", "VEL-AUTH-SSO-STATE-400"],
      },
      {
        code: "VEL-AUTH-SAML-PROVISION-403",
        http: "403",
        symptom:
          "\"This account is not provisioned in Velaris.\" — or \"An account already uses this " +
          "email address and is not linked to your identity provider.\"",
        rootCause:
          "The identity is genuine and the platform still refuses it: JIT provisioning is off " +
          "and no account exists, the email domain is not allowlisted, or a local account holds " +
          "that email and is not linked to this provider. The last one is deliberate — adopting " +
          "an account by email alone would let anyone controlling a matching mailbox at the IdP " +
          "inherit a local admin.",
        resolution:
          "Operator: create the account, allowlist the domain, or link the existing account " +
          "deliberately (Studio → Linked accounts). link_by_email defaults to off; leave it off " +
          "unless you own every domain on the allowlist. " +
          "User: your provider signed you in correctly, but the account does not exist here yet.",
        related: ["VEL-AUTH-SSO-PROVISION-403", "VEL-AUTH-EMAIL-TAKEN-409"],
      },
      {
        code: "VEL-AUTH-EMAIL-TAKEN-409",
        http: "409",
        symptom: "\"That email is already in use by another account.\"",
        rootCause: "Email is unique per account; another user already holds it.",
        resolution:
          "Operator: find the existing account before creating a duplicate — merging " +
          "afterwards is far harder. User: use a different address or recover the account.",
        related: ["VEL-AUTH-CREDENTIALS-401"],
      },
    ],
  },

  documents: {
    label: "Documents",
    entries: [
      {
        code: "VEL-DOC-STORAGE-503",
        http: "503",
        symptom: "Document upload fails with a server error; nothing is stored.",
        rootCause:
          "The configured storage path is not writable, or the MinIO backend is unreachable. " +
          "Most common: local storage pointing at a directory the service user cannot create " +
          "(e.g. /var/lib/velaris/documents — /var/lib is root-owned), so the filesystem write raises PermissionError.",
        resolution:
          "Operator: point VELARIS_CASE_STORAGE_LOCAL_PATH at a writable, install-local path " +
          "(recommended <install-dir>/data/documents) or create the configured directory with correct ownership, " +
          "then restart case-service. For MinIO: verify endpoint, credentials, and that the bucket exists. " +
          "User: retry; if it persists, contact your administrator — the file was not saved.",
        related: ["VEL-SYS-STORAGE-PRECHECK", "VEL-SYS-BODY-413"],
      },
      {
        code: "VEL-DOC-ENCRYPT-503",
        http: "503",
        symptom: "Upload or download of a valid document fails with a server error.",
        rootCause:
          "The HxVault envelope path could not produce a usable data key — the KEK is unset, " +
          "OpenBao is sealed or unreachable, or the tenant's DEK has been shredded. Distinct " +
          "from VEL-DOC-STORAGE-503, which is the filesystem failing rather than key material.",
        resolution:
          "Operator: check OpenBao is unsealed and reachable and the KEK is present. A shredded " +
          "DEK is NOT recoverable — that is what shredding means, and those documents are gone. " +
          "User: retry once; if it persists this needs an operator.",
        related: ["VEL-DOC-STORAGE-503"],
      },
      {
        code: "VEL-DOC-UUID-422",
        http: "422",
        symptom: "Load or upload fails with 422 before anything happens.",
        rootCause:
          "The case identifier is not a well-formed UUID. A UUID is 36 chars (8-4-4-4-12 hex); " +
          "a fragment like the first 8-hex block fails validation before the handler runs.",
        resolution:
          "User: paste the complete 36-character case UUID (copy it from the Cases list / case URL), not a fragment.",
        related: ["VEL-DOC-CASE-404"],
      },
      {
        code: "VEL-DOC-CASE-404",
        http: "404",
        symptom: "Upload returns 404 'case not found' even though the UUID is well-formed.",
        rootCause:
          "The UUID is valid in format but no case with that id exists (wrong tenant, deleted case, or non-existent id).",
        resolution:
          "User: use a case UUID from an existing case you have access to. Operator: confirm the case exists in the expected tenant.",
        related: ["VEL-DOC-UUID-422"],
      },
      {
        code: "VEL-DOC-FILETYPE-400",
        http: "400",
        symptom: "Upload blocked with 'File rejected: …'.",
        rootCause:
          "The filename failed validation: a blocked extension (.env, .key, .py, .sh …), a blocked pattern " +
          "(secrets.*, *config.json), path traversal (../), a null byte, or a type outside the document allowlist.",
        resolution:
          "User: upload an allowed type (PDF, Office, images, common archives, txt/csv/json/xml) and avoid blocked filename patterns. " +
          "Operator: if a legitimate type is rejected, add it to ALLOWED_DOCUMENT_EXTENSIONS (never remove blocklist entries).",
        related: ["VEL-SYS-BODY-413"],
      },
      {
        code: "VEL-DOC-NOTFOUND-404",
        http: "404",
        symptom: "404 'document not found' / 'version not found'.",
        rootCause:
          "The document id doesn't exist, is soft-deleted, or the requested version number doesn't exist for that document.",
        resolution:
          "User: refresh the document list; the item may have been deleted. Operator: a soft-deleted document is intentionally hidden.",
      },
      {
        code: "VEL-DOC-PREVIEW-415",
        http: "415",
        symptom: "415 'Preview not available for this content type'.",
        rootCause:
          "Preview generation supports only PDF and common image types; anything else (zip, Office) returns no preview. This is by design.",
        resolution:
          "User: download the file to view it; previews exist only for PDFs and images.",
      },
      {
        code: "VEL-DOC-DELETE-403",
        http: "403",
        symptom: "403 'Only the case owner or an admin can delete…'.",
        rootCause:
          "The caller is neither an admin nor the owner (created_by) of the case the document belongs to.",
        resolution:
          "User: ask the case owner or an administrator to delete it. Operator: grant admin or have the owner perform the deletion.",
        related: ["VEL-DOC-LASTVERSION-409"],
      },
      {
        code: "VEL-DOC-LASTVERSION-409",
        http: "409",
        symptom: "409 'cannot delete the only remaining version; delete the document instead'.",
        rootCause:
          "A document must retain at least one version; deleting the last one is blocked to avoid an orphaned record.",
        resolution:
          "User: delete the whole document instead of the last version.",
        related: ["VEL-DOC-DELETE-403"],
      },
    ],
  },

  cases: {
    label: "Cases",
    entries: [
      {
        code: "VEL-CASE-AI-ACTION-400",
        http: "400",
        symptom: "400 on executing an `ai_action` step.",
        rootCause: "The step config is incomplete or invalid — usually a missing `output_variable` or prompt. Caught before the model is called, so no tokens are spent on a step that cannot store its answer.",
        resolution: "Operator: Fix the step config in Case Designer; the panel warns when approval is switched off. User: Report it to a designer.",
      },
      {
        code: "VEL-CASE-GUARD-403",
        http: "403",
        symptom: "403 with the policy's own reason text.",
        rootCause: "HxGuard's PDP refused the action. The reason is the policy decision, not a generic denial — case-level enforcement has been the default since v2.2.0.",
        resolution: "Operator: Check the user's access groups and any case-level ReBAC tuples. If enforcement is unexpected, verify `VELARIS_CASE_HXGUARD_CASE_ENFORCEMENT`. User: Request access from a case manager.",
      },
      {
        code: "VEL-CASE-PROCDEF-400",
        http: "400",
        symptom: "400 when starting a linked process.",
        rootCause: "The case type points at an HxFusion definition that does not exist or is not `active`. Starting an inactive definition would produce an instance nobody can complete.",
        resolution: "Operator: Activate the definition, or clear the link on the case type. User: Report it to a designer.",
      },
      {
        code: "VEL-CASE-RULE-DISABLED-409",
        http: "409",
        symptom: "409 on rule execution.",
        rootCause: "The rule exists but is disabled. Disabled rules are kept rather than deleted so their history stays readable.",
        resolution: "Operator: Enable it in Case Designer if it should run. User: Ask a designer to enable the rule.",
      },
      {
        code: "VEL-CASE-RULE-SCOPE-403",
        http: "403",
        symptom: "403 when applying a rule to a case.",
        rootCause: "The rule is scoped to another case type. Scope is not advisory — applying it here would evaluate conditions against fields that may not exist.",
        resolution: "Operator: Re-scope the rule, or create one for this case type. User: Pick a rule listed for this case type.",
      },
      {
        code: "VEL-CASE-SUBPROCESS-500",
        http: "500",
        symptom: "500 with the underlying error.",
        rootCause: "The parent case exists; the child process could not be created. Distinct from `VEL-CASE-PROCDEF-400`, which is a configuration problem rather than a runtime one.",
        resolution: "Operator: Check engine reachability and the case-service log for the cause. User: Retry; if it persists this needs an operator.",
      },
      {
        code: "VEL-CASE-TYPED-VAR-400",
        http: "400",
        symptom: "400 naming keys that must be written through the variables API.",
        rootCause: "Case Variables v2 promoted these keys out of the free-form `data` blob. Writing them directly would bypass typing, validation and the projection.",
        resolution: "Operator: None — the migration is intentional. User: Write through the variables API (`velaris.*` projection), not the raw data blob.",
      },
      {
        code: "VEL-CASE-VAR-FILTER-400",
        http: "400",
        symptom: "400 quoting the filter received.",
        rootCause: "A `var` filter must be `namespace.name:value`. A bare name is ambiguous once namespaces exist.",
        resolution: "Operator: None. User: Qualify the filter with its namespace.",
      },
      {
        code: "VEL-CASE-NOTFOUND-404",
        http: "404",
        symptom: "404 'Case not found' / 'Case type not found' / 'Rule not found' / 'Step … not found'.",
        rootCause: "The referenced case, case type, rule, share, or step id does not exist (deleted, wrong tenant, or bad id).",
        resolution: "User: refresh and pick a valid item. Operator: confirm the record exists in the expected tenant.",
      },
      {
        code: "VEL-CASE-AUTHZ-403",
        http: "403",
        symptom: "403 'Not authorized: …' or 'Rule is scoped to a different case type'.",
        rootCause: "A HxGuard/decision check denied the action, or a rule is being applied to a case type it isn't scoped to.",
        resolution: "User: request access or use the correct case type. Operator: review the access group / rule scope.",
      },
      {
        code: "VEL-CASE-RULEDISABLED-409",
        http: "409",
        symptom: "409 'Rule is disabled'.",
        rootCause: "The rule exists but is disabled, so it cannot be executed.",
        resolution: "Operator: enable the rule in the Case Designer before invoking it.",
      },
      {
        code: "VEL-CASE-VARTYPED-400",
        http: "400",
        symptom: "400 '… these keys are typed variables now; write them through the owning integration…'.",
        rootCause: "Attempt to write case-data keys that are now typed/governed variables; direct writes are blocked.",
        resolution: "User: write through the owning integration, or flag the variable in the Case Designer Variables panel.",
      },
      {
        code: "VEL-CASE-CONNCONFIG-400",
        http: "400",
        symptom: "400 'service_task has no connector_id configured' / 'subprocess step has no process_definition_id'.",
        rootCause: "A case-type step is missing required wiring (connector or sub-process definition).",
        resolution: "Operator: configure the connector / process definition on the step in the Case Designer.",
      },
      {
        code: "VEL-CASE-SUBPROC-500",
        http: "500",
        symptom: "500 'Failed to start subprocess: …'.",
        rootCause: "The sub-process could not be started — bad/inactive process definition or a downstream failure.",
        resolution: "Operator: check the referenced process definition is active; inspect server logs for the cause.",
        related: ["VEL-CASE-CONNCONFIG-400"],
      },

      // ── HxMeet ──
      // Filed under Cases, not a page of their own: Sessions is a tab inside
      // Case Manager, so this is the page a staff user is on when they hit one.
      // The public guest page (/meet/join) shows the bare code with no link,
      // the same treatment the customer portal gets.
      {
        code: "VEL-MEET-DRIVER-501",
        http: "501",
        symptom: "501 when starting or joining an embedded session.",
        rootCause:
          "LiveKit url / API key / API secret are not all set, so `livekit.configured()` is false — " +
          "or `meet_driver` names a driver that does not exist yet.",
        resolution:
          "Operator: set VELARIS_CASE_LIVEKIT_URL / _API_KEY / _API_SECRET and restart; confirm via " +
          "GET /meet/providers → embedded_available. User: meetings are not switched on here yet.",
        related: ["VEL-MEET-RECORDING-501"],
      },
      {
        code: "VEL-MEET-RECORDING-501",
        http: "501",
        symptom: "501 only when recording is requested — the session itself starts fine.",
        rootCause:
          "The egress recordings directory is unset or unwritable. A different setting from the " +
          "LiveKit credentials: the driver can be perfectly configured and recording still absent.",
        resolution:
          "Operator: set the meet recordings dir, check the egress container's /out mount points at " +
          "it and is writable. User: hold the meeting without recording.",
        related: ["VEL-MEET-DRIVER-501", "VEL-MEET-CONSENT-409"],
      },
      {
        code: "VEL-MEET-ASR-501",
        http: "501",
        symptom: "501 on requesting analysis of a sealed recording.",
        rootCause:
          "faster-whisper is an optional dependency so a base install stays lean. Nothing is wrong " +
          "with the recording.",
        resolution:
          "Operator: pip install .[intelligence] and restart; model size is " +
          "VELARIS_CASE_MEET_WHISPER_MODEL (default base). User: transcription is not installed here.",
        related: ["VEL-MEET-KYC-501", "VEL-MEET-NORECORDING-404"],
      },
      {
        code: "VEL-MEET-KYC-501",
        http: "501",
        symptom: "501 on running the passive KYC signal pass.",
        rootCause:
          "A different optional extra from transcription, and raised only when NO check could run at " +
          "all (no .[kyc] deps AND no sealed transcript). Checks missing their own dependency are " +
          "skipped honestly instead of failing the run.",
        resolution:
          "Operator: pip install .[kyc]. A sealed transcript alone enables the transcript-based " +
          "checks without it. User: these checks are not installed here.",
        related: ["VEL-MEET-ASR-501"],
      },
      {
        code: "VEL-MEET-NOTFOUND-404",
        http: "404",
        symptom: "404 naming the entity, with no further detail.",
        rootCause:
          "Malformed id, another tenant's row, genuinely absent, OR the caller lacks the HxGuard case " +
          "action — all four answer identically by design (404 anti-oracle). In enforce mode an " +
          "unregistered action fail-closes to 404 for everyone, admins included.",
        resolution:
          "Operator: check id and tenant first, then that meet.start / meet.join / " +
          "meet.recording.view are still in hxguard CASE_ACTIONS — an unregistered action presents " +
          "exactly like a missing session. User: reload the case.",
        related: ["VEL-MEET-INVITE-404"],
      },
      {
        code: "VEL-MEET-INVITE-404",
        http: "404",
        symptom: "404 'Invite not found' on the public join page, whatever actually went wrong.",
        rootCause:
          "Used (single-use), expired, session ended, wrong tenant, or never existed. The service " +
          "raises the same 'invalid invite' from six places on purpose — an anonymous caller must not " +
          "be able to tell a consumed invite from a fabricated one, so the code stays uniform too.",
        resolution:
          "Operator: check the participant row's consumed_at/expiry and the session status, then " +
          "issue a fresh invite — tokens are stored hashed, so you cannot look one up by raw value. " +
          "User: ask for a new link; each works once.",
        related: ["VEL-MEET-NOTFOUND-404", "VEL-MEET-RATE-429"],
      },
      {
        code: "VEL-MEET-NORECORDING-404",
        http: "404",
        symptom: "404 on a session that plainly exists.",
        rootCause:
          "No sealed evidence on the session yet. Not the same as a missing session: the fix is to " +
          "record and seal one, not to check the id. Sealing happens on the egress_ended webhook, so " +
          "a just-stopped recording is briefly 'processing'.",
        resolution:
          "Operator: confirm recording_status reached 'sealed'; stuck at 'processing' means the " +
          "LiveKit egress_ended webhook is not arriving. User: wait a moment and retry.",
        related: ["VEL-MEET-ASR-501", "VEL-MEET-TAMPER-409"],
      },
      {
        code: "VEL-MEET-OPTIN-400",
        http: "400",
        symptom: "400 saying session intelligence or video-KYC is not enabled.",
        rootCause:
          "Both are per-tenant opt-ins, default OFF. 400 not 501 on purpose: the software is " +
          "installed and working, an administrator simply has not turned it on — a different fix " +
          "from a missing dependency.",
        resolution:
          "Operator: set intelligence / kyc in the tenant's meet settings. User: ask an administrator.",
        related: ["VEL-MEET-ASR-501", "VEL-MEET-KYC-501"],
      },
      {
        code: "VEL-MEET-STATE-409",
        http: "409",
        symptom: "409 describing the session's current state.",
        rootCause:
          "The operation needs a state the session is not in: not active, already recording, nothing " +
          "to stop, analysis already running, or challenges outside a running recording (issue AND " +
          "response must land on the sealed tape).",
        resolution:
          "Operator: read the state named in detail — these are ordering rules, not faults. " +
          "User: refresh; someone else may have changed the session.",
        related: ["VEL-MEET-CONSENT-409"],
      },
      {
        code: "VEL-MEET-CONSENT-409",
        http: "409",
        symptom: "409 listing the identities that have not consented to being recorded.",
        rootCause:
          "Every participant in the room must carry a consent stamp before egress starts. Kept apart " +
          "from the state code deliberately — this is about people's rights, not session bookkeeping, " +
          "and must never read as a transient state to retry past.",
        resolution:
          "Operator: the named participants must rejoin through a consent-carrying path. Do NOT work " +
          "around it by ending and restarting the session — that removes the record, not the " +
          "requirement. User: everyone must accept the recording notice first.",
        related: ["VEL-MEET-STATE-409", "VEL-MEET-RECORDING-501"],
      },
      {
        code: "VEL-MEET-TAMPER-409",
        http: "409",
        symptom: "409 'tampered or key mismatch' on a recording or transcript.",
        rootCause:
          "The stored ciphertext did not authenticate against the tenant DEK: altered after sealing, " +
          "or the key does not belong to it. Its own code rather than a state error, for the same " +
          "reason a shredded vault key is not a transient one. This is not a retry.",
        resolution:
          "Operator: treat as an incident — check the tenant DEK and whether the document bytes were " +
          "touched outside the platform; /recording/verify recomputes the hash against the " +
          "audit-chain seal. User: do not retry, report it.",
        related: ["VEL-VAULT-SHREDDED-410", "VEL-MEET-NORECORDING-404"],
      },
      {
        code: "VEL-MEET-PROVIDER-502",
        http: "502",
        symptom: "502 naming the upstream error.",
        rootCause:
          "The tenant's own meeting provider (Teams / Zoom / Meet via an HxBridge connector) or the " +
          "LiveKit egress service refused or was unreachable. Upstream fault, not the caller's.",
        resolution:
          "Operator: check the connector's credentials and token expiry in HxBridge, and egress " +
          "container health. User: retry.",
        related: ["VEL-CONN-DISABLED-400"],
      },
      {
        code: "VEL-MEET-RATE-429",
        http: "429",
        symptom: "429 on the public guest join page.",
        rootCause:
          "Per-IP limit (10/min) on the two unauthenticated guest endpoints. They take a token and " +
          "answer a uniform 404, which makes them the obvious place to brute-force invites — the " +
          "limit is what makes that pointless.",
        resolution:
          "Operator: leave it alone unless a legitimate shared-NAT audience is being caught. " +
          "User: wait a minute and click the link again.",
        related: ["VEL-MEET-INVITE-404"],
      },
    ],
  },

  "access-directory": {
    label: "Access & Identity",
    entries: [
      {
        code: "VEL-AUTH-BADCREDS-401",
        http: "401",
        symptom: "401 'Incorrect password. N attempt(s) left' / 'Current password is incorrect'.",
        rootCause: "Wrong password. After MAX_ATTEMPTS the account locks.",
        resolution: "User: re-enter the correct password or reset it. Operator: unlock the account if it locked out.",
      },
      {
        code: "VEL-AUTH-DISABLED-401",
        http: "401",
        symptom: "401 'Account is disabled.'",
        rootCause: "The user account has been disabled.",
        resolution: "Operator: re-enable the account in Access & Identity if access should be restored.",
      },
      {
        code: "VEL-AUTH-MFA-401",
        http: "401/400",
        symptom: "'Invalid MFA token' / 'Invalid or expired OTP' / 'No pending MFA enrolment'.",
        rootCause: "MFA/OTP code is wrong, expired, or the enrolment flow wasn't started.",
        resolution: "User: use a fresh code from your authenticator; restart enrolment if needed.",
      },
      {
        code: "VEL-AUTH-SSO-400",
        http: "400",
        symptom: "400 'This account uses SSO login. Use the SSO button instead.'",
        rootCause: "Password login attempted on an SSO-only account.",
        resolution: "User: sign in with the SSO button.",
      },
      {
        code: "VEL-AUTH-PROVIDER-400",
        http: "400",
        symptom: "400 'Unknown provider …' / 'Token exchange failed' / 'Provider did not return an email'.",
        rootCause: "OAuth/SSO provider is misconfigured or the exchange failed.",
        resolution: "Operator: verify the provider config (client id/secret, endpoints, allowed email scope).",
      },
      {
        code: "VEL-AUTH-GROUP-404",
        http: "404",
        symptom: "404 'Access group not found' / 'Access role not found' / 'Portal not found'.",
        rootCause: "Referenced access group, role, or portal does not exist.",
        resolution: "Operator: confirm the access group/role/portal exists and is in scope.",
      },
      {
        code: "VEL-AUTH-GROUPMEMBER-403",
        http: "403",
        symptom: "403 'You are not a member of this access group'.",
        rootCause: "The user isn't a member of the access group required for the action.",
        resolution: "Operator: add the user to the access group if appropriate.",
      },
    ],
  },

  tenants: {
    label: "Tenants",
    entries: [
      {
        code: "VEL-TENANT-NOTFOUND-404",
        http: "404",
        symptom: "404 'Tenant not found' / 'Membership not found'.",
        rootCause: "The tenant or the user-tenant membership does not exist.",
        resolution: "Operator: confirm the tenant/membership id.",
      },
      {
        code: "VEL-TENANT-SLUG-400",
        http: "400",
        symptom: "400 'Slug must be lowercase alphanumeric with optional hyphens'.",
        rootCause: "Invalid tenant slug format.",
        resolution: "User: use lowercase letters, digits, and hyphens only.",
      },
      {
        code: "VEL-TENANT-DUP-409",
        http: "409",
        symptom: "409 'Tenant with slug … already exists' / 'User already a member of this tenant'.",
        rootCause: "Slug collision or duplicate membership.",
        resolution: "User: choose a unique slug; the membership already exists.",
      },
      {
        code: "VEL-TENANT-DEFAULT-400",
        http: "400",
        symptom: "400 'Cannot delete default tenant' / 'Invalid role'.",
        rootCause: "The default tenant is protected from deletion; or an unrecognized role was supplied.",
        resolution: "Operator: don't delete the default tenant; use a valid role value.",
      },
    ],
  },

  hxbridge: {
    label: "Connectors (HxBridge)",
    entries: [
      {
        code: "VEL-CONN-ROLE-403",
        http: "403",
        symptom: "403 on a connector write.",
        rootCause: "Connector configuration holds outbound credentials, so it is restricted beyond ordinary case access.",
        resolution: "Operator: Grant the `integration` role if appropriate. User: Ask an administrator.",
      },
      {
        code: "VEL-CONN-UNKNOWN-TYPE-400",
        http: "400",
        symptom: "400 quoting the type.",
        rootCause: "The type is not in the first-party allowlist. Connector types are a platform release, never configuration — an arbitrary type would mean arbitrary outbound code.",
        resolution: "Operator: Use a supported type, or ship the connector as a marketplace package. User: None.",
      },
      {
        code: "VEL-CONN-NOTFOUND-404",
        http: "404",
        symptom: "404 'Connector not found' / 'Connector no longer exists' / 'DLQ item not found'.",
        rootCause: "The connector or dead-letter item id does not exist (or the connector was deleted).",
        resolution: "Operator: confirm the connector/DLQ id; recreate the connector if it was removed.",
      },
      {
        code: "VEL-CONN-DISABLED-400",
        http: "400",
        symptom: "400 'Connector is disabled'.",
        rootCause: "The connector exists but is disabled, so it can't run.",
        resolution: "Operator: enable the connector before invoking it.",
      },
      {
        code: "VEL-CONN-TYPE-400",
        http: "400",
        symptom: "400 \"Unknown connector_type: '…'\".",
        rootCause: "The requested connector type isn't registered.",
        resolution: "Operator: use a supported connector type; check the connector registry.",
      },
      {
        code: "VEL-CONN-AUTHZ-403",
        http: "403",
        symptom: "403 'admin or integration role required'.",
        rootCause: "The caller lacks the admin/integration role needed to manage connectors.",
        resolution: "Operator: grant the admin or integration role.",
      },
      {
        code: "VEL-CONN-DUP-409",
        http: "409",
        symptom: "409 \"Connector '…' already exists\" / 'Already resolved: …'.",
        rootCause: "Duplicate connector name, or a DLQ item already resolved.",
        resolution: "User: pick a unique connector name; the DLQ item was already handled.",
      },
    ],
  },

  marketplace: {
    label: "Marketplace",
    entries: [
      {
        code: "VEL-MKT-ACCESS-403",
        http: "403",
        symptom: "403 on a marketplace operation.",
        rootCause: "The caller does not own the workspace or lacks the marketplace role.",
        resolution: "Operator: Grant the role, or transfer workspace ownership. User: Ask an administrator.",
      },
      {
        code: "VEL-MKT-INSTALLED-409",
        http: "409",
        symptom: "409 on install.",
        rootCause: "The package is already installed in this workspace. Installing twice is refused rather than silently upgrading — an upgrade is a separate, reviewable act.",
        resolution: "Operator: Uninstall first, or use the update path. User: Use the installed copy.",
      },
      {
        code: "VEL-MKT-NOTFOUND-404",
        http: "404",
        symptom: "404 naming the entity.",
        rootCause: "One code across marketplace entities — the entity is in `detail` and the fix is the same.",
        resolution: "Operator: Confirm the id and the caller's tenant. User: Reload the marketplace listing.",
      },
      {
        code: "VEL-MKT-OFFICIAL-400",
        http: "400",
        symptom: "400 on source deletion.",
        rootCause: "Official Velaris sources are pinned. Removing one would let a lower-trust source shadow official packages — the trust tier is derived from source origin.",
        resolution: "Operator: None — this is the trust model working. User: None.",
      },
      {
        code: "VEL-MKT-STATE-400",
        http: "400",
        symptom: "400 on a release-request transition.",
        rootCause: "The workspace is not `submitted`. Release transitions are ordered so a package cannot be approved before it is reviewed.",
        resolution: "Operator: Move the workspace through the review flow in order. User: Wait for review.",
      },
      {
        code: "VEL-MKT-VERIFY-502",
        http: "502",
        symptom: "502 on install or update, naming the check that failed.",
        rootCause: "A digest, conformance or drift check could not be completed — usually the source is unreachable or the bundle changed since it was reviewed. **The update is not applied**; refusing is the safe direction.",
        resolution: "Operator: Check source reachability, then re-run. A genuine digest change means the publisher re-cut the release and it needs re-review. User: Retry later.",
      },

      // ── Added 2026-08-11 with the remaining marketplace raise sites. Most of
      // those sites reused the codes above; these are the conditions the
      // existing six could not already say.
      {
        code: "VEL-MKT-DISABLED-404",
        http: "404",
        symptom: "404, as though the module did not exist.",
        rootCause:
          "The marketplace release flag is off. 404 rather than 403 — the same choice HxDB Manager " +
          "makes: the flag hides the module rather than advertising a locked door.",
        resolution:
          "Operator: enable it in the Release Scheduler; marketplace_kill_switch and " +
          "marketplace_dev_only are separate gates. User: not available on this instance.",
        related: ["VEL-MKT-ACCESS-403"],
      },
      {
        code: "VEL-MKT-BLOCKED-403",
        http: "403",
        symptom: "403 naming a blacklist reason or an access-rule denial.",
        rootCause:
          "The org, source or package is blacklisted, or an access rule denies this group. Kept " +
          "apart from VEL-MKT-ACCESS-403 deliberately: that one means who you are, this one means " +
          "what the policy says, and the fix is to change the policy rather than grant a role.",
        resolution:
          "Operator: review the blacklist entry or access rule — both carry an audit reason, read it " +
          "before removing. User: ask an administrator why it is blocked.",
        related: ["VEL-MKT-ACCESS-403"],
      },
      {
        code: "VEL-MKT-CONFORMANCE-422",
        http: "422",
        symptom: "422 naming the failed conformance checks.",
        rootCause:
          "The conformance suite refused the package. 422 not 400: the request was well-formed and " +
          "the package failed. On the update path the installed version is explicitly unaffected and " +
          "keeps running — refusing an update must never take down what already works.",
        resolution:
          "Operator: the gate has off/warn/enforce modes; warn lets it through with a failed badge. " +
          "Do not switch to off to unblock one package. User: the publisher must fix it.",
        related: ["VEL-MKT-VERIFY-502"],
      },
      {
        code: "VEL-MKT-SANDBOX-400",
        http: "400",
        symptom: "400 — HxSandbox not enabled, or the WASM module was rejected.",
        rootCause:
          "Either the platform has HxSandbox off and the package declares runtime 'wasm', or the " +
          "guest module failed its ABI/capability checks. The second is deliberate: the mandate is " +
          "install-time failure, never first-run, so a module that cannot run is refused while " +
          "someone is still watching.",
        resolution:
          "Operator: VELARIS_CASE_HXSANDBOX_ENABLED (default OFF) lifts the first; a rejected module " +
          "is a publisher problem. User: this package needs a capability that is not switched on.",
        related: ["VEL-MKT-CONFORMANCE-422"],
      },
      {
        code: "VEL-MKT-QUOTA-429",
        http: "429",
        symptom: "429 naming the active-workspace limit.",
        rootCause: "Per-user cap on active marketplace workspaces.",
        resolution:
          "Operator: VELARIS_CASE_MARKETPLACE_MAX_WORKSPACES_PER_USER. User: delete a workspace or " +
          "wait for one to be approved.",
        related: ["VEL-MKT-STATE-400"],
      },
      {
        code: "VEL-MKT-GRANT-409",
        http: "409",
        symptom: "409 — the capability grant is revoked, or already revoked.",
        rootCause:
          "A revoked grant is terminal by design: it is not re-approved, the package is reinstalled " +
          "to request again. The code exists so this does not read as a transient state worth " +
          "retrying — silently re-approving a revoked capability would defeat revoking it.",
        resolution:
          "Operator: reinstall the package to raise a fresh grant request. User: ask an administrator.",
        related: ["VEL-MKT-INSTALLED-409"],
      },
      {
        code: "VEL-MKT-PACK-409",
        http: "409",
        symptom: "409 describing an accelerator pack's current state.",
        rootCause:
          "The pack lifecycle has ordering rules — a pack must be provisioned before it can be " +
          "upgraded or forked, and forking is stricter than installing.",
        resolution:
          "Operator: read the state in detail; these are ordering rules, not faults. User: provision " +
          "the pack first.",
        related: ["VEL-MKT-STATE-400"],
      },
      {
        code: "VEL-MKT-INSTALL-400",
        http: "400",
        symptom: "400 naming a provisioning or verification failure during install.",
        rootCause:
          "Execution provisioning (the L2 container broker) failed, or the bundle would not verify " +
          "at install time. Note a status inconsistency this preserves rather than hides: " +
          "install-time verification answers 400 while the update path's identical check answers " +
          "502. Changing an install endpoint's status is a client-visible break, so it was recorded " +
          "instead of silently fixed.",
        resolution:
          "Operator: for provisioning check the container broker and digest pin; for verification " +
          "treat it as VEL-MKT-VERIFY-502 — same cause, different door. User: nothing was applied.",
        related: ["VEL-MKT-VERIFY-502"],
      },
      {
        code: "VEL-MKT-WSSTATE-400",
        http: "400",
        symptom: "400 \"Cannot install into workspace with status '…'\" / 'Workspace not found or not in submitted state'.",
        rootCause: "The workspace isn't in a state that permits the requested lifecycle action.",
        resolution: "User: move the workspace to the correct state (e.g. submit before review) and retry.",
      },
      {
        code: "VEL-MKT-OFFICIALSRC-400",
        http: "400",
        symptom: "400 'Official Velaris sources cannot be removed.'",
        rootCause: "The baked-in official source is protected from removal.",
        resolution: "Operator: leave the official source in place; only community/private sources are removable.",
      },
      {
        code: "VEL-MKT-VERIFY-400",
        http: "400",
        symptom: "400 'Package verification failed: …'.",
        rootCause: "The package failed checksum/manifest validation or trust-tier checks.",
        resolution: "Publisher: fix the manifest/checksum; ensure the id and tier match the registry.",
      },
      {
        code: "VEL-MKT-FLOW-400",
        http: "400",
        symptom: "400 'Only official packages use the release-request flow…' / 'must go through sandbox testing first'.",
        rootCause: "Wrong lifecycle flow for the package tier, or new outbound domains require sandbox review.",
        resolution: "Publisher: use the sandbox workflow for community packages / when domains change.",
      },
      {
        code: "VEL-MKT-AUDIT-400",
        http: "400",
        symptom: "400 'reason is required for the audit trail' / 'Decommissioning reason is required'.",
        rootCause: "An audited action was attempted without the required reason.",
        resolution: "User: supply a reason for the action.",
      },
      {
        code: "VEL-MKT-DENIED-403",
        http: "403",
        symptom: "403 'Access denied'.",
        rootCause: "The caller lacks permission for the marketplace admin action.",
        resolution: "Operator: grant the required marketplace admin role.",
      },
    ],
  },

  /**
   * Portal errors are raised on an unauthenticated, customer-facing surface,
   * but they are documented *here* — in Studio — because this page is for the
   * staff member fielding the support call. The portal itself shows customers
   * the bare code with no link to this catalog; they have no access to it.
   */
  "portal-admin": {
    label: "Customer Portal",
    entries: [
      {
        code: "VEL-PORTAL-NOTFOUND-404",
        http: "404",
        symptom: "404 naming what was not found — portal, tenant, case type, meeting session, action or logo.",
        rootCause:
          "The slug, case type, session, customer action or branding asset does not exist, or the " +
          "portal was never enabled for it. Not the code for a case the caller may not see — that " +
          "is VEL-PORTAL-ACCESS-404.",
        resolution:
          "Operator: check the portal slug and that the tenant is enabled for portal use; for a case " +
          "type, confirm portal_enabled in Portal Admin. User: check the link you were given.",
        related: ["VEL-PORTAL-INACTIVE-403", "VEL-PORTAL-ACCESS-404"],
      },
      {
        code: "VEL-PORTAL-INACTIVE-403",
        http: "403",
        symptom: "The portal resolves but refuses to load or accept a submission.",
        rootCause:
          "The tenant exists and the portal is switched off. Deliberately distinct from a 404: " +
          "'not built' and 'turned off' have different fixes, and this one is a toggle.",
        resolution:
          "Operator: enable the portal in Portal Admin → Tenants. User: the organisation has not " +
          "opened its portal yet.",
        related: ["VEL-PORTAL-NOTFOUND-404"],
      },
      {
        code: "VEL-PORTAL-CASETYPE-400",
        http: "400",
        symptom: "400 on submission, before any case is created.",
        rootCause:
          "Either the case type is not portal-enabled, or it is not in this portal's allowed list. " +
          "Both refuse to create a case the customer was never meant to open.",
        resolution:
          "Operator: enable the case type for the portal AND add it to the portal's allowed list — " +
          "both are required. User: choose a different request type.",
        related: ["VEL-PORTAL-INACTIVE-403"],
      },
      {
        code: "VEL-PORTAL-ACCESS-404",
        http: "404",
        symptom:
          "404 with a deliberately vague message — 'Case not found or email does not match', " +
          "'Tracking token not found'.",
        rootCause:
          "The case does not exist, OR it exists and this caller cannot see it: wrong email, unknown " +
          "tracking token, or no link to the signed-in customer account. **These are one code on " +
          "purpose** — they already share one message so an anonymous caller cannot probe which case " +
          "ids exist, and splitting them by code would rebuild that enumeration oracle in a field " +
          "any client can read.",
        resolution:
          "Operator: check the case's submitter email and the portal_customer_case_links row. Do not " +
          "tell the caller which of the two failed. User: check the tracking link and that you used " +
          "the same email you submitted with.",
        related: ["VEL-PORTAL-NOTFOUND-404", "VEL-AUTH-CREDENTIALS-401"],
      },
      {
        code: "VEL-PORTAL-RATE-LIMIT-429",
        http: "429",
        symptom: "429 after repeated use of portal ask, case chat, messaging or ask feedback.",
        rootCause:
          "A per-IP or per-case limit was reached. Portal AI and messaging are unauthenticated and " +
          "cost money per call, so they are capped. The limiter is in-process and resets on restart.",
        resolution:
          "Operator: limits are at the _rate_check call sites in portal.py — raise them deliberately, " +
          "they are the cost control on anonymous AI. User: wait and try again.",
        related: ["VEL-PORTAL-OTP-RATE-429"],
      },
      {
        code: "VEL-PORTAL-OTP-RATE-429",
        http: "429",
        symptom: "429 either requesting a verification code or entering one.",
        rootCause:
          "Kept separate from the general limit because the resolution differs, and this is the one " +
          "support actually gets called about. A request lockout lasts 24 hours; an attempt lockout " +
          "is cleared by requesting a fresh code.",
        resolution:
          "Operator: confirm which of the two limits fired before advising — the waits are not the " +
          "same. User: if you were entering a code, request a new one; if requesting, wait 24 hours.",
        related: ["VEL-PORTAL-OTP-INVALID-400", "VEL-PORTAL-RATE-LIMIT-429"],
      },
      {
        code: "VEL-PORTAL-OTP-INVALID-400",
        http: "400",
        symptom: "400 on submitting the verification code.",
        rootCause:
          "The code was wrong, already used, or expired. Codes are single-use and compared in " +
          "constant time; 'wrong' and 'expired' are not distinguished in the response on purpose.",
        resolution:
          "Operator: nothing to fix — this is the check working; repeated failures on a valid code " +
          "are worth checking against clock skew. User: request a new code and enter it promptly.",
        related: ["VEL-PORTAL-OTP-RATE-429"],
      },
      {
        code: "VEL-PORTAL-CLOSED-400",
        http: "400",
        symptom: "400 when uploading, replying or completing an action.",
        rootCause:
          "The case is resolved, closed or cancelled. Closed cases stop accepting customer input so " +
          "the record of what was decided cannot change after the fact.",
        resolution:
          "Operator: reopen the case if the customer genuinely has more to add. User: this request " +
          "is finished — raise a new one.",
        related: ["VEL-PORTAL-ACTION-409"],
      },
      {
        code: "VEL-PORTAL-UPLOAD-400",
        http: "400",
        symptom: "400 naming what was wrong with the file.",
        rootCause:
          "Extension not allowed, unsafe filename, oversized, the case type does not accept portal " +
          "uploads, or — for logos — the bytes do not match the declared image type. That last one " +
          "is a magic-byte check: the content type is attacker-controlled.",
        resolution:
          "Operator: allowed extensions are in middleware/file_security.py; logo limits are " +
          "_LOGO_TYPES / _LOGO_MAX_BYTES in portal.py. User: convert or shrink the file and retry.",
        related: ["VEL-DOC-FILETYPE-400", "VEL-PORTAL-CLOSED-400"],
      },
      {
        code: "VEL-PORTAL-ACTION-409",
        http: "409",
        symptom: "409 on submitting a customer action or a rating a second time.",
        rootCause:
          "The action is already completed, or the case has already been rated. Both are once-only " +
          "by design — a second submission would overwrite a recorded decision.",
        resolution:
          "Operator: none. To allow a genuine change, remove the existing record deliberately rather " +
          "than making the endpoint idempotent. User: your response was already recorded.",
        related: ["VEL-PORTAL-CLOSED-400"],
      },
    ],
  },

  hxdbmanager: {
    label: "HxDB Manager",
    entries: [
      {
        code: "VEL-DBM-DISABLED-404",
        http: "404",
        symptom: "404, as though the module did not exist.",
        rootCause:
          "The hxdbmanager release flag is off. 404 rather than 403 on purpose — the flag hides the " +
          "module rather than advertising a locked door.",
        resolution: "Operator: enable hxdbmanager in the Release Scheduler. User: not available here.",
        related: ["VEL-DBM-ROLE-403"],
      },
      {
        code: "VEL-DBM-ROLE-403",
        http: "403",
        symptom: "403 naming the role or privilege required.",
        rootCause:
          "Three tiers: viewing needs admin/designer/developer, the SQL editor needs admin, and " +
          "DDL/DML writes need superadmin. Masked account data additionally needs the DBView privilege.",
        resolution:
          "Operator: grant the role, or use a superadmin account for writes. User: ask an administrator.",
        related: ["VEL-DBM-DISABLED-404", "VEL-DBM-REAUTH-401"],
      },
      {
        code: "VEL-DBM-TABLE-404",
        http: "404",
        symptom: "404 naming the table.",
        rootCause:
          "Not a valid SQL identifier, does not exist, OR is on the hidden list — one code " +
          "deliberately, because hidden tables answer 404 precisely so their existence is never " +
          "confirmed. Invalid identifiers are rejected before any DB query; otherwise a NUL byte " +
          "reaches information_schema and Postgres raises a 500.",
        resolution:
          "Operator: check spelling first; if you expect it to exist, check _HIDDEN_TABLES — " +
          "concealment and absence look identical from outside by design. User: check the schema browser.",
        related: ["VEL-DBM-TABLE-403"],
      },
      {
        code: "VEL-DBM-TABLE-403",
        http: "403",
        symptom: "403 naming the table and the access required.",
        rootCause:
          "Superadmin-only, or otherwise outside your role. The other side of the split above: for " +
          "these tables admitting existence is acceptable, so the refusal is honest not concealed.",
        resolution:
          "Operator: use a superadmin account, or fix the classification. User: ask an administrator.",
        related: ["VEL-DBM-TABLE-404", "VEL-DBM-PROTECTED-403"],
      },
      {
        code: "VEL-DBM-REAUTH-401",
        http: "401",
        symptom: "401 on confirming your password to unmask account data.",
        rootCause:
          "Wrong password, or the account has none to check against (SSO-only). Viewing masked data " +
          "requires re-proving identity even inside a live session.",
        resolution:
          "Operator: an SSO-only account cannot re-auth this way — use a local admin account. " +
          "User: re-enter your password.",
        related: ["VEL-DBM-ROLE-403"],
      },
      {
        code: "VEL-DBM-RATE-429",
        http: "429",
        symptom: "429 naming the limit and window.",
        rootCause:
          "Per-user query rate limit on /execute. Service accounts are exempt — trusted internal " +
          "callers, not humans at a SQL prompt.",
        resolution: "Operator: _EXECUTE_RATE_LIMIT / _EXECUTE_RATE_WINDOW. User: wait a moment.",
        related: ["VEL-DBM-TOOLARGE-413"],
      },
      {
        code: "VEL-DBM-TOOLARGE-413",
        http: "413",
        symptom: "413 at 50 KB of SQL.",
        rootCause: "Statement size cap, checked before parsing.",
        resolution:
          "Operator: genuinely large migrations belong in a migration file. User: split the statement.",
        related: ["VEL-DBM-RATE-429"],
      },
      {
        code: "VEL-DBM-BLOCKED-400",
        http: "400",
        symptom: "400 'Query blocked: …' naming the pattern.",
        rootCause:
          "Matched an abuse pattern. NOT validation — the same path logs a rejected query and fires a " +
          "HIGH ABUSE_PATTERN_BLOCKED breach event.",
        resolution:
          "Operator: look the query up in HxShield by this code; a false positive is a guard bug worth " +
          "fixing, not working around. User: rewrite the query.",
        related: ["VEL-DBM-PROTECTED-403", "VEL-DBM-STMT-400"],
      },
      {
        code: "VEL-DBM-PROTECTED-403",
        http: "403",
        symptom: "403 naming the protected target.",
        rootCause:
          "DML against a table the editor will not write regardless of role. Fires a HIGH " +
          "PROTECTED_TABLE_WRITE breach event.",
        resolution:
          "Operator: change it through the feature that owns it; if it truly must be edited directly " +
          "that is a reviewed migration, not an ad-hoc statement. User: this table cannot be edited here.",
        related: ["VEL-DBM-BLOCKED-400", "VEL-DBM-TABLE-403"],
      },
      {
        code: "VEL-DBM-STMT-400",
        http: "400",
        symptom: "400 saying which statement types the endpoint accepts.",
        rootCause:
          "Each endpoint takes one class: /execute is SELECT+DML, /ddl is DDL, /dml/preview is DML, " +
          "/explain and the optimiser are SELECT. The classifier is an allowlist, so anything it " +
          "cannot positively identify is refused rather than passed through. DDL-via-/execute and " +
          "unknown statements both fire breach events.",
        resolution:
          "Operator: route DDL through /ddl with confirm=true and a reason; a legitimate statement " +
          "classified 'unknown' is a _classify gap worth reporting. User: use the endpoint named.",
        related: ["VEL-DBM-CONFIRM-400", "VEL-DBM-BLOCKED-400"],
      },
      {
        code: "VEL-DBM-CONFIRM-400",
        http: "400",
        symptom: "400 asking for confirm=true and a reason.",
        rootCause:
          "Schema changes are deliberately two-step: the reason is stored with the statement, so the " +
          "query log answers why a column was dropped, not only that it was.",
        resolution:
          "Operator: resend with confirm=true and a real reason — it gets read later. User: confirm " +
          "and say why.",
        related: ["VEL-DBM-STMT-400"],
      },
      {
        code: "VEL-DBM-QUERY-400",
        http: "400",
        symptom: "400 carrying the database engine's own error text.",
        rootCause:
          "The statement passed every platform guard and the database rejected it — syntax, missing " +
          "column, constraint. The code only says the fault is in the SQL rather than the platform.",
        resolution:
          "Operator: read the engine message; note the dialect, since the same SQL is not valid across " +
          "PostgreSQL, MySQL and MariaDB. User: fix the statement.",
        related: ["VEL-DBM-STMT-400"],
      },
      {
        code: "VEL-DBM-AI-503",
        http: "503",
        symptom: "503 — HxNexus unavailable or did not respond.",
        rootCause:
          "The configured AI backend is down, unreachable or misconfigured. Split from the 502 because " +
          "the fixes share nothing: here nothing answered.",
        resolution:
          "Operator: check GET /hxnexus/status — `available` shows liveness, and `misconfigured` names " +
          "a provider set without its API key, which silently falls back to the local model, so an AI " +
          "feature can fail while the status line looks healthy. User: the SQL editor still works.",
        related: ["VEL-DBM-AI-502", "VEL-DBM-PGSTAT-503"],
      },
      {
        code: "VEL-DBM-AI-502",
        http: "502",
        symptom: "502 — empty response, unparseable JSON, or no SQL in the reply.",
        rootCause:
          "The backend is up and answered; the answer was unusable. Usually a model too small to hold " +
          "the JSON format instruction, overloaded, or one that wrapped its reply in prose. Nothing is " +
          "down, which is why this is not a 503.",
        resolution:
          "Operator: use a larger or instruction-tuned model; the message carries the first 200 chars " +
          "of the raw reply — read it before assuming an outage. User: rephrase and retry.",
        related: ["VEL-DBM-AI-503"],
      },
      {
        code: "VEL-DBM-PGSTAT-503",
        http: "503",
        symptom: "503 saying slow-query stats are unavailable on this backend.",
        rootCause:
          "pg_stat_statements (or the MySQL/MariaDB equivalent) is not enabled, so there is no query " +
          "history to advise from. A different 503 from the AI one: the AI is fine, the extension is " +
          "missing.",
        resolution:
          "Operator: enable the extension and restart the database — it cannot be loaded at runtime. " +
          "User: index advice needs history that is not being collected.",
        related: ["VEL-DBM-AI-503"],
      },
      {
        code: "VEL-DBM-PREVIEW-404",
        http: "404",
        symptom: "404 on confirming a DML preview.",
        rootCause:
          "The preview id is unknown — evicted, lost on restart, or never existed. Previews are " +
          "process-local and do not survive a restart.",
        resolution: "Operator: expected after a restart or failover. User: re-run /dml/preview.",
        related: ["VEL-DBM-PREVIEW-410", "VEL-DBM-PREVIEW-403"],
      },
      {
        code: "VEL-DBM-PREVIEW-403",
        http: "403",
        symptom: "403 on confirming a DML preview.",
        rootCause:
          "The preview exists and belongs to a different user. The one preview failure that re-running " +
          "does not fix — usually a copied URL or a stale tab.",
        resolution:
          "Operator: worth a glance in a shared-admin incident — two people editing the same data at " +
          "once is the real finding. User: create your own preview.",
        related: ["VEL-DBM-PREVIEW-404"],
      },
      {
        code: "VEL-DBM-PREVIEW-410",
        http: "410",
        symptom: "410 after the 5-minute window.",
        rootCause:
          "Previews expire so a confirmed statement always reflects data seen moments ago — a " +
          "before-image captured against a stale preview would misrepresent what changed.",
        resolution: "Operator: none, the window is deliberate. User: re-run and confirm promptly.",
        related: ["VEL-DBM-PREVIEW-404"],
      },
    ],
  },

  hxsentinel: {
    label: "HxSentinel (Risk)",
    entries: [
      // Three codes for 26 raise sites, on purpose: 15 are a missing entity
      // already named in the message, 6 are the engine refusing an operation.
      {
        code: "VEL-RISK-NOTFOUND-404",
        http: "404",
        symptom: "\"Finding not found\", \"Hold not found\", \"Official pack not found\", \"Unknown rule\", …",
        rootCause:
          "The entity does not exist, or is outside the caller's tenant — risk data is " +
          "tenant-scoped, so a valid id from another tenant reads as absent. That is the intended " +
          "answer, not a bug.",
        resolution:
          "User: reopen from the list — findings are re-scored and can be superseded. " +
          "Operator: 'official pack not found' usually means the catalogue was never synced.",
        related: ["VEL-RISK-REJECTED-400"],
      },
      {
        code: "VEL-RISK-REJECTED-400",
        http: "400",
        symptom: "A message from the risk service describing what it refused.",
        rootCause:
          "The refusal came from the engine's own rules — label vocabulary, rule shape, taxonomy, " +
          "model state — not from request shape. Labelling is the engine's ground truth, so its " +
          "validation is deliberately stricter than ordinary case work: a wrong label poisons " +
          "everything learned afterwards.",
        resolution: "Operator: the message names the constraint — taxonomy for labels, CONFORMING rules for expressions, active-version state for models.",
        related: ["VEL-RISK-URL-400"],
      },
      {
        code: "VEL-RISK-URL-400",
        http: "400",
        symptom: "\"URL rejected: …\" naming the blocked host or scheme.",
        rootCause:
          "The SSRF guard refused the host — private range, link-local, or the cloud metadata " +
          "address. Kept apart from the other 400 because the fix is nothing like it: the URL is " +
          "well-formed and the manifest may be perfect; the host is refused.",
        resolution:
          "Operator: host the pack publicly over https. Never relax the guard to install one pack " +
          "— it is what stops a pack URL probing internal infrastructure.",
        related: ["VEL-RISK-REJECTED-400"],
      },
    ],
  },

  caseVariables: {
    label: "Case Variables",
    entries: [
      // Most refusals here are enum/regex shape and stay bare. These are the
      // ones a caller cannot deduce from their own request — they point at
      // namespace grants and definitions instead of the request body.
      {
        code: "VEL-VAR-NOTFOUND-404",
        http: "404",
        symptom: "\"Namespace not found\", \"Grant not found\", \"Variable not found\".",
        rootCause: "The named entity does not exist. One code for all three — the entity is in the message and the fix is identical.",
        resolution: "Operator: confirm the namespace was not retired. User: reload the list.",
        related: ["VEL-VAR-EXISTS-409"],
      },
      {
        code: "VEL-VAR-EXISTS-409",
        http: "409",
        symptom: "\"Namespace '…' already exists\", \"Grant already exists\", \"Variable '…' already defined for this case type\".",
        rootCause: "Uniqueness is checked before the write so this arrives as a clean 409 rather than a database error surfacing as a 500.",
        resolution: "User: edit the existing entry rather than creating a second. Operator: often a double-submit or a re-run provisioning script.",
        related: ["VEL-VAR-NOTFOUND-404"],
      },
      {
        code: "VEL-VAR-RESERVED-400",
        http: "400",
        symptom: "\"'…' is a reserved namespace\", \"Reserved namespaces cannot be modified\", \"velaris.* variables are virtual\".",
        rootCause:
          "Some namespaces are the platform's own. `velaris.*` is the sharpest: those are not " +
          "stored variables but a projection over the case's native fields, so defining one would " +
          "create a second, diverging source of truth for the same value.",
        resolution: "User: choose a different namespace. Operator: none — the reservation is structural.",
        related: ["VEL-VAR-NSSTATUS-400"],
      },
      {
        code: "VEL-VAR-NSSTATUS-400",
        http: "400",
        symptom: "\"Namespace '…' is frozen\" / \"… is retired\".",
        rootCause:
          "Namespace lifecycle is active → frozen → retired. Frozen takes no new definitions " +
          "(existing values still resolve); retired is closed. Nothing about the request is " +
          "malformed — the target is closed for writes.",
        resolution: "Operator: re-activate if the freeze was temporary, else point the user at an active namespace.",
        related: ["VEL-VAR-RESERVED-400"],
      },
      {
        code: "VEL-VAR-SENSITIVITY-400",
        http: "400",
        symptom: "\"sensitivity_override may only be STRICTER than the namespace\".",
        rootCause:
          "A variable may tighten its namespace's sensitivity, never loosen it — otherwise one " +
          "permissive variable silently defeats the classification its whole namespace was given, " +
          "and redaction is driven from that classification.",
        resolution: "User: pick a level at least as strict as the namespace's. Operator: raise the namespace's level if the looser one is genuinely right for everything in it.",
        related: ["VEL-VAR-RESERVED-400"],
      },
      {
        code: "VEL-VAR-REJECTED-400",
        http: "400",
        symptom: "A VariableError message describing what the service refused.",
        rootCause:
          "The refusal came from the variables layer — grants, definitions, typing or redaction — " +
          "not from request shape. That distinction is the point: look at namespace grants and " +
          "definitions, not at the request body.",
        resolution: "Operator: check the caller's namespace grants and the variable's definition and sensitivity. Promotion also forces PII carry-over stricter-only.",
        related: ["VEL-VAR-NSSTATUS-400", "VEL-CASE-TYPED-VAR-400"],
      },
    ],
  },

  hxbranch: {
    label: "HxBranch",
    entries: [
      // A branch is a state machine with an owner, a reviewer and a remote, so
      // its refusals answer one of four questions: what state, who are you, is
      // the remote usable, did main move. The status and role are always in the
      // message — the code says which question was answered.
      {
        code: "VEL-BRANCH-STATE-400",
        http: "400",
        symptom: "\"Branch must be 'open' to submit…\", \"Branch already 'merged'.\", \"Cannot edit a branch with status '…'.\"",
        rootCause:
          "The lifecycle is open → pending_review → approved/merged/closed, and each action is " +
          "legal from specific states only. Nothing is wrong with the request — it arrived at the " +
          "wrong point in the flow. The current status is always named.",
        resolution: "User: read the status in the message; a merged or closed branch is finished. Operator: none.",
        related: ["VEL-BRANCH-LOCKED-423"],
      },
      {
        code: "VEL-BRANCH-LOCKED-423",
        http: "423",
        symptom: "\"Branch is locked for review. Recall the branch first to make changes.\"",
        rootCause:
          "A branch in review is frozen: a reviewer is looking at a specific diff, and editing " +
          "underneath them would mean approving something nobody read. 423 rather than 400 — it is " +
          "a lock with an explicit release, not a malformed request.",
        resolution: "User: recall, change, resubmit — the reviewer then sees the new diff. Operator: none, this is the review guarantee.",
        related: ["VEL-BRANCH-STATE-400", "VEL-BRANCH-SOD-400"],
      },
      {
        code: "VEL-BRANCH-ACTOR-403",
        http: "403",
        symptom: "\"Only the branch owner may …\", \"Only the assigned reviewer may post a review decision.\"",
        rootCause:
          "HxBranch authorizes by relationship to this branch — owner, assigned reviewer, admin — " +
          "not by role alone. The required actor is named in the message. Admins bypass ownership; " +
          "they do not bypass the reviewer check on approval.",
        resolution: "User: ask the owner or assigned reviewer. Operator: reassign the reviewer if they are unavailable.",
        related: ["VEL-BRANCH-SOD-400"],
      },
      {
        code: "VEL-BRANCH-SOD-400",
        http: "400",
        symptom: "\"Separation of duties: the reviewer must be a different person from the branch owner.\"",
        rootCause:
          "The control the review flow exists for. Self-approval would make every other guarantee " +
          "here decorative, so it is refused at submit — the earliest point it is cheap to fix. Its " +
          "own code because a bare 400 reads as a form error, and this is a rule auditors ask about.",
        resolution: "User: assign a different reviewer. Operator: never relax this — no second reviewer is an organisational gap, not a software one.",
        related: ["VEL-BRANCH-ACTOR-403"],
      },
      {
        code: "VEL-BRANCH-CONFLICT-409",
        http: "409",
        symptom: "\"Conflict detected: dev main has changed since this branch was created.\"",
        rootCause:
          "The merge compares the branch's base snapshot against current main. A mismatch means " +
          "someone else changed the same artifact, and merging anyway would silently discard their work.",
        resolution: "User: recall, rebase, resubmit — and read the new diff, because something else changed. Operator: none.",
        related: ["VEL-DRAFT-STALE-409"],
      },
      {
        code: "VEL-BRANCH-NOTFOUND-404",
        http: "404",
        symptom: "\"Branch not found\", \"Environment not found\", \"No app packages found in remote environment.\"",
        rootCause: "The named entity does not exist. For the pull case the connection worked — the remote simply has nothing published.",
        resolution: "Operator: for the pull case, publish a package in the source environment; the connection is fine.",
        related: ["VEL-BRANCH-ARTIFACT-404"],
      },
      {
        code: "VEL-BRANCH-ARTIFACT-404",
        http: "404",
        symptom: "\"Rule <id> not found in dev — cannot merge.\"",
        rootCause:
          "Separate from the plain 404 on purpose: the branch and environment are fine, but the " +
          "artifact this branch would merge was deleted between branching and merging. No retry " +
          "will change that.",
        resolution: "Operator: find out who deleted it; if intended, close the branch — the change it carried is moot.",
        related: ["VEL-BRANCH-NOTFOUND-404"],
      },
      {
        code: "VEL-BRANCH-ENV-400",
        http: "400",
        symptom: "\"No API token configured for environment '…'\", \"Environment '…' has no URL configured.\"",
        rootCause: "The environment record exists but is incomplete. Caught before the outbound call, so nothing is sent half-configured.",
        resolution: "Operator: HxBranch → Connections, set the URL and API token. User: ask an administrator.",
        related: ["VEL-BRANCH-ENVAUTH-400"],
      },
      {
        code: "VEL-BRANCH-ENVAUTH-400",
        http: "400",
        symptom: "\"API token for '…' is invalid or expired.\"",
        rootCause:
          "Split from ENV-400 deliberately: there the token is missing and someone must set it; " +
          "here it is set and refused, and the fix is to rotate it at the remote. Collapsing them " +
          "sends an operator to a settings screen that already looks correctly filled in.",
        resolution: "Operator: issue a new token at the remote and update Connections — check it was not revoked with a departed user's account.",
        related: ["VEL-BRANCH-ENV-400", "VEL-BRANCH-REMOTE-502"],
      },
      {
        code: "VEL-BRANCH-REMOTE-502",
        http: "502",
        symptom: "\"Remote environment '…' returned 500: …\"",
        rootCause: "We reached the remote and it failed. 502 not 400 — the fault is upstream, and a client error would send someone to fix a correct request.",
        resolution: "Operator: the failure is in the OTHER environment's logs. The remote's status and body are included in the message.",
        related: ["VEL-BRANCH-ENVAUTH-400"],
      },
      {
        code: "VEL-BRANCH-UNSUPPORTED-400",
        http: "400",
        symptom: "\"Merge not yet supported for artifact_type='…'\", \"Pull not supported for artifact_type='…'\".",
        rootCause: "The request is well-formed and the type valid — the operation just does not extend to it yet. Distinct from the enum check, which means the value is not a type at all.",
        resolution: "User: promote this type manually. Operator: a gap, not a defect — record which type was wanted.",
        related: ["VEL-BRANCH-ARTIFACT-404"],
      },
    ],
  },

  hxmigrate: {
    label: "HxMigrate",
    entries: [
      {
        code: "VEL-MIGRATE-CREATOR-AUTH-400",
        http: "400",
        symptom: "400 on a Creator-mode run.",
        rootCause: "`creator_mode` or `creator_auth_type` is invalid, or no auth token is available.",
        resolution: "Operator: Set the Creator env vars, or switch to file-upload mode. User: Use file upload.",
      },
      {
        code: "VEL-MIGRATE-NOTFOUND-404",
        http: "404",
        symptom: "404 naming the run.",
        rootCause: "The run id does not exist or belongs to another tenant.",
        resolution: "Operator: Confirm the run id. User: Reload the migration history.",
      },
      {
        code: "VEL-MIGRATE-RUN-STATE-400",
        http: "400",
        symptom: "400 quoting the current status.",
        rootCause: "Applying a run that has not finished importing would write a partial case type.",
        resolution: "Operator: Wait for completion, or inspect why it stalled. User: Wait for the run to finish.",
      },
      {
        code: "VEL-MIGRATE-TOO-LARGE-413",
        http: "413",
        symptom: "413 stating the 100 MB limit.",
        rootCause: "HxMigrate has its own higher body cap; this exceeds even that. The cap bounds zip-bomb exposure during extraction.",
        resolution: "Operator: Split the export, or raise `max_migrate_upload_bytes` weighing the extraction risk. User: Export fewer processes at a time.",
      },
      {
        code: "VEL-MIGRATE-TOOBIG-413",
        http: "413",
        symptom: "413 'File too large. Maximum upload size is 100 MB.'",
        rootCause: "The migration export exceeds HxMigrate's 100 MB per-file SEC-7 limit (separate from the transport cap).",
        resolution: "User: split the export or trim it under 100 MB.",
        related: ["VEL-SYS-BODY-413"],
      },
      {
        code: "VEL-MIGRATE-RUNNOTFOUND-404",
        http: "404",
        symptom: "404 'Run not found'.",
        rootCause: "The migration run id does not exist.",
        resolution: "User: re-open the run from the HxMigrate list.",
      },
      {
        code: "VEL-MIGRATE-RUNSTATE-400",
        http: "400",
        symptom: "400 'Run must be completed before applying (status: …)'.",
        rootCause: "Apply was attempted before the run finished.",
        resolution: "User: wait for the run to complete, then apply.",
      },
      {
        code: "VEL-MIGRATE-PLATFORM-400",
        http: "400",
        symptom: "400 'Unsupported platform.'",
        rootCause: "The selected source BPM platform isn't supported by the importer.",
        resolution: "User: choose a supported vendor/platform.",
      },
      {
        code: "VEL-MIGRATE-CREATORAUTH-400",
        http: "400",
        symptom: "400 'No valid auth token available for Creator…' / 'Invalid creator_auth_type'.",
        rootCause: "Creator mode lacks a usable auth token or got an invalid auth type.",
        resolution: "Operator: set VELARIS_SERVICE_TOKEN or ensure the user is logged in; use a valid creator auth type.",
      },
    ],
  },

  "email-admin": {
    label: "Email",
    entries: [
      {
        code: "VEL-EMAIL-NOACCOUNT-400",
        http: "400",
        symptom: "400 'No outbound email account configured'.",
        rootCause: "No outbound email account is set up, so mail can't be sent.",
        resolution: "Operator: configure an outbound email account in Email admin.",
      },
      {
        code: "VEL-EMAIL-NOTFOUND-404",
        http: "404",
        symptom: "404 'Account not found' / 'Template not found' / 'Message not found' / 'No active accounts to poll'.",
        rootCause: "The referenced email account, template, or message doesn't exist (or no active accounts).",
        resolution: "Operator: confirm the account/template/message; activate an account to poll.",
      },
      {
        code: "VEL-EMAIL-TEMPLATE-400",
        http: "400",
        symptom: "400 'Template error: …'.",
        rootCause: "The email template failed to render (bad placeholder / syntax).",
        resolution: "User: fix the template variables/syntax.",
      },
      {
        code: "VEL-EMAIL-SCOPE-400",
        http: "400",
        symptom: "400 \"case_type_id required when scope='case_type'\".",
        rootCause: "A case-type-scoped operation was missing its case_type_id.",
        resolution: "User: supply case_type_id for case-type scope.",
      },
    ],
  },

  // System-wide errors that can surface on any page.
  channels: {
    label: "Omnichannel",
    entries: [
      {
        code: "VEL-CHANNEL-NOTFOUND-404",
        http: "404",
        symptom: "\"Not found.\" on a provider webhook; \"Conversation not found.\" on a reply.",
        rootCause:
          "On a webhook: unsupported provider, malformed endpoint id, no matching endpoint, or a " +
          "signature that did not verify — all four answer the same bare 404 deliberately, so an " +
          "unauthenticated caller cannot learn which endpoints exist or whether the signature was " +
          "the problem. On a reply: the conversation does not exist, or belongs to another tenant.",
        resolution:
          "Operator: compare the URL the provider calls with the endpoint row, then the signing " +
          "secret. The real reason is in the security event; the response will never tell you.",
        related: ["VEL-CHANNEL-OPTED-OUT-403", "VEL-CHANNEL-WINDOW-409"],
      },
      {
        code: "VEL-CHANNEL-OPTED-OUT-403",
        http: "403",
        symptom:
          "\"This number has opted out of messages. They can text START to opt back in.\"",
        rootCause:
          "The recipient sent STOP and the platform holds an opt-out for that address. Checked " +
          "before the provider window, because it is a legal obligation rather than a provider " +
          "preference.",
        resolution:
          "Operator: do not work around it — reach the customer on a channel they have not opted " +
          "out of. Only their own START re-subscribes them. User: text START to that number.",
        related: ["VEL-CHANNEL-WINDOW-409"],
      },
      {
        code: "VEL-CHANNEL-WINDOW-409",
        http: "409",
        symptom:
          "\"More than 24 hours have passed since this customer's last message, so WhatsApp only " +
          "accepts a pre-approved template. Choose one to reply.\"",
        rootCause:
          "Meta allows free-form replies only within 24 hours of the customer's last inbound " +
          "message. The platform refuses here rather than at the provider because Meta accepts " +
          "the message and silently never delivers it.",
        resolution:
          "Operator: reply with an approved template, or wait for the customer to message again, " +
          "which reopens the window. Approve templates at Meta before they are needed.",
        related: ["VEL-CHANNEL-OPTED-OUT-403", "VEL-CHANNEL-NOTFOUND-404"],
      },
    ],
  },

  system: {
    label: "System",
    entries: [
      {
        code: "VEL-SYS-STORAGE-PRECHECK-000",
        http: "000",
        symptom: "Startup warning (or silent) — storage path is not writable.",
        rootCause:
          "A proposed boot-time check that the configured document storage path exists and is " +
          "writable, so the failure surfaces at startup rather than on a user's first upload.",
        resolution:
          "Operator: create the directory with correct ownership, or repoint " +
          "VELARIS_CASE_STORAGE_LOCAL_PATH at an install-local path.",
        related: ["VEL-DOC-STORAGE-503"],
      },
      {
        code: "VEL-SYS-BODY-413",
        http: "413",
        symptom: "413 'Request body too large. Limit is N MB.'",
        rootCause:
          "BodyLimitMiddleware enforces tiered caps (10 MB JSON, 25 MB multipart uploads, 200 MB on hxmigrate routes), " +
          "checked via both the Content-Length header and a streamed byte counter.",
        resolution:
          "User: upload a smaller file or split the payload. Operator: raise max_upload_bytes / the gateway limit only after weighing the DoS surface.",
        related: ["VEL-DOC-FILETYPE-400"],
      },
      {
        code: "VEL-SYS-STORAGE-PRECHECK",
        http: "—",
        symptom: "(Proposed) A clear boot-log error if the storage path isn't writable, instead of the first upload failing later.",
        rootCause: "Same misconfiguration as VEL-DOC-STORAGE-503, caught proactively at startup.",
        resolution: "Operator: fix the path/ownership before the service accepts traffic (see VEL-DOC-STORAGE-503).",
        related: ["VEL-DOC-STORAGE-503"],
      },
    ],
  },
};

/** All entries flattened with their owning component label — used for global search. */
export interface FlatErrorEntry extends ErrorEntry {
  componentKey:   string;
  componentLabel: string;
}

export function allErrors(): FlatErrorEntry[] {
  const out: FlatErrorEntry[] = [];
  for (const [key, comp] of Object.entries(ERROR_CATALOG)) {
    for (const e of comp.entries) {
      out.push({ ...e, componentKey: key, componentLabel: comp.label });
    }
  }
  return out;
}

/**
 * Is this code documented here?
 *
 * Used before offering a "what does this mean" link: a link to an entry that
 * does not exist promises an explanation and then fails to give one, which is
 * worse than showing the bare code.
 */
export function hasEntry(code: string): boolean {
  const c = code.trim().toLowerCase();
  return allErrors().some(e => e.code.toLowerCase() === c);
}

/** Case-insensitive search across code, symptom, root cause, resolution, and component label. */
export function searchErrors(query: string): FlatErrorEntry[] {
  const q = query.trim().toLowerCase();
  if (!q) return [];
  return allErrors().filter(e =>
    e.code.toLowerCase().includes(q) ||
    e.symptom.toLowerCase().includes(q) ||
    e.rootCause.toLowerCase().includes(q) ||
    e.resolution.toLowerCase().includes(q) ||
    e.componentLabel.toLowerCase().includes(q) ||
    e.http.toLowerCase().includes(q),
  );
}
