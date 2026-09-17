"""F-00 — attributing an action to an agent.

The reason this whole feature exists. A ledger that says "the AI decided" is
not evidence; one that says *this agent, running this model, at this version*
is. Two helpers:

* `model_id()` — what actually answered, not what is configured. It reads
  `llm.factory.active_model()`, which already resolves the egress ladder's real
  route (the local model answers most calls; the external one only when the
  local cannot). "Whatever is configured" would be a plausible-looking lie.
* `check()` — the `agent` namespace's enforcement point, on top of the ordinary
  `hxarbiter.pep`. Adding it changes nothing today: `agent` ships clamped to
  `shadow`, so `check()` returns the caller's own answer and files a decision
  beside it, which is the evidence a promotion is later argued from.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import logging
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from hxarbiter_core import SubjectKind

from . import identity
from .tokens import AgentCaller

log = logging.getLogger(__name__)

#: The `agent` namespace's actions. The same strings as the capability
#: vocabulary in `identity.CAPABILITIES`, and deliberately so: a capability the
#: engine cannot decide about would be authority nothing evaluates, and an
#: action no token can carry would be a rule that never fires.
ACTION_READ_CASE = "agent.read_case"
ACTION_ACT_ON_CASE = "agent.act_on_case"
ACTION_PROPOSE = "agent.propose"


class AgentRefused(PermissionError):
    """Policy refused this agent the action it was about to take.

    Its own type, and not a bare `PermissionError`, because the routers already
    map that to **400** — "you got something wrong". A policy refusal is not
    the caller's mistake: the request was well-formed and the caller
    authorised, and the platform declined on policy grounds, which is **403**.
    The same distinction the DSAR export path had to be given (design §30)
    after a refused export surfaced as an unhandled 500.
    """
    def __init__(self, message: str, *, action: str = "", agent_id: str = ""):
        super().__init__(message)
        self.action = action
        self.agent_id = agent_id


def model_id(llm: Any = None) -> str:
    """`provider/model` for the call that just happened, e.g. `ollama/llama3.2`.

    Call it immediately after the completion whose provenance you want.
    Never raises: an attribution that could fail would make the ledger's
    coverage depend on the weather, and a decision must not fail because its
    metadata could not be resolved.
    """
    try:
        from velaris_shared.llm.factory import active_model
        ident = active_model(llm)
        provider = str(ident.get("provider") or "unknown")
        model = str(ident.get("model") or "unknown")
        return f"{provider}/{model}"[:255]
    except Exception:                                          # noqa: BLE001
        log.debug("agent attribution: model identity unresolved", exc_info=True)
        return "unknown/unknown"


def subject_attrs(
    agent_kind: str, model: str | None = None,
) -> dict[str, Any]:
    """The `subject.*` attributes a policy may read about an agent.

    Written here and nowhere else. Neither path is resolvable by a PIP, which
    is the point: an agent must not be able to answer a question about itself
    that a policy then uses to decide about it. These come from the registry
    row and from the backend that answered — never from the token, and never
    from the caller's request body.
    """
    attrs: dict[str, Any] = {"agent_kind": str(agent_kind)}
    if model:
        attrs["model_id"] = str(model)[:255]
    return attrs


async def check(
    session: AsyncSession,
    *,
    agent_id: Any,
    agent_kind: str,
    tenant_id: Any,
    action: str,
    legacy_allowed: bool,
    model: str | None = None,
    resource_kind: str = "case",
    resource_id: Any | None = None,
    resource_attrs: dict[str, Any] | None = None,
    context: dict[str, Any] | None = None,
    claimed_by: str = "agents.attribution",
) -> bool:
    """Ask HxArbiter whether this agent may do this, recording who asked.

    Returns `legacy_allowed` unless the `agent` namespace has been promoted —
    the standard PEP contract, so wiring this in changes no answer today.
    """
    from case_service.hxarbiter import pep

    return await pep.check(
        session,
        tenant_id=str(tenant_id),
        action=str(action),
        subject_id=str(agent_id),
        subject_kind=SubjectKind.AGENT,
        subject_attrs=subject_attrs(agent_kind, model),
        resource_kind=resource_kind,
        resource_id=str(resource_id) if resource_id is not None else None,
        resource_attrs=resource_attrs or {},
        context=context or {},
        legacy_allowed=legacy_allowed,
        claimed_by=claimed_by,
    )


async def check_caller(
    session: AsyncSession,
    caller: AgentCaller,
    *,
    action: str,
    model: str | None = None,
    resource_kind: str = "case",
    resource_id: Any | None = None,
    resource_attrs: dict[str, Any] | None = None,
    context: dict[str, Any] | None = None,
    claimed_by: str = "agents.attribution",
) -> bool:
    """`check()` for a request that arrived on an agent token.

    The capability is the legacy answer: before this feature there was no agent
    principal at all, so "does the token carry it" is the control the engine is
    shadowing, and parity is measured against exactly that.
    """
    return await check(
        session,
        agent_id=caller.agent_id,
        agent_kind=caller.kind,
        tenant_id=caller.tenant_id,
        action=action,
        legacy_allowed=caller.can(action),
        model=model or caller.model_binding,
        resource_kind=resource_kind,
        resource_id=resource_id,
        resource_attrs=resource_attrs,
        context=context,
        claimed_by=claimed_by,
    )


#: The platform's own AI workers, and the capability each is registered with.
#:
#: Registered on first use rather than seeded by the migration, deliberately:
#: an install that never runs case Q&A should not carry an agent for it, and a
#: migration that invents principals nobody asked for is the kind of thing an
#: operator finds later and cannot explain. `agent.propose` for both — HxNexus
#: answers questions and drafts text; it advances nothing on its own.
INTERNAL_AGENTS: dict[str, tuple[str, str]] = {
    #  slug                 (display name,                    capability)
    "hxnexus.case_qa":      ("HxNexus — case Q&A",            ACTION_PROPOSE),
    "hxnexus.voicefill":    ("HxNexus — voice form-fill",     ACTION_PROPOSE),
    "workflow.ai_action":   ("Workflow — ai_action step",     ACTION_PROPOSE),
    #: F-06. Propose-only by construction: capture writes nothing itself — an
    #: accepted proposal is applied by the person who confirmed it, through
    #: their own variable-write path. There is no capability here that would
    #: let it act on a case.
    "capture.ambient":      ("Ambient capture",               ACTION_PROPOSE),
}


async def internal_agent_id(tenant_id: Any, slug: str) -> str:
    """The id to attribute an internal worker's call to.

    Never raises and never returns nothing: when the registry cannot be reached
    the slug is returned prefixed, because a decision attributed to
    `unregistered:hxnexus.case_qa` still says which worker acted, while a
    missing attribution says nothing at all. A missing registry row must never
    mean a missing ledger entry, or the coverage claim becomes "everything is
    attributed, except where it isn't".
    """
    display, capability = INTERNAL_AGENTS.get(slug, (slug, ACTION_PROPOSE))
    agent_id = await identity.register_out_of_band(
        tenant_id=tenant_id, slug=slug, kind="internal",
        display_name=display, capabilities=[capability])
    return agent_id or f"unregistered:{slug}"


async def check_internal(
    session: AsyncSession,
    *,
    tenant_id: Any,
    slug: str,
    action: str,
    legacy_allowed: bool,
    model: str | None = None,
    resource_kind: str = "case",
    resource_id: Any | None = None,
    context: dict[str, Any] | None = None,
    claimed_by: str = "agents.attribution",
) -> bool:
    """`check()` for a platform worker that holds no token, only a slug.

    An internal worker runs in-process and authenticates by being the code it
    is; what it lacked was an *identity to attribute*. That is what this
    resolves.
    """
    return await check(
        session,
        agent_id=await internal_agent_id(tenant_id, slug),
        agent_kind="internal",
        tenant_id=tenant_id,
        action=action,
        legacy_allowed=legacy_allowed,
        model=model,
        resource_kind=resource_kind,
        resource_id=resource_id,
        context=context,
        claimed_by=claimed_by,
    )
