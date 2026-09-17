"""hxarbiter-core — a deterministic policy evaluator.

Policies are data, the condition language is a closed operator set, and
evaluation performs **no I/O of any kind**. Those three facts are the package:
everything a decision needs is resolved before `Evaluator.evaluate` is called,
so the same request against the same policy set produces the same decision,
always — which is what makes a decision log worth keeping.

The host platform supplies two things and nothing else:

* an **attribute catalog** (`AttributeCatalog`) naming every path a condition may
  reference, with its type; and
* resolved **attribute values** at decision time.

`hxarbiter_core` is the Apache-2.0 half of Velaris' HxArbiter. The enforcement
points, the store, the decision chain and the authoring UI are not here.

    from hxarbiter_core import (
        AttrSpec, AttrType, AttributeCatalog, PolicyCompiler, Evaluator,
        Effect, PolicyMode, PolicyVersion, Rule,
    )

    catalog = AttributeCatalog([
        AttrSpec(path="tenant.region", type=AttrType.STRING),
        AttrSpec(path="context.provider_class", type=AttrType.STRING,
                 allowed=("local", "external")),
    ])
    version  = PolicyVersion(policy_id=..., version=1, rules=[...])
    compiled = PolicyCompiler(catalog).compile_version(
        policy_id=..., tenant_id=None, namespace="ai",
        version=version, mode=PolicyMode.ENFORCE)

    decision = Evaluator().evaluate(
        policies=[compiled],
        attrs={"tenant.region": "eu-west", "context.provider_class": "external"},
        namespace_default=Effect.DENY,
        namespace_max_mode=PolicyMode.ENFORCE)

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from .analyzer import Finding, analyze, overlaps, seed_requests, subsumes
from .catalog import AttributeCatalog, AttrSpec, AttrType
from .compiler import (
    CompiledPolicy, CompiledRule, CompileError, CompileReport, PolicyCompiler,
)
from .evaluator import Evaluator
from .protocols import AttributeResolver, RequestResolver, StaticResolver
from .schema import (
    AllOf, AnyOf, AttrRef, Comparison, Condition, Decision, DecisionRequest, Effect,
    MatchedRule, NamespaceSpec, NotOf, Obligation, ObligationKind, Op, Policy,
    PolicyMode, PolicyStatus, PolicyVersion, ResourceRef, Rule, SubjectKind,
    SubjectRef, clamp,
)

__version__ = "0.1.0"

__all__ = [
    "AllOf", "AnyOf", "AttrRef", "AttrSpec", "AttrType", "AttributeCatalog",
    "AttributeResolver", "Comparison", "CompileError", "CompileReport",
    "CompiledPolicy", "CompiledRule", "Condition", "Decision",
    "DecisionRequest", "Effect", "Evaluator", "Finding", "MatchedRule",
    "NamespaceSpec", "NotOf", "Obligation", "ObligationKind", "Op", "Policy",
    "PolicyCompiler", "PolicyMode", "PolicyStatus", "PolicyVersion",
    "RequestResolver", "ResourceRef", "Rule", "StaticResolver", "SubjectKind",
    "SubjectRef", "analyze", "clamp", "overlaps", "seed_requests", "subsumes",
    "__version__",
]
