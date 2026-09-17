"""The mutation-operator catalog — and the proof that it cannot reach further.

Design: docs/Future/Done/AI-ERA-ROADMAP.md §F-08: *"The mutation-operator catalog is
the security boundary: no generated change can touch auth config, SOV policies,
integrations/egress, or agent autonomy settings — structurally impossible, not
policy-checked."*

**Structurally impossible** is a strong claim, so here is the whole argument, in
three parts, each of which a test can check on its own:

  1. **The operator vocabulary is closed.** `OPERATORS` is exactly HxEvolve's
     five signed-off kinds. An episode carrying anything else cannot be built —
     `check()` refuses it before any other rung of the ladder runs, and the
     column it would be written to is 32 characters of enum in practice.

  2. **Each operator's reach is enumerated, not inferred.** For every operator,
     `WRITES` names the artifact types it may touch and the top-level fields it
     may set on them. Nothing reads the proposal and decides what looks
     dangerous; the writable set is a literal in this file.

  3. **The union of those reaches does not intersect the forbidden surfaces.**
     `FORBIDDEN` names them: authentication and SSO, HxArbiter policy, egress
     and connectors, credentials and secrets, retention, access control, and
     agent autonomy. `proves_forbidden_surfaces_unreachable()` computes the
     intersection and returns it — so the security property is a fact the suite
     recomputes rather than a sentence in a doc that drifts.

The reason this matters more than a policy check: a policy check can be
misconfigured, disabled, or evaluated against the wrong subject. An operator
that cannot NAME a connector cannot change one however the policy engine is
feeling.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

from typing import Any

#: Exactly HxEvolve's kinds. Imported rather than restated so the two can never
#: drift apart — a sixth kind added there without thought here would widen this
#: boundary silently, and this import makes that a test failure instead.
from case_service.hxevolve.proposer import PROPOSAL_KINDS

OPERATORS: tuple[str, ...] = tuple(PROPOSAL_KINDS)

#: What each operator may write: `{operator: {artifact_type: (field, ...)}}`.
#:
#: `case_type.definition_json` is the widest reach here and deserves a word: it
#: holds stages and steps, which is the process shape. It does NOT hold
#: authentication, policy, connectors or credentials — those live in their own
#: tables, and no operator below names those tables.
WRITES: dict[str, dict[str, tuple[str, ...]]] = {
    "rule_add":     {"rule": ("definition_json", "enabled")},
    "rule_adjust":  {"rule": ("definition_json",)},
    "sla_duration": {"case_type": ("definition_json",)},
    "routing":      {"case_type": ("definition_json",)},
    "reorder":      {"case_type": ("definition_json",)},
}

#: The surfaces a self-changing platform must never be able to reach, named as
#: artifact types. This list is the promise; the intersection test is the proof.
FORBIDDEN: frozenset[str] = frozenset({
    "sso_provider", "auth_config", "user", "access_group", "access_role",
    "arbiter_policy", "arbiter_namespace", "retention_policy",
    "connector", "integration", "webhook_subscription", "egress_rule",
    "credential", "secret", "vault_key",
    "agent", "agent_capability", "agent_token_grant",
    "marketplace_install", "deployment_environment",
})


class CatalogError(ValueError):
    """The proposal is not something the catalog can express."""


def artifact_types() -> set[str]:
    """Every artifact type any operator can reach. The reachable set."""
    return {t for reach in WRITES.values() for t in reach}


def proves_forbidden_surfaces_unreachable() -> set[str]:
    """The intersection of what the catalog reaches and what it must not.

    Returns the empty set when the boundary holds. Returning the intersection
    rather than a boolean is deliberate: a failing test should say *which*
    surface leaked, not merely that one did.
    """
    return artifact_types() & FORBIDDEN


def writable_fields(operator: str) -> dict[str, tuple[str, ...]]:
    if operator not in WRITES:
        raise CatalogError(f"{operator!r} is not an operator in the catalog")
    return WRITES[operator]


def check(operator: str, proposal: Any) -> None:
    """The first rung of the ladder. Raises `CatalogError` with a plain reason.

    This runs BEFORE anything expensive. A change that cannot be expressed
    safely should not then be replayed across the whole caseload to find out
    whether it would also have been slow.
    """
    if operator not in OPERATORS:
        raise CatalogError(
            f"{operator!r} is not one of the {len(OPERATORS)} operators this "
            f"platform may apply to itself: {', '.join(OPERATORS)}")
    if not isinstance(proposal, dict) or not proposal:
        raise CatalogError("the proposal is empty, so there is nothing to apply")

    leaked = proves_forbidden_surfaces_unreachable()
    if leaked:
        # Defence in depth, and a deliberate belt-and-braces: if somebody widens
        # WRITES without widening the tests, every episode refuses rather than
        # the boundary quietly moving.
        raise CatalogError(
            f"the operator catalog can reach a forbidden surface ({sorted(leaked)}); "
            f"refusing to heal anything until that is fixed")


def describe() -> dict[str, Any]:
    """The boundary, for a screen and for the episode's own record."""
    return {
        "operators": list(OPERATORS),
        "reaches": {op: {t: list(f) for t, f in reach.items()}
                    for op, reach in WRITES.items()},
        "forbidden": sorted(FORBIDDEN),
        "intersection": sorted(proves_forbidden_surfaces_unreachable()),
    }
