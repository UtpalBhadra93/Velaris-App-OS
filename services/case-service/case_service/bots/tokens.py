"""Per-job result tokens — how a container returns work without broker writes.

The obvious way to let a bot write its findings back is to add write scopes to
the scoped broker. That would put mutation capability inside **every** container
on the apps network, permanently, to serve one feature.

Instead the bot returns and the platform writes. The runner mints one token for
one job, hands it to that container only, and the container can do exactly one
thing with it: POST its result once. The token is:

* **random** — 256 bits from `secrets`, not derived from anything guessable;
* **stored hashed** — only SHA-256 is on the row, so a database read yields
  nothing that works;
* **compared in constant time** — `==` on a credential leaks the prefix a forger
  has guessed correctly;
* **single use** — `result_token_used_at` is set on redemption, so a leaked
  token is a one-shot rather than a standing capability;
* **bound to its job** — a token that verifies against a different job's row is
  simply not that job's token, so there is no separate ownership check to forget.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import hashlib
import secrets


def mint() -> tuple[str, str]:
    """Return ``(plaintext, sha256)``. The plaintext is never stored."""
    token = secrets.token_urlsafe(32)
    return token, hashlib.sha256(token.encode()).hexdigest()


def matches(presented: str | None, stored_hash: str | None) -> bool:
    """Constant-time comparison. A missing token or hash never matches."""
    if not presented or not stored_hash:
        return False
    return secrets.compare_digest(
        hashlib.sha256(presented.encode()).hexdigest(), stored_hash,
    )
