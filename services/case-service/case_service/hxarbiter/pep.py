"""HxArbiter — the helper every enforcement point calls.

A PEP is three lines at a call site that already works, and the whole risk of
this project lives in those three lines. So the contract is deliberately narrow:

    legacy_allowed = <the check that is already there>
    allowed = await pep.check(session, ..., legacy_allowed=legacy_allowed)
    if not allowed: <refuse exactly as before>

`check()` **returns `legacy_allowed` unchanged** unless the engine's answer is
binding. That is not a convenience — it is the guarantee that wiring a PEP into
a running system changes nothing until an operator promotes the namespace. Both
answers are recorded either way, which is what the parity report reads.

Two failure modes are handled here rather than at forty call sites:

* **The engine throws.** It should not — `pdp.decide()` does not raise — but a
  PEP that can be broken by a bug in the thing it is measuring is worse than no
  PEP. Anything unexpected is logged and the legacy answer is returned.
* **The engine is binding and says deny.** Then, and only then, does the caller's
  answer change, and the caller refuses through its own existing path so the
  error the user sees is the one they would already have seen.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import logging
from typing import Any, Iterable, Sequence

from sqlalchemy.ext.asyncio import AsyncSession

from . import pdp
from hxarbiter_core import AttributeResolver
from hxarbiter_core import (
    Decision,
    DecisionRequest,
    Effect,
    Obligation,
    ResourceRef,
    SubjectKind,
    SubjectRef,
)

log = logging.getLogger(__name__)


#: Convenience wrappers, one per ARB-C enforcement point. They exist so a call
#: site names its action once, in one place — an action spelled two ways at two
#: sites is two namespaces, and the namespace is what carries the default and
#: the clamp.
ACTION_EXPORT_CASE = "export.case_data"
ACTION_EXPORT_EVIDENCE = "export.evidence"
ACTION_VAULT_CREATE = "vault.dek.create"
ACTION_VAULT_ROTATE = "vault.dek.rotate"
ACTION_VAULT_SHRED = "vault.dek.shred"
ACTION_SESSION_ELEVATE = "auth.session.elevate"
ACTION_STAGE_TRANSITION = "case.stage.transition"
#: 198 — a status transition, which until the action dispatcher existed had SIX
#: routes and therefore no sensible place to put a PEP. A status change is a
#: governance-relevant act in its own right: reaching a terminal status cancels
#: the case's SLAs, stops its lifecycle workflow and triggers HxSentinel signal
#: extraction. A sovereignty or retention policy that can refuse a stage
#: transition and not a close was only ever half a control.
ACTION_STATUS_TRANSITION = "case.status.transition"
#: 2026-09-17 — a case leaving one tenant for another. Asked at request and
#: again immediately before the move; the only act that is supposed to cross
#: the tenant boundary is exactly the class of act a policy must be able to
#: refuse.
ACTION_TENANT_TRANSFER = "case.tenant.transfer"
ACTION_AI_EXTERNAL = "ai.external_call"
ACTION_EGRESS_HTTP = "egress.http"


# ── obligations ──────────────────────────────────────────────────────────────
#
# §4.5, and the sentence that decides the whole design: **a PEP that receives an
# obligation it does not implement MUST deny.**
#
# An obligation is the "yes, if" half of an allow — *you may export this, if you
# watermark it*. Until now the evaluator produced them, the store recorded them
# and the API returned them, and `check()` handed back a bare bool, so **every
# call site silently discarded them**. A policy author could write "allow, but
# pseudonymize" and get a plain allow. That is worse than not supporting
# obligations at all: the rule reads as a control and is not one.
#
# Handlers are registered per (action-prefix-free) kind and are *side-effecting*
# — they do the thing the obligation names. A kind with no handler is not
# ignored: `discharge()` refuses, and `check()` turns that refusal into a deny
# when the decision is binding.

ObligationHandler = Any   # async (session, obligation, ctx) -> None | raises

#: kind -> handler. Populated by `register_obligation`; deliberately empty at
#: import so a deployment that wires none gets the fail-closed behaviour rather
#: than a silent no-op.
_HANDLERS: dict[str, ObligationHandler] = {}


def register_obligation(kind: str, handler: ObligationHandler) -> None:
    """Register the code that actually carries out an obligation."""
    _HANDLERS[str(kind)] = handler


def registered_obligations() -> frozenset[str]:
    return frozenset(_HANDLERS)


class ObligationFailed(RuntimeError):
    """An obligation could not be discharged, so the allow does not stand."""


async def discharge(
    session: AsyncSession,
    obligations: Sequence[Obligation],
    *,
    context: dict[str, Any] | None = None,
) -> None:
    """Carry out every obligation, or raise.

    Fail-closed twice over, and both matter:

    * an obligation whose **kind has no handler** raises — the platform was told
      to do something it cannot do, and proceeding would turn a conditional
      allow into an unconditional one;
    * a handler that **raises** propagates — an obligation that was supposed to
      run and did not is exactly the same situation.

    There is no partial success. If two obligations attach to one allow and the
    second fails, the caller is denied; a handler that has already had an effect
    must therefore be idempotent, which is why they take `params` rather than
    arbitrary state.
    """
    for ob in obligations or ():
        handler = _HANDLERS.get(str(ob.kind))
        if handler is None:
            raise ObligationFailed(
                f"policy attached the obligation {ob.kind!r} and nothing in "
                f"this deployment implements it, so the allow it qualifies "
                f"cannot be honoured")
        try:
            result = handler(session, ob, dict(context or {}))
            if hasattr(result, "__await__"):
                await result
        except ObligationFailed:
            raise
        except Exception as exc:                               # noqa: BLE001
            raise ObligationFailed(
                f"the obligation {ob.kind!r} could not be discharged: {exc}"
            ) from exc


async def evaluate(
    session: AsyncSession,
    *,
    tenant_id: str,
    action: str,
    subject_id: str,
    subject_kind: SubjectKind = SubjectKind.SERVICE,
    subject_attrs: dict[str, Any] | None = None,
    resource_kind: str = "platform",
    resource_id: str | None = None,
    resource_attrs: dict[str, Any] | None = None,
    context: dict[str, Any] | None = None,
    legacy_allowed: bool | None = None,
    resolvers: Iterable[AttributeResolver] | None = None,
    claimed_by: str | None = None,
) -> Decision:
    """Ask the engine, recording the legacy answer beside it."""
    legacy = (
        None if legacy_allowed is None
        else (Effect.ALLOW if legacy_allowed else Effect.DENY)
    )
    req = DecisionRequest(
        tenant_id=str(tenant_id),
        subject=SubjectRef(kind=subject_kind, id=str(subject_id)[:255],
                           attrs=subject_attrs or {}),
        action=action,
        resource=ResourceRef(kind=resource_kind, id=resource_id,
                             attrs=resource_attrs or {}),
        context=context or {},
    )
    return await pdp.decide(
        session, req, resolvers=resolvers, legacy_effect=legacy,
        claimed_by=claimed_by,
    )


async def check(
    session: AsyncSession,
    *,
    legacy_allowed: bool,
    caller_handles: Iterable[str] | None = None,
    **kwargs: Any,
) -> bool:
    """The answer the call site should act on.

    Returns `legacy_allowed` unless the engine is binding — so a PEP added to a
    running system is inert until somebody promotes its namespace, and the
    decision log fills with the evidence needed to justify doing that.

    **When the decision is binding and carries obligations, they are discharged
    before this returns True** (§4.5). One that cannot be discharged turns the
    allow into a **deny**: an allow qualified by a condition nobody carried out
    is not the allow the policy author wrote.

    In shadow this changes nothing at all: the decision is not binding, so the
    obligations are logged and the caller's own answer is returned untouched.
    """
    allowed, _ = await check_with_obligations(
        session, legacy_allowed=legacy_allowed,
        caller_handles=caller_handles, **kwargs)
    return allowed


async def check_with_obligations(
    session: AsyncSession,
    *,
    legacy_allowed: bool,
    caller_handles: Iterable[str] | None = None,
    **kwargs: Any,
) -> tuple[bool, list[Obligation]]:
    """`check()`, plus the obligations this caller said it would honour itself.

    `caller_handles` names the kinds the call site can act on — `pseudonymize`
    and `watermark_export` act on the payload the caller is about to produce,
    and no central handler can reach it. Those are returned for the caller to
    apply; **everything else is still discharged here**, and anything neither
    side can honour is a **deny**. Declaring a kind and then not acting on it
    is the one way to defeat this, which is why the set is written at the call
    site, next to the code that honours it.

    The returned obligations come only from a **binding** allow. A shadow
    decision returns an empty list however many the policy carried: handing a
    caller instructions from a decision that is not in force would make shadow
    mode have effects, which is the one thing it must not do.
    """
    try:
        decision = await evaluate(
            session, legacy_allowed=legacy_allowed, **kwargs)
    except Exception as exc:                                   # noqa: BLE001
        # decide() is not supposed to raise. If it somehow does, the call site
        # behaves exactly as it did before this PEP existed: a bug in the thing
        # being measured must never be able to deny live traffic.
        log.error("hxarbiter: PEP evaluation failed, falling back to the "
                  "legacy answer for %s: %s", kwargs.get("action"), exc)
        return legacy_allowed, []

    if not decision.enforced:
        return legacy_allowed, []
    if decision.effect is not Effect.ALLOW:
        return False, []

    obligations = list(decision.obligations or [])
    if not obligations:
        return True, []

    mine = {str(k) for k in (caller_handles or ())}
    for_caller = [o for o in obligations if str(o.kind) in mine]
    centrally = [o for o in obligations if str(o.kind) not in mine]

    if centrally:
        try:
            await discharge(session, centrally,
                            context={"action": kwargs.get("action"),
                                     "subject_id": kwargs.get("subject_id"),
                                     "resource_id": kwargs.get("resource_id"),
                                     "decision_id": str(
                                         getattr(decision, "decision_id", "") or "")})
        except ObligationFailed as exc:
            # Fail closed. The policy said "allow, provided X"; X did not
            # happen, so the allow does not stand. Logged at ERROR because a
            # deployment reaching this has a policy naming something it cannot
            # do, which an operator has to fix — it is not a user error.
            log.error(
                "hxarbiter: denying %s because an obligation could not be "
                "discharged: %s", kwargs.get("action"), exc)
            return False, []
    return True, for_caller
