"""HxArbiter — the deterministic evaluator.

This file is the product's central claim, so it is worth stating what it does
not do: **no I/O of any kind**. Every fact it needs has already been resolved
into the attribute map before it is called. That is what makes §5.1 true — the
same request against the same active policy set produces the same decision,
always — and it is what an auditor is really asking about when they say "prove
this rule was active on 3 March".

Two semantics from §5 live here and nowhere else:

* **deny-overrides (§5.2).** Any matching deny wins, whatever else matched.
* **Cross-scope (§5.3).** Platform policies (`tenant_id is None`) and the
  tenant's own policies are evaluated **together** in one set, so a tenant can
  tighten the platform baseline but can never loosen it. This is not enforced
  by ordering or by precedence rules — it falls out of deny-overrides applying
  across both scopes at once, which is why it cannot be got wrong later.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

from typing import Any, Iterable, Mapping

from .compiler import CompiledPolicy
from .schema import (
    AllOf,
    AnyOf,
    Comparison,
    Condition,
    Decision,
    Effect,
    MatchedRule,
    NotOf,
    Obligation,
    Op,
    PolicyMode,
    clamp,
)

#: Sentinel for "this attribute was not resolved". Distinct from None, which is
#: a legitimate resolved value meaning "resolved, and it is null".
MISSING = object()


def _domain_matches(host: Any, pattern: Any) -> bool:
    """Suffix match on label boundaries, which is the only safe way to do this.

    `example.com` matches `example.com` and `a.example.com`, and does NOT match
    `evil-example.com` or `example.com.attacker.net`. A plain `endswith` gets
    the first of those wrong, which is exactly the bypass the compiler refuses
    `contains` in order to prevent.
    """
    if not isinstance(host, str) or not isinstance(pattern, str):
        return False
    h = host.strip().rstrip(".").lower()
    p = pattern.strip().rstrip(".").lower()
    if not h or not p:
        return False
    return h == p or h.endswith("." + p)


def _as_set(value: Any) -> set | None:
    if isinstance(value, (list, tuple, set, frozenset)):
        return set(value)
    return None


class Evaluator:
    """Pure evaluation over a resolved attribute map."""

    def evaluate(
        self,
        *,
        policies: Iterable[CompiledPolicy],
        attrs: Mapping[str, Any],
        namespace_default: Effect,
        namespace_max_mode: PolicyMode,
    ) -> Decision:
        matched: list[MatchedRule] = []
        obligations: list[Obligation] = []
        denies: list[MatchedRule] = []
        allows: list[MatchedRule] = []
        #: The strongest mode among the versions that actually matched. A
        #: decision is only binding if something binding produced it — a shadow
        #: policy must never make a decision enforced just by being in the set.
        effective_mode = PolicyMode.SHADOW
        any_policy_seen = False

        for policy in policies:
            any_policy_seen = True
            mode = clamp(policy.mode, namespace_max_mode)
            for rule in policy.rules:
                if not self._matches(rule.condition, attrs):
                    continue
                m = MatchedRule(
                    policy_id=policy.policy_id,
                    version=policy.version,
                    rule_id=rule.rule_id,
                    effect=rule.effect,
                )
                matched.append(m)
                if rule.effect is Effect.DENY:
                    denies.append(m)
                else:
                    allows.append(m)
                    obligations.extend(rule.obligations)
                if _MODE_RANK[mode] > _MODE_RANK[effective_mode]:
                    effective_mode = mode

        if denies:
            # deny-overrides: the explanation lists only what caused the deny,
            # because a `matched` list mixing the deny with allows that lost is
            # read by humans and misleads every time.
            return Decision(
                effect=Effect.DENY,
                enforced=effective_mode is PolicyMode.ENFORCE,
                matched=denies,
                obligations=[],
                default_applied=False,
            )

        if allows:
            return Decision(
                effect=Effect.ALLOW,
                enforced=effective_mode is PolicyMode.ENFORCE,
                matched=allows,
                obligations=_dedupe(obligations),
                default_applied=False,
            )

        # Nothing matched — the namespace default applies. Its bindingness is
        # the namespace's clamp, not any policy's, since no policy spoke.
        _ = any_policy_seen
        return Decision(
            effect=namespace_default,
            enforced=namespace_max_mode is PolicyMode.ENFORCE,
            matched=[],
            obligations=[],
            default_applied=True,
        )

    # -- condition matching ---------------------------------------------------

    def _matches(self, cond: Condition, attrs: Mapping[str, Any]) -> bool:
        if isinstance(cond, AllOf):
            return all(self._matches(c, attrs) for c in cond.all)
        if isinstance(cond, AnyOf):
            return any(self._matches(c, attrs) for c in cond.any)
        if isinstance(cond, NotOf):
            return not self._matches(cond.not_, attrs)
        if isinstance(cond, Comparison):
            return self._compare(cond, attrs)
        return False

    def _compare(self, cmp_: Comparison, attrs: Mapping[str, Any]) -> bool:
        left = attrs.get(cmp_.attr, MISSING)
        op = cmp_.op

        if op is Op.IS_PRESENT:
            return left is not MISSING and left is not None
        if op is Op.IS_ABSENT:
            return left is MISSING or left is None

        # An unresolved attribute makes every other comparison false. It never
        # makes one true: an attribute nobody could resolve must not be able to
        # satisfy an allow rule by accident. A deny that depends on a missing
        # attribute simply does not fire, and the namespace default — which is
        # `deny` for every sovereignty-bearing namespace (§5.4) — is what
        # catches it.
        if left is MISSING:
            return False

        right = cmp_.value
        if isinstance(right, dict) and set(right) == {"attr"}:
            right = attrs.get(right["attr"], MISSING)
            if right is MISSING:
                return False

        try:
            return self._apply(op, left, right)
        except TypeError:
            # A comparison the compiler could not have caught (an attribute
            # whose resolved runtime type disagrees with its catalog type).
            # False, never an exception: one bad PIP value must not take down
            # every decision in the namespace.
            return False

    def _apply(self, op: Op, left: Any, right: Any) -> bool:
        if op is Op.EQ:
            return _as_set(left) == _as_set(right) if _as_set(left) is not None and _as_set(right) is not None else left == right
        if op is Op.NE:
            return not self._apply(Op.EQ, left, right)
        if op is Op.IN:
            return left in right if isinstance(right, (list, tuple, set, frozenset)) else False
        if op is Op.NOT_IN:
            return not self._apply(Op.IN, left, right)
        if op is Op.CONTAINS:
            s = _as_set(left)
            return right in s if s is not None else (isinstance(left, str) and isinstance(right, str) and right in left)
        if op is Op.NOT_CONTAINS:
            return not self._apply(Op.CONTAINS, left, right)
        if op is Op.INTERSECTS:
            s, r = _as_set(left), _as_set(right)
            return bool(s & r) if s is not None and r is not None else False
        if op is Op.IS_SUBSET_OF:
            s, r = _as_set(left), _as_set(right)
            return s <= r if s is not None and r is not None else False
        if op is Op.GT:
            return left > right
        if op is Op.GTE:
            return left >= right
        if op is Op.LT:
            return left < right
        if op is Op.LTE:
            return left <= right
        if op is Op.MATCHES_DOMAIN:
            if isinstance(right, (list, tuple, set, frozenset)):
                return any(_domain_matches(left, p) for p in right)
            return _domain_matches(left, right)
        return False


_MODE_RANK: dict[PolicyMode, int] = {
    PolicyMode.SHADOW: 0,
    PolicyMode.DRY_RUN: 1,
    PolicyMode.ENFORCE: 2,
}


def _dedupe(obligations: list[Obligation]) -> list[Obligation]:
    """Union of obligations from every matching allow (§5.2 step 3)."""
    seen: list[Obligation] = []
    keys: set[str] = set()
    for o in obligations:
        key = f"{o.kind}:{sorted(o.params.items())}"
        if key not in keys:
            keys.add(key)
            seen.append(o)
    return seen
