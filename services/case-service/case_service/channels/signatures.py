"""Inbound webhook signature verification, one function per provider.

The inbound route has no token and no session: the caller is Twilio or Meta, and
**the signature is the entire authentication**. Everything here therefore fails
closed — a missing header is a forgery, not a legacy client — and compares with
:func:`hmac.compare_digest`, because `==` on a MAC leaks the prefix a forger has
guessed correctly.

This is a solved problem in this codebase (`checkout/webhooks.py`,
`stripe_connector.py`, `onfido_connector.py`, `docusign_connector.py`,
`hxdeploy.py` all do it), and the shape here matches theirs deliberately. The
one lesson those five carry is recorded in
`[[project_unsigned_webhook_defect]]`: four of them once treated a *missing*
signature as verified. None of the functions below has a branch that can return
True without computing a MAC.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import base64
import hashlib
import hmac
from urllib.parse import urlencode

#: Providers with a signature scheme we implement. A provider outside this set
#: cannot be configured, rather than being configured and unverified.
SUPPORTED = ("twilio", "meta", "generic")


def verify_twilio(
    secret: str, *, url: str, params: dict[str, str], header: str | None,
) -> bool:
    """Twilio: HMAC-SHA1 over the full URL followed by sorted POST params.

    The URL is part of the signed material, which is what stops a captured
    request being replayed at a different endpoint of ours — so it must be the
    URL Twilio was configured with, not one rebuilt from a `Host` header an
    attacker upstream may control.
    """
    if not secret or not header:
        return False
    payload = url + "".join(f"{k}{params[k]}" for k in sorted(params))
    digest = hmac.new(secret.encode(), payload.encode("utf-8"), hashlib.sha1).digest()
    return hmac.compare_digest(base64.b64encode(digest).decode(), header)


def verify_meta(secret: str, *, body: bytes, header: str | None) -> bool:
    """Meta (WhatsApp Cloud): HMAC-SHA256 over the raw body, hex, `sha256=` prefixed.

    Over the **raw** bytes — re-serialising the parsed JSON changes whitespace
    and key order and the MAC no longer matches, which is why the route reads
    the body before FastAPI parses it.
    """
    if not secret or not header:
        return False
    presented = header[7:] if header.startswith("sha256=") else header
    expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, presented)


def verify_generic(secret: str, *, body: bytes, header: str | None) -> bool:
    """Our own scheme for a customer's bespoke gateway — matches `devconn.py`."""
    if not secret or not header:
        return False
    expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, presented := (
        header[7:] if header.startswith("sha256=") else header))


def signature_header(provider: str) -> str:
    return {
        "twilio": "X-Twilio-Signature",
        "meta": "X-Hub-Signature-256",
        "generic": "X-Velaris-Signature",
    }.get(provider, "X-Velaris-Signature")


def verify(
    provider: str,
    secret: str,
    *,
    body: bytes,
    header: str | None,
    url: str = "",
    form: dict[str, str] | None = None,
) -> bool:
    """Dispatch to the provider's scheme. An unknown provider is never verified."""
    if provider == "twilio":
        return verify_twilio(secret, url=url, params=form or {}, header=header)
    if provider == "meta":
        return verify_meta(secret, body=body, header=header)
    if provider == "generic":
        return verify_generic(secret, body=body, header=header)
    return False


def sign_twilio(secret: str, *, url: str, params: dict[str, str]) -> str:
    """Produce a Twilio signature — for tests and for a self-check endpoint."""
    payload = url + "".join(f"{k}{params[k]}" for k in sorted(params))
    return base64.b64encode(
        hmac.new(secret.encode(), payload.encode("utf-8"), hashlib.sha1).digest()
    ).decode()


def sign_body(secret: str, body: bytes) -> str:
    """Produce the `sha256=` form used by Meta and by our generic scheme."""
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def form_to_params(form) -> dict[str, str]:
    """Flatten a starlette FormData into the `{key: value}` Twilio signs."""
    return {k: str(v) for k, v in form.items() if not hasattr(v, "filename")}


def encode_form(params: dict[str, str]) -> bytes:
    return urlencode(params).encode()
