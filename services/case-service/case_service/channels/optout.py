"""STOP / START — the SMS suppression list, recorded in the consent ledger.

SMS opt-out is a legal requirement in most markets, and the obligation is on the
*sender*: once a person texts STOP, nothing may go to that number until they
text START. So this gate sits in the send path, and it covers the **existing**
workflow-step sends as well as new channel replies — an opt-out that only
applied to the feature built last would be worse than none.

**Why the ledger and not a private table.** A suppression list is a consent
decision about a person; putting it somewhere else would mean an operator
looking at the consent record sees a permission the platform is not honouring,
and a subject-access request misses it. `compliance/consent.py` already is that
record.

**Why `has_consent()` is the wrong primitive here, and using it would be a
silent outage.** `has_consent` fails closed: no record at all means False. That
is right for a purpose you must opt *in* to, and exactly wrong for SMS, where
the default is that you may send until told otherwise. Wiring the send gate to
`has_consent(MARKETING)` would stop every SMS to every number nobody had ever
recorded a grant for — which is all of them. This module therefore reads the
ledger for an **explicit withdrawal** and treats silence as permission.

For the same reason `MARKETING` is not added to `GATED_PURPOSES`: that set turns
a recorded decision into a platform-wide enforcement point, and its own comment
warns that doing so makes every caller fail closed.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import logging
import re

from sqlalchemy.ext.asyncio import AsyncSession

from case_service.compliance import consent

log = logging.getLogger(__name__)

#: The keywords carriers and regulators expect to be honoured. Matched on the
#: whole message, case-insensitively, after stripping punctuation — "Stop." and
#: "STOP" are the same instruction, and a person who means to opt out should not
#: have to type it exactly.
STOP_WORDS = {"stop", "stopall", "unsubscribe", "cancel", "end", "quit"}
START_WORDS = {"start", "unstop", "yes", "subscribe"}
HELP_WORDS = {"help", "info"}

#: The purpose the suppression is recorded against. Channel messaging to a
#: person who asked us to stop is what MARKETING covers; case correspondence
#: under a contract basis is a different question, and one an operator answers
#: by not messaging a number that said stop.
PURPOSE = consent.MARKETING

_PUNCT = re.compile(r"[^\w]+")


def classify(body: str) -> str | None:
    """``"stop"``, ``"start"``, ``"help"`` or None. Never raises."""
    word = _PUNCT.sub("", (body or "").strip().lower())
    if not word:
        return None
    if word in STOP_WORDS:
        return "stop"
    if word in START_WORDS:
        return "start"
    if word in HELP_WORDS:
        return "help"
    return None


def _subject(address: str) -> str:
    return (address or "").strip()


async def record_stop(
    session: AsyncSession, address: str, *, tenant_id: str | None, source: str = "sms_stop",
) -> None:
    await consent.record(
        session,
        subject_ref=_subject(address),
        subject_type="phone",
        purpose=PURPOSE,
        action=consent.WITHDRAWN,
        basis=consent.CONSENT,
        source=source,
        tenant_id=tenant_id,
        evidence={"keyword": "STOP", "channel": "sms"},
    )


async def record_start(
    session: AsyncSession, address: str, *, tenant_id: str | None, source: str = "sms_start",
) -> None:
    await consent.record(
        session,
        subject_ref=_subject(address),
        subject_type="phone",
        purpose=PURPOSE,
        action=consent.GRANTED,
        basis=consent.CONSENT,
        source=source,
        tenant_id=tenant_id,
        evidence={"keyword": "START", "channel": "sms"},
    )


async def may_send(
    session: AsyncSession, address: str, *, tenant_id: str | None = None,
) -> bool:
    """May we send to this number?

    **Silence is permission** — see the module docstring. Only an explicit
    withdrawal on the ledger suppresses, and a later START lifts it because
    `current_state` returns the latest decision per purpose.
    """
    subject = _subject(address)
    if not subject:
        return False
    try:
        state = await consent.current_state(session, subject, tenant_id=tenant_id)
    except Exception:                       # a ledger fault must not stop the world
        log.exception("opt-out check failed; allowing send")
        return True
    entry = state.get(PURPOSE)
    if entry is None:
        return True
    return bool(entry["granted"])


async def suppressed_numbers(
    session: AsyncSession, addresses: list[str], *, tenant_id: str | None = None,
) -> set[str]:
    """Which of these may NOT be sent to. For bulk paths that would otherwise
    make one query per recipient."""
    blocked: set[str] = set()
    for address in addresses:
        if not await may_send(session, address, tenant_id=tenant_id):
            blocked.add(address)
    return blocked
