"""HxArbiter ARB-B — the parity report, which is what earns a promotion.

Promoting a namespace to `enforce` is the moment this engine starts changing
answers on a running install. §8 makes that conditional on evidence: 100%
agreement with the legacy control, over a soak window, **with nonzero traffic**.

Each of those three is load-bearing, and the third is the one that gets dropped:

* **100%, not "high".** A 99.9% agreement rate on a sovereignty control means one
  request in a thousand goes somewhere it should not.
* **A window, not a total.** Agreement accumulated over a year says nothing about
  the policy that was edited yesterday.
* **Nonzero traffic.** A namespace nobody exercised agrees perfectly, and that is
  the most dangerous report this module can produce — a green light earned by
  silence. `eligible` is False without a floor of observed decisions, and the
  reason says so in words.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from velaris_shared.db.models import ArbiterDecisionModel

log = logging.getLogger(__name__)

#: Default soak, per §8.
DEFAULT_SOAK_DAYS = 14

#: Fewest compared decisions a promotion may rest on. A namespace with three
#: observations agreeing is not evidence of anything.
MIN_OBSERVATIONS = 100


@dataclass
class ParityReport:
    namespace: str
    window_days: int
    since: datetime
    compared: int
    agreements: int
    disagreements: int
    #: Split out because they fail in opposite directions and mean different
    #: things. `engine_stricter` is the engine denying what the legacy control
    #: allowed — a candidate false positive, and an outage if promoted blind.
    #: `engine_weaker` is the engine allowing what the legacy control refused,
    #: which is a hole and must be zero before anything is promoted.
    engine_stricter: int
    engine_weaker: int
    errors: int
    eligible: bool
    reasons: list[str] = field(default_factory=list)
    examples: list[dict[str, Any]] = field(default_factory=list)

    @property
    def agreement_rate(self) -> float:
        return (self.agreements / self.compared) if self.compared else 0.0

    def to_dict(self) -> dict[str, Any]:
        """JSON-safe by construction.

        Not merely tidy: this report is embedded in an `HTTPException` detail
        when a promotion is refused, and FastAPI serialises that with plain
        `json.dumps` rather than its encoder. A `datetime` in here turned the
        409 that refuses an unsafe promotion into a 500 — so the one path whose
        job is to stop a dangerous change was the one that crashed.
        """
        return {
            "namespace": self.namespace,
            "window_days": self.window_days,
            "since": self.since.isoformat(),
            "compared": self.compared,
            "agreements": self.agreements,
            "disagreements": self.disagreements,
            "engine_stricter": self.engine_stricter,
            "engine_weaker": self.engine_weaker,
            "errors": self.errors,
            "agreement_rate": round(self.agreement_rate, 6),
            "eligible_for_promotion": self.eligible,
            "reasons": self.reasons,
            "examples": self.examples,
        }


async def parity_report(
    session: AsyncSession,
    *,
    tenant_id,
    namespace: str,
    window_days: int = DEFAULT_SOAK_DAYS,
    min_observations: int = MIN_OBSERVATIONS,
    example_limit: int = 20,
) -> ParityReport:
    """Compare engine and legacy answers over the window.

    Only rows that actually carry a legacy answer are compared. A decision with
    `legacy_effect IS NULL` was never shadowed by anything and is not evidence
    either way — counting it as agreement is how a bridge that stopped running
    would still report a clean sheet.
    """
    since = datetime.now(timezone.utc) - timedelta(days=max(1, window_days))

    base = (
        ArbiterDecisionModel.tenant_id == tenant_id,
        ArbiterDecisionModel.namespace == namespace,
        ArbiterDecisionModel.evaluated_at >= since,
        ArbiterDecisionModel.simulated.is_(False),
        ArbiterDecisionModel.legacy_effect.isnot(None),
    )

    compared = (await session.execute(
        select(func.count()).select_from(ArbiterDecisionModel).where(*base)
    )).scalar_one()

    agreements = (await session.execute(
        select(func.count()).select_from(ArbiterDecisionModel).where(
            *base, ArbiterDecisionModel.legacy_effect == ArbiterDecisionModel.effect)
    )).scalar_one()

    stricter = (await session.execute(
        select(func.count()).select_from(ArbiterDecisionModel).where(
            *base,
            ArbiterDecisionModel.effect == "deny",
            ArbiterDecisionModel.legacy_effect == "allow")
    )).scalar_one()

    weaker = (await session.execute(
        select(func.count()).select_from(ArbiterDecisionModel).where(
            *base,
            ArbiterDecisionModel.effect == "allow",
            ArbiterDecisionModel.legacy_effect == "deny")
    )).scalar_one()

    errors = (await session.execute(
        select(func.count()).select_from(ArbiterDecisionModel).where(
            ArbiterDecisionModel.tenant_id == tenant_id,
            ArbiterDecisionModel.namespace == namespace,
            ArbiterDecisionModel.evaluated_at >= since,
            ArbiterDecisionModel.error.isnot(None))
    )).scalar_one()

    rows = (await session.execute(
        select(ArbiterDecisionModel).where(
            *base, ArbiterDecisionModel.legacy_effect != ArbiterDecisionModel.effect
        ).order_by(ArbiterDecisionModel.evaluated_at.desc()).limit(example_limit)
    )).scalars().all()

    disagreements = compared - agreements
    reasons: list[str] = []
    if compared < min_observations:
        reasons.append(
            f"only {compared} compared decisions in {window_days} days; "
            f"at least {min_observations} are needed — a namespace nobody "
            f"exercised agrees perfectly, which is not evidence")
    if weaker:
        reasons.append(
            f"{weaker} decision(s) where the engine ALLOWED what the legacy "
            f"control refused. This is a hole and must be zero")
    if stricter:
        reasons.append(
            f"{stricter} decision(s) where the engine DENIED what the legacy "
            f"control allowed. Promoting would refuse work that succeeds today")
    if errors:
        reasons.append(
            f"{errors} decision(s) could not be evaluated (PIP or store "
            f"failure). Enforcing on an engine that cannot answer means "
            f"denying on infrastructure trouble")

    return ParityReport(
        namespace=namespace,
        window_days=window_days,
        since=since,
        compared=compared,
        agreements=agreements,
        disagreements=disagreements,
        engine_stricter=stricter,
        engine_weaker=weaker,
        errors=errors,
        eligible=not reasons,
        reasons=reasons,
        examples=[
            {
                "id": str(r.id),
                "action": r.action,
                "subject_id": r.subject_id,
                "engine": r.effect,
                "legacy": r.legacy_effect,
                "matched": r.matched,
                "default_applied": r.default_applied,
                "evaluated_at": (r.evaluated_at.isoformat()
                                 if r.evaluated_at else None),
            }
            for r in rows
        ],
    )
