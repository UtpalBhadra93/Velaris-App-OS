"""The rollout sweep — guard, ramp, shadow (Oct 18a, phases 2–3).

Acts ONLY on rollouts an operator gave a policy. With no policy rows it reads
one table, finds nothing, and sleeps — which is what every install taking this
update has, so it runs by default. `VELARIS_ROLLOUT_SWEEP=off` stops it.

Order within a cycle is the design: the GUARD first (a candidate doing worse is
rolled back or held before anything widens it), then the RAMP (only when the
guard holds), then SHADOW (no effect on anything, so it goes last).

Every automatic change is made through the same service functions a person
uses, under a named actor (`rollout-guard`, `rollout-ramp`), so it appears in
the rollout's history and fires the same webhook events.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from velaris_shared.db.models import ContentRolloutModel, ContentRolloutPolicyModel

from case_service.rollouts import outcomes, service, shadow

log = logging.getLogger(__name__)

SWEEP_INTERVAL_SECONDS = 300
SWEEP_STARTUP_DELAY_SECONDS = 90
GUARD_ACTOR = "rollout-guard"
RAMP_ACTOR = "rollout-ramp"


def enabled() -> bool:
    return os.environ.get("VELARIS_ROLLOUT_SWEEP", "").strip().lower() not in (
        "0", "off", "false", "no")


def _aware(d: datetime | None) -> datetime | None:
    if d is None:
        return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


async def _guard(session: AsyncSession, row: ContentRolloutModel,
                 pol: ContentRolloutPolicyModel) -> str | None:
    if not pol.guard or row.state != "rolling" or pol.guard_state == "tripped":
        return None
    result = await outcomes.compare(session, row.id, min_cases=pol.guard["min_cases"])
    breach = outcomes.guard_breach(result, pol.guard)
    if breach is None:
        return None
    pol.guard_detail = breach
    reason = (f"guard: {breach['metric']} is {breach['candidate']} on the new version vs "
              f"{breach['baseline']} on the baseline (worse by {breach['delta']}, "
              f"limit {breach['max_delta']})")
    service._event(session, row, "guard_tripped", actor=GUARD_ACTOR, reason=reason,
                   from_state=row.state, from_percent=row.percent)
    if pol.guard["action"] == "rollback":
        pol.guard_state = "tripped"
        await service.rollback(session, row, actor=GUARD_ACTOR, reason=reason)
        return "rolled_back"
    pol.guard_state = "held"
    pol.next_step_at = None
    return "held"


async def _ramp(session: AsyncSession, row: ContentRolloutModel,
                pol: ContentRolloutPolicyModel, now: datetime) -> bool:
    if not pol.ramp or row.state != "rolling" or pol.guard_state != "ok":
        return False
    steps = [s for s in pol.ramp["steps"] if s > int(row.percent or 0)]
    if not steps:
        pol.next_step_at = None
        return False
    due = _aware(pol.next_step_at)
    if due is None:
        pol.next_step_at = now + timedelta(hours=pol.ramp["every_hours"])
        return False
    if due > now:
        return False
    await service.set_percent(session, row, percent=steps[0], actor=RAMP_ACTOR,
                              reason=f"scheduled ramp step {steps[0]}%")
    pol.next_step_at = now + timedelta(hours=pol.ramp["every_hours"]) if len(steps) > 1 else None
    return True


async def sweep(session: AsyncSession, *, now: datetime | None = None) -> dict[str, Any]:
    moment = now or datetime.now(timezone.utc)
    out = {"rollouts": 0, "rolled_back": 0, "held": 0, "ramped": 0, "shadowed": 0}
    if not await service._table_ready(session):
        return out
    try:
        pairs = (await session.execute(
            select(ContentRolloutModel, ContentRolloutPolicyModel)
            .join(ContentRolloutPolicyModel,
                  ContentRolloutPolicyModel.rollout_id == ContentRolloutModel.id)
            .where(ContentRolloutModel.state.in_(service.OPEN_STATES)))).all()
    except Exception as exc:                                     # noqa: BLE001
        log.debug("rollout sweep: no policy table yet (%s)", exc)
        return out
    for row, pol in pairs:
        out["rollouts"] += 1
        try:
            async with session.begin_nested():
                verdict = await _guard(session, row, pol)
                if verdict == "rolled_back":
                    out["rolled_back"] += 1
                elif verdict == "held":
                    out["held"] += 1
                elif await _ramp(session, row, pol, moment):
                    out["ramped"] += 1
                if pol.shadow and row.state in service.OPEN_STATES:
                    for case in await shadow.pending(session, row, now=moment):
                        await shadow.evaluate(session, rollout=row, case=case)
                        out["shadowed"] += 1
        except Exception as exc:                                 # noqa: BLE001
            log.warning("rollout sweep: %s failed this cycle: %s", row.id, exc)
    await session.commit()
    return out


async def cron() -> None:
    import asyncio

    from velaris_shared.db.session import get_session_factory

    if not enabled():
        log.info("Rollout sweep OFF (VELARIS_ROLLOUT_SWEEP=off)")
        return
    log.info("Rollout sweep ON (every %ss); it acts only on rollouts with a policy",
             SWEEP_INTERVAL_SECONDS)
    await asyncio.sleep(SWEEP_STARTUP_DELAY_SECONDS)
    while True:
        try:
            async with get_session_factory()() as session:
                result = await sweep(session)
            if result["rolled_back"] or result["held"] or result["ramped"]:
                log.info("rollout sweep: %s", result)
        except Exception as exc:                                 # noqa: BLE001
            log.warning("rollout sweep cycle failed: %s", exc)
        await asyncio.sleep(SWEEP_INTERVAL_SECONDS)
