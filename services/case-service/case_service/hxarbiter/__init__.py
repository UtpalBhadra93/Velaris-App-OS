"""HxArbiter — sovereignty & security policy engine.

One policy model, one deterministic evaluator, one anchored decision log. The
controls Velaris already has become enforcement points of this engine rather
than seven separate answers to the same question (design `docs/Future/
Done/HXARBITER-DESIGN.md`).

**Everything is clamped to `shadow` in ARB-A.** The engine computes and logs; it
changes no answer anybody receives. That is not a phase-one shortcut, it is how
this is safe to ship to an install that is already running: a namespace is
promoted only after its parity has been proven against the legacy control it is
replacing.

**The evaluator lives in `hxarbiter-core`** (`libs/hxarbiter-core`, Apache-2.0),
not here. It is re-exported below so every existing import keeps working, and so
there is exactly ONE copy of the code — a second copy in this tree would drift
from the published package, and the drift would be invisible until somebody
compared two decisions.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

from hxarbiter_core import (
    AttributeCatalog,
    AttributeResolver,
    AttrSpec,
    AttrType,
    CompiledPolicy,
    CompileError,
    CompileReport,
    Decision,
    DecisionRequest,
    Effect,
    Evaluator,
    MatchedRule,
    NamespaceSpec,
    Obligation,
    ObligationKind,
    Op,
    Policy,
    PolicyCompiler,
    PolicyMode,
    PolicyStatus,
    PolicyVersion,
    RequestResolver,
    ResourceRef,
    Rule,
    StaticResolver,
    SubjectKind,
    SubjectRef,
    clamp,
)

from .velaris_catalog import VELARIS_CATALOG

# Registers the centrally dischargeable obligation handlers (§4.5). Imported
# for its side effect and at package level on purpose: an obligation with no
# handler DENIES, so which handlers exist must not depend on which module a
# particular service happened to import first.
from . import obligations as _obligations   # noqa: F401

__all__ = [
    "AttrSpec", "AttrType", "AttributeCatalog", "AttributeResolver",
    "VELARIS_CATALOG",
    "CompileError", "CompileReport", "CompiledPolicy", "PolicyCompiler",
    "Evaluator", "RequestResolver", "StaticResolver",
    "Decision", "DecisionRequest", "Effect", "MatchedRule", "NamespaceSpec",
    "Obligation", "ObligationKind", "Op", "Policy", "PolicyMode",
    "PolicyStatus", "PolicyVersion", "ResourceRef", "Rule", "SubjectKind",
    "SubjectRef", "clamp",
]
