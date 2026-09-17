"""HxArbiter ARB-F — where the model comes from (design §19).

The resolution ladder, in order, and the last rung is the one that matters:

1. **Host-provided** — the tenant's own HxNexus configuration, including its
   `egress: local_only | external_allowed` choice. There is no second AI config
   here, deliberately: one place an operator sets a model, one egress ladder,
   one `local_only` switch. A second provider chain is a second thing to audit
   and a second thing to get wrong.
2. **A local model** — the sovereignty-safe rung.
3. **Nothing, and the feature is simply absent.** Manual authoring, exactly as
   before. **Never an error, never a prompt, never silently on.**

Rung 3 is a direct application of the platform's "someone is already running
Velaris" rule: unset must mean "as before". An install that has never configured
AI gets a Studio pane saying "configure a provider" and a policy engine that
works completely without one.

Two hard rules, both from §19.5:

* **The authoring call is itself governed by HxArbiter.** The engine asks itself
  whether this tenant may call this model, so an EU tenant on `local_only` gets
  a local model or gets manual authoring.
* **The model never sees tenant data.** It receives the policy catalog, the
  namespace schema and the operator's own words. Never case rows, never
  decision-log subjects.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

log = logging.getLogger(__name__)


class ProviderUnavailable(RuntimeError):
    """No provider resolved. The FEATURE is absent, not broken.

    Callers surface this as "configure a provider" on the authoring pane and
    nowhere else — nothing about policy evaluation depends on it.
    """


@dataclass
class ResolvedProvider:
    backend: Any
    #: "local" or "external" — recorded on the draft as provenance, because
    #: "your compliance control was written by an LLM" is the first question in
    #: the room and the answer needs to name which one.
    provider_class: str
    name: str


async def resolve(
    session: AsyncSession, *, tenant_slug: str, feature_label: str = "Policy authoring",
) -> ResolvedProvider:
    """Walk the ladder. Raises ProviderUnavailable at the bottom."""
    try:
        from case_service.hxnexus.case_qa import resolve_backend
    except Exception as exc:                                    # noqa: BLE001
        raise ProviderUnavailable("HxNexus is not available") from exc

    try:
        # `resolve_backend` returns (backend, external_consented) — NOT a bare
        # backend. Assuming the latter produced a tuple that quietly failed at
        # generation time and left a junk draft row behind instead of the
        # honest 503 this function exists to raise. Unpacked defensively so a
        # future change of shape is a ProviderUnavailable, not a bad draft.
        resolved = await resolve_backend(
            session, tenant_slug, feature_label=feature_label)
        if isinstance(resolved, tuple):
            backend = resolved[0] if resolved else None
        else:
            backend = resolved
    except PermissionError as exc:
        # The tenant has not opted in. That is a configuration choice, not a fault.
        raise ProviderUnavailable(str(exc)) from exc
    except Exception as exc:                                    # noqa: BLE001
        raise ProviderUnavailable(f"no AI backend resolved: {exc}") from exc

    if backend is None:
        raise ProviderUnavailable("no AI backend configured")
    if not any(hasattr(backend, m) for m in ("generate", "complete", "chat", "ask")):
        # Refuse here rather than at generation time. A backend that cannot be
        # called is "no provider", and the caller turns that into a 503 on the
        # authoring pane — not a draft row recording a failure.
        raise ProviderUnavailable(
            f"the resolved backend ({type(backend).__name__}) exposes no "
            f"generation method")

    is_external = bool(getattr(backend, "is_external", False))
    return ResolvedProvider(
        backend=backend,
        provider_class="external" if is_external else "local",
        name=str(getattr(backend, "name", None)
                 or getattr(backend, "_model", None) or type(backend).__name__),
    )


async def resolve_governed(
    session: AsyncSession, *, tenant_slug: str,
) -> ResolvedProvider:
    """`resolve()`, with the engine asked whether this call is permitted.

    This is the clearest demonstration of the product: the policy engine governs
    its own use of a model. An EU tenant whose policy forbids external inference
    cannot have its policies drafted by an external model.
    """
    provider = await resolve(session, tenant_slug=tenant_slug)

    from .. import pep

    allowed = await pep.check(
        session,
        tenant_id=tenant_slug,
        action=pep.ACTION_AI_EXTERNAL,
        subject_id="hxarbiter-authoring",
        resource_kind="policy_draft",
        context={"provider_class": provider.provider_class,
                 "provider": provider.name,
                 "purpose": "policy_authoring"},
        legacy_allowed=True,
        claimed_by="hxarbiter.nlp",
    )
    if not allowed:
        raise ProviderUnavailable(
            "policy refuses this provider for policy authoring in this tenant")
    return provider
