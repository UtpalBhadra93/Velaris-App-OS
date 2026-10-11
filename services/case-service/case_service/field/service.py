"""Field nodes: register, dispatch, sync, reconcile, shred.

Design: docs/Future/Done/AI-ERA-ROADMAP.md §F-10. This is the HUB half — the side
that must be right, because a node cannot be trusted to enforce anything about
itself.

**A field node is exactly as untrusted as an F-09 counterparty**, and is treated
with the same machinery: a pinned key, a data-class ceiling, and a projection
computed rather than filtered. The difference is only the shape of the grant — a
counterparty gets one case for hours, a node gets a charter for days.

Four rules hold throughout.

**A stolen node cannot contain what it was never given.** The charter is the
security boundary, and it is enforced twice: at dispatch (only charter cases are
sent) and at sync (an event about a case outside the charter is `rejected`, not
merged). The second check is the one that matters — the first only decides what
a well-behaved node holds.

**Fail closed on staleness.** A node whose policy snapshot has aged past
`snapshot_max_age_hours` must stop acting. The hub records `stale_after` on the
dispatch and refuses to merge work a node did after it, because a node that
ignored its own lockout is precisely the node whose work should not be trusted.

**The node's clock decides nothing.** `node_time` is recorded because it is what
a field worker will describe, and it is never used to order field work against
hub work. A disconnected clock is the thing that drifts.

**Nothing sensitive merges itself.** A conflict on a field whose data class is
personal goes to a person, always — there is no precedence rule that silently
overwrites somebody's contact details with a value from a device that has been
out of contact for three days.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import hashlib
import json
import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from velaris_shared.db.models import (
    CaseInstanceModel,
    FieldDispatchModel,
    FieldNodeModel,
    FieldSyncEventModel,
)

from case_service.field import subchain
from case_service.handoff import projection as proj
from case_service.handoff import service as handoff

log = logging.getLogger(__name__)

#: What a node may record. Deliberately small: a node may say what a worker did
#: to a CASE, never what an administrator did to the platform. There is no
#: `policy_set`, no `user_create`, no `grant`.
EVENT_KINDS: tuple[str, ...] = ("variable_set", "note", "step_complete")

#: Namespaces a node must never act in while disconnected. Sent with every
#: dispatch. `export` first, because exfiltration from a device nobody can see
#: is the thing a disconnected period most needs to forbid.
DEFAULT_OFFLINE_DENY: tuple[str, ...] = ("export", "vault", "auth")

DEFAULT_STALE_HOURS = 72
MAX_CHARTER_CASES = 500

RESOLUTIONS = ("keep_field", "keep_hub")

#: Oct item 8. When on, a sync must carry the node's signature over its chain
#: head. OFF by default — as released, the signature was optional, and a hub
#: that started refusing unsigned syncs on upgrade would strand every node out
#: in the field with work it could not hand back. The shipped node always
#: signs, so an operator can turn this on once their nodes run it.
REQUIRE_SIGNATURE_ENV = "VELARIS_FIELD_REQUIRE_SIGNATURE"


def signature_required() -> bool:
    import os
    raw = os.environ.get(REQUIRE_SIGNATURE_ENV, "")
    return str(raw).strip().lower() in ("1", "true", "yes", "on")


class FieldError(ValueError):
    """The field operation cannot proceed as asked. The message is for a human."""


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _sha(payload: Any) -> str:
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":"),
                   default=str).encode()).hexdigest()


# ── the registry ─────────────────────────────────────────────────────────────


async def register(
    session: AsyncSession, *, tenant_uuid: uuid.UUID, slug: str,
    display_name: str, public_key: str, charter: dict[str, Any],
    ceiling: str = proj.CEILING_INTERNAL,
    snapshot_max_age_hours: int = DEFAULT_STALE_HOURS,
    created_by: str | None = None,
) -> FieldNodeModel:
    """Enrol a node. Its key is pinned, exactly as a counterparty's is."""
    proj.rank(ceiling)
    fingerprint = handoff.key_fingerprint(public_key)
    if not isinstance(charter, dict) or not (
            charter.get("case_type_ids") or charter.get("case_ids")):
        raise FieldError(
            "a node needs a charter naming case types or cases; a node with an "
            "open charter is a node that may hold anything, which is the "
            "opposite of what a charter is for")
    existing = (await session.execute(
        select(FieldNodeModel).where(FieldNodeModel.tenant_id == tenant_uuid,
                                     FieldNodeModel.slug == slug))).scalars().first()
    if existing is not None:
        raise FieldError(f"a node {slug!r} is already registered")
    node = FieldNodeModel(
        id=uuid.uuid4(), tenant_id=tenant_uuid, slug=slug,
        display_name=display_name, public_key=public_key, key_id=fingerprint,
        status="active", charter=charter, ceiling=ceiling,
        snapshot_max_age_hours=max(1, int(snapshot_max_age_hours)),
        created_by=created_by)
    session.add(node)
    await session.flush()
    return node


async def charter_cases(session: AsyncSession, node: FieldNodeModel
                        ) -> list[CaseInstanceModel]:
    """The cases this node's charter admits. The boundary, resolved.

    An explicit `case_ids` list wins over `case_type_ids` when both are given —
    naming cases is the narrower statement, and the narrower one is the one an
    operator meant.
    """
    charter = node.charter or {}
    stmt = select(CaseInstanceModel).where(
        CaseInstanceModel.tenant_id == node.tenant_id)
    case_ids = [c for c in (charter.get("case_ids") or []) if c]
    if case_ids:
        stmt = stmt.where(CaseInstanceModel.id.in_([uuid.UUID(str(c)) for c in case_ids]))
    else:
        type_ids = [uuid.UUID(str(t)) for t in (charter.get("case_type_ids") or []) if t]
        if not type_ids:
            return []
        stmt = stmt.where(CaseInstanceModel.case_type_id.in_(type_ids))
    cap = min(int(charter.get("max_cases") or MAX_CHARTER_CASES), MAX_CHARTER_CASES)
    return list((await session.execute(stmt.limit(cap))).scalars().all())


def in_charter(node: FieldNodeModel, case: CaseInstanceModel) -> bool:
    """Enforced again at SYNC, which is the check that matters.

    The dispatch check only decides what a well-behaved node holds. This one
    decides what the hub will accept back, and a node that sends work about a
    case it was never given is exactly the node whose word must not be taken.
    """
    charter = node.charter or {}
    case_ids = {str(c) for c in (charter.get("case_ids") or [])}
    if case_ids:
        return str(case.id) in case_ids
    type_ids = {str(t) for t in (charter.get("case_type_ids") or [])}
    return str(case.case_type_id) in type_ids


# ── dispatch ─────────────────────────────────────────────────────────────────


async def dispatch(
    session: AsyncSession, *, node: FieldNodeModel, tenant_token: str,
    policy_snapshot: dict[str, Any] | None = None,
    offline_deny: list[str] | None = None,
    created_by: str | None = None,
) -> tuple[FieldDispatchModel, dict[str, Any]]:
    """Build the bundle a node takes away. Returns `(row, bundle)`.

    The bundle is what leaves; the row is what the hub keeps so that "what was
    on that device" has an answer after the device does not.
    """
    if node.status != "active":
        raise FieldError(f"this node is {node.status}")

    cases = await charter_cases(session, node)
    projected: list[dict[str, Any]] = []
    record: dict[str, Any] = {}
    for case in cases:
        view = await proj.project(session, case=case, ceiling=node.ceiling,
                                  tenant_token=tenant_token)
        projected.append(view["case"])
        # NAMES only. The values are on the cases; this row is read by anybody
        # who may read the node's history.
        record[str(case.id)] = {"disclosed": view["disclosed"],
                                "withheld": [w["field"] for w in view["withheld"]]}

    snapshot = policy_snapshot if policy_snapshot is not None else \
        await _policy_snapshot(session)
    deny = sorted({str(d) for d in (offline_deny or DEFAULT_OFFLINE_DENY)})
    genesis = _sha({"node": str(node.id), "at": _utcnow().isoformat(),
                    "cases": sorted(str(c.id) for c in cases)})

    # A previous dispatch that was never synced is SUPERSEDED, not left open:
    # two live dispatches to one node means two sub-chains and no way to say
    # which one the node is continuing.
    for old in (await session.execute(
        select(FieldDispatchModel).where(
            FieldDispatchModel.node_id == node.id,
            FieldDispatchModel.status == "issued"))).scalars().all():
        old.status = "superseded"

    row = FieldDispatchModel(
        id=uuid.uuid4(), node_id=node.id, tenant_id=node.tenant_id,
        case_ids=[str(c.id) for c in cases], projection=record,
        policy_snapshot=snapshot, policy_snapshot_hash=_sha(snapshot),
        offline_deny=deny, subchain_genesis=genesis, status="issued",
        stale_after=_utcnow() + timedelta(hours=int(node.snapshot_max_age_hours)),
        created_by=created_by)
    session.add(row)
    await session.flush()

    bundle = {
        "dispatch_id": str(row.id),
        "node": {"slug": node.slug, "ceiling": node.ceiling},
        "cases": projected,
        "policy_snapshot": snapshot,
        "policy_snapshot_hash": row.policy_snapshot_hash,
        "offline_deny": deny,
        "subchain_genesis": genesis,
        "stale_after": row.stale_after.isoformat(),
        "note": ("Stop acting on this snapshot after `stale_after`. Work "
                 "recorded later than that is refused at sync."),
    }
    return row, bundle


async def _policy_snapshot(session: AsyncSession) -> dict[str, Any]:
    """The HxArbiter namespaces as they stand, pinned for the trip.

    Modes and default effects only — not the policy bodies. A node evaluates
    little and refuses much; what it needs is to know which namespaces are
    enforcing and which way they fail.
    """
    try:
        from velaris_shared.db.models import ArbiterNamespaceModel

        rows = (await session.execute(
            select(ArbiterNamespaceModel.namespace,
                   ArbiterNamespaceModel.default_effect,
                   ArbiterNamespaceModel.max_mode))).all()
        return {"namespaces": sorted(
            [{"namespace": n, "default_effect": e, "max_mode": m} for n, e, m in rows],
            key=lambda d: d["namespace"])}
    except Exception as exc:                                     # noqa: BLE001
        log.info("field: policy snapshot unavailable: %s", exc)
        return {"namespaces": []}


# ── sync ─────────────────────────────────────────────────────────────────────


async def sync(
    session: AsyncSession, *, node: FieldNodeModel, dispatch_row: FieldDispatchModel,
    events: list[dict[str, Any]], signature: str | None = None,
) -> dict[str, Any]:
    """Take a node's stream back: verify, then merge event by event.

    Verification is ALL OR NOTHING and comes first. A stream whose chain does
    not hold is not partially trustworthy — the point of the chain is that a
    single altered event invalidates everything after it, and merging the part
    before the break would be accepting exactly the events an attacker chose to
    leave alone.
    """
    if node.status == "shredded":
        raise FieldError("this node has been shredded and may not sync")
    if dispatch_row.status != "issued":
        # Found live. Without this a node can hand the SAME stream back twice
        # and the hub records it twice — every note duplicated, every conflict
        # queued a second time for a person who already decided it. A dispatch
        # is synced once; a node with more work needs a new one.
        raise FieldError(
            f"this dispatch is {dispatch_row.status}, not outstanding. A "
            f"dispatch is synced once — issue a new one for further work.")

    ok, head, reason = subchain.verify(events, dispatch_row.subchain_genesis)
    if not ok:
        # Recorded on the node, not raised into nothing: a broken sub-chain is
        # the single most important thing this feature can detect.
        node.last_contact_at = _utcnow()
        await session.flush()
        raise FieldError(f"the node's log does not verify: {reason}")

    if signature is None and signature_required():
        raise FieldError(
            "this hub requires every sync to be signed by the node's pinned key "
            f"({REQUIRE_SIGNATURE_ENV}); a staff token alone cannot hand work back")
    if signature is not None:
        verified, detail = handoff.verify_signature(node, head, signature)
        if not verified:
            raise FieldError(f"the sync is not signed by the node's pinned key: {detail}")

    stale_after = _aware(dispatch_row.stale_after)
    applied = conflicts = rejected = 0
    for raw in events:
        state, why = await _merge_one(session, node=node, dispatch_row=dispatch_row,
                                      event=raw, stale_after=stale_after)
        if state == "applied":
            applied += 1
        elif state == "conflict":
            conflicts += 1
        else:
            rejected += 1
        session.add(FieldSyncEventModel(
            id=uuid.uuid4(), node_id=node.id, dispatch_id=dispatch_row.id,
            tenant_id=node.tenant_id,
            case_id=uuid.UUID(str(raw["case_id"])) if raw.get("case_id") else None,
            seq=int(raw.get("seq") or 0), kind=str(raw.get("kind")),
            target=raw.get("target"), payload=raw.get("payload"),
            payload_sha256=str(raw.get("payload_sha256") or ""),
            node_time=_aware(_parse_time(raw.get("node_time"))),
            prev_hash=str(raw.get("prev_hash")),
            content_hash=str(raw.get("content_hash")),
            merge_state=state, conflict_reason=why))

    node.subchain_head = head
    node.last_contact_at = _utcnow()
    dispatch_row.status = "synced"
    dispatch_row.synced_at = _utcnow()

    # The shred order rides home on the next contact, which is the only moment
    # the hub gets to give it.
    shred = node.status == "shred_ordered"
    if shred:
        node.status = "shredded"
        node.shredded_at = _utcnow()
    await session.flush()

    return {
        "verified": True, "head": head,
        "applied": applied, "conflicts": conflicts, "rejected": rejected,
        "shred_ordered": shred,
        "note": ("Wipe the node now." if shred else
                 "Conflicts are queued for a person; nothing sensitive merged "
                 "itself." if conflicts else "Converged."),
    }


def _parse_time(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except Exception:                                            # noqa: BLE001
        return None


async def _merge_one(
    session: AsyncSession, *, node: FieldNodeModel,
    dispatch_row: FieldDispatchModel, event: dict[str, Any],
    stale_after: datetime | None,
) -> tuple[str, str | None]:
    """Merge one event. Returns `(merge_state, reason)`.

    The order of these checks is the design. Charter first, because a node
    sending work about a case it was never given is the strongest signal there
    is; staleness second, because work done after a node should have stopped is
    work done under rules nobody could refresh; and only then the actual
    conflict question.
    """
    kind = str(event.get("kind") or "")
    if kind not in EVENT_KINDS:
        return "rejected", (
            f"{kind!r} is not something a node may record; a node reports what a "
            f"worker did to a case, never what an administrator did to the platform")

    case_id = event.get("case_id")
    case = await session.get(CaseInstanceModel, uuid.UUID(str(case_id))) if case_id else None
    if case is None:
        return "rejected", "the case this event is about does not exist here"
    if not in_charter(node, case):
        return "rejected", (
            "this case is outside the node's charter — it was never dispatched, "
            "so work about it cannot be accepted back")

    node_time = _aware(_parse_time(event.get("node_time")))
    if stale_after is not None and node_time is not None and node_time > stale_after:
        return "rejected", (
            "recorded after the node's policy snapshot went stale; a node that "
            "ignored its own lockout is the node whose work must not be trusted")

    if kind in ("note", "step_complete"):
        # Additive by nature. Two people adding notes is not a conflict, and
        # inventing one would send them to a queue for no reason.
        return "applied", None

    target = str(event.get("target") or "")
    if not target:
        return "rejected", "a variable_set with no target changes nothing"

    data = case.data if isinstance(case.data, dict) else {}
    hub_value = data.get(target)
    field_value = (event.get("payload") or {}).get("value")
    if hub_value == field_value:
        return "applied", None
    if target not in data:
        # The hub has no opinion, so there is nothing to conflict with.
        case.data = {**data, target: field_value}
        return "applied", None

    # A real disagreement. Whether it may be decided without a person depends
    # entirely on what the field is.
    category, special = proj.classify(target, {})
    if special or category:
        return "conflict", (
            f"{target!r} carries {category or 'personal'} data and the hub and the "
            f"node disagree. Nothing sensitive merges itself: a person decides "
            f"whether the field value or the hub's is right.")
    # Deterministic, and stated rather than implied: the HUB wins on ordinary
    # fields. Somebody on the hub has been able to see the whole picture the
    # whole time; the node has been guessing from a snapshot.
    return "conflict", (
        f"{target!r} was changed on both sides. The hub's value stands unless a "
        f"person chooses the field's.")


async def resolve(
    session: AsyncSession, *, event: FieldSyncEventModel, resolution: str,
    actor: str,
) -> FieldSyncEventModel:
    """A person decides one conflict. The only way a conflict ever clears."""
    if event.merge_state != "conflict":
        raise FieldError(f"this event is {event.merge_state}, not a conflict")
    if resolution not in RESOLUTIONS:
        raise FieldError(f"resolution must be one of {', '.join(RESOLUTIONS)}")

    if resolution == "keep_field" and event.kind == "variable_set" and event.target:
        case = await session.get(CaseInstanceModel, event.case_id)
        if case is None:
            raise FieldError("the case no longer exists")
        data = case.data if isinstance(case.data, dict) else {}
        case.data = {**data, event.target: (event.payload or {}).get("value")}

    event.merge_state = "applied"
    event.resolution = resolution
    event.resolved_by = actor
    event.resolved_at = _utcnow()
    await session.flush()
    return event


# ── losing a device ──────────────────────────────────────────────────────────


async def order_shred(session: AsyncSession, *, node: FieldNodeModel,
                      reason: str) -> FieldNodeModel:
    """Wipe it at next contact — and lock it out in the meantime.

    Two mechanisms, because they cover different disasters. The shred order
    reaches a node that comes back; the staleness lockout handles the one that
    never does. Relying on either alone leaves a real case uncovered.
    """
    if node.status == "shredded":
        return node
    node.status = "shred_ordered"
    node.shred_ordered_at = _utcnow()
    node.shred_reason = reason
    await session.flush()
    return node


def contact_message(node: FieldNodeModel, at: str) -> str:
    """What a node signs to say "this is me, now". The slug and key id are in
    it so a signature for one node, or for a rotated key, says nothing here."""
    return f"velaris-field-contact:{node.slug}:{node.key_id}:{at}"


#: How far a signed contact's own timestamp may be from the hub's clock. It
#: bounds replay of a captured contact, and it is generous because a device
#: that has been in a field for three days has a clock that has drifted.
CONTACT_SKEW = timedelta(minutes=15)


async def contact(session: AsyncSession, *, node: FieldNodeModel,
                  at: str | None = None, signature: str | None = None,
                  ) -> dict[str, Any]:
    """A node checking in, before anything else it does on a connection.

    Oct item 8. The shred order used to reach a node only inside a SUCCESSFUL
    sync — so a node whose stream was refused (stale, broken, already synced)
    was never told to wipe, and kept its data. Now every contact carries it.

    Signed or not follows the sync rule: optional unless the hub requires it.
    A signed contact is checked against the pinned key and the clock, so a
    captured one cannot be replayed later.
    """
    if signature is not None or signature_required():
        if not signature or not at:
            raise FieldError("this hub requires a signed contact from the node's pinned key")
        when = _aware(_parse_time(at))
        if when is None or abs(_utcnow() - when) > CONTACT_SKEW:
            raise FieldError("the contact's timestamp is not current; check the node's clock")
        verified, _detail = handoff.verify_signature(node, contact_message(node, at), signature)
        if not verified:
            raise FieldError("the contact is not signed by the node's pinned key")

    node.last_contact_at = _utcnow()
    wipe = node.status in ("shred_ordered", "shredded")
    if node.status == "shred_ordered" and signature is not None:
        # Recorded as executed only when the DEVICE asked — a signature by its
        # pinned key. An unsigned contact is any dispatcher's token; it is told
        # to wipe, but the hub does not claim a device wiped on its word.
        node.status = "shredded"
        node.shredded_at = _utcnow()
    latest = (await session.execute(
        select(FieldDispatchModel).where(FieldDispatchModel.node_id == node.id,
                                         FieldDispatchModel.status == "issued")
        .order_by(FieldDispatchModel.issued_at.desc()).limit(1))).scalars().first()
    await session.flush()
    return {"status": node.status, "wipe": wipe, "signed": signature is not None,
            "outstanding_dispatch": str(latest.id) if latest else None,
            "staleness": staleness(node, latest),
            "note": "Wipe the node now." if wipe else "Contact recorded."}


def staleness(node: FieldNodeModel, dispatch_row: FieldDispatchModel | None,
              now: datetime | None = None) -> dict[str, Any]:
    """Whether this node should still be acting on what it holds."""
    moment = now or _utcnow()
    stale_after = _aware(getattr(dispatch_row, "stale_after", None))
    if stale_after is None:
        return {"stale": False, "reason": "no dispatch outstanding"}
    if moment > stale_after:
        return {"stale": True, "stale_after": stale_after.isoformat(),
                "reason": ("the node's policy snapshot is older than this node's "
                           "limit; it must fail closed until it reconnects")}
    return {"stale": False, "stale_after": stale_after.isoformat()}
