"""Every JWT verification must be told about the RSA public key.

`decode_jwt_token` chooses its algorithm from whether `public_key` is supplied —
RS256 when it is, HS256 when it is not. That design is deliberate and correct
(the algorithm is never caller-supplied, which is what blocks alg-confusion and
HS256/RS256 swap attacks). The cost is that *omitting* the argument is not a
weaker check, it is the **wrong** check: on a deployment with
`VELARIS_CASE_AUTH_RSA_*` set, an RS256 token fails with
`InvalidAlgorithmError`.

Three call sites omitted it, found 2026-08-15 while migrating the role check in
`hxstream.py`:

  * `hxstream.py`  — every HxStream websocket closed 4001 for every caller.
  * `realtime.py`  — same, for the realtime socket.
  * `middleware/audit.py` — the worst of the three, because it failed *quietly*:
    the caller swallows the exception and returns None, so every audit record
    lost its actor and the superadmin → `__system__` masking never ran.

None of them had a test, and none could have one that did not first notice the
argument was missing — hence the AST scan below rather than three unit tests.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[2] / "case_service"


def _decode_calls_missing_public_key() -> list[str]:
    out: list[str] = []
    for path in SRC.rglob("*.py"):
        try:
            tree = ast.parse(path.read_text())
        except SyntaxError:                      # pragma: no cover
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            fn = node.func
            name = fn.attr if isinstance(fn, ast.Attribute) else getattr(fn, "id", "")
            if name != "decode_jwt_token":
                continue
            kwargs = {k.arg for k in node.keywords if k.arg}
            # `**kwargs` forwarding counts as supplying it.
            if any(k.arg is None for k in node.keywords):
                continue
            if "public_key" not in kwargs:
                out.append(f"{path.relative_to(SRC)}:{node.lineno}")
    return out


def test_every_decode_supplies_the_public_key():
    missing = _decode_calls_missing_public_key()
    assert not missing, (
        "decode_jwt_token() verifies HS256 when public_key is omitted, so these "
        f"call sites reject every RS256 token: {missing}")


def test_the_scan_is_not_vacuous():
    """A scanner that matched no call at all would pass the test above while
    proving nothing — the same trap the authz parity table guards against."""
    found = 0
    for path in SRC.rglob("*.py"):
        try:
            tree = ast.parse(path.read_text())
        except SyntaxError:                      # pragma: no cover
            continue
        for node in ast.walk(tree):
            fn = getattr(node, "func", None)
            name = getattr(fn, "attr", None) or getattr(fn, "id", None)
            if isinstance(node, ast.Call) and name == "decode_jwt_token":
                found += 1
    assert found >= 4, f"expected several decode_jwt_token call sites, saw {found}"


def test_rs256_token_fails_hs256_only_verification():
    """Pins *why* the argument matters, so the scan above is not folklore."""
    from case_service.auth.jwt_handler import (create_dev_token, decode_jwt_token,
                                               generate_rsa_keypair)

    private_key, public_key = generate_rsa_keypair()
    token = create_dev_token(user_id="u1", username="u1", roles=["admin"],
                             secret="a-shared-secret", private_key=private_key)

    with pytest.raises(Exception):
        decode_jwt_token(token, secret="a-shared-secret")

    claims = decode_jwt_token(token, secret="a-shared-secret", public_key=public_key)
    assert claims["sub"] == "u1"
