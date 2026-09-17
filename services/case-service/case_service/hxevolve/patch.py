"""Turn one HxEvolve proposal into the definition it would produce — once.

This logic existed inside the `/hxevolve/insights/{id}/stage` route, where it
was the only caller. F-08 needs exactly the same answer — a healing episode has
to rehearse and then apply the same shape a staged insight would — and a second
copy of "what does this proposal actually change" is precisely the kind of
duplicate that ends up disagreeing with the first one after a year.

So it lives here, and both callers use it. The route's behaviour is unchanged:
same validators, same order, same patched shapes.

Each proposal kind has its own shape, decided by the builders in
`hxevolve.proposer`, and none of them is "the new definition":

  · `rule_add` / `rule_adjust` — a normalized RULE draft. It changes no
    definition at all, which is why this returns `None` for them.
  · `sla_duration` — `{"policy": …, "replaces_policy_id": …}`; the patch swaps
    that one policy out of `sla_policies`.
  · `routing` — a normalized routing draft; the patch is
    `routing_builder.patch_step_assignment`, a surgical replacement of one
    step's assignment.
  · `reorder` — carries the whole proposed definition under `definition_json`.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

from typing import Any

from case_service.hxevolve import gates
from case_service.nlp import routing_builder, rule_builder, sla_builder

#: The kinds that change `case_type.definition_json`. The other two change a
#: rule row and leave the definition alone.
DEFINITION_KINDS = ("sla_duration", "routing", "reorder")


def validate_and_patch(
    definition: dict[str, Any], kind: str, proposal: dict[str, Any],
) -> tuple[dict[str, Any] | None, list[str]]:
    """`(patched_definition_or_None, errors)`, re-validated against `definition`.

    `definition` is the case type AS IT STANDS NOW, not as it stood when the
    proposal was mined — the whole point of re-validating is that the world
    moved.
    """
    definition = definition or {}
    proposal = proposal or {}

    if kind in ("rule_add", "rule_adjust"):
        return None, list(rule_builder.validate_rule_draft(proposal) or [])

    if kind == "sla_duration":
        clean = proposal.get("policy") or {}
        pid = proposal.get("replaces_policy_id")
        policies = [p for p in definition.get("sla_policies", []) if isinstance(p, dict)]
        if not any(p.get("id") == pid for p in policies):
            return None, [f"SLA policy {pid!r} no longer exists on this case type"]
        remainder = [p for p in policies if p.get("id") != pid]
        errors = list(sla_builder.validate_sla_draft(
            {"policy": clean}, {**definition, "sla_policies": remainder}) or [])
        patched = {**definition, "sla_policies": [*remainder, clean]}
        return (None if errors else patched), errors

    if kind == "routing":
        errors = list(routing_builder.validate_routing_draft(proposal, definition) or [])
        patched = None if errors else routing_builder.patch_step_assignment(
            definition, proposal)
        return patched, errors

    if kind == "reorder":
        patched = proposal.get("definition_json")
        errors = list(gates.validate_reorder(definition, patched) or [])
        return (None if errors else patched), errors

    return None, [f"Unknown proposal kind {kind!r}"]
