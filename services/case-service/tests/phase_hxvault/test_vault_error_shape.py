"""HxVault's two error codes, asserted where a caller actually sees them.

HxVault has no router — its faults are raised deep in the crypto layer and are
mapped by app-level exception handlers in `main.py`. That is why the codes were
recorded as "raised nowhere": nothing greps as a raise site. They were wired,
but nothing tested the wiring, so the mapping was only as good as its reading.

The two must not collapse into each other. They differ in exactly one way that
matters to whoever is paged:

  * `VEL-VAULT-DEK-503` — transient. The vault is sealed or unreachable, the
    data is fine, retry once it is fixed.
  * `VEL-VAULT-SHREDDED-410` — terminal. The key was destroyed on purpose and
    the data is not coming back, not from a backup either.

Telling an operator the second is the first costs a retry loop, an escalation
and a restore attempt that cannot work.

`raise_app_exceptions=False`: Starlette's ServerErrorMiddleware responds and
then re-raises so the traceback is still logged; a test client on the default
would see the re-raise rather than the response the client gets. See
`tests/test_unhandled_error_shape.py`.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import uuid

import pytest
from httpx import ASGITransport, AsyncClient

from velaris_shared.core.error_codes import (
    KNOWN_CODES,
    VAULT_DEK_503,
    VAULT_SHREDDED_410,
    docs_url,
)
from case_service.hxvault import DekNotLoadedError
from case_service.hxvault.keyring import KeyShreddedError
from case_service.main import create_app

pytestmark = pytest.mark.asyncio


def _app_raising(exc: BaseException):
    app = create_app()

    @app.get("/_vault_boom")
    async def _boom():  # noqa: ANN202
        raise exc

    return app


async def _get(app) -> tuple[int, dict, str]:
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        r = await c.get("/_vault_boom")
    return r.status_code, r.json(), r.headers.get("content-type", "")


# ── The mapping ───────────────────────────────────────────────────


async def test_shredded_key_is_a_410_not_a_500():
    status, body, ctype = await _get(_app_raising(KeyShreddedError("DEK for tenant x shredded")))

    assert status == 410, "410 Gone: it existed, it was destroyed, it is not coming back"
    assert "application/json" in ctype
    assert body["error"]["code"] == VAULT_SHREDDED_410
    assert body["error"]["docs"] == docs_url(VAULT_SHREDDED_410)


async def test_unavailable_dek_is_a_503():
    status, body, ctype = await _get(_app_raising(DekNotLoadedError("no DEK cached")))

    assert status == 503
    assert "application/json" in ctype
    assert body["error"]["code"] == VAULT_DEK_503


async def test_the_two_never_share_a_status():
    """The distinction is the entire reason there are two codes."""
    shredded, _, _ = await _get(_app_raising(KeyShreddedError("x")))
    unavailable, _, _ = await _get(_app_raising(DekNotLoadedError("x")))
    assert shredded != unavailable


async def test_shredded_response_says_unrecoverable_not_retryable():
    """Wording is the deliverable here — the code is only useful if the text agrees."""
    _, body, _ = await _get(_app_raising(KeyShreddedError("x")))
    detail = body["detail"].lower()
    assert "cannot be recovered" in detail
    assert "backup" in detail, "an operator's next instinct is a restore; pre-empt it"


async def test_dek_response_says_the_data_is_intact():
    _, body, _ = await _get(_app_raising(DekNotLoadedError("x")))
    assert "intact" in body["detail"].lower()


# ── Neither may leak what the exception carried ───────────────────


async def test_vault_errors_leak_no_internals():
    """The messages name tenants, cache keys and vault hosts. None may ship."""
    secret = "tenant 3f9a-dead-beef via https://bao.internal:8200 kek=hunter2"  # noqa: S105
    for exc in (KeyShreddedError(secret), DekNotLoadedError(secret)):
        _, body, _ = await _get(_app_raising(exc))
        serialised = str(body)
        for leaked in ("hunter2", "bao.internal", "3f9a-dead-beef", "Traceback"):
            assert leaked not in serialised, f"{leaked!r} reached the client via {type(exc).__name__}"


# ── End to end: a real shredded tenant, not a hand-thrown exception ─


async def test_real_shredded_decrypt_surfaces_as_410(session):
    """The whole path: shred a tenant, then let a route decrypt its ciphertext.

    Hand-raising the exception proves the handler; this proves the crypto layer
    raises the exception the handler expects. Before the shredded set existed,
    this produced `DekNotLoadedError` and therefore a 503 telling the operator to
    retry data that no longer exists.
    """
    from case_service import hxvault

    tid = uuid.uuid4()
    await hxvault.ensure_dek(session, tid)
    ct = hxvault.encrypt(tid, b"gdpr-erased", "ctx")
    await hxvault.shred(session, tid)

    app = create_app()

    @app.get("/_vault_boom")
    async def _read():  # noqa: ANN202
        return {"plaintext": hxvault.decrypt(ct).decode()}

    status, body, _ = await _get(app)
    assert status == 410
    assert body["error"]["code"] == VAULT_SHREDDED_410


async def test_both_codes_are_registered():
    assert VAULT_SHREDDED_410 in KNOWN_CODES
    assert VAULT_DEK_503 in KNOWN_CODES
