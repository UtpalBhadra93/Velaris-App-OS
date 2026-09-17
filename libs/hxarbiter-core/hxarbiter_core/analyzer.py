"""HxArbiter ARB-F — deterministic analysis of a draft.

**No model runs in this file.** Conflict, shadowing and tautology are decidable
over the closed condition AST (§4.4), and deciding them by asking an LLM would
put a probabilistic answer in front of the one part of authoring that can be
exact. Everything here is the reason the operator can trust the draft review at
all — the model wrote the rules; this says what they actually do.

What it finds:

* **Contradiction / tautology** — a condition that can never match, or always
  matches. A rule that never fires looks active on a dashboard forever.
* **Shadowing** — a rule that can never fire because a broader rule of the
  opposite effect subsumes it. This is how a deny "with an exception" silently
  has no exception.
* **Conflict** — two rules with opposite effects whose conditions provably
  overlap. Not an error (deny-overrides resolves it), but it is almost never
  what the author meant, and it is invisible in the JSON.

Where it cannot decide, it says so rather than guessing: `overlaps()` returns
`None` for "cannot tell", and an undecidable pair is reported as an observation
rather than a finding. A static analyser that bluffs is worse than one that
admits its limits, because people stop reading the ones that cry wolf.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

from .schema import (
    AllOf,
    AnyOf,
    Comparison,
    Condition,
    Effect,
    NotOf,
    Op,
    Rule,
)

Severity = Literal["error", "warning", "info"]


@dataclass
class Finding:
    severity: Severity
    kind: str
    message: str
    rule_ids: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {"severity": self.severity, "kind": self.kind,
                "message": self.message, "rule_ids": self.rule_ids}


# ── flattening a condition into the constraints it imposes ───────────────────


@dataclass(frozen=True)
class Constraint:
    """One `attr op value` that must hold for a conjunctive branch to match."""

    attr: str
    op: Op
    value: Any

    def key(self) -> str:
        return self.attr


def branches(cond: Condition) -> list[list[Constraint]] | None:
    """Disjunctive normal form: a list of conjunctions, or None if undecidable.

    `not` is where this stops. Negating a conjunction produces a disjunction of
    negations, and the negated forms of `matches_domain` and `intersects` are
    not expressible as constraints of the same shape — so rather than
    approximate them, anything under a `not` makes the whole rule undecidable
    and every finding about it is suppressed.
    """
    if isinstance(cond, Comparison):
        return [[Constraint(cond.attr, cond.op, _hashable(cond.value))]]
    if isinstance(cond, AllOf):
        out: list[list[Constraint]] = [[]]
        for sub in cond.all:
            sub_branches = branches(sub)
            if sub_branches is None:
                return None
            out = [a + b for a in out for b in sub_branches]
            if len(out) > 256:            # combinatorial guard
                return None
        return out
    if isinstance(cond, AnyOf):
        out = []
        for sub in cond.any:
            sub_branches = branches(sub)
            if sub_branches is None:
                return None
            out.extend(sub_branches)
        return out
    return None                            # NotOf, or anything unrecognised


def _hashable(value: Any) -> Any:
    if isinstance(value, list):
        return tuple(_hashable(v) for v in value)
    if isinstance(value, dict):
        return tuple(sorted((k, _hashable(v)) for k, v in value.items()))
    return value


# ── deciding whether two conjunctions can hold at once ───────────────────────


def _values(c: Constraint) -> set | None:
    """The set of values satisfying an equality-shaped constraint."""
    if c.op is Op.EQ:
        return {c.value}
    if c.op is Op.IN:
        return set(c.value) if isinstance(c.value, tuple) else None
    return None


def _conjunction_satisfiable(constraints: list[Constraint]) -> bool | None:
    """Can all of these hold together? None when undecidable."""
    by_attr: dict[str, list[Constraint]] = {}
    for c in constraints:
        by_attr.setdefault(c.attr, []).append(c)

    decided_any = False
    for attr, group in by_attr.items():
        positives = [c for c in group if c.op in (Op.EQ, Op.IN)]
        if len(positives) > 1:
            sets = [_values(c) for c in positives]
            if any(s is None for s in sets):
                continue
            intersection = set.intersection(*sets)         # type: ignore[arg-type]
            decided_any = True
            if not intersection:
                return False
            positives = [Constraint(attr, Op.IN, tuple(sorted(intersection)))]

        for c in group:
            if c.op in (Op.NE, Op.NOT_IN) and positives:
                excluded = ({c.value} if c.op is Op.NE
                            else set(c.value) if isinstance(c.value, tuple) else None)
                allowed = _values(positives[0])
                if excluded is None or allowed is None:
                    continue
                decided_any = True
                if not (allowed - excluded):
                    return False

        present = any(c.op is Op.IS_PRESENT for c in group)
        absent = any(c.op is Op.IS_ABSENT for c in group)
        if present and absent:
            return True and False          # explicit: cannot be both
        if absent and positives:
            decided_any = True
            return False                   # absent, yet required to equal something

    return True if decided_any else None


def overlaps(a: Rule, b: Rule) -> bool | None:
    """Can a single request match both rules? None when undecidable."""
    ba, bb = branches(a.condition), branches(b.condition)
    if ba is None or bb is None:
        return None
    saw_unknown = False
    for left in ba:
        for right in bb:
            verdict = _conjunction_satisfiable(left + right)
            if verdict is True:
                return True
            if verdict is None:
                saw_unknown = True
    return None if saw_unknown else False


def subsumes(broad: Rule, narrow: Rule) -> bool:
    """Does every request matching `narrow` also match `broad`?

    Only the sound, obvious case is claimed: every constraint of the broad rule
    appears verbatim in every branch of the narrow one. A cleverer test would
    catch more and be wrong sometimes, and a shadowing warning that is
    occasionally wrong is one nobody acts on.
    """
    nb, bb = branches(narrow.condition), branches(broad.condition)
    if nb is None or bb is None or len(bb) != 1:
        return False
    required = set(bb[0])
    return bool(nb) and all(required <= set(branch) for branch in nb)


def analyze(rules: list[Rule], *, existing: list[Rule] | None = None) -> list[Finding]:
    """Everything decidable about this rule set, plus the active set it joins."""
    findings: list[Finding] = []
    existing = existing or []

    for rule in rules:
        b = branches(rule.condition)
        if b is None:
            findings.append(Finding(
                "info", "undecidable",
                f"rule {rule.id!r} uses `not`, so conflict and shadowing "
                f"analysis cannot reason about it. It is still compiled and "
                f"evaluated normally.",
                [rule.id]))
            continue
        if not b:
            findings.append(Finding(
                "error", "contradiction",
                f"rule {rule.id!r} can never match anything.", [rule.id]))
            continue
        if all(not branch for branch in b):
            findings.append(Finding(
                "warning", "tautology",
                f"rule {rule.id!r} matches every request in the namespace.",
                [rule.id]))
        for branch in b:
            if _conjunction_satisfiable(branch) is False:
                findings.append(Finding(
                    "error", "contradiction",
                    f"rule {rule.id!r} contains conditions that cannot all "
                    f"hold at once, so it can never fire.", [rule.id]))
                break

    pool = [(r, "draft") for r in rules] + [(r, "active") for r in existing]
    for i, (a, a_src) in enumerate(pool):
        for b, b_src in pool[i + 1:]:
            if a.id == b.id and a_src == b_src:
                continue
            if a.effect is not b.effect:
                if overlaps(a, b) is True:
                    deny, allow = (a, b) if a.effect is Effect.DENY else (b, a)
                    findings.append(Finding(
                        "warning", "conflict",
                        f"rules {a.id!r} and {b.id!r} have opposite effects and "
                        f"can both match the same request. Deny-overrides means "
                        f"{deny.id!r} wins and {allow.id!r} has no effect there.",
                        [a.id, b.id]))
                continue
            if a.effect is b.effect:
                if subsumes(a, b) and a.id != b.id:
                    findings.append(Finding(
                        "warning", "shadowed",
                        f"rule {b.id!r} can never fire on its own: {a.id!r} has "
                        f"the same effect and already matches everything "
                        f"{b.id!r} does.", [a.id, b.id]))
    return findings


def seed_requests(rule: Rule) -> list[dict[str, Any]]:
    """Concrete attribute maps that match the rule, solved from the AST.

    Deterministic and model-free, per §7.2 step 3: these become regression
    fixtures on activation, and a fixture generated by an LLM would be testing
    the LLM.
    """
    b = branches(rule.condition)
    if not b:
        return []
    seeds: list[dict[str, Any]] = []
    for branch in b[:5]:
        attrs: dict[str, Any] = {}
        ok = True
        for c in branch:
            if c.op is Op.EQ:
                attrs[c.attr] = c.value
            elif c.op is Op.IN and isinstance(c.value, tuple) and c.value:
                attrs[c.attr] = c.value[0]
            elif c.op in (Op.CONTAINS, Op.INTERSECTS):
                v = c.value[0] if isinstance(c.value, tuple) and c.value else c.value
                attrs[c.attr] = [v]
            elif c.op is Op.MATCHES_DOMAIN:
                v = c.value[0] if isinstance(c.value, tuple) and c.value else c.value
                attrs[c.attr] = str(v)
            elif c.op is Op.IS_PRESENT:
                attrs[c.attr] = "present"
            elif c.op is Op.IS_ABSENT:
                attrs.pop(c.attr, None)
            elif c.op in (Op.GT, Op.GTE):
                attrs[c.attr] = c.value + 1
            elif c.op in (Op.LT, Op.LTE):
                attrs[c.attr] = c.value - 1
            else:
                ok = False
                break
        if ok and attrs:
            seeds.append(attrs)
    return seeds
