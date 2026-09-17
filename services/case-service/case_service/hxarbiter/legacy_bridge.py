"""HxArbiter ARB-B — mirroring the controls this engine will replace.

**The engine does not take over. It is checked against what already works.**

Velaris already refuses external AI for a tenant on `local_only`, and already
holds a marketplace connector to its manifest's `outbound_domains`. Those
controls stay authoritative. This module generates a policy that says the same
thing, so both answers can be computed for the same request and compared — which
is the only honest basis for ever promoting a namespace to enforce (§8).

Three properties make that safe, and each is easy to lose:

* **A mirror policy is generated, never authored.** It carries
  `origin="legacy_bridge"`, is rebuilt from the live legacy configuration, and
  is not editable until the namespace is promoted. An operator "fixing" a mirror
  would be changing the oracle rather than the system under test.
* **Regeneration is idempotent and content-addressed.** The signature of the
  rules decides whether a new version is appended at all, so a bridge that runs
  every boot does not grow a version per boot.
* **A mirror is only ever as good as the facts it is given.** Where the legacy
  control reads something the PIPs cannot supply, the bridge refuses to mirror
  it rather than emitting a policy that quietly says something weaker. A mirror
  that is subtly more permissive than the control it shadows would report
  agreement and hide a hole.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import logging
import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from velaris_shared.db.models import (
    ArbiterNamespaceModel, ArbiterPolicyModel, ArbiterPolicyVersionModel)

from . import store
from .velaris_catalog import VELARIS_CATALOG
from hxarbiter_core import PolicyCompiler
from hxarbiter_core import Effect, PolicyMode, PolicyStatus, PolicyVersion, Rule

log = logging.getLogger(__name__)

#: The generated policy names. Stable, because regeneration finds them by name.
AI_LOCAL_ONLY = "legacy: AI egress (local_only)"
EGRESS_OUTBOUND_DOMAINS = "legacy: connector egress (outbound_domains)"

BRIDGE_ORIGIN = "legacy_bridge"


def ai_local_only_rules(*, local_only: bool) -> list[Rule]:
    """`local_only=true` ⇒ an external provider is refused.

    Expressed as a deny on `provider_class == "external"` rather than an allow on
    `"local"`, because the two are not the same under deny-overrides: an allow
    would be silently defeated by any other deny, and a mirror that can be
    overridden is not a mirror. When `local_only` is off the legacy control
    permits both, and the mirror must say exactly that — a *permitted* mirror is
    still a policy, not an absence of one, or the namespace default (deny) would
    make the engine look stricter than the system it is shadowing.
    """
    if local_only:
        return [
            Rule.model_validate({
                "id": "local_only",
                "description": "Tenant is on local_only: external inference is refused.",
                "effect": "deny",
                "condition": {"attr": "context.provider_class",
                              "op": "eq", "value": "external"},
            }),
            # And LOCAL inference is permitted — which the legacy control does
            # and a lone deny does not. The `ai` namespace defaults to deny, so
            # without this rule a local call falls through to that default and
            # the mirror is STRICTER than the thing it is shadowing. Parity
            # would then report `engine_stricter` forever and the namespace
            # could never be promoted — the mirror would have quietly made
            # itself unusable. Found on the live box, not by the suite.
            Rule.model_validate({
                "id": "local_permitted",
                "description": "Local inference stays permitted under local_only.",
                "effect": "allow",
                "condition": {"attr": "context.provider_class",
                              "op": "eq", "value": "local"},
            }),
        ]
    return [Rule.model_validate({
        "id": "external_allowed",
        "description": "Tenant opted into external inference (egress=external_allowed).",
        "effect": "allow",
        "condition": {"attr": "context.provider_class",
                      "op": "in", "value": ["local", "external"]},
    })]


def outbound_domain_rules(domains: list[str]) -> list[Rule]:
    """A connector may reach the hosts its manifest declared, and no others.

    `matches_domain`, never `contains` — the compiler would refuse `contains`
    here anyway, which is the point of having made it unrepresentable.

    An **empty** list mirrors the marketplace's own Layer-2 default of
    egress-DROP: no rule is emitted, so nothing matches and the namespace default
    (deny) applies. That is the legacy behaviour exactly, and emitting an
    allow-nothing rule instead would only obscure it.
    """
    if not domains:
        return []
    return [Rule.model_validate({
        "id": "declared_domains",
        "description": "Hosts declared in the package manifest.",
        "effect": "allow",
        "condition": {"attr": "context.target_domain",
                      "op": "matches_domain", "value": sorted(set(domains))},
    })]


async def _upsert_mirror(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID | None,
    name: str,
    namespace: str,
    rules: list[Rule],
    description: str,
) -> tuple[uuid.UUID | None, bool]:
    """Create or update a generated mirror. Returns (policy_id, changed).

    **An empty rule set is a legitimate mirror, not a failure.** It is what
    `outbound_domains` produces for a tenant with no installed packages — the
    ordinary state of every new install — and it means "nothing is permitted",
    which the namespace default (deny) already says exactly. So no policy is
    written, and an existing mirror is retired rather than left behind claiming
    a permission that no longer exists.

    This was a live 500: `outbound_domain_rules([])` correctly returned no
    rules, `add_version` correctly refused a version with no rules, and each was
    tested on its own. Composed, the commonest tenant on the box could not
    regenerate its mirrors at all.

    Content-addressed: if the rules hash to the version already active, nothing
    is written. A bridge that runs on every boot must not append a version per
    boot — the activation history is an audit record, and filling it with
    identical entries destroys its usefulness.
    """
    key = str(tenant_id) if tenant_id else store.PLATFORM_KEY
    policy = (await session.execute(
        select(ArbiterPolicyModel).where(
            ArbiterPolicyModel.tenant_key == key,
            ArbiterPolicyModel.name == name)
    )).scalars().first()

    if not rules:
        if policy is None:
            return None, False
        if policy.status == str(PolicyStatus.RETIRED):
            return policy.id, False
        await store.retire_policy(session, policy_id=policy.id)
        return policy.id, True

    if policy is None:
        policy = await store.create_policy(
            session, tenant_id=tenant_id, name=name, namespace=namespace,
            description=description, origin=BRIDGE_ORIGIN,
        )
    elif policy.origin != BRIDGE_ORIGIN:
        # Somebody made a hand-written policy with this name. Refuse rather than
        # overwrite it: the bridge owns generated rows, never authored ones.
        raise ValueError(
            f"policy {name!r} exists and is not a legacy bridge; refusing to "
            f"overwrite an authored policy")

    # A mirror TRACKS ITS NAMESPACE rather than carrying its own rollout mode.
    #
    # The ceiling can only clamp a version DOWN (§5.5), so a mirror pinned at
    # `shadow` would stay inert no matter what an operator promoted — and
    # promoting the namespace is the single action §8 step 4 describes. Making
    # the operator also re-issue every mirror would be a footgun with no upside:
    # a mirror is not independently tuned, it exists to reproduce the legacy
    # control at whatever bindingness the namespace is at.
    mode = PolicyMode.SHADOW
    ns_row = (await session.execute(
        select(ArbiterNamespaceModel)
        .where(ArbiterNamespaceModel.namespace == namespace)
    )).scalars().first()
    if ns_row is not None:
        mode = PolicyMode(ns_row.max_mode)

    wanted = PolicyVersion(policy_id=policy.id, version=1, rules=rules)
    if policy.active_version is not None:
        current = (await session.execute(
            select(ArbiterPolicyVersionModel).where(
                ArbiterPolicyVersionModel.policy_id == policy.id,
                ArbiterPolicyVersionModel.version == policy.active_version)
        )).scalars().first()
        # Mode is compared as well as the signature: the signature covers the
        # RULES only, so a namespace promoted since the last sync would
        # otherwise leave the mirror sitting at its old mode and silently inert.
        if (current is not None
                and current.signature == wanted.signature()
                and current.mode == str(mode)):
            return policy.id, False

    row = await store.add_version(
        session, policy_id=policy.id, rules=rules,
        compiler=PolicyCompiler(VELARIS_CATALOG), mode=mode,
    )
    await store.activate_version(session, policy_id=policy.id, version=row.version)
    return policy.id, True


async def mirror_ai_egress(
    session: AsyncSession, *, tenant_id: uuid.UUID, local_only: bool,
) -> tuple[uuid.UUID | None, bool]:
    """Mirror one tenant's `egress: local_only | external_allowed`."""
    return await _upsert_mirror(
        session,
        tenant_id=tenant_id,
        name=AI_LOCAL_ONLY,
        namespace="ai",
        rules=ai_local_only_rules(local_only=local_only),
        description=(
            "Generated from the tenant's HxNexus AI egress setting. Read-only "
            "until the `ai` namespace is promoted out of shadow."),
    )


async def mirror_connector_egress(
    session: AsyncSession, *, tenant_id: uuid.UUID, domains: list[str],
) -> tuple[uuid.UUID | None, bool]:
    """Mirror the union of installed packages' declared outbound domains."""
    return await _upsert_mirror(
        session,
        tenant_id=tenant_id,
        name=EGRESS_OUTBOUND_DOMAINS,
        namespace="egress",
        rules=outbound_domain_rules(domains),
        description=(
            "Generated from installed package manifests' outbound_domains. "
            "Read-only until the `egress` namespace is promoted out of shadow."),
    )


def is_bridge_managed(policy: ArbiterPolicyModel, *, namespace_promoted: bool) -> bool:
    """Is this row still the bridge's to own?

    After promotion a mirror becomes an ordinary editable policy (§8 step 4) —
    the engine is authoritative by then, and freezing the row would leave the
    operator unable to change the rule that now decides.
    """
    return policy.origin == BRIDGE_ORIGIN and not namespace_promoted
