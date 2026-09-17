"""HxArbiter — policy JSON to a compiled evaluation tree.

The compiler is where a policy stops being text and becomes something the
evaluator can run without thinking. Everything it refuses, it refuses **at
activation time**, which is the only moment a human is present to be told.

What it enforces (§4.4):

* Unknown attribute path, unknown operator, or a literal of the wrong type ⇒
  the version cannot be activated. A rule that never matches is indistinguishable
  from a rule that always passes, and both look fine on a dashboard.
* `matches_domain` is the **only** substring-ish operator allowed on a domain.
  `contains` on a hostname is a bypass class, not a style preference:
  `evil-example.com` contains `example.com`.
* Depth and node-count limits, so a condition cannot be made expensive.
* The set of attribute paths a policy actually reads is recorded, so the PDP can
  resolve **only those** PIPs, lazily and in parallel (§6).

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .catalog import AttributeCatalog, AttrType
from .schema import (
    AllOf,
    AnyOf,
    Comparison,
    Condition,
    Effect,
    LIST_VALUE_OPS,
    NotOf,
    ORDERING_OPS,
    Op,
    PolicyVersion,
    Rule,
    UNARY_OPS,
)

DEFAULT_MAX_DEPTH = 8
DEFAULT_MAX_NODES = 128

#: Operators that are meaningful on a domain-typed attribute. `contains` is
#: absent by design; see the module docstring.
_DOMAIN_OPS: frozenset[Op] = frozenset(
    {Op.MATCHES_DOMAIN, Op.EQ, Op.NE, Op.IN, Op.NOT_IN, Op.IS_PRESENT, Op.IS_ABSENT}
)

#: Operators that are meaningful on a set.
_SET_OPS: frozenset[Op] = frozenset(
    {Op.CONTAINS, Op.NOT_CONTAINS, Op.INTERSECTS, Op.IS_SUBSET_OF,
     Op.IS_PRESENT, Op.IS_ABSENT, Op.EQ, Op.NE}
)

_TYPE_OF_LITERAL = {
    AttrType.STRING: (str,),
    AttrType.DOMAIN: (str,),
    AttrType.NUMBER: (int, float),
    AttrType.BOOLEAN: (bool,),
}


class CompileError(Exception):
    """Raised for anything that must stop a version being activated."""


@dataclass(frozen=True)
class CompiledRule:
    rule_id: str
    effect: Effect
    condition: Condition
    obligations: tuple[Any, ...]


@dataclass
class CompiledPolicy:
    """One active policy version, ready to evaluate."""

    policy_id: Any
    tenant_id: str | None
    namespace: str
    version: int
    mode: Any
    rules: tuple[CompiledRule, ...]
    #: Every attribute path this version reads. The PDP resolves exactly this
    #: set and no more.
    required_attrs: frozenset[str] = field(default_factory=frozenset)


@dataclass
class CompileReport:
    """What the compiler found. Returned to Studio and to the NL authoring loop."""

    ok: bool
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    required_attrs: set[str] = field(default_factory=set)
    node_count: int = 0
    max_depth: int = 0


class PolicyCompiler:
    def __init__(
        self,
        catalog: AttributeCatalog,
        *,
        max_depth: int = DEFAULT_MAX_DEPTH,
        max_nodes: int = DEFAULT_MAX_NODES,
    ) -> None:
        self._catalog = catalog
        self._max_depth = max_depth
        self._max_nodes = max_nodes

    # -- public ---------------------------------------------------------------

    def check_version(self, version: PolicyVersion) -> CompileReport:
        """Validate every rule. Never raises; collects everything it finds.

        Collecting rather than raising is deliberate: an author fixing one typo
        at a time, told about one problem per attempt, is how a five-minute edit
        becomes an afternoon.
        """
        report = CompileReport(ok=True)
        if not version.rules:
            report.ok = False
            report.errors.append("a policy version must contain at least one rule")
            return report

        for rule in version.rules:
            try:
                self._check_rule(rule, report)
            except CompileError as exc:
                report.ok = False
                report.errors.append(f"rule {rule.id}: {exc}")
        return report

    def compile_version(
        self,
        *,
        policy_id: Any,
        tenant_id: str | None,
        namespace: str,
        version: PolicyVersion,
        mode: Any,
    ) -> CompiledPolicy:
        report = self.check_version(version)
        if not report.ok:
            raise CompileError("; ".join(report.errors))
        return CompiledPolicy(
            policy_id=policy_id,
            tenant_id=tenant_id,
            namespace=namespace,
            version=version.version,
            mode=mode,
            rules=tuple(
                CompiledRule(
                    rule_id=r.id,
                    effect=r.effect,
                    condition=r.condition,
                    obligations=tuple(r.obligations),
                )
                for r in version.rules
            ),
            required_attrs=frozenset(report.required_attrs),
        )

    # -- internals ------------------------------------------------------------

    def _check_rule(self, rule: Rule, report: CompileReport) -> None:
        state = {"nodes": 0}
        self._walk(rule.condition, depth=1, state=state, report=report)
        report.node_count += state["nodes"]

    def _walk(
        self, cond: Condition, *, depth: int, state: dict, report: CompileReport
    ) -> None:
        if depth > self._max_depth:
            raise CompileError(
                f"condition nests deeper than the limit of {self._max_depth}"
            )
        report.max_depth = max(report.max_depth, depth)

        state["nodes"] += 1
        if state["nodes"] > self._max_nodes:
            raise CompileError(
                f"condition has more than {self._max_nodes} nodes"
            )

        if isinstance(cond, AllOf):
            for c in cond.all:
                self._walk(c, depth=depth + 1, state=state, report=report)
            return
        if isinstance(cond, AnyOf):
            for c in cond.any:
                self._walk(c, depth=depth + 1, state=state, report=report)
            return
        if isinstance(cond, NotOf):
            self._walk(cond.not_, depth=depth + 1, state=state, report=report)
            return
        if isinstance(cond, Comparison):
            self._check_comparison(cond, report)
            return
        raise CompileError(f"unknown condition node: {type(cond).__name__}")

    def _check_comparison(self, cmp_: Comparison, report: CompileReport) -> None:
        spec = self._catalog.get(cmp_.attr)
        if spec is None:
            raise CompileError(
                f"unknown attribute {cmp_.attr!r} "
                f"(not in the registered catalog)"
            )
        report.required_attrs.add(cmp_.attr)

        op = cmp_.op

        # -- arity ------------------------------------------------------------
        if op in UNARY_OPS:
            if cmp_.value is not None:
                raise CompileError(f"{op} takes no value")
            return
        if cmp_.value is None:
            raise CompileError(f"{op} requires a value")

        # -- attribute-to-attribute comparison --------------------------------
        rhs = cmp_.value
        if isinstance(rhs, dict) and set(rhs) == {"attr"}:
            other = self._catalog.get(rhs["attr"])
            if other is None:
                raise CompileError(
                    f"unknown attribute {rhs['attr']!r} on the right-hand side"
                )
            report.required_attrs.add(rhs["attr"])
            if other.type is not spec.type:
                raise CompileError(
                    f"cannot compare {spec.type} attribute {cmp_.attr!r} "
                    f"with {other.type} attribute {rhs['attr']!r}"
                )
            return

        # -- operator/type agreement ------------------------------------------
        if spec.type is AttrType.DOMAIN and op not in _DOMAIN_OPS:
            raise CompileError(
                f"{op} is not allowed on the domain attribute {cmp_.attr!r}; "
                f"use matches_domain — substring matching on a hostname is a "
                f"bypass class"
            )
        if spec.type is AttrType.STRING_SET and op not in _SET_OPS:
            raise CompileError(
                f"{op} is not allowed on the set attribute {cmp_.attr!r}"
            )
        if op in ORDERING_OPS and spec.type is not AttrType.NUMBER:
            raise CompileError(
                f"{op} requires a numeric attribute; {cmp_.attr!r} is {spec.type}"
            )
        if op is Op.MATCHES_DOMAIN and spec.type is not AttrType.DOMAIN:
            raise CompileError(
                f"matches_domain requires a domain attribute; "
                f"{cmp_.attr!r} is {spec.type}"
            )

        # -- literal shape ----------------------------------------------------
        # `matches_domain` takes EITHER one host or a list of them, and a list
        # means "any of these" — which is what an allowlist is, and what the
        # legacy `outbound_domains` bridge generates. The evaluator has always
        # implemented both; the compiler accepted only the scalar, so the one
        # shape the product actually needs would not compile.
        if op is Op.MATCHES_DOMAIN and isinstance(rhs, list):
            if not rhs:
                raise CompileError("matches_domain was given an empty list")
            for item in rhs:
                self._check_literal(item, spec, cmp_.attr)
        elif op in LIST_VALUE_OPS:
            if not isinstance(rhs, list):
                raise CompileError(f"{op} requires a list value")
            if not rhs:
                # An empty list is almost always an authoring slip, and its
                # meaning differs per operator in ways nobody predicts. Refuse
                # it rather than let it read as "matches nothing" in one rule
                # and "matches everything" in the next.
                raise CompileError(f"{op} was given an empty list")
            for item in rhs:
                self._check_literal(item, spec, cmp_.attr)
        elif spec.type is AttrType.STRING_SET:
            # contains / not_contains take a single member; eq / ne take a list.
            if op in (Op.CONTAINS, Op.NOT_CONTAINS):
                self._check_literal(rhs, spec, cmp_.attr)
            elif not isinstance(rhs, list):
                raise CompileError(
                    f"{op} on the set attribute {cmp_.attr!r} requires a list"
                )
        else:
            self._check_literal(rhs, spec, cmp_.attr)

    def _check_literal(self, value: Any, spec, path: str) -> None:
        if spec.type is AttrType.STRING_SET:
            expected: tuple[type, ...] = (str,)
        else:
            expected = _TYPE_OF_LITERAL[spec.type]

        # bool is a subclass of int in Python; a boolean where a number belongs
        # is a mistake worth naming rather than silently coercing to 0/1.
        if spec.type is AttrType.NUMBER and isinstance(value, bool):
            raise CompileError(f"{path!r} is numeric; got a boolean")
        if not isinstance(value, expected):
            raise CompileError(
                f"{path!r} is {spec.type}; got {type(value).__name__} {value!r}"
            )
        if spec.allowed is not None and value not in spec.allowed:
            raise CompileError(
                f"{value!r} is not a permitted value for {path!r} "
                f"(allowed: {', '.join(spec.allowed)})"
            )
