"""HxArbiter — the code that actually carries out an obligation (§4.5).

An obligation is the *"yes, if"* half of an allow: **you may export this, if you
watermark it.** The engine has been able to express them since ARB-A and the
decision log has recorded them since — but `pep.check()` returned a bare bool,
so every call site discarded them. A policy author could write "allow, but
pseudonymize" and get a plain allow, which is worse than not supporting
obligations at all: the rule reads as a control and is not one.

**The design rule is fail-closed.** `ObligationKind`'s own docstring says a PEP
receiving one it does not implement MUST deny, and `pep.discharge` enforces it.
That makes the split below load-bearing rather than tidy:

* **Centrally dischargeable** — `record_purpose` and `notify` need nothing from
  the caller but the decision itself, so they are registered here and every PEP
  gets them for free.
* **Caller-cooperative** — `pseudonymize`, `watermark_export` and
  `require_step_up` act on *the thing the caller is about to do*, and no generic
  handler can reach it. They are deliberately **left unregistered**, so a policy
  attaching one to an action whose call site has not opted in **denies** rather
  than quietly allowing. A call site opts in with
  `pep.check_with_obligations(..., discharge_obligations=False)` and honours
  them itself at the right moment.

That asymmetry is the honest one. The alternative — registering a no-op for
every kind so nothing ever denies — is how an obligation becomes decoration.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from hxarbiter_core import Obligation, ObligationKind
from velaris_shared.db.models import SecurityEventModel

from . import pep

log = logging.getLogger(__name__)

#: Kinds no central handler can honour, because they act on the caller's own
#: payload or session. Named here so the reason is discoverable from the code
#: rather than inferred from an absence.
CALLER_COOPERATIVE = frozenset({
    str(ObligationKind.PSEUDONYMIZE),
    str(ObligationKind.WATERMARK_EXPORT),
    str(ObligationKind.REQUIRE_STEP_UP),
})


async def record_purpose(
    session: AsyncSession, ob: Obligation, ctx: dict[str, Any],
) -> None:
    """Write down why this was permitted.

    The decision row already says *what* was decided. This says what the
    permission was *for*, which is the question a lawful-basis audit asks and
    the decision log cannot answer on its own.

    `flush`, never `commit`: an obligation runs inside the caller's transaction,
    and committing here would persist a permission record for an operation that
    may still fail and roll back.
    """
    purpose = str(ob.params.get("purpose") or ctx.get("action") or "unspecified")
    session.add(SecurityEventModel(
        id=uuid.uuid4(),
        event_type="arbiter_obligation",
        severity="info",
        user_id=str(ctx.get("subject_id") or "")[:255] or None,
        resource_type="policy_decision",
        resource_id=str(ctx.get("decision_id") or "")[:255] or None,
        action=str(ctx.get("action") or "")[:100] or None,
        outcome="allowed",
        details={"obligation": str(ob.kind), "purpose": purpose,
                 "params": ob.params},
        timestamp=datetime.now(timezone.utc),
    ))
    await session.flush()


async def notify(
    session: AsyncSession, ob: Obligation, ctx: dict[str, Any],
) -> None:
    """Raise a security event for an allow somebody wants to know about.

    Deliberately a security event and not an email: a notification channel can
    be unreachable, and an obligation that fails denies the request. Making
    "somebody should know" depend on an SMTP server would let a mail outage
    refuse legitimate work.
    """
    session.add(SecurityEventModel(
        id=uuid.uuid4(),
        event_type="arbiter_obligation_notify",
        severity=str(ob.params.get("severity") or "warning")[:20],
        user_id=str(ctx.get("subject_id") or "")[:255] or None,
        resource_type="policy_decision",
        resource_id=str(ctx.get("decision_id") or "")[:255] or None,
        action=str(ctx.get("action") or "")[:100] or None,
        outcome="allowed",
        details={"obligation": str(ob.kind), "params": ob.params,
                 "message": str(ob.params.get("message") or "")[:1000]},
        timestamp=datetime.now(timezone.utc),
    ))
    await session.flush()


def register_defaults() -> None:
    """Wire the centrally dischargeable kinds. Idempotent."""
    pep.register_obligation(str(ObligationKind.RECORD_PURPOSE), record_purpose)
    pep.register_obligation(str(ObligationKind.NOTIFY), notify)


register_defaults()
