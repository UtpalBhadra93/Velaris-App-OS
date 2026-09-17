"""HxArbiter ARB-F — saying what the policy actually does.

§7.1 P4, and the reason this is a separate file: **readback never sees the
original intent.** It is given the compiled rules and nothing else, so it
describes what the policy *does* rather than paraphrasing what the author
*meant*. A readback generated with the intent in context will agree with the
intent — including when the rules do not — which turns the one review step that
could catch a mistranslation into a rubber stamp.

The human approves the **readback**, and that exact text is stored in
`nl_readback`. It is what was signed off on, and it is what an auditor is shown
next to the rules.

A deterministic fallback exists for the same reason the whole engine has one: an
install with no model still needs a plain-language description beside a policy,
and "AI unavailable" is not an acceptable answer at the moment somebody is being
asked to approve a compliance control.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import json
import logging
from typing import Any

from hxarbiter_core import (
    AllOf,
    AnyOf,
    Comparison,
    Condition,
    Effect,
    NotOf,
    Op,
    Rule,
)

log = logging.getLogger(__name__)

READBACK_PROMPT = """\
Describe, in plain English, exactly what these policy rules do.

You are NOT told what the author intended, and you must not guess at it. Describe
only the mechanics of the rules as given.

Rules to describe:
1. State what is denied, and under precisely which conditions.
2. State what is allowed, and under precisely which conditions.
3. State what happens when no rule matches (the namespace default is: %(default)s).
4. If a rule can never fire, or is made redundant by another, say so plainly.

Write short sentences. No preamble, no bullet characters, no markdown.

RULES (JSON):
%(rules)s
"""

_OP_WORDS = {
    Op.EQ: "is", Op.NE: "is not",
    Op.IN: "is one of", Op.NOT_IN: "is none of",
    Op.CONTAINS: "includes", Op.NOT_CONTAINS: "does not include",
    Op.INTERSECTS: "shares any value with",
    Op.IS_SUBSET_OF: "is entirely within",
    Op.GT: "is greater than", Op.GTE: "is at least",
    Op.LT: "is less than", Op.LTE: "is at most",
    Op.MATCHES_DOMAIN: "is that host or a subdomain of",
    Op.IS_PRESENT: "is present", Op.IS_ABSENT: "is absent",
}


def describe_condition(cond: Condition) -> str:
    """Deterministic English for a condition. No model involved."""
    if isinstance(cond, AllOf):
        return " and ".join(f"({describe_condition(c)})" for c in cond.all)
    if isinstance(cond, AnyOf):
        return " or ".join(f"({describe_condition(c)})" for c in cond.any)
    if isinstance(cond, NotOf):
        return f"it is not the case that ({describe_condition(cond.not_)})"
    if isinstance(cond, Comparison):
        words = _OP_WORDS.get(cond.op, str(cond.op))
        if cond.op in (Op.IS_PRESENT, Op.IS_ABSENT):
            return f"{cond.attr} {words}"
        value = cond.value
        if isinstance(value, dict) and set(value) == {"attr"}:
            return f"{cond.attr} {words} {value['attr']}"
        if isinstance(value, list):
            return f"{cond.attr} {words} {', '.join(str(v) for v in value)}"
        return f"{cond.attr} {words} {value}"
    return "an unrecognised condition"


def deterministic_readback(rules: list[Rule], *, default_effect: str) -> str:
    """A truthful description with no model. Used as a fallback, and as a check.

    It is deliberately plainer than what a model produces. Plain and correct
    beats fluent and approximate for text somebody signs.
    """
    lines: list[str] = []
    for rule in rules:
        verb = "REFUSED" if rule.effect is Effect.DENY else "ALLOWED"
        lines.append(
            f"{rule.id}: the request is {verb} when "
            f"{describe_condition(rule.condition)}.")
        for ob in rule.obligations:
            lines.append(f"    ...and only if {ob.kind} is carried out.")
    lines.append(
        f"When no rule above matches, the request is "
        f"{'REFUSED' if default_effect == 'deny' else 'ALLOWED'} "
        f"by the namespace default.")
    return "\n".join(lines)


async def readback(
    backend: Any | None,
    rules: list[Rule],
    *,
    default_effect: str,
) -> tuple[str, str]:
    """Returns (text, source) where source is "model" or "deterministic".

    The source is recorded rather than hidden: an approver should know whether
    the sentence they are signing was written by a model or derived from the
    rules, and those are different kinds of evidence.
    """
    plain = deterministic_readback(rules, default_effect=default_effect)
    if backend is None:
        return plain, "deterministic"

    payload = json.dumps(
        [r.model_dump(mode="json", by_alias=True) for r in rules],
        indent=2, sort_keys=True)
    prompt = READBACK_PROMPT % {"default": default_effect, "rules": payload}
    try:
        from .author import call_model
        # The readback prompt is the WHOLE prompt. Nothing from the authoring
        # template reaches the model here — not the intent, and not even the
        # word "INTENT", which is why this calls `call_model` rather than the
        # assembler `draft_rules` uses.
        text = await call_model(backend, prompt)
        text = (text or "").strip()
        if not text:
            return plain, "deterministic"
        return text, "model"
    except Exception as exc:                                   # noqa: BLE001
        log.warning("hxarbiter: readback generation failed, using the "
                    "deterministic description: %s", exc)
        return plain, "deterministic"
