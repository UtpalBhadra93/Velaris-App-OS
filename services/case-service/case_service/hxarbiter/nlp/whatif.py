"""HxArbiter ARB-F/G — what a draft would have changed, replayed on real traffic.

`arbiter_drafts.whatif_report` has existed since migration 179 and **nothing
ever wrote to it**: §7.2 lists a what-if step and ARB-G lists "HxReplay
what-if", and neither was built, so the column was NULL on every row ever
created. This is that step.

The question is the one an operator actually asks before activating anything:
*if this had been live, what would have decided differently?* It is answered by
replaying the draft's rules against decisions the platform really made, through
the same `Evaluator` the real path uses — a simulator with its own logic proves
nothing about the engine that will run.

**The honest limitation, and why it is a refusal rather than a caveat.** The
decision log stores the request (`subject`, `resource`, `context`) and *not* the
resolved attribute map: PIP-resolved paths like `tenant.region` and
`consent.basis` were never written down. A replay of a rule that reads one of
them would silently treat it as missing — every comparison false — and report
"nothing would change" for a residency rule that would in fact deny half the
traffic. That is the most dangerous possible output from this function.

So a draft whose rules depend on any non-request attribute is **not replayed at
all**, and the report names the paths and says why. A partial answer here is
worse than none, because it would be read as a full one.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from hxarbiter_core import (
    AttributeCatalog,
    Effect,
    Evaluator,
    PolicyCompiler,
    PolicyMode,
    PolicyVersion,
    Rule,
)
from velaris_shared.db.models import ArbiterDecisionModel, ArbiterNamespaceModel

log = logging.getLogger(__name__)

#: Prefixes the decision log can actually reconstruct, because they came with
#: the request rather than from a PIP.
REPLAYABLE_PREFIXES = ("subject", "resource", "context")

#: How far back, and how many rows. Bounded because this runs inside a draft
#: creation that a person is waiting on.
DEFAULT_WINDOW_DAYS = 30
MAX_ROWS = 2_000


def unreplayable_paths(required: Sequence[str]) -> list[str]:
    """Paths a replay cannot reconstruct from the log."""
    return sorted({
        p for p in required
        if p.split(".", 1)[0] not in REPLAYABLE_PREFIXES
    })


def _attrs_from(row: ArbiterDecisionModel) -> dict[str, Any]:
    """Rebuild the request-derived half of the attribute map.

    Mirrors `RequestResolver` deliberately — including that the structural
    fields win over the free-form `attrs` map, so a replay cannot reach a
    conclusion the live path would not have.
    """
    out: dict[str, Any] = {}
    subject = row.subject or {}
    resource = row.resource or {}
    for key, value in (subject.get("attrs") or {}).items():
        out[f"subject.{key}"] = value
    out["subject.kind"] = row.subject_kind
    out["subject.id"] = row.subject_id
    for key, value in (resource.get("attrs") or {}).items():
        out[f"resource.{key}"] = value
    if resource.get("kind"):
        out["resource.kind"] = resource["kind"]
    if resource.get("id"):
        out["resource.id"] = resource["id"]
    for key, value in (row.context or {}).items():
        out[f"context.{key}"] = value
    return out


async def replay(
    session: AsyncSession,
    *,
    rules: Sequence[Rule],
    namespace: str,
    tenant_id: uuid.UUID | None,
    catalog: AttributeCatalog,
    window_days: int = DEFAULT_WINDOW_DAYS,
) -> dict[str, Any]:
    """What this draft would have decided, on traffic that really happened."""
    compiled = PolicyCompiler(catalog).compile_version(
        policy_id=uuid.UUID(int=0), tenant_id=None, namespace=namespace,
        version=PolicyVersion(policy_id=uuid.UUID(int=0), version=1,
                              rules=list(rules)),
        mode=PolicyMode.ENFORCE)

    blocked = unreplayable_paths(compiled.required_attrs)
    if blocked:
        return {
            "replayed": False,
            "reason": "attributes_not_recorded",
            "unreplayable_attributes": blocked,
            "explanation": (
                "These rules read attributes the decision log does not store — "
                "they are resolved by a PIP at decision time, not carried on "
                "the request. Replaying anyway would treat them as missing, "
                "make every comparison on them false, and report 'nothing "
                "would change' for a rule that may change a great deal. Use "
                "Simulate to try specific attribute values instead."),
        }

    spec = (await session.execute(
        select(ArbiterNamespaceModel)
        .where(ArbiterNamespaceModel.namespace == namespace)
    )).scalars().first()
    default_effect = Effect(spec.default_effect) if spec else Effect.DENY

    where = [
        ArbiterDecisionModel.namespace == namespace,
        ArbiterDecisionModel.simulated.is_(False),
        ArbiterDecisionModel.evaluated_at
        >= datetime.now(timezone.utc) - timedelta(days=max(1, window_days)),
    ]
    if tenant_id is not None:
        where.append(ArbiterDecisionModel.tenant_id == tenant_id)

    rows = (await session.execute(
        select(ArbiterDecisionModel).where(*where)
        .order_by(ArbiterDecisionModel.evaluated_at.desc()).limit(MAX_ROWS)
    )).scalars().all()

    if not rows:
        return {"replayed": False, "reason": "no_traffic", "compared": 0,
                "explanation": (
                    "No decisions were recorded in this namespace over the "
                    "window, so there is nothing to replay against. That is "
                    "not evidence the rules are harmless.")}

    evaluator = Evaluator()
    changed_to_deny: list[dict[str, Any]] = []
    changed_to_allow = 0
    unchanged = 0
    for row in rows:
        # The draft is evaluated ALONE. It answers "what would this policy
        # decide", not "what would the whole set decide with it added" — the
        # second needs the other policies' attributes too, which the log has
        # the same problem with.
        outcome = evaluator.evaluate(
            policies=[compiled], attrs=_attrs_from(row),
            namespace_default=default_effect,
            namespace_max_mode=PolicyMode.ENFORCE)
        if str(outcome.effect) == row.effect:
            unchanged += 1
        elif outcome.effect is Effect.DENY:
            if len(changed_to_deny) < 20:
                changed_to_deny.append({
                    "decision_id": str(row.id), "action": row.action,
                    "was": row.effect, "would_be": "deny",
                    "matched": [m.rule_id for m in outcome.matched]})
        else:
            changed_to_allow += 1

    would_deny = len(changed_to_deny)
    return {
        "replayed": True,
        "window_days": window_days,
        "compared": len(rows),
        "unchanged": unchanged,
        "would_now_deny": would_deny,
        "would_now_allow": changed_to_allow,
        "examples": changed_to_deny,
        # The number to read first. A draft that refuses nothing on real
        # traffic is either harmless or does not fit the requests arriving,
        # and those look identical from the rule text alone.
        "headline": (
            f"On {len(rows)} real decisions, this draft would have refused "
            f"{would_deny} that succeeded and permitted {changed_to_allow} "
            f"that were refused."),
    }
