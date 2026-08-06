"""Every error response is JSON — including the ones nobody classified.

On 2026-08-05 a stopped Postgres produced, in the browser, `JSON.parse:
unexpected character at line 1 column 1`. That message is about the *content
type*, not the fault: no handler claimed the error, so Starlette's built-in
fallback returned `text/plain` "Internal Server Error", and the front-end
parsed it as JSON. The database being down was invisible from the client.

Two gaps produced that, and these tests pin both:

1. `OperationalError` alone does not catch a database that is *absent*.
   SQLAlchemy only wraps a fault into it once a connection exists; when the
   server is not listening, asyncpg's connect fails first and a bare
   `ConnectionRefusedError` propagates. The handler describing exactly that
   situation was one exception type away from firing.
2. Nothing sat under the classified handlers, so any unclassified exception
   fell through to the text/plain default.

A fresh app from `create_app()` rather than the shared module-level one, so
the throwaway routes these tests need cannot leak into another test's app.

`raise_app_exceptions=False` is required, not incidental: Starlette's
ServerErrorMiddleware sends the handler's response and *then* re-raises so the
server still logs the traceback. Production clients get the JSON; a test client
left on the default would see the re-raise instead of the response.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.exc import InterfaceError, OperationalError

from case_service.core.error_codes import (
    DB_UNAVAILABLE_503,
    SYS_UNHANDLED_500,
    KNOWN_CODES,
    docs_url,
)
from case_service.main import create_app

pytestmark = pytest.mark.asyncio


def _app_raising(exc: BaseException):
    """An app whose one route raises `exc`, with the real handler stack."""
    app = create_app()

    @app.get("/_boom")
    async def _boom():  # noqa: ANN202
        raise exc

    return app


async def _get(app) -> tuple[int, dict, str]:
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        r = await c.get("/_boom")
    return r.status_code, r.json(), r.headers.get("content-type", "")


# ── The reported bug ──────────────────────────────────────────────


async def test_connection_refused_is_a_json_503_not_a_text_500():
    """The exact 2026-08-05 failure: Postgres not listening."""
    status, body, ctype = await _get(_app_raising(ConnectionRefusedError(111, "Connection refused")))

    assert status == 503, "a database that is not running is not the caller's fault"
    assert "application/json" in ctype, (
        "text/plain here is what the browser reported as a JSON.parse error"
    )
    assert body["error"]["code"] == DB_UNAVAILABLE_503


async def test_operational_error_still_maps_to_the_same_503():
    """The pre-existing path must not regress now that it shares a handler."""
    status, body, _ = await _get(
        _app_raising(OperationalError("SELECT 1", {}, Exception("gone")))
    )
    assert status == 503
    assert body["error"]["code"] == DB_UNAVAILABLE_503


async def test_interface_error_maps_to_the_same_503():
    """A connection dropped mid-statement is the same story to a caller."""
    status, body, _ = await _get(
        _app_raising(InterfaceError("SELECT 1", {}, Exception("closed")))
    )
    assert status == 503
    assert body["error"]["code"] == DB_UNAVAILABLE_503


# ── The floor under everything ────────────────────────────────────


async def test_unclassified_error_is_json_500_with_a_code():
    status, body, ctype = await _get(_app_raising(RuntimeError("something nobody handled")))

    assert status == 500
    assert "application/json" in ctype
    assert body["error"]["code"] == SYS_UNHANDLED_500
    assert body["error"]["docs"] == docs_url(SYS_UNHANDLED_500)


async def test_unhandled_response_leaks_nothing_about_the_exception():
    """An unclassified error is the most likely to carry a secret in its text."""
    secret = "postgresql://velaris:hunter2@10.0.0.5:5432/velaris"  # noqa: S105
    status, body, _ = await _get(_app_raising(RuntimeError(secret)))

    assert status == 500
    serialised = str(body)
    for leaked in (secret, "hunter2", "10.0.0.5", "RuntimeError", "Traceback"):
        assert leaked not in serialised, f"{leaked!r} reached the client"


async def test_http_exceptions_are_not_swallowed_by_the_floor():
    """A deliberate 404 must stay a 404 — the floor is for bugs only."""
    from fastapi import HTTPException

    status, body, _ = await _get(_app_raising(HTTPException(status_code=404, detail="nope")))

    assert status == 404
    assert body["detail"] == "nope"
    assert "error" not in body, "a routine 404 should not be dressed as an unhandled bug"


async def test_the_new_code_is_registered():
    """A code outside KNOWN_CODES deep-links to a page that explains nothing."""
    assert SYS_UNHANDLED_500 in KNOWN_CODES
