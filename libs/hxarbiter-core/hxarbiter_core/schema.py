"""HxArbiter — the policy model, as data.

Everything a policy *is* lives here, and nothing here performs I/O. That is not
tidiness: the whole product claim is that the evaluator is deterministic and
inspectable, and an evaluator whose types can reach a database or a model
provider cannot be either. `schema.py`, `compiler.py` and `evaluator.py` are the
three files that leave this repository as the Apache-2.0 `hxarbiter-core`
package (design §18.2), so they import from the platform not at all.

Two design decisions from §4.1 are enforced by the types below rather than by
convention:

* **D1 — policies are data, not code.** A rule is validated JSON. There is no
  expression string, no `eval`, and no place for a caller to hand us Python.
* **D2 — the condition language is a closed AST.** Every operator this engine
  will ever evaluate is named in `Op`. An operator that is not in that enum
  fails compilation, which is what makes NL authoring safe (a model cannot emit
  an operator the compiler does not know) and conflict analysis tractable.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import json
import hashlib
import uuid
from datetime import datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


# ─── Enumerations ────────────────────────────────────────────────────────────


class Effect(StrEnum):
    ALLOW = "allow"
    DENY = "deny"


class PolicyMode(StrEnum):
    """Rollout mode, per policy version and clamped per namespace (§5.5).

    The ordering matters and is relied on by `clamp()`: a namespace held at
    `shadow` during bring-up must be able to hold every version in it down,
    however the version was authored.
    """

    SHADOW = "shadow"      # computed and logged; the PEP receives the legacy answer
    DRY_RUN = "dry_run"    # returned with enforced=False; the PEP proceeds anyway
    ENFORCE = "enforce"    # binding


_MODE_RANK: dict[PolicyMode, int] = {
    PolicyMode.SHADOW: 0,
    PolicyMode.DRY_RUN: 1,
    PolicyMode.ENFORCE: 2,
}


def clamp(mode: PolicyMode, ceiling: PolicyMode) -> PolicyMode:
    """The weaker of the two. A namespace ceiling can only ever hold a version DOWN.

    This is the mechanism that makes ARB-A safe to ship on a running install:
    every namespace is registered at `shadow`, so nothing this engine computes
    can change an answer until an operator raises the ceiling deliberately.
    """
    return mode if _MODE_RANK[mode] <= _MODE_RANK[ceiling] else ceiling


class PolicyStatus(StrEnum):
    DRAFT = "draft"
    ACTIVE = "active"
    RETIRED = "retired"


class SubjectKind(StrEnum):
    USER = "user"
    SERVICE = "service"
    APP = "app"
    WORKFLOW = "workflow"
    #: An AI agent acting as itself. Distinct from SERVICE on purpose: a
    #: service is part of the platform and its behaviour is fixed by code,
    #: while an agent's behaviour comes from a model, so "which agent, which
    #: model" is a question only asked of this kind. A policy that says
    #: `subject.kind == "agent"` must not accidentally catch a background job.
    AGENT = "agent"


class Op(StrEnum):
    """The closed operator set (§4.4). Adding one here is a deliberate act.

    `matches_domain` exists because `contains` on a hostname is a known bypass
    class — "evil-example.com" contains "example.com". The compiler refuses
    `contains` on any path that names a domain, so the safe operator is not
    merely available, it is the only one reachable.
    """

    EQ = "eq"
    NE = "ne"
    IN = "in"
    NOT_IN = "not_in"
    CONTAINS = "contains"
    NOT_CONTAINS = "not_contains"
    INTERSECTS = "intersects"
    GT = "gt"
    GTE = "gte"
    LT = "lt"
    LTE = "lte"
    MATCHES_DOMAIN = "matches_domain"
    IS_SUBSET_OF = "is_subset_of"
    IS_PRESENT = "is_present"
    IS_ABSENT = "is_absent"


#: Operators that take no `value` at all. Supplying one is a compile error
#: rather than a silently ignored field, because a rule that reads as though it
#: compares something and does not is the worst kind of wrong.
UNARY_OPS: frozenset[Op] = frozenset({Op.IS_PRESENT, Op.IS_ABSENT})

#: Operators whose right-hand side must be a list.
LIST_VALUE_OPS: frozenset[Op] = frozenset(
    {Op.IN, Op.NOT_IN, Op.INTERSECTS, Op.IS_SUBSET_OF}
)

#: Operators that only mean anything on an ordered scalar.
ORDERING_OPS: frozenset[Op] = frozenset({Op.GT, Op.GTE, Op.LT, Op.LTE})


class ObligationKind(StrEnum):
    """§4.5. A PEP that receives one it does not implement MUST deny.

    Fail-closed on unknown obligations is the reason this is an enum and not a
    free string: an obligation the platform has never heard of must be
    unrepresentable in a policy, not merely unhandled at the far end.
    """

    PSEUDONYMIZE = "pseudonymize"
    REQUIRE_STEP_UP = "require_step_up"
    RECORD_PURPOSE = "record_purpose"
    NOTIFY = "notify"
    WATERMARK_EXPORT = "watermark_export"


# ─── Condition AST ───────────────────────────────────────────────────────────


class AttrRef(BaseModel):
    """A reference to another attribute, used as a right-hand side.

    This is what makes a rule expressible without naming a jurisdiction:

        {"attr": "resource.region", "op": "ne",
         "value": {"attr": "tenant.region"}}

    reads as *"the data is not where the tenant is"* and works for any region
    scheme the host resolves — an ISO code, a cloud region, a business unit.
    The engine never interprets the value.

    Both sides must be in the catalog and must have the **same type**; the
    compiler refuses the comparison otherwise rather than letting it evaluate
    to a silent False for the life of the policy. It is written as the plain
    `{"attr": ...}` shape in policy JSON — this class exists so an integrator
    building conditions in Python has a name for it.
    """

    model_config = ConfigDict(extra="forbid")
    attr: str


class Comparison(BaseModel):
    model_config = ConfigDict(extra="forbid")

    attr: str
    op: Op
    #: A literal, or an AttrRef to compare two attributes with each other.
    #: Absent for the unary operators.
    value: Any = None


class AllOf(BaseModel):
    model_config = ConfigDict(extra="forbid")
    all: list["Condition"] = Field(min_length=1)


class AnyOf(BaseModel):
    model_config = ConfigDict(extra="forbid")
    any: list["Condition"] = Field(min_length=1)


class NotOf(BaseModel):
    model_config = ConfigDict(extra="forbid")
    not_: "Condition" = Field(alias="not")


Condition = AllOf | AnyOf | NotOf | Comparison

AllOf.model_rebuild()
AnyOf.model_rebuild()
NotOf.model_rebuild()


# ─── Policy entities ─────────────────────────────────────────────────────────


class Obligation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: ObligationKind
    params: dict[str, Any] = Field(default_factory=dict)


class Rule(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, max_length=64)
    description: str = ""
    effect: Effect
    condition: Condition
    obligations: list[Obligation] = Field(default_factory=list)

    @field_validator("obligations")
    @classmethod
    def _deny_carries_no_obligations(
        cls, v: list[Obligation], info
    ) -> list[Obligation]:
        # An obligation is an instruction attached to an *allow* — "you may, if
        # you also do this". Hanging one off a deny is meaningless and reads as
        # though the deny were conditional, so it is refused rather than ignored.
        if v and info.data.get("effect") == Effect.DENY:
            raise ValueError("a deny rule cannot carry obligations")
        return v


class PolicyVersion(BaseModel):
    model_config = ConfigDict(extra="forbid")

    policy_id: uuid.UUID
    version: int = Field(ge=1)
    rules: list[Rule]
    mode: PolicyMode = PolicyMode.SHADOW
    created_by: uuid.UUID | None = None
    approved_by: uuid.UUID | None = None
    nl_source: str | None = None
    nl_readback: str | None = None

    @field_validator("rules")
    @classmethod
    def _unique_rule_ids(cls, v: list[Rule]) -> list[Rule]:
        seen = {r.id for r in v}
        if len(seen) != len(v):
            raise ValueError("rule ids must be unique within a version")
        return v

    def canonical_json(self) -> str:
        """The exact bytes that get signed.

        Sorted keys and no whitespace, so the same policy signs to the same
        digest on any machine, in any Python version, in any dict order. The
        signature is worthless otherwise: a version whose digest depends on
        insertion order cannot prove it was the version that ran on 3 March.
        """
        payload = {
            "policy_id": str(self.policy_id),
            "version": self.version,
            "rules": [r.model_dump(mode="json", by_alias=True) for r in self.rules],
        }
        return json.dumps(payload, sort_keys=True, separators=(",", ":"))

    def signature(self) -> str:
        return hashlib.sha256(self.canonical_json().encode("utf-8")).hexdigest()


class Policy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: uuid.UUID
    #: None ⇒ platform-level. Platform and tenant policies are BOTH evaluated
    #: and deny-overrides applies across the two (§5.3), so a tenant can only
    #: ever tighten the platform baseline, never loosen it.
    tenant_id: str | None = None
    name: str
    namespace: str
    active_version: int | None = None
    status: PolicyStatus = PolicyStatus.DRAFT
    origin: Literal["user", "legacy_bridge"] = "user"


class NamespaceSpec(BaseModel):
    """A registered action namespace and its defaults (§5.4)."""

    model_config = ConfigDict(extra="forbid")

    namespace: str
    default_effect: Effect
    #: The global clamp. ARB-A registers every namespace at `shadow`.
    max_mode: PolicyMode = PolicyMode.SHADOW
    description: str = ""


# ─── Decision request / response ─────────────────────────────────────────────


class SubjectRef(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: SubjectKind
    #: Bounded to the column that stores it. Unbounded, an over-long id passes
    #: validation and fails at the INSERT — turning a bad request into a 500 and
    #: losing the decision that should have been logged.
    id: str = Field(min_length=1, max_length=255)
    attrs: dict[str, Any] = Field(default_factory=dict)


class ResourceRef(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: str = Field(min_length=1, max_length=128)
    id: str | None = Field(default=None, max_length=255)
    attrs: dict[str, Any] = Field(default_factory=dict)


class DecisionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tenant_id: str
    subject: SubjectRef
    action: str = Field(min_length=1, max_length=128)
    resource: ResourceRef
    context: dict[str, Any] = Field(default_factory=dict)

    @property
    def namespace(self) -> str:
        """`ai.external_call` ⇒ `ai`. Namespaces carry the default and the clamp."""
        return self.action.split(".", 1)[0]


class MatchedRule(BaseModel):
    model_config = ConfigDict(extra="forbid")

    policy_id: uuid.UUID
    version: int
    rule_id: str
    effect: Effect


class Decision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    effect: Effect
    #: False under dry_run, and under any namespace not yet promoted to enforce.
    #: A PEP treats `enforced=False` as "carry on, but tell the log".
    enforced: bool
    matched: list[MatchedRule] = Field(default_factory=list)
    obligations: list[Obligation] = Field(default_factory=list)
    default_applied: bool = False
    decision_id: uuid.UUID | None = None
    evaluated_at: datetime | None = None
    latency_us: int = 0
    #: Set when the evaluator could not answer at all. The PDP, not the
    #: evaluator, decides what that means for the caller (§5.1 keeps the
    #: evaluator pure, so it never chooses the failure policy itself).
    error: str | None = None
