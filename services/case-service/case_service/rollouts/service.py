"""Staged rollout of content versions — which case type a NEW case gets.

Oct item 18a. A client ships a new version of a case type and chooses how it
reaches their customers:

* **immediate** — every new case gets it at once. Today's behaviour, and the
  default: no strategy, an older package, or no rollout table all mean this.
* **flag** — installed and dormant; an operator switches it on (and off).
* **percentage** — N% of customers, sticky, raised step by step.
* **blue_green** — both installed; one switch moves 100% between them.

Then it is **promoted** (the candidate becomes the baseline) or **rolled back**.

The rules that make it safe, each enforced here rather than hoped for:

* **One resolver.** Every path that creates a case calls `resolve()`; a path
  that did not would silently ignore the rollout, so a test walks the code for
  every place a case is constructed and holds each to either calling this or
  being named as exempt.
* **In-flight cases are pinned.** A rollout only chooses for NEW cases. A case
  keeps the `case_type_id` it was created with; rolling back never touches it.
* **Sticky per customer.** `bucket = sha256(rollout id : subject) mod 100` — no
  table decides it, so the same customer lands in the same arm on every call,
  on every replica. The arm is recorded on the case for audit.
* **One open rollout per (tenant, kind, name).** A customer only ever sees
  baseline or candidate, never three versions. The lock is a UNIQUE index on
  `open_key` — refused by the database, not by a check that can race.
* **The operator turns it on.** Every rollout starts `requested` and has no
  effect until someone else approves it — the two-person rule HxDeploy
  promotion already carries.
* **A closed rollout still redirects.** A promoted one sends requests for its
  baseline to its candidate; a rolled-back one sends its candidate back to the
  baseline. So a channel, portal or schedule configured with an old id follows.

**Only case types can be rolled out.** Rules and process definitions are not
chosen per case: every enabled rule for a case type fires, and a process is
reached through its case type. They roll out AS PART OF a case type version
(the candidate row carries its own lifecycle process and definition), never on
their own — `start()` refuses them by name.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import hashlib
import logging
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from velaris_shared.db.models import (
    CaseTypeModel,
    ContentRolloutCaseModel,
    ContentRolloutEventModel,
    ContentRolloutModel,
    ContentRolloutPolicyModel,
)

log = logging.getLogger(__name__)

KINDS = ("case_type",)
#: Named so the refusal can say why, not just that.
NOT_ROLLABLE = {
    "rule": ("rules are not chosen per case — every enabled rule for a case type "
             "fires — so two versions cannot be split between customers. Ship the "
             "rule change inside a new case type version, and roll that out"),
    "process": ("a process is reached through its case type's lifecycle process, "
                "not chosen per case. Ship it inside a new case type version, and "
                "roll that out"),
}
STRATEGIES = ("immediate", "flag", "percentage", "blue_green")
OPEN_STATES = ("requested", "dormant", "rolling")
CLOSED_STATES = ("promoted", "rolled_back", "superseded")
#: A redirect chain longer than this is a loop somebody built by hand.
MAX_HOPS = 8


class RolloutError(ValueError):
    """The rollout cannot do that. The message is for a human."""


class RolloutLocked(RolloutError):
    """Another rollout is open for this name. Carries it, and the ways forward."""

    def __init__(self, open_rollout: ContentRolloutModel):
        self.open_rollout = open_rollout
        super().__init__(
            f"{open_rollout.artifact_name!r} already has an open rollout "
            f"({open_rollout.id}, {open_rollout.strategy}, {open_rollout.state}, "
            f"{open_rollout.percent}%). Finish it (promote), roll it back, or "
            f"supersede it — one rollout per case type at a time, so a customer "
            f"only ever sees the baseline or the new version.")

    def detail(self) -> dict[str, Any]:
        return {"message": str(self), "open_rollout": to_json(self.open_rollout),
                "options": ["promote", "rollback", "supersede"]}


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _open_key(tenant_uuid: uuid.UUID, kind: str, name: str) -> str:
    return f"{tenant_uuid}:{kind}:{name}"[:400]


def bucket(rollout_id: uuid.UUID | str, subject: str) -> int:
    """0..99, stable for (rollout, subject). Nothing stored decides it."""
    digest = hashlib.sha256(f"{rollout_id}:{subject}".encode()).hexdigest()
    return int(digest[:8], 16) % 100


def subject_of(*candidates: Any) -> str | None:
    """The first non-empty of: customer, then requester/submitter, in order."""
    for c in candidates:
        if c is not None and str(c).strip():
            return str(c).strip()
    return None


# ── the table may not exist yet ──────────────────────────────────────────────

_table_state: dict[str, Any] = {"ok": None, "until": 0.0}


async def _table_ready(session: AsyncSession) -> bool:
    """Missing table ⇒ immediate, as before — never an error on a create path.

    An install whose code updated before its migration ran must keep creating
    cases. The answer is cached: True for good, False for a minute.
    """
    if _table_state["ok"] is True:
        return True
    if _table_state["ok"] is False and time.monotonic() < _table_state["until"]:
        return False

    def _check(sync_session) -> bool:
        from sqlalchemy import inspect
        return inspect(sync_session.connection()).has_table("content_rollouts")

    try:
        ok = bool(await session.run_sync(_check))
    except Exception:                                            # noqa: BLE001
        ok = False
    _table_state.update(ok=ok, until=time.monotonic() + 60)
    return ok


# ── the resolver ─────────────────────────────────────────────────────────────


@dataclass
class Resolution:
    case_type_id: uuid.UUID
    rollout_id: uuid.UUID | None = None
    arm: str | None = None
    bucket: int | None = None
    #: percentage | pilot | excluded | not_targeted
    reason: str | None = None


#: Limitation 15 (phase 2): a tenant with no rollout rows at all — every install
#: that never uses this feature — pays one query per TEN SECONDS rather than
#: one per case. Only the negative answer is cached; a tenant that has rollouts
#: is looked up on every creation, so an approval takes effect at once.
_NO_ROLLOUTS_TTL = 10.0
_no_rollouts: dict[uuid.UUID, float] = {}


def forget_cache(tenant_uuid: uuid.UUID | None = None) -> None:
    if tenant_uuid is None:
        _no_rollouts.clear()
    else:
        _no_rollouts.pop(tenant_uuid, None)


async def _tenant_has_rollouts(session: AsyncSession, tenant_uuid: uuid.UUID) -> bool:
    until = _no_rollouts.get(tenant_uuid)
    if until is not None and until > time.monotonic():
        return False
    found = (await session.execute(select(ContentRolloutModel.id).where(
        ContentRolloutModel.tenant_id == tenant_uuid).limit(1))).first() is not None
    if found:
        _no_rollouts.pop(tenant_uuid, None)
    else:
        _no_rollouts[tenant_uuid] = time.monotonic() + _NO_ROLLOUTS_TTL
    return found


async def get_policy(session: AsyncSession,
                     rollout_id: uuid.UUID) -> ContentRolloutPolicyModel | None:
    try:
        return await session.get(ContentRolloutPolicyModel, rollout_id)
    except Exception:                                            # noqa: BLE001
        # 211 not applied yet: no policy, which is phase 1 exactly.
        return None


async def _tenant_uuid(session: AsyncSession, tenant: Any) -> uuid.UUID | None:
    from case_service.tenancy.identity import resolve_tenant
    resolved = await resolve_tenant(session, tenant if tenant not in (None, "") else "default")
    return resolved[0] if resolved else None


async def resolve(session: AsyncSession, *, case_type_id: uuid.UUID | str,
                  tenant: Any, subject: str | None,
                  data: dict[str, Any] | None = None) -> Resolution:
    """The case type a NEW case of `case_type_id` gets, for this tenant and subject.

    No rollout touching this id ⇒ the id itself, exactly as before.
    """
    try:
        current = uuid.UUID(str(case_type_id))
    except (TypeError, ValueError):
        return Resolution(case_type_id=case_type_id)          # type: ignore[arg-type]
    if not await _table_ready(session):
        return Resolution(case_type_id=current)
    tenant_uuid = await _tenant_uuid(session, tenant)
    if tenant_uuid is None or not await _tenant_has_rollouts(session, tenant_uuid):
        return Resolution(case_type_id=current)

    seen: set[uuid.UUID] = set()
    for _hop in range(MAX_HOPS):
        seen.add(current)
        rows = list((await session.execute(
            select(ContentRolloutModel).where(
                ContentRolloutModel.tenant_id == tenant_uuid,
                ContentRolloutModel.artifact_kind == "case_type",
                or_(ContentRolloutModel.baseline_id == current,
                    ContentRolloutModel.candidate_id == current))
            .order_by(ContentRolloutModel.created_at.desc()))).scalars().all())
        open_row = next((r for r in rows if r.state in OPEN_STATES), None)
        if open_row is not None:
            b = bucket(open_row.id, subject or str(uuid.uuid4()))
            use_candidate = open_row.state == "rolling" and b < int(open_row.percent or 0)
            reason = "percentage"
            if open_row.state in ("dormant", "rolling"):
                from case_service.rollouts import policy as _policy
                pol = await get_policy(session, open_row.id)
                forced = _policy.target(pol.targeting if pol else None,
                                        subject=subject, data=data)
                if forced is not None:
                    use_candidate = forced == "candidate"
                    reason = {"candidate": "pilot", "baseline": "excluded"}[forced]
                    if forced == "baseline" and _policy.norm(subject) not in set(
                            (pol.targeting or {}).get("exclude") or []):
                        reason = "not_targeted"
            return Resolution(
                case_type_id=open_row.candidate_id if use_candidate else open_row.baseline_id,
                rollout_id=open_row.id, arm="candidate" if use_candidate else "baseline",
                bucket=b, reason=reason)
        nxt = None
        for r in rows:                         # newest first
            if r.state == "promoted" and r.baseline_id == current:
                nxt = r.candidate_id
                break
            if r.state in ("rolled_back", "superseded") and r.candidate_id == current:
                nxt = r.baseline_id
                break
        if nxt is None or nxt in seen:
            return Resolution(case_type_id=current)
        current = nxt
    log.warning("rollout redirect chain for %s is longer than %d hops", case_type_id, MAX_HOPS)
    return Resolution(case_type_id=current)


async def resolve_case_type(session: AsyncSession, *, case_type: CaseTypeModel,
                            tenant: Any, subject: str | None,
                            data: dict[str, Any] | None = None,
                            ) -> tuple[CaseTypeModel, Resolution]:
    """`resolve()` for a caller that already loaded the case type row.

    Every case-creation path comes through here, so this is also where a
    default the tenant has FORKED becomes the fork (8k) — before any rollout,
    because a tenant's rollouts are recorded against its own rows.
    """
    from case_service.case_types.ownership import fork_for_use
    case_type = await fork_for_use(session, case_type, await _tenant_uuid(session, tenant))
    res = await resolve(session, case_type_id=case_type.id, tenant=tenant, subject=subject,
                        data=data)
    if res.case_type_id == case_type.id:
        return case_type, res
    chosen = await session.get(CaseTypeModel, res.case_type_id)
    if chosen is None or getattr(chosen, "is_deleted", False):
        # The rollout points at a version that is gone: the one asked for
        # still exists, so a case is still created — on it.
        log.warning("rollout %s points at missing case type %s", res.rollout_id, res.case_type_id)
        return case_type, Resolution(case_type_id=case_type.id)
    return chosen, res


async def record(session: AsyncSession, *, case_id: uuid.UUID, res: Resolution,
                 tenant: Any, via: str) -> None:
    """Which arm this case landed in. Nothing to record when no rollout decided."""
    if res.rollout_id is None:
        return
    tenant_uuid = await _tenant_uuid(session, tenant)
    session.add(ContentRolloutCaseModel(
        case_id=case_id, rollout_id=res.rollout_id, tenant_id=tenant_uuid,
        arm=res.arm or "baseline", bucket=int(res.bucket or 0), via=via[:40]))


# ── the lifecycle ────────────────────────────────────────────────────────────


#: Limitation 14 (phase 2): every change is also an outbound webhook event, so
#: whoever subscribed to the case type — or the operator's platform-wide
#: subscription — is told. Queued in the same transaction (the outbox).
ROLLOUT_EVENTS = ("rollout.requested", "rollout.approved", "rollout.percent_changed",
                  "rollout.activated", "rollout.deactivated", "rollout.switched",
                  "rollout.promoted", "rollout.rolled_back", "rollout.superseded",
                  "rollout.policy_changed", "rollout.guard_tripped", "rollout.rebased")


def _event(session: AsyncSession, row: ContentRolloutModel, action: str, *,
           actor: str | None, reason: str | None = None,
           from_state: str | None = None, from_percent: int | None = None) -> None:
    session.add(ContentRolloutEventModel(
        id=uuid.uuid4(), rollout_id=row.id, tenant_id=row.tenant_id, action=action,
        from_state=from_state, to_state=row.state, from_percent=from_percent,
        to_percent=row.percent, actor=actor, reason=reason, at=_utcnow()))
    from velaris_shared.db.models import OutboxEventModel
    event = f"rollout.{action}"
    if event in ROLLOUT_EVENTS:
        # Names and ids only: a reason is free text an operator typed.
        session.add(OutboxEventModel(event_type=event, case_type_id=row.baseline_id, payload={
            "event": event, "timestamp": _utcnow().isoformat(),
            "rollout_id": str(row.id), "tenant_id": str(row.tenant_id),
            "case_type": row.artifact_name, "baseline_id": str(row.baseline_id),
            "candidate_id": str(row.candidate_id), "strategy": row.strategy,
            "state": row.state, "percent": row.percent,
            "from_state": from_state, "from_percent": from_percent, "actor": actor}))


async def get(session: AsyncSession, rollout_id: uuid.UUID,
              tenant_uuid: uuid.UUID) -> ContentRolloutModel:
    row = await session.get(ContentRolloutModel, rollout_id)
    if row is None or row.tenant_id != tenant_uuid:
        raise LookupError("no such rollout")
    return row


async def open_for(session: AsyncSession, tenant_uuid: uuid.UUID, kind: str,
                   name: str) -> ContentRolloutModel | None:
    return (await session.execute(select(ContentRolloutModel).where(
        ContentRolloutModel.open_key == _open_key(tenant_uuid, kind, name)))).scalars().first()


async def _case_type_visible(session: AsyncSession, ct: CaseTypeModel | None,
                             tenant_uuid: uuid.UUID) -> bool:
    if ct is None or getattr(ct, "is_deleted", False):
        return False
    if ct.tenant_id is None:
        return True
    from case_service.tenancy.identity import same_tenant
    return await same_tenant(session, ct.tenant_id, str(tenant_uuid))


async def start(session: AsyncSession, *, tenant_uuid: uuid.UUID, kind: str,
                baseline_id: uuid.UUID, candidate_id: uuid.UUID, strategy: str,
                percent: int = 0, actor: str | None, source: str = "local",
                request: dict[str, Any] | None = None, note: str | None = None,
                ) -> ContentRolloutModel:
    """Request a rollout. It has NO effect until somebody else approves it."""
    if kind in NOT_ROLLABLE:
        raise RolloutError(f"a {kind} cannot be rolled out on its own: {NOT_ROLLABLE[kind]}")
    if kind not in KINDS:
        raise RolloutError(f"unknown artifact kind {kind!r}")
    if strategy not in STRATEGIES:
        raise RolloutError(f"strategy must be one of {', '.join(STRATEGIES)}")
    percent = _check_percent(strategy, percent)
    if baseline_id == candidate_id:
        raise RolloutError("the baseline and the candidate are the same version")
    base = await session.get(CaseTypeModel, baseline_id)
    cand = await session.get(CaseTypeModel, candidate_id)
    if not await _case_type_visible(session, base, tenant_uuid) or \
            not await _case_type_visible(session, cand, tenant_uuid):
        raise LookupError("no such case type")
    if base.name != cand.name:
        raise RolloutError(
            f"a rollout moves customers between two versions of ONE case type; "
            f"{base.name!r} and {cand.name!r} are different case types")

    existing = await open_for(session, tenant_uuid, kind, base.name)
    if existing is not None:
        raise RolloutLocked(existing)

    row = ContentRolloutModel(
        id=uuid.uuid4(), tenant_id=tenant_uuid, artifact_kind=kind,
        artifact_name=base.name, baseline_id=baseline_id, candidate_id=candidate_id,
        strategy=strategy, percent=percent, state="requested",
        open_key=_open_key(tenant_uuid, kind, base.name), source=source,
        request=request, requested_by=actor, note=note,
        created_at=_utcnow(), updated_at=_utcnow())
    try:
        async with session.begin_nested():
            session.add(row)
            await session.flush()
    except IntegrityError:
        # Lost a race with another request for the same name: the index is the
        # lock, and it has just done its job.
        other = await open_for(session, tenant_uuid, kind, base.name)
        if other is not None:
            raise RolloutLocked(other) from None
        raise
    _event(session, row, "requested", actor=actor, reason=note)
    await session.flush()
    forget_cache(tenant_uuid)
    return row


def _check_percent(strategy: str, percent: int | None) -> int:
    p = int(percent or 0)
    if not 0 <= p <= 100:
        raise RolloutError("percent is 0 to 100")
    if strategy in ("flag", "blue_green") and p not in (0, 100):
        raise RolloutError(f"a {strategy} rollout is either off (0) or fully on (100)")
    return p


def _same_actor(a: str | None, b: str | None) -> bool:
    from case_service.hxdeploy.service import _same_actor as same
    return same(a, b)


async def approve(session: AsyncSession, row: ContentRolloutModel, *, actor: str | None,
                  strategy: str | None = None, percent: int | None = None,
                  reason: str | None = None) -> ContentRolloutModel:
    """The operator's decision — the requested strategy may be overridden here."""
    if row.state != "requested":
        raise RolloutError(f"this rollout is {row.state}, not awaiting approval")
    if _same_actor(row.requested_by, actor):
        raise PermissionError(
            "A rollout must be approved by somebody other than the person who "
            "requested it.")
    before = (row.state, row.percent)
    if strategy is not None:
        if strategy not in STRATEGIES:
            raise RolloutError(f"strategy must be one of {', '.join(STRATEGIES)}")
        row.strategy = strategy
    row.percent = _check_percent(row.strategy, row.percent if percent is None else percent)
    row.approved_by = actor
    row.approved_at = _utcnow()
    if row.strategy == "immediate":
        row.state = "rolling"
        row.percent = 100
        _event(session, row, "approved", actor=actor, reason=reason,
               from_state=before[0], from_percent=before[1])
        return await promote(session, row, actor=actor, reason="immediate strategy")
    row.state = "rolling" if row.percent > 0 or row.strategy in ("percentage", "blue_green") \
        else "dormant"
    row.updated_at = _utcnow()
    _event(session, row, "approved", actor=actor, reason=reason,
           from_state=before[0], from_percent=before[1])
    await session.flush()
    return row


async def set_percent(session: AsyncSession, row: ContentRolloutModel, *, percent: int,
                      actor: str | None, reason: str | None = None) -> ContentRolloutModel:
    """Raise, lower, switch on/off, or flip blue-green. New cases only."""
    if row.state not in ("dormant", "rolling"):
        raise RolloutError(f"this rollout is {row.state}; only an approved, open "
                           f"rollout can be changed")
    before = (row.state, row.percent)
    row.percent = _check_percent(row.strategy, percent)
    if row.strategy == "flag":
        row.state = "rolling" if row.percent else "dormant"
    row.updated_at = _utcnow()
    action = {"flag": "activated" if row.percent else "deactivated",
              "blue_green": "switched"}.get(row.strategy, "percent_changed")
    _event(session, row, action, actor=actor, reason=reason,
           from_state=before[0], from_percent=before[1])
    await session.flush()
    return row


async def _close(session: AsyncSession, row: ContentRolloutModel, state: str, *,
                 actor: str | None, reason: str | None, action: str) -> ContentRolloutModel:
    if row.state not in OPEN_STATES:
        raise RolloutError(f"this rollout is already {row.state}")
    before = (row.state, row.percent)
    row.state = state
    row.open_key = None
    row.closed_by = actor
    row.closed_at = _utcnow()
    row.updated_at = _utcnow()
    if state == "promoted":
        row.percent = 100
    elif state in ("rolled_back", "superseded"):
        row.percent = 0
    _event(session, row, action, actor=actor, reason=reason,
           from_state=before[0], from_percent=before[1])
    await session.flush()
    return row


async def promote(session: AsyncSession, row: ContentRolloutModel, *, actor: str | None,
                  reason: str | None = None) -> ContentRolloutModel:
    """The candidate becomes the baseline for every new case, for good."""
    if row.state == "requested":
        raise RolloutError("a rollout nobody has approved cannot be promoted; approve it first")
    return await _close(session, row, "promoted", actor=actor, reason=reason, action="promoted")


async def rollback(session: AsyncSession, row: ContentRolloutModel, *, actor: str | None,
                   reason: str | None = None) -> ContentRolloutModel:
    """Every new case goes back to the baseline. In-flight cases are untouched."""
    return await _close(session, row, "rolled_back", actor=actor, reason=reason,
                        action="rolled_back")


async def supersede(session: AsyncSession, row: ContentRolloutModel, *,
                    candidate_id: uuid.UUID, strategy: str, percent: int = 0,
                    actor: str | None, reason: str | None = None) -> ContentRolloutModel:
    """Roll this one back and request a new candidate against the SAME baseline."""
    baseline = row.baseline_id
    await _close(session, row, "superseded", actor=actor, reason=reason, action="superseded")
    new = await start(session, tenant_uuid=row.tenant_id, kind=row.artifact_kind,
                      baseline_id=baseline, candidate_id=candidate_id, strategy=strategy,
                      percent=percent, actor=actor, note=reason)
    row.superseded_by = new.id
    await session.flush()
    return new


# ── reading ──────────────────────────────────────────────────────────────────


def to_json(r: ContentRolloutModel) -> dict[str, Any]:
    from case_service.core.timestamps import iso_utc
    return {
        "id": str(r.id), "artifact_kind": r.artifact_kind, "artifact_name": r.artifact_name,
        "baseline_id": str(r.baseline_id), "candidate_id": str(r.candidate_id),
        "strategy": r.strategy, "percent": r.percent, "state": r.state,
        "open": r.state in OPEN_STATES, "source": r.source, "request": r.request,
        "requested_by": r.requested_by, "approved_by": r.approved_by,
        "closed_by": r.closed_by,
        "superseded_by": str(r.superseded_by) if r.superseded_by else None,
        "note": r.note, "created_at": iso_utc(r.created_at),
        "approved_at": iso_utc(r.approved_at), "closed_at": iso_utc(r.closed_at),
    }


async def arm_counts(session: AsyncSession, rollout_id: uuid.UUID) -> dict[str, int]:
    rows = (await session.execute(
        select(ContentRolloutCaseModel.arm, func.count())
        .where(ContentRolloutCaseModel.rollout_id == rollout_id)
        .group_by(ContentRolloutCaseModel.arm))).all()
    out = {"baseline": 0, "candidate": 0}
    out.update({a: int(n) for a, n in rows})
    return out


async def events(session: AsyncSession, rollout_id: uuid.UUID) -> list[dict[str, Any]]:
    from case_service.core.timestamps import iso_utc
    rows = (await session.execute(
        select(ContentRolloutEventModel).where(ContentRolloutEventModel.rollout_id == rollout_id)
        .order_by(ContentRolloutEventModel.at))).scalars().all()
    return [{"action": e.action, "from_state": e.from_state, "to_state": e.to_state,
             "from_percent": e.from_percent, "to_percent": e.to_percent,
             "actor": e.actor, "reason": e.reason, "at": iso_utc(e.at)} for e in rows]


# ── phase 2–3: policy, rebase, version status ────────────────────────────────


async def set_policy(session: AsyncSession, row: ContentRolloutModel, *,
                     shadow: bool | None, targeting: Any, ramp: Any, guard: Any,
                     actor: str | None) -> ContentRolloutPolicyModel:
    """Set the rollout's optional policy. Only while it is open."""
    from case_service.rollouts import policy as _policy

    if row.state not in OPEN_STATES:
        raise RolloutError(f"this rollout is {row.state}; a closed rollout's policy is history")
    try:
        t = _policy.validate_targeting(targeting)
        r = _policy.validate_ramp(ramp, row.strategy)
        g = _policy.validate_guard(guard)
    except _policy.PolicyError as exc:
        raise RolloutError(str(exc)) from exc
    pol = await get_policy(session, row.id)
    if pol is None:
        pol = ContentRolloutPolicyModel(rollout_id=row.id, tenant_id=row.tenant_id)
        session.add(pol)
    if shadow is not None:
        pol.shadow = bool(shadow)
    pol.targeting, pol.guard = t, g
    if r != pol.ramp:
        pol.next_step_at = None            # the sweep schedules the first step
    pol.ramp = r
    if g is None or pol.guard_state == "held":
        # Changing the guard after it held the ramp is how an operator says
        # "I have looked, carry on".
        pol.guard_state, pol.guard_detail = "ok", None
    pol.updated_by, pol.updated_at = actor, _utcnow()
    _event(session, row, "policy_changed", actor=actor,
           reason=f"shadow={pol.shadow} targeting={'on' if t else 'off'} "
                  f"ramp={'on' if r else 'off'} guard={'on' if g else 'off'}",
           from_state=row.state, from_percent=row.percent)
    await session.flush()
    return pol


def policy_json(pol: ContentRolloutPolicyModel | None) -> dict[str, Any] | None:
    if pol is None:
        return None
    from case_service.core.timestamps import iso_utc
    return {"shadow": pol.shadow, "targeting": pol.targeting, "ramp": pol.ramp,
            "guard": pol.guard, "guard_state": pol.guard_state,
            "guard_detail": pol.guard_detail, "next_step_at": iso_utc(pol.next_step_at),
            "updated_by": pol.updated_by, "updated_at": iso_utc(pol.updated_at)}


async def rebase(session: AsyncSession, row: ContentRolloutModel, *, actor: str | None,
                 reason: str, dry_run: bool = True, limit: int = 500) -> dict[str, Any]:
    """Move OPEN cases off a withdrawn candidate, back to the baseline.

    Limitation 7 (phase 2). Pinning is the default and stays the default: this
    is a deliberate clean-up after a rollback, never part of one. A case moves
    only when nothing it has done is lost — its current stage, and every stage
    it completed steps in, must exist in the baseline with the same ids. Anything
    else is listed and left alone. A dry run (the default) changes nothing.
    SLAs already started keep running as started.
    """
    from velaris_shared.db.models import (
        CaseAuditLogModel, CaseInstanceModel, CaseStepCompletionModel,
    )

    if row.state not in ("rolled_back", "superseded"):
        raise RolloutError("rebase is the clean-up after a rollback; roll this rollout "
                           "back (or supersede it) first")
    base = await session.get(CaseTypeModel, row.baseline_id)
    if base is None:
        raise RolloutError("the baseline case type no longer exists")
    stages = {str(s.get("id")): {str(st.get("id")) for st in (s.get("steps") or [])}
              for s in ((base.definition_json or {}).get("stages") or []) if s.get("id")}
    cases = list((await session.execute(
        select(CaseInstanceModel)
        .join(ContentRolloutCaseModel, ContentRolloutCaseModel.case_id == CaseInstanceModel.id)
        .where(ContentRolloutCaseModel.rollout_id == row.id,
               ContentRolloutCaseModel.arm == "candidate",
               CaseInstanceModel.case_type_id == row.candidate_id,
               CaseInstanceModel.resolved_at.is_(None),
               CaseInstanceModel.closed_at.is_(None))
        .order_by(CaseInstanceModel.created_at).limit(limit))).scalars().all())
    movable, blocked = [], []
    for case in cases:
        done = (await session.execute(
            select(CaseStepCompletionModel.stage_id, CaseStepCompletionModel.step_id)
            .where(CaseStepCompletionModel.case_id == case.id))).all()
        problems = []
        if case.current_stage_id and str(case.current_stage_id) not in stages:
            problems.append(f"its stage {case.current_stage_id!r} is not in the baseline")
        for stage_id, step_id in done:
            if str(stage_id) not in stages or (stages[str(stage_id)] and
                                               str(step_id) not in stages[str(stage_id)]):
                problems.append(f"completed step {step_id!r} in stage {stage_id!r} is not "
                                f"in the baseline")
                break
        (blocked if problems else movable).append(
            {"case_id": str(case.id), "case_number": case.case_number, "problems": problems})
        if not problems and not dry_run:
            case.case_type_id = base.id
            case.case_type_version = base.version
            session.add(CaseAuditLogModel(
                id=uuid.uuid4(), case_id=case.id, action="rollout_rebased",
                actor_id=(actor or "unknown")[:255], actor_type="user",
                details={"rollout_id": str(row.id), "from": str(row.candidate_id),
                         "to": str(base.id), "reason": reason[:500]},
                timestamp=_utcnow()))
    if not dry_run and movable:
        _event(session, row, "rebased", actor=actor,
               reason=f"{len(movable)} open case(s) moved to the baseline: {reason}",
               from_state=row.state, from_percent=row.percent)
        await session.flush()
    return {"dry_run": dry_run, "movable": movable, "blocked": blocked,
            "note": "SLAs already started on a moved case keep running as started."}


async def version_status(session: AsyncSession, tenant_uuid: uuid.UUID) -> list[dict[str, Any]]:
    """Limitation 12 (phase 2): for every case type version a rollout touched,
    where a request for it now goes — so a designer picking an old version in
    Studio can see it is retired, withdrawn, or in a rollout."""
    rows = list((await session.execute(
        select(ContentRolloutModel).where(ContentRolloutModel.tenant_id == tenant_uuid)
        .order_by(ContentRolloutModel.created_at.desc()))).scalars().all())
    seen: dict[str, dict[str, Any]] = {}
    for r in rows:
        if r.state in OPEN_STATES:
            for cid, role in ((r.baseline_id, "baseline"), (r.candidate_id, "candidate")):
                seen.setdefault(str(cid), {"status": f"in rollout ({role})",
                                           "rollout_id": str(r.id)})
        elif r.state == "promoted":
            seen.setdefault(str(r.baseline_id), {"status": "retired",
                                                 "rollout_id": str(r.id)})
        else:
            seen.setdefault(str(r.candidate_id), {"status": "withdrawn",
                                                  "rollout_id": str(r.id)})
    out = []
    for cid, info in seen.items():
        ct = await session.get(CaseTypeModel, uuid.UUID(cid))
        res = await resolve(session, case_type_id=cid, tenant=tenant_uuid, subject="-")
        out.append({"case_type_id": cid, "name": ct.name if ct else None,
                    "version": ct.version if ct else None, **info,
                    "new_cases_go_to": None if info["status"].startswith("in rollout")
                    else str(res.case_type_id)})
    return out
