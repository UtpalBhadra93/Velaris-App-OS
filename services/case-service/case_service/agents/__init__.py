"""F-00 — agent identity.

A first-class principal for AI actors, so that every later feature of the
AI-era roadmap can say *which agent, which model, which version* did a thing
instead of attributing it to a service account or to whichever human's request
happened to trigger it.

Three parts:

* `identity` — the registry: agents, their capability ceiling, their status.
* `tokens`   — short-lived, capability-scoped JWTs anchored to a grant row that
               is re-read on every call, so revocation is instant.
* `attribution` — what the ledger records: agent id plus the model that
               actually answered, never the one that was configured.

Nothing here is on by default. `agent_tokens_enabled` gates minting and
acceptance, the `agent` HxArbiter namespace ships clamped to `shadow`, and an
install that never creates an agent behaves exactly as it did before.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

from . import attribution, identity, tokens, workforce  # noqa: F401

__all__ = ["attribution", "identity", "tokens", "workforce"]
