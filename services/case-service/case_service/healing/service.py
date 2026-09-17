"""Healing episodes: open, climb the ladder, approve, apply, watch, undo.

Design: docs/Future/Done/AI-ERA-ROADMAP.md §F-08.

**Off until two separate switches are on**, and neither implies the other:
`VELARIS_CASE_HEALING_ENABLED` is the operator's, per install, and
`tenant.settings["healing"]["enabled"]` is each tenant's. A platform that could
change a customer's live process is not something an update may switch on, and
an operator who is willing to allow it on their box has still not decided it for
every tenant on it.

Four rules hold everywhere below.

**One approval, never none.** The roadmap's phrase is "one-approval deploy
card", and the emphasis belongs on *approval*. `apply()` refuses an episode
nobody approved, and applies through HxBranch's own merge primitive rather than
opening a second way into config.

**The rollback is pre-signed.** The exact bytes to write back are computed and
stored when the human approves, not when something goes wrong. Undoing a healing
change at 3 a.m. then replays a snapshot a named person already agreed to,
instead of needing a new privileged path at the worst possible moment.

**Deployed is not healed.** A change earns `healed` by the thing that was wrong
staying un-wrong for a whole watch window. Until then it is `watching`, and if
the metric regresses beyond its bound it is rolled back and an incident case is
opened — because a process that healed itself and got worse is exactly what a
person needs to be told about.

**Every episode is chained.** Sequence, previous hash, content hash — the same
construction as the F-01 ledger and the F-03 proofs, so the record of the
platform changing itself cannot be quietly rewritten afterwards.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from velaris_shared.db.models import (
    CaseTypeModel,
    HealingEpisodeModel,
    HxEvolveInsightModel,
    RuleDefinitionModel,
    TenantModel,
)

from case_service.healing import catalog, ladder

log = logging.getLogger(__name__)

#: How long a change is watched before it may be called healed, and how far the
#: watched metric may move the wrong way before it is a regression rather than
#: noise. Both are tenant-overridable; these are the conservative defaults.
DEFAULT_WATCH_HOURS = 24
DEFAULT_BOUND_PCT = 10.0
#: A rehearsal is required by default. The alternative — applying a change the
#: platform generated, to a live process, on replay evidence alone — is not a
#: default anybody should get without choosing it.
DEFAULT_REQUIRE_TWIN = True

GENESIS = "0" * 64


class HealingError(ValueError):
    """The episode cannot proceed as asked. The message is for a human."""


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _canonical(payload: Any) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)


# ── the two switches ─────────────────────────────────────────────────────────


def operator_enabled() -> bool:
    """The install's answer. Off unless somebody set it."""
    raw = os.environ.get("VELARIS_CASE_HEALING_ENABLED", "")
    return str(raw).strip().lower() in ("1", "true", "yes", "on")


def tenant_config(tenant: TenantModel | None) -> dict[str, Any]:
    """`tenant.settings["healing"]`, with the conservative defaults filled in."""
    raw = ((tenant.settings or {}).get("healing") or {}) if tenant is not None else {}
    if not isinstance(raw, dict):
        raw = {}
    return {
        "enabled": bool(raw.get("enabled", False)),
        "watch_hours": int(raw.get("watch_hours", DEFAULT_WATCH_HOURS) or DEFAULT_WATCH_HOURS),
        "bound_pct": float(raw.get("bound_pct", DEFAULT_BOUND_PCT)),
        "require_twin": bool(raw.get("require_twin", DEFAULT_REQUIRE_TWIN)),
        "twin_cases": int(raw.get("twin_cases", 5) or 5),
    }


async def enabled_for(session: AsyncSession, tenant_uuid: uuid.UUID) -> tuple[bool, str]:
    """`(allowed, why_not)`. Both switches, reported separately.

    An operator who has switched healing on for the box and a tenant who has not
    opted in are two different situations, and telling somebody "healing is off"
    without saying which one would send them to the wrong screen.
    """
    if not operator_enabled():
        return False, ("self-healing is switched off on this install "
                       "(VELARIS_CASE_HEALING_ENABLED)")
    tenant = await session.get(TenantModel, tenant_uuid)
    if not tenant_config(tenant)["enabled"]:
        return False, ('this tenant has not opted in to self-healing '
                       '(settings["healing"]["enabled"])')
    return True, ""


# ── the chain ────────────────────────────────────────────────────────────────


async def _tip(session: AsyncSession) -> tuple[int, str]:
    row = (await session.execute(
        select(HealingEpisodeModel)
        .order_by(HealingEpisodeModel.sequence.desc()).limit(1))).scalars().first()
    return (0, GENESIS) if row is None else (int(row.sequence), row.content_hash)


def content_hash(document: dict[str, Any], prev_hash: str) -> str:
    """`sha256(prev || canonical(document))` — the address is also the link.

    One hash rather than two: an address and a chain link that could disagree
    are two things to keep in step, and eventually they would not be.
    """
    return hashlib.sha256((prev_hash + _canonical(document)).encode()).hexdigest()


def _episode_document(episode: HealingEpisodeModel) -> dict[str, Any]:
    """What the hash covers: the decision, not the bookkeeping.

    `status`, the watch outcome and the rollback are deliberately OUTSIDE it.
    They change after the episode is written, and an address that moved every
    time the outcome arrived would chain nothing.

    **`created_at` is outside it too, and that is not an oversight.** A
    timestamp is the one field here that does not survive a database round trip
    unchanged: it goes in timezone-aware and comes back naive on some backends,
    so `isoformat()` produces a different string and every episode fails its own
    verification. `sequence` orders the chain just as well and is an integer.
    """
    return {
        "sequence": int(episode.sequence),
        "case_type_id": str(episode.case_type_id),
        "insight_id": str(episode.insight_id) if episode.insight_id else None,
        "diagnosis": episode.diagnosis,
        "operator": episode.operator,
        "proposal": episode.proposal,
        "ladder": episode.ladder,
        "stopped_at": episode.stopped_at,
    }


async def verify_chain(session: AsyncSession) -> dict[str, Any]:
    """Recompute every link. Reports the FIRST break and stops.

    Continuing past a break would report every later episode as broken too,
    which buries the one that actually was.
    """
    rows = list((await session.execute(
        select(HealingEpisodeModel).order_by(HealingEpisodeModel.sequence)
    )).scalars().all())
    prev = GENESIS
    for row in rows:
        if row.prev_hash != prev:
            return {"verified": False, "episodes": len(rows),
                    "broken_at": int(row.sequence),
                    "reason": "this episode does not follow the one before it"}
        recomputed = content_hash(_episode_document(row), prev)
        if recomputed != row.content_hash:
            return {"verified": False, "episodes": len(rows),
                    "broken_at": int(row.sequence),
                    "reason": "this episode's contents no longer hash to its address"}
        prev = row.content_hash
    return {"verified": True, "episodes": len(rows), "head": prev}


# ── opening an episode ───────────────────────────────────────────────────────


async def open_episode(
    session: AsyncSession, *, tenant_uuid: uuid.UUID, insight_id: uuid.UUID,
    created_by: str | None = None,
) -> HealingEpisodeModel:
    """Take a surfaced HxEvolve insight and climb the ladder against it.

    The episode is written whatever the ladder says. A refusal is the record
    that matters most here — "the platform wanted to change this and was not
    allowed to" is exactly what somebody reviewing an autonomous system needs to
    be able to read.
    """
    allowed, why = await enabled_for(session, tenant_uuid)
    if not allowed:
        raise HealingError(why)

    insight = await session.get(HxEvolveInsightModel, insight_id)
    if insight is None:
        raise HealingError("no such insight")
    case_type = await session.get(CaseTypeModel, insight.case_type_id)
    if case_type is None:
        raise HealingError("the case type this insight is about no longer exists")

    existing = (await session.execute(
        select(HealingEpisodeModel).where(
            HealingEpisodeModel.insight_id == insight_id,
            HealingEpisodeModel.status.in_(
                ("pending", "ready", "applied", "watching")))
    )).scalars().first()
    if existing is not None:
        # Two open episodes for one insight would race to change the same
        # artifact, and the second would silently overwrite the first's
        # pre-signed rollback.
        raise HealingError(
            f"this insight already has an open episode ({existing.status})")

    tenant = await session.get(TenantModel, tenant_uuid)
    cfg = tenant_config(tenant)

    result = await ladder.run(
        session, operator=insight.proposal_kind or "", proposal=insight.proposal or {},
        insight=insight, case_type=case_type, tenant_uuid=tenant_uuid,
        require_twin=cfg["require_twin"], twin_cases=cfg["twin_cases"])

    sequence, prev = await _tip(session)
    episode = HealingEpisodeModel(
        id=uuid.uuid4(), tenant_id=tenant_uuid, case_type_id=insight.case_type_id,
        insight_id=insight.id, diagnosis=insight.signal or {},
        operator=insight.proposal_kind or "", proposal=insight.proposal or {},
        ladder=result["rungs"], stopped_at=result["stopped_at"],
        stopped_reason=result["stopped_reason"],
        replay_run_id=result["refs"].get("replay_run_id"),
        twin_run_id=result["refs"].get("twin_run_id"),
        status="ready" if result["ok"] else "refused",
        watch_metric=str((insight.signal or {}).get("kind") or "sla"),
        watch_bound_pct=cfg["bound_pct"],
        sequence=sequence + 1, prev_hash=prev, content_hash=GENESIS,
        created_by=created_by, created_at=_utcnow())
    episode.content_hash = content_hash(_episode_document(episode), prev)
    session.add(episode)
    await session.flush()
    return episode


# ── approving, and applying ──────────────────────────────────────────────────


async def approve(
    session: AsyncSession, *, episode: HealingEpisodeModel, actor: str,
    reason: str = "",
) -> HealingEpisodeModel:
    """One approval. It also signs the rollback.

    The inverse patch is computed HERE, from the artifact as it stands at the
    moment of approval — not from the proposal, and not later. What gets written
    back on a rollback is therefore exactly what the approver was looking at.
    """
    if episode.status != "ready":
        raise HealingError(
            f"an episode is approved from 'ready'; this one is {episode.status!r}")

    artifact_type, artifact_id, base = await _artifact_and_base(session, episode)
    episode.rollback_artifact_type = artifact_type
    episode.rollback_artifact_id = str(artifact_id)
    episode.rollback_patch = base
    episode.approved_by = actor
    episode.approved_at = _utcnow()
    episode.decided_reason = reason or ""
    await session.flush()
    return episode


async def reject(
    session: AsyncSession, *, episode: HealingEpisodeModel, actor: str, reason: str,
) -> HealingEpisodeModel:
    if episode.status not in ("ready", "pending"):
        raise HealingError(f"this episode is {episode.status!r}")
    if len((reason or "").strip()) < 5:
        # The rejection is the only record of why the platform was told no, and
        # a future scan will propose the same thing again.
        raise HealingError("a rejection needs a reason somebody can read later")
    episode.status = "rejected"
    episode.approved_by = None
    episode.decided_reason = reason
    await session.flush()
    return episode


async def _artifact_and_base(session: AsyncSession, episode: HealingEpisodeModel):
    """`(artifact_type, artifact_id, current_snapshot)` — the pre-signed inverse."""
    if episode.operator in ("rule_add", "rule_adjust"):
        rule_id = (episode.proposal or {}).get("id")
        if episode.operator == "rule_add" or not rule_id:
            # There is nothing to restore for a rule that did not exist before;
            # the inverse of adding is disabling, which is what this snapshot
            # says. Deleting it would destroy the audit trail of a rule that
            # really did run.
            return "rule", rule_id or "", {"enabled": False}
        rule = await session.get(RuleDefinitionModel, uuid.UUID(str(rule_id)))
        if rule is None:
            raise HealingError("the rule this episode changes no longer exists")
        return "rule", rule.id, {"definition_json": rule.definition_json,
                                 "enabled": rule.enabled}
    ct = await session.get(CaseTypeModel, episode.case_type_id)
    if ct is None:
        raise HealingError("the case type this episode changes no longer exists")
    return "case_type", ct.id, {"definition_json": ct.definition_json}


async def apply(
    session: AsyncSession, *, episode: HealingEpisodeModel, watch_hours: int | None = None,
) -> HealingEpisodeModel:
    """Write the change, through HxBranch's own merge primitive.

    Refuses an episode nobody approved, and refuses one with no pre-signed
    rollback: applying a change the platform generated without knowing how to
    undo it is the one thing this feature must never do.
    """
    if not episode.approved_by:
        raise HealingError("nothing is applied without an approval")
    if episode.status != "ready":
        raise HealingError(f"this episode is {episode.status!r}")
    if not episode.rollback_patch or not episode.rollback_artifact_id:
        raise HealingError(
            "this episode has no pre-signed rollback, so it will not be applied")

    from case_service.api.routers.branches import _apply_artifact_to_main

    content = await _forward_patch(session, episode)
    try:
        await _apply_artifact_to_main(
            session, episode.rollback_artifact_type,
            str(episode.rollback_artifact_id), content)
    except Exception as exc:                                     # noqa: BLE001
        episode.status = "failed"
        episode.outcome = {"error": f"{type(exc).__name__}: {exc}"}
        episode.outcome_at = _utcnow()
        await session.flush()
        raise HealingError(f"the change could not be applied: {exc}") from exc

    hours = watch_hours if watch_hours is not None else DEFAULT_WATCH_HOURS
    episode.applied_at = _utcnow()
    episode.status = "watching"
    episode.watch_until = episode.applied_at + timedelta(hours=max(1, int(hours)))
    episode.watch_baseline = await _metric_snapshot(session, episode)
    await session.flush()
    return episode


async def _forward_patch(session: AsyncSession, episode: HealingEpisodeModel) -> dict:
    """The change itself, in the shape `_apply_artifact_to_main` writes."""
    if episode.rollback_artifact_type == "rule":
        proposal = episode.proposal or {}
        return {"definition_json": proposal.get("definition_json") or proposal,
                "enabled": True}
    ct = await session.get(CaseTypeModel, episode.case_type_id)
    return {"definition_json": ladder.patched_definition(
        episode.operator, (ct.definition_json if ct else {}) or {},
        episode.proposal or {})}


# ── the watch window ─────────────────────────────────────────────────────────


async def _metric_snapshot(session: AsyncSession,
                           episode: HealingEpisodeModel) -> dict[str, Any]:
    """What the watched process looks like right now.

    HxEvolve's own drift metrics, so "better" and "worse" mean here exactly what
    they mean everywhere else in this platform.
    """
    from case_service.hxevolve import drift

    try:
        return await drift.current_metrics(session, episode.case_type_id, days=7)
    except Exception as exc:                                     # noqa: BLE001
        log.info("healing: metrics unavailable for %s: %s", episode.case_type_id, exc)
        return {}


#: Metrics compared across the watch window, and WHICH WAY IS WRONG for each.
#:
#: This pairing is the whole correctness of the watch. `conformance_rate` is a
#: percentage where HIGHER IS BETTER, so a collapse in conformance shows up as a
#: NEGATIVE delta — a one-directional "did it rise above the bound?" check reads
#: that as an improvement and declares the change healed. The directions match
#: `hxevolve.drift._problems`, which is where this platform already decided what
#: better and worse mean.
WATCHED_METRICS: tuple[tuple[str, str], ...] = (
    ("avg_duration_hours", "lower_is_better"),
    ("conformance_rate", "higher_is_better"),
)

#: Too little traffic on either side of the window and the comparison is noise.
#: Mirrors `hxevolve.drift.MIN_CASES_FOR_COMPARISON` for the same reason.
MIN_CASES_TO_JUDGE = 5


def _regressed(baseline: dict[str, Any], now: dict[str, Any],
               bound_pct: float) -> tuple[bool, str]:
    """Did the watched process get materially worse?

    Three things this is careful about:

    * **Direction.** See `WATCHED_METRICS`. A conformance collapse is a fall,
      not a rise.
    * **Absence.** A metric present on one side and missing on the other is
      skipped rather than guessed at: a missing number is not a regression, and
      treating it as one would roll back healthy changes on a quiet week.
    * **Volume.** Too few cases on either side and nothing is concluded — the
      change stays healed rather than being undone on the strength of three
      cases.
    """
    if (int(baseline.get("cases") or 0) < MIN_CASES_TO_JUDGE
            or int(now.get("cases") or 0) < MIN_CASES_TO_JUDGE):
        return False, ("too few cases on one side of the window to judge; "
                       "the change is left in place")
    worse: list[str] = []
    for key, direction in WATCHED_METRICS:
        before, after = baseline.get(key), now.get(key)
        if before is None or after is None or float(before) == 0:
            continue
        raw = 100.0 * (float(after) - float(before)) / abs(float(before))
        delta = raw if direction == "lower_is_better" else -raw
        if delta > bound_pct:
            moved = "rose" if direction == "lower_is_better" else "fell"
            worse.append(f"{key} {moved} {abs(raw):.1f}% (bound {bound_pct}%)")
    return bool(worse), "; ".join(worse)


async def close_watch(
    session: AsyncSession, *, episode: HealingEpisodeModel, now: datetime | None = None,
) -> HealingEpisodeModel:
    """Decide one watched episode: healed, or rolled back.

    Called by the sweep when the window is up. A change is not healed because it
    deployed — it is healed because the thing that was wrong stayed un-wrong.
    """
    if episode.status != "watching":
        return episode
    current = await _metric_snapshot(session, episode)
    bound = float(episode.watch_bound_pct or DEFAULT_BOUND_PCT)
    regressed, why = _regressed(episode.watch_baseline or {}, current, bound)
    episode.outcome = {"baseline": episode.watch_baseline, "after": current,
                       "regressed": regressed, "detail": why}
    episode.outcome_at = now or _utcnow()

    if not regressed:
        episode.status = "healed"
        await session.flush()
        return episode
    return await rollback(session, episode=episode, reason=why or "regression",
                          automatic=True)


async def rollback(
    session: AsyncSession, *, episode: HealingEpisodeModel, reason: str,
    automatic: bool = False,
) -> HealingEpisodeModel:
    """Write the pre-signed snapshot back, and open an incident.

    No new authority is needed: the bytes being written were computed and
    approved by a named person before the change ever went in. That is the whole
    point of pre-signing them.
    """
    if not episode.rollback_patch or not episode.rollback_artifact_id:
        raise HealingError("this episode has no pre-signed rollback")
    from case_service.api.routers.branches import _apply_artifact_to_main

    await _apply_artifact_to_main(
        session, episode.rollback_artifact_type,
        str(episode.rollback_artifact_id), episode.rollback_patch)
    episode.status = "rolled_back"
    episode.rolled_back_at = _utcnow()
    episode.rollback_reason = reason
    if automatic:
        # A process that healed itself and got worse is exactly what somebody
        # needs to be told about, and a status change on a row nobody opens is
        # not telling anybody.
        episode.incident_case_id = await _open_incident(session, episode, reason)
    await session.flush()
    return episode


async def _open_incident(session: AsyncSession, episode: HealingEpisodeModel,
                         reason: str) -> uuid.UUID | None:
    """An incident case on the same case type. Best effort, and honest about it.

    A failure to open one must not leave the rollback half-done, so it is
    swallowed and recorded rather than raised — but it IS recorded, because an
    automatic rollback nobody was told about is the quiet failure this whole
    feature exists to avoid.
    """
    from velaris_shared.db.models import CaseInstanceModel

    try:
        ct = await session.get(CaseTypeModel, episode.case_type_id)
        if ct is None:
            return None
        stages = sorted((ct.definition_json or {}).get("stages") or [],
                        key=lambda st: st.get("order", 0))
        case = CaseInstanceModel(
            id=uuid.uuid4(), case_type_id=ct.id, case_type_version=ct.version,
            status="new", priority="high",
            current_stage_id=stages[0]["id"] if stages else None,
            tenant_id=episode.tenant_id,
            data={"healing_incident": {
                "episode_id": str(episode.id),
                "operator": episode.operator,
                "reason": reason,
                "approved_by": episode.approved_by,
                "applied_at": (episode.applied_at.isoformat()
                               if episode.applied_at else None)}})
        session.add(case)
        await session.flush()
        return case.id
    except Exception as exc:                                     # noqa: BLE001
        log.warning("healing: could not open an incident for %s: %s", episode.id, exc)
        episode.outcome = {**(episode.outcome or {}),
                           "incident_error": f"{type(exc).__name__}: {exc}"}
        return None


async def due_watches(session: AsyncSession, *, now: datetime | None = None,
                      limit: int = 50) -> list[HealingEpisodeModel]:
    moment = now or _utcnow()
    return list((await session.execute(
        select(HealingEpisodeModel)
        .where(HealingEpisodeModel.status == "watching",
               HealingEpisodeModel.watch_until <= moment)
        .order_by(HealingEpisodeModel.watch_until).limit(limit))).scalars().all())


# ── the watch sweep ──────────────────────────────────────────────────────────

#: Long enough that a restart does not immediately re-scan, and short enough
#: that a change applied just before one is still closed on time.
SWEEP_STARTUP_DELAY_SECONDS = 45
SWEEP_INTERVAL_SECONDS = 900


async def sweep(session: AsyncSession, *, limit: int = 50) -> dict[str, Any]:
    """Close every watch window that is up, across every tenant.

    Each episode is its own committed decision. One episode whose metrics cannot
    be read must not cost the others their close — a change left `watching`
    forever is a change nobody ever declared healed *or* undid.
    """
    closed = 0
    rolled_back = 0
    errors: list[str] = []
    for episode in await due_watches(session, limit=limit):
        try:
            await close_watch(session, episode=episode)
            await session.commit()
            closed += 1
            if episode.status == "rolled_back":
                rolled_back += 1
        except Exception as exc:                                 # noqa: BLE001
            await session.rollback()
            log.warning("healing: watch %s could not be closed: %s", episode.id, exc)
            errors.append(f"{episode.id}: {type(exc).__name__}")
    return {"closed": closed, "rolled_back": rolled_back, "errors": errors}


async def watch_cron() -> None:
    """The sweep on a timer. **Off unless an operator turns it on.**

    It reports OFF and returns rather than not existing, so the startup log says
    the feature is here and how to switch it on — the same posture as the F-06
    capture sweep.
    """
    import asyncio

    from velaris_shared.db.session import get_session_factory

    if not operator_enabled():
        log.info("Self-healing watch sweep OFF — set "
                 "VELARIS_CASE_HEALING_ENABLED=true to enable (each tenant must "
                 'also set settings["healing"]["enabled"])')
        return

    log.info("Self-healing watch sweep ON (every %ss)", SWEEP_INTERVAL_SECONDS)
    await asyncio.sleep(SWEEP_STARTUP_DELAY_SECONDS)
    while True:
        try:
            async with get_session_factory()() as session:
                out = await sweep(session)
            if out["closed"]:
                log.info("healing: closed %s watch window(s), %s rolled back",
                         out["closed"], out["rolled_back"])
        except Exception as exc:                                 # noqa: BLE001
            log.warning("healing: watch sweep cycle failed: %s", exc)
        await asyncio.sleep(SWEEP_INTERVAL_SECONDS)
