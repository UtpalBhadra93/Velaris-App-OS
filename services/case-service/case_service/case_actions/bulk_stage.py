"""Bulk STAGE transition — the part of B3 that was left out on purpose.

Design: `docs/components/case-actions.md` §6c.

`POST /cases/bulk` shipped on 2026-09-07 without stage moves, deliberately. Every
other bulk action changes a field on a case. A stage move **fires things per
case** — the new stage's SLA starts, its `on_enter_status` is applied, the
lifecycle and interpreter workflows are signalled and may run steps — and none
of that is undone by moving the case back. So it gets what the other actions do
not need:

* **Its own privilege**, `case.actions.bulk_stage`. Holding `case.update` on a
  case lets an operator move THAT case; it has never meant "may move five
  hundred at once". Admin and manager by default, not case worker. The
  per-case `case.update` relationship check is still asked for every case —
  the privilege is added, never substituted.
* **A two-step confirmation that the server enforces.** `preview` touches
  nothing and answers, per case, whether it would move and what firing it
  would cause; it returns a token bound to the caller, the target stage and
  the EXACT set of cases it found eligible. `execute` refuses without that
  token, and refuses any case outside that set — so nothing moves that the
  operator was not shown moving. A confirm dialog that lives only in the
  client is a dialog a script skips.
* **The ceiling refuses, never truncates** — the same `BULK_CEILING` as the
  rest of bulk, so an operator learns one number.
* **The 2026-09-08 stage rules are respected, not re-implemented.** The move
  goes through the same code as `POST /cases/{id}/stage`, so the entered
  stage's `on_enter_status` is applied by `stages.apply_on_enter_status` and
  wins over anything else — this path writes no status of its own.

Stricter than the single-case route in two places, both because a bulk run is
not looked at case by case: a target stage that does not exist in a case's
case type is refused (the single route writes any string, and changing that is
an upgrade break), and a case already in the target stage is left alone rather
than re-entered.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import time
import uuid
from typing import Iterable

#: HxGuard platform action. Registered in `velaris_shared.hxguard.actions`.
PRIVILEGE = "case.actions.bulk_stage"

#: How long a preview stays confirmable. Long enough to read a preview of a few
#: hundred cases; short enough that the state it described is still roughly
#: true. Execution re-checks every case regardless.
CONFIRM_TTL_SECONDS = 10 * 60

_TOKEN_VERSION = "v1"
_TOKEN_PURPOSE = b"velaris.bulk-stage-confirm.v1"


def _key() -> bytes:
    """Domain-separated from every other use of the auth secret — the same
    derivation SAML's RelayState uses, with a different purpose string, so a
    token minted here can never verify as anything else."""
    from velaris_shared.config import get_settings

    return hmac.new(
        (get_settings().auth_secret or "").encode(), _TOKEN_PURPOSE, hashlib.sha256,
    ).digest()


def _digest(user_id: str, target_stage_id: str, case_ids: Iterable[uuid.UUID | str],
            expires_at: int) -> str:
    # Sorted, so the order a client sends ids back in is not part of the binding;
    # the SET is.
    ids = ",".join(sorted({str(c) for c in case_ids}))
    payload = "\n".join(
        (_TOKEN_VERSION, str(user_id), str(target_stage_id), str(expires_at), ids),
    ).encode()
    return base64.urlsafe_b64encode(
        hmac.new(_key(), payload, hashlib.sha256).digest()).decode().rstrip("=")


def mint(user_id: str, target_stage_id: str, case_ids: Iterable[uuid.UUID | str],
         *, now: float | None = None) -> tuple[str, int]:
    """A confirmation for exactly these cases. Returns (token, expires_at)."""
    expires_at = int(now if now is not None else time.time()) + CONFIRM_TTL_SECONDS
    sig = _digest(user_id, target_stage_id, case_ids, expires_at)
    return f"{_TOKEN_VERSION}.{expires_at}.{sig}", expires_at


def verify(token: str | None, user_id: str, target_stage_id: str,
           case_ids: Iterable[uuid.UUID | str], *, now: float | None = None) -> str | None:
    """None when the token confirms exactly this request; otherwise the reason.

    The reasons are distinct because "preview again" and "you changed the
    selection after previewing" are different things to tell an operator.
    """
    if not token:
        return "No confirmation. Preview the move first and confirm it."
    parts = token.split(".")
    if len(parts) != 3 or parts[0] != _TOKEN_VERSION or not parts[1].isdigit():
        return "The confirmation is malformed. Preview the move again."
    expires_at = int(parts[1])
    if (now if now is not None else time.time()) > expires_at:
        return "The confirmation has expired. Preview the move again."
    expected = _digest(user_id, target_stage_id, case_ids, expires_at)
    if not hmac.compare_digest(expected, parts[2]):
        return ("The confirmation does not match this request — a different "
                "target stage, a different set of cases, or a different operator. "
                "Preview the move again.")
    return None
