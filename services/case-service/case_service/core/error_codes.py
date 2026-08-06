"""Stable error codes — the spine of the error catalog (roadmap doc debt).

`docs/Future/error-catalog.md` has documented codes since 2026-06-27, but
nothing emitted them: failures surfaced as a bare 500 or FastAPI's default
422 body, so the catalog described an identifier that did not exist anywhere
in the running system. A support engineer could not look up what they were
looking at.

The contract the catalog asks for is that **the same code appears in the API
response, the server log, and the catalog entry**. This module is the code
registry and the raiser; `main.py` installs the handler that puts the code in
the response body and the log line.

Response shape when a code is attached::

    {"detail": "...", "error": {"code": "VEL-DOC-STORAGE-503",
                                "docs": ".../errors#VEL-DOC-STORAGE-503"}}

`detail` is kept exactly where FastAPI already puts it, so every existing
client keeps working — the code is additive.

Adding a code: add the constant here **and** the entry in the catalog. The
`KNOWN_CODES` set exists so a typo raises at import rather than shipping a
deep link to a page that documents nothing.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import logging
from typing import Any

from fastapi import HTTPException

logger = logging.getLogger(__name__)

#: Where a code deep-links. Kept relative so a self-hosted docs site works.
DOCS_BASE = "/docs/errors"

# ── Documents & Storage ───────────────────────────────────────────
DOC_STORAGE_503 = "VEL-DOC-STORAGE-503"
DOC_ENCRYPT_503 = "VEL-DOC-ENCRYPT-503"
DOC_NOTFOUND_404 = "VEL-DOC-NOTFOUND-404"
DOC_CASE_404 = "VEL-DOC-CASE-404"
DOC_UUID_422 = "VEL-DOC-UUID-422"
DOC_FILETYPE_400 = "VEL-DOC-FILETYPE-400"
DOC_PREVIEW_415 = "VEL-DOC-PREVIEW-415"
DOC_DELETE_403 = "VEL-DOC-DELETE-403"
DOC_LASTVERSION_409 = "VEL-DOC-LASTVERSION-409"

# ── Authentication ────────────────────────────────────────────────
# The failures a user actually hits and cannot self-diagnose. Deliberately
# NOT one code per raise site: `auth_real.py` alone raises 50 times, and
# most are variations of "not found" that a code would not clarify.
#
# Note what is missing on purpose — there is no code distinguishing "no such
# user" from "wrong password". Both must stay a single opaque failure, or the
# code becomes a user-enumeration oracle for anyone reading the response.
AUTH_CREDENTIALS_401 = "VEL-AUTH-CREDENTIALS-401"
AUTH_DISABLED_403 = "VEL-AUTH-DISABLED-403"
AUTH_SSO_REQUIRED_400 = "VEL-AUTH-SSO-REQUIRED-400"
AUTH_OTP_INVALID_400 = "VEL-AUTH-OTP-INVALID-400"
AUTH_RATE_LIMIT_429 = "VEL-AUTH-RATE-LIMIT-429"
AUTH_DEVICE_MISMATCH_401 = "VEL-AUTH-DEVICE-MISMATCH-401"
AUTH_SSO_UNCONFIGURED_404 = "VEL-AUTH-SSO-UNCONFIGURED-404"
AUTH_EMAIL_TAKEN_409 = "VEL-AUTH-EMAIL-TAKEN-409"

# ── Cases ─────────────────────────────────────────────────────────
# One `NOTFOUND` code covers case, case type, step, rule, share and proposal.
# A code per entity would be noise: the entity is already named in `detail`,
# and the resolution is identical for all of them.
CASE_NOTFOUND_404 = "VEL-CASE-NOTFOUND-404"
CASE_GUARD_403 = "VEL-CASE-GUARD-403"
CASE_RULE_SCOPE_403 = "VEL-CASE-RULE-SCOPE-403"
CASE_RULE_DISABLED_409 = "VEL-CASE-RULE-DISABLED-409"
CASE_TYPED_VAR_400 = "VEL-CASE-TYPED-VAR-400"
CASE_VAR_FILTER_400 = "VEL-CASE-VAR-FILTER-400"
CASE_AI_ACTION_400 = "VEL-CASE-AI-ACTION-400"
CASE_PROCDEF_400 = "VEL-CASE-PROCDEF-400"
CASE_SUBPROCESS_500 = "VEL-CASE-SUBPROCESS-500"

# ── Marketplace ───────────────────────────────────────────────────
MKT_NOTFOUND_404 = "VEL-MKT-NOTFOUND-404"
MKT_ACCESS_403 = "VEL-MKT-ACCESS-403"
MKT_INSTALLED_409 = "VEL-MKT-INSTALLED-409"
MKT_STATE_400 = "VEL-MKT-STATE-400"
MKT_OFFICIAL_400 = "VEL-MKT-OFFICIAL-400"
MKT_VERIFY_502 = "VEL-MKT-VERIFY-502"

# ── Connectors (HxBridge) ─────────────────────────────────────────
CONN_NOTFOUND_404 = "VEL-CONN-NOTFOUND-404"
CONN_DISABLED_400 = "VEL-CONN-DISABLED-400"
CONN_UNKNOWN_TYPE_400 = "VEL-CONN-UNKNOWN-TYPE-400"
CONN_ROLE_403 = "VEL-CONN-ROLE-403"

# ── LLM / HxNexus ─────────────────────────────────────────────────
LLM_NOTFOUND_404 = "VEL-LLM-NOTFOUND-404"
LLM_NOT_ASSIGNED_403 = "VEL-LLM-NOT-ASSIGNED-403"

# ── HxMigrate ─────────────────────────────────────────────────────
MIGRATE_NOTFOUND_404 = "VEL-MIGRATE-NOTFOUND-404"
MIGRATE_RUN_STATE_400 = "VEL-MIGRATE-RUN-STATE-400"
MIGRATE_PLATFORM_400 = "VEL-MIGRATE-PLATFORM-400"
MIGRATE_CREATOR_AUTH_400 = "VEL-MIGRATE-CREATOR-AUTH-400"
MIGRATE_TOO_LARGE_413 = "VEL-MIGRATE-TOO-LARGE-413"

# ── Vault (HxVault) ───────────────────────────────────────────────
# Both were raised deep in the crypto layer and caught nowhere, so they
# surfaced as bare 500s. A shredded DEK is UNRECOVERABLE and a 500 gives the
# operator no way to know that — they retry, escalate, and eventually restore
# a backup that cannot help either.
VAULT_SHREDDED_410 = "VEL-VAULT-SHREDDED-410"
VAULT_DEK_503 = "VEL-VAULT-DEK-503"

# ── Consent (HxConsent) ───────────────────────────────────────────
#: Raised when every recipient of a consent-gated send lacks a lawful basis.
#: A partial drop is NOT an error — the send proceeds for whoever remains, and
#: the drops are audited. This code means nobody was left to send to.
CONSENT_REFUSED_403 = "VEL-CONSENT-REFUSED-403"

# ── Database ──────────────────────────────────────────────────────
DB_UNAVAILABLE_503 = "VEL-DB-UNAVAILABLE-503"

# ── System / Startup ──────────────────────────────────────────────
SYS_BODY_413 = "VEL-SYS-BODY-413"
SYS_STORAGE_PRECHECK_000 = "VEL-SYS-STORAGE-PRECHECK-000"
#: The floor under every other handler. Anything that reaches it is a bug we
#: have not classified yet — but the client still gets JSON, because Starlette's
#: default 500 is `text/plain` and every caller in this platform parses JSON.
#: A browser hitting that default reports `JSON.parse: unexpected character at
#: line 1 column 1`, which describes the response format and says nothing about
#: the actual fault — as it did on 2026-08-05, when a dead database presented as
#: a front-end parser error.
SYS_UNHANDLED_500 = "VEL-SYS-UNHANDLED-500"

#: Every code the catalog documents. A code raised outside this set is a bug:
#: it deep-links to a page that explains nothing.
KNOWN_CODES: frozenset[str] = frozenset({
    DOC_STORAGE_503, DOC_ENCRYPT_503, DOC_NOTFOUND_404, DOC_CASE_404,
    DOC_UUID_422, DOC_FILETYPE_400, DOC_PREVIEW_415, DOC_DELETE_403,
    DOC_LASTVERSION_409,
    AUTH_CREDENTIALS_401, AUTH_DISABLED_403, AUTH_SSO_REQUIRED_400,
    AUTH_OTP_INVALID_400, AUTH_RATE_LIMIT_429, AUTH_DEVICE_MISMATCH_401,
    AUTH_SSO_UNCONFIGURED_404, AUTH_EMAIL_TAKEN_409,
    CASE_NOTFOUND_404, CASE_GUARD_403, CASE_RULE_SCOPE_403,
    CASE_RULE_DISABLED_409, CASE_TYPED_VAR_400, CASE_VAR_FILTER_400,
    CASE_AI_ACTION_400, CASE_PROCDEF_400, CASE_SUBPROCESS_500,
    MKT_NOTFOUND_404, MKT_ACCESS_403, MKT_INSTALLED_409,
    MKT_STATE_400, MKT_OFFICIAL_400, MKT_VERIFY_502,
    CONN_NOTFOUND_404, CONN_DISABLED_400, CONN_UNKNOWN_TYPE_400, CONN_ROLE_403,
    LLM_NOTFOUND_404, LLM_NOT_ASSIGNED_403,
    MIGRATE_NOTFOUND_404, MIGRATE_RUN_STATE_400, MIGRATE_PLATFORM_400,
    MIGRATE_CREATOR_AUTH_400, MIGRATE_TOO_LARGE_413,
    VAULT_SHREDDED_410, VAULT_DEK_503,
    CONSENT_REFUSED_403,
    DB_UNAVAILABLE_503,
    SYS_BODY_413, SYS_STORAGE_PRECHECK_000, SYS_UNHANDLED_500,
})


def docs_url(code: str) -> str:
    return f"{DOCS_BASE}#{code}"


class CodedHTTPException(HTTPException):
    """An HTTPException carrying a catalog code.

    Subclasses HTTPException rather than replacing it so every existing
    handler, middleware and test that expects one keeps working — the code is
    strictly additive.
    """

    def __init__(self, status_code: int, code: str, detail: str, **kwargs: Any) -> None:
        if code not in KNOWN_CODES:
            # Loud, because a code that is not in the catalog is worse than no
            # code: it promises documentation that does not exist.
            raise ValueError(
                f"{code!r} is not a documented error code — add it to "
                f"error_codes.KNOWN_CODES and to docs/Future/error-catalog.md"
            )
        super().__init__(status_code=status_code, detail=detail, **kwargs)
        self.code = code


def coded(status_code: int, code: str, detail: str, **kwargs: Any) -> CodedHTTPException:
    """Convenience raiser: `raise coded(503, DOC_STORAGE_503, "…")`."""
    return CodedHTTPException(status_code, code, detail, **kwargs)


def error_payload(code: str, detail: Any) -> dict[str, Any]:
    """The response body for a coded error."""
    return {"detail": detail, "error": {"code": code, "docs": docs_url(code)}}
