"""HxShield response layer — what happens after the alert (roadmap #25).

`engine.evaluate()` has returned an `action` for every incident since the day it
shipped — flag, alert, suspend, block — and nothing ever performed one. The
string reached a caller that wrote it down. This module is the half that acts,
and almost all of it is about **not** acting:

* **Shadow first.** Every response is recorded. It is executed only when
  `shield_response_enabled` is on, which no install has by default and no update
  ever turns on. An operator can read a week of "this is what would have
  happened" before anything at all happens. An automated response that surprises
  its operator is switched off permanently on its first false positive.
* **A destructive response is never automatic.** Suspending an account or
  killing every session waits for a person, whatever the switch says. The switch
  governs the reversible ones.
* **The vocabulary is closed and lives in code.** A response type this platform
  has never heard of is unrepresentable, not merely unrecognised — the same
  reasoning that fixed HxArbiter's advisor after a policy name turned out to be
  a prompt.
* **One target, one open proposal.** A burst of a hundred events must not queue
  a hundred identical suspensions for one person to click through.

**What it deliberately does not do.** It does not invent responses the detection
rules never asked for. The rules already carry an action; this maps that action
onto a primitive that already exists elsewhere in the platform, and refuses to
map the ones that have no safe primitive.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from sqlalchemy import select

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ResponseDef:
    """One thing HxShield may do, and how much trust it needs to do it."""

    name: str
    label: str
    target_kind: str
    #: Destructive responses take something away and are ALWAYS proposed for a
    #: human, whatever the platform switch says.
    destructive: bool
    summary: str
    #: The action that undoes this one, where one exists. A response with no
    #: reverse should give an approver pause, and saying so is the cheapest way
    #: to cause it.
    reversible_by: str | None = None


RESPONSES: dict[str, ResponseDef] = {}


def _reg(definition: ResponseDef) -> None:
    RESPONSES[definition.name] = definition


_reg(ResponseDef(
    name="notify",
    label="Notify the security contact",
    target_kind="incident",
    destructive=False,
    summary="Record the incident and raise it where people look. Takes nothing "
            "away, so it runs unattended once responses are switched on.",
))
_reg(ResponseDef(
    name="revoke_sessions",
    label="Revoke every session and device",
    target_kind="user",
    destructive=True,
    summary="Signs the actor out everywhere and revokes their refresh tokens. "
            "The right response to a stolen session and the wrong one to a "
            "false positive at 3am, which is why it waits for a person.",
))
_reg(ResponseDef(
    name="suspend_user",
    label="Suspend the account",
    target_kind="user",
    destructive=True,
    summary="Disables the account outright. Reversible, and still the heaviest "
            "thing here: it stops somebody working.",
    reversible_by="reinstate_user",
))
#: **Two responses were written and then removed on the same day, 2026-09-09,
#: for the reason this platform keeps re-learning.** `quarantine_case` wrote a
#: `_shield_quarantine` marker into `case.data` that **nothing reads** — a
#: response claiming to stop a case being routed while routing never asked —
#: and `revoke_agent_tokens` was **unreachable**: no rule action maps to it, so
#: it could sit in the catalogue forever and never once be proposed. That is
#: HxShield's own history repeating (eight patterns, one caller, zero
#: incidents), and a catalogue entry with nothing behind it is a promise that
#: cannot be kept. Both come back when a rule action asks for them AND
#: something acts on the result.

#: What the detection rules already say, mapped onto what exists. `flag` and
#: `alert` are the reversible ones; `suspend` and `block` are not.
#:
#: `block` is mapped to session revocation rather than to a network block on
#: purpose: this platform has no packet-level control to offer, and a response
#: named "block" that quietly did something weaker would be worse than one that
#: says what it actually does.
ACTION_TO_RESPONSE: dict[str, str] = {
    "flag": "notify",
    "alert": "notify",
    "suspend": "suspend_user",
    "block": "revoke_sessions",
}

#: How long a proposal for the same target counts as "already asked". Long
#: enough that a burst does not become a queue; short enough that a genuine
#: second incident tomorrow is its own decision.
DEDUPE_WINDOW = timedelta(hours=6)


def responses_enabled() -> bool:
    from velaris_shared.config import get_settings
    return bool(getattr(get_settings(), "shield_response_enabled", False))


def catalogue() -> list[dict[str, Any]]:
    """What exists, generated from the registry rather than typed into a UI."""
    return [
        {
            "name": r.name, "label": r.label, "target_kind": r.target_kind,
            "destructive": r.destructive, "summary": r.summary,
            "reversible_by": r.reversible_by,
            "needs_approval": r.destructive,
        }
        for r in RESPONSES.values()
    ]


async def _already_proposed(session, response_type: str, target_id: str) -> bool:
    from velaris_shared.db.models import ShieldResponseModel

    since = datetime.now(timezone.utc) - DEDUPE_WINDOW
    row = (await session.execute(
        select(ShieldResponseModel).where(
            ShieldResponseModel.response_type == response_type,
            ShieldResponseModel.target_id == target_id,
            ShieldResponseModel.created_at >= since,
            ShieldResponseModel.state.in_(("proposed", "approved", "executed", "shadowed")),
        ).limit(1)
    )).scalars().first()
    return row is not None


async def propose(
    session,
    *,
    action: str,
    incident_id: uuid.UUID | None,
    actor_id: str | None,
    tenant_id: str | None,
    case_id: str | None = None,
    reason: str | None = None,
    patterns: list[str] | None = None,
    score: float | None = None,
):
    """Record what should happen. Execute it only if it is safe to.

    Returns the row, or None when there is nothing to propose — an unmapped
    action, or a target already proposed for.

    **Never raises into the caller.** This runs from the detection hook, which
    runs from inside `log_security_event`, which runs from inside login. A
    response layer that can fail a login is a worse outcome than a missed
    response, and it is how a security feature becomes the outage.
    """
    from velaris_shared.db.models import ShieldResponseModel

    try:
        response_name = ACTION_TO_RESPONSE.get((action or "").strip().lower())
        if response_name is None:
            return None
        definition = RESPONSES[response_name]

        target_id = case_id if definition.target_kind == "case" else (actor_id or "")
        if definition.target_kind == "incident":
            target_id = str(incident_id or "")
        if not target_id:
            # Nothing to act on. An anonymous actor cannot be suspended, and
            # inventing a target is how the wrong account gets disabled.
            return None

        if await _already_proposed(session, response_name, target_id):
            return None

        row = ShieldResponseModel(
            id=uuid.uuid4(),
            tenant_id=tenant_id,
            incident_id=incident_id,
            response_type=response_name,
            target_kind=definition.target_kind,
            target_id=str(target_id),
            destructive=definition.destructive,
            reason=(reason or "")[:2000] or None,
            patterns=json.dumps(patterns or []),
            score=score,
            created_at=datetime.now(timezone.utc),
        )

        if not responses_enabled():
            # Shadow: recorded, never performed, and SAID so rather than left
            # looking like a proposal somebody forgot to approve.
            row.state = "shadowed"
            row.result = ("Responses are switched off on this install "
                          "(VELARIS_CASE_SHIELD_RESPONSE_ENABLED). Recorded only.")
        elif definition.destructive:
            row.state = "proposed"
        else:
            row.state = "approved"

        session.add(row)
        await session.flush()

        if row.state == "approved":
            await execute(session, row, actor="hxshield")
        return row
    except Exception:  # noqa: BLE001
        logger.exception("hxshield: proposing a response failed — the incident "
                         "was still recorded")
        return None


# ── The primitives ────────────────────────────────────────────────────────────
#
# Each one is something the platform already does; this module only decides
# WHEN. Nothing here reimplements a control, because a second implementation of
# "revoke a session" is a second thing to get wrong.


async def _notify(session, row) -> str:
    """The alert already exists — the incident row and the HxStream event.

    This response's job is to make it visible where people look, which today
    means the incident queue. It is not a no-op dressed as an action: it is the
    action, and pretending otherwise by wiring an email nobody configured would
    be worse.
    """
    return "Incident raised for review."



async def _revoke_sessions(session, row) -> str:
    from velaris_shared.auth.devices import revoke_device
    from velaris_shared.db.models import AuthDeviceModel

    devices = (await session.execute(
        select(AuthDeviceModel).where(
            AuthDeviceModel.user_id == row.target_id,
            AuthDeviceModel.revoked_at.is_(None),
        )
    )).scalars().all()
    tokens = 0
    for device in devices:
        tokens += await revoke_device(session, device, revoked_by="hxshield")
    return f"Revoked {len(devices)} device(s) and {tokens} refresh token(s)."


async def _suspend_user(session, row) -> str:
    from velaris_shared.db.models import HelixUserModel, UserDirectoryModel

    directory = (await session.execute(
        select(UserDirectoryModel).where(UserDirectoryModel.user_id == row.target_id)
    )).scalars().first()
    auth_user = (await session.execute(
        select(HelixUserModel).where(HelixUserModel.username == row.target_id)
    )).scalars().first()
    if directory is None and auth_user is None:
        raise LookupError(f"no account for {row.target_id}")
    if directory is not None:
        directory.is_active = False
    if auth_user is not None:
        # Both tables, or the account is disabled in the directory and can
        # still log in — which is the shape of the bug this mirrors around.
        auth_user.is_active = False
    await session.flush()
    return "Account suspended in the directory and the login table."



_PRIMITIVES: dict[str, Callable] = {
    "notify": _notify,
    "revoke_sessions": _revoke_sessions,
    "suspend_user": _suspend_user,
}


async def execute(session, row, *, actor: str) -> str:
    """Perform a response that is allowed to be performed.

    Refuses a row in any state but `approved`. That check is here, not only at
    the routes: a response is the one thing in this module that changes the
    world, and the guard belongs where the change happens.
    """
    from velaris_shared.db.models import ShieldResponseModel  # noqa: F401

    if row.state != "approved":
        raise PermissionError(
            f"a response in state '{row.state}' is not executed — "
            "approve it first")

    primitive = _PRIMITIVES.get(row.response_type)
    if primitive is None:
        row.state = "failed"
        row.result = f"No primitive for '{row.response_type}'."
        await session.flush()
        return row.result

    try:
        result = await primitive(session, row)
        row.state = "executed"
        row.result = result[:2000]
    except Exception as exc:  # noqa: BLE001
        # A failed response is a state, not an exception thrown at a caller who
        # is in the middle of a login. It is recorded loudly so nobody reads the
        # queue and believes the account was suspended.
        logger.exception("hxshield: response %s failed", row.id)
        row.state = "failed"
        row.result = f"{type(exc).__name__}: {exc}"[:2000]
    row.executed_at = datetime.now(timezone.utc)
    await session.flush()
    return row.result or ""


async def approve(session, row, *, actor: str) -> str:
    """A person says yes. Then, and only then, it runs."""
    if row.state not in ("proposed", "shadowed"):
        raise PermissionError(f"a response in state '{row.state}' cannot be approved")
    if row.state == "shadowed" and not responses_enabled():
        raise PermissionError(
            "Responses are switched off on this install, so an approval would "
            "have nothing to run. This row records what would have happened; "
            "set VELARIS_CASE_SHIELD_RESPONSE_ENABLED to act on it.")
    row.approved_by = actor
    row.approved_at = datetime.now(timezone.utc)
    row.state = "approved"
    await session.flush()
    return await execute(session, row, actor=actor)


async def decline(session, row, *, actor: str, reason: str | None = None) -> None:
    """A person says no, and that is recorded as firmly as a yes.

    A declined proposal is evidence about the detector: a rule declined every
    time is a rule that should change, and that only shows up if 'no' is
    written down.
    """
    if row.state in ("executed", "failed"):
        raise PermissionError(f"a response in state '{row.state}' cannot be declined")
    row.declined_by = actor
    row.declined_at = datetime.now(timezone.utc)
    row.state = "declined"
    if reason:
        row.result = f"Declined: {reason}"[:2000]
    await session.flush()
