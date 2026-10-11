"""A join link for the next rep on a live contact — Oct item 1.

A warm transfer moved the RECORD: a new interaction for the receiving rep,
carrying the caller's verification tier. Nothing let that rep open it. Here rep
A creates a link for a named rep, or for a queue, and shares it however the
floor shares things; rep B opens it into the same contact and the same 360.

Settled while designing (the engineering choices the item left open):

* **A rep-desk link, not an HxMeet invite.** HxMeet's invites exist to admit a
  GUEST with no account; this one must never admit anybody without one. Reusing
  that machinery would mean carrying its guest path and switching it off.
* **The link is a credential**, so it is treated as one: 32 random bytes shown
  exactly once, only the SHA-256 stored, single-use, 10 minutes by default and
  30 at most, revocable, and bound to the tenant AND to a staff identity — the
  named rep, or a rep whose access group admits the queue. Every way it can be
  wrong answers with the same sentence, so a guessed token learns nothing.
* **Joining makes a NEW interaction for rep B**, exactly as transfer does: two
  reps handled the contact and a record that said one did would be wrong about
  handling time and about who spoke to the customer. It inherits the tier, so
  the customer is not re-verified.
* **Warm vs cold.** Warm: rep A stays on the contact; both rows are open. Cold:
  rep A's row is wrapped *when B joins* — not when the link is made, because a
  link nobody opens must not strand the customer with no rep at all.
  `handoff_kind` on B's row says which, and the floor analytics count both.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import hashlib
import secrets
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from velaris_shared.db.models import (
    CustomerInteractionJoinModel,
    CustomerInteractionModel,
    UserDirectoryModel,
    WorkQueueModel,
)

MODES = ("warm", "cold")
DEFAULT_TTL_MINUTES = 10
MAX_TTL_MINUTES = 30
#: The one answer to every way a link can be wrong — unknown, expired, used,
#: revoked, another tenant's, somebody else's. Distinguishing them would tell a
#: guesser which tokens exist.
NOT_VALID = "This join link is not valid."


class JoinError(ValueError):
    def __init__(self, message: str, status: int = 422):
        super().__init__(message)
        self.status = status


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def may_manage(actor: Any, interaction: CustomerInteractionModel | None,
               created_by: str | None = None) -> bool:
    """Who may bring somebody onto a contact, or withdraw the invitation: the
    rep on it, the rep who made the link, or a supervisor. Any rep in the tenant
    could do either until the 2026-09-29 audit — pulling a colleague's caller
    into a stranger's desk, or cancelling another rep's handover."""
    from velaris_shared import hxguard

    me = _identities(actor)
    if created_by and created_by.strip().lower() in me:
        return True
    if interaction is not None:
        if not interaction.agent_user_id:
            return True           # nobody holds it; any rep may
        if interaction.agent_user_id.strip().lower() in me:
            return True
    return hxguard.user_allows(actor, "engage.analytics.read")


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _identities(user: Any) -> set[str]:
    return {str(v).strip().lower() for v in
            (getattr(user, "user_id", None), getattr(user, "username", None),
             getattr(user, "email", None)) if v}


async def _rep_in_tenant(session: AsyncSession, rep: str, tenant_token: str) -> bool:
    """Is `rep` a person in this tenant? Their directory row decides; a row with
    no tenant is the platform's default tenant, as everywhere else."""
    from velaris_shared.tenancy.identity import DEFAULT_TENANT, same_tenant

    rows = (await session.execute(select(UserDirectoryModel).where(
        (UserDirectoryModel.user_id == rep) | (UserDirectoryModel.email == rep)
    ))).scalars().all()
    for row in rows:
        if await same_tenant(session, row.tenant_id or DEFAULT_TENANT, tenant_token):
            return True
    return False


async def _queue_admits(session: AsyncSession, user: Any, queue: str,
                        tenant_uuid: uuid.UUID) -> bool:
    """Does this rep's active access group admit the queue?

    `allowed_queue_ids` holds queue ids or `*`; the interaction names its queue
    by NAME, so the name is looked up in this tenant (or the platform's shared
    queues) and either spelling is accepted.
    """
    from sqlalchemy import or_

    group = getattr(user, "active_access_group", None)
    allowed = [str(a) for a in (getattr(group, "allowed_queue_ids", None) or ["*"])]
    if "*" in allowed or queue in allowed:
        return True
    ids = (await session.execute(select(WorkQueueModel.id).where(
        WorkQueueModel.name == queue,
        or_(WorkQueueModel.tenant_id == tenant_uuid, WorkQueueModel.tenant_id.is_(None)),
    ))).scalars().all()
    return any(str(i) in allowed for i in ids)


async def create(
    session: AsyncSession, *, tenant_uuid: uuid.UUID, tenant_token: str,
    interaction: CustomerInteractionModel, actor: Any, mode: str,
    for_user_id: str | None, for_queue: str | None, ttl_minutes: int | None = None,
) -> tuple[CustomerInteractionJoinModel, str]:
    """Mint a link. Returns the row and the token — the only time it exists."""
    if mode not in MODES:
        raise JoinError(f"mode is one of {', '.join(MODES)}")
    rep = (for_user_id or "").strip()
    queue = (for_queue or "").strip()
    if bool(rep) == bool(queue):
        raise JoinError("name exactly one of a rep or a queue")
    if interaction.status != "open":
        raise JoinError("this contact has ended; there is nothing to join", 409)
    if not may_manage(actor, interaction):
        raise JoinError("only the rep on this contact, or a supervisor, can bring "
                        "somebody onto it", 403)
    if rep:
        if rep.lower() in _identities(actor):
            raise JoinError("a join link is for somebody else")
        if not await _rep_in_tenant(session, rep, tenant_token):
            # Deliberately the same wording for "no such person" and "somebody in
            # another tenant": the second must not be learnable from here.
            raise JoinError("no rep by that name in this tenant")
    ttl = DEFAULT_TTL_MINUTES if ttl_minutes is None else int(ttl_minutes)
    if not 1 <= ttl <= MAX_TTL_MINUTES:
        raise JoinError(f"a link lives 1 to {MAX_TTL_MINUTES} minutes")

    token = secrets.token_urlsafe(32)
    now = _utcnow()
    row = CustomerInteractionJoinModel(
        id=uuid.uuid4(), tenant_id=tenant_uuid, interaction_id=interaction.id,
        token_hash=hash_token(token), mode=mode,
        for_user_id=rep or None, for_queue=queue or None,
        created_by=str(getattr(actor, "user_id", None) or "unknown"),
        created_at=now, expires_at=now + timedelta(minutes=ttl))
    session.add(row)
    await session.flush()
    return row, token


async def redeem(
    session: AsyncSession, *, tenant_uuid: uuid.UUID, token: str, actor: Any,
) -> tuple[CustomerInteractionJoinModel, CustomerInteractionModel, CustomerInteractionModel]:
    """Open the contact for rep B. Returns (link, A's interaction, B's interaction).

    The row is locked first, so two tabs racing the same link cannot both win.
    """
    now = _utcnow()
    row = (await session.execute(
        select(CustomerInteractionJoinModel)
        .where(CustomerInteractionJoinModel.token_hash == hash_token(token or ""))
        .with_for_update())).scalars().first()
    if (row is None or row.tenant_id != tenant_uuid or row.redeemed_at is not None
            or row.revoked_at is not None or _aware(row.expires_at) <= now):
        raise JoinError(NOT_VALID, 404)
    me = _identities(actor)
    if row.created_by.strip().lower() in me:
        raise JoinError(NOT_VALID, 404)
    if row.for_user_id:
        if row.for_user_id.strip().lower() not in me:
            raise JoinError(NOT_VALID, 404)
    elif not await _queue_admits(session, actor, row.for_queue or "", tenant_uuid):
        raise JoinError(NOT_VALID, 404)

    origin = await session.get(CustomerInteractionModel, row.interaction_id)
    if origin is None or origin.tenant_id != tenant_uuid:
        raise JoinError(NOT_VALID, 404)
    if origin.status != "open":
        raise JoinError("the contact ended before the link was opened", 409)

    joined = CustomerInteractionModel(
        id=uuid.uuid4(), tenant_id=tenant_uuid, customer_id=origin.customer_id,
        channel=origin.channel, direction=origin.direction,
        agent_user_id=str(getattr(actor, "user_id", None) or "unknown"),
        external_call_id=origin.external_call_id, ani=origin.ani, dnis=origin.dnis,
        queue=row.for_queue or origin.queue, transferred_from=origin.id,
        # Inherited, as transfer inherits it: the customer is not re-verified.
        verification_level=origin.verification_level,
        verified_by=origin.verified_by, verified_at=origin.verified_at,
        status="open", handoff_kind=row.mode)
    session.add(joined)
    # Written BEFORE the link points at it. The models declare no relationship
    # between the two, so the ORM cannot order the INSERT ahead of the UPDATE,
    # and PostgreSQL rejects the foreign key (SQLite, which enforces none, did
    # not — found by the PG race test, 2026-09-29).
    await session.flush()
    if row.mode == "cold":
        # Only now, when somebody has actually taken the contact.
        origin.status = "wrapped"
        origin.ended_at = origin.ended_at or now
        origin.outcome = origin.outcome or "handed_over"
    row.redeemed_at = now
    row.redeemed_by = str(getattr(actor, "user_id", None) or "unknown")
    row.joined_interaction_id = joined.id
    await session.flush()

    if origin.customer_id is not None:
        from case_service.engage import identity

        await identity.link(session, tenant_uuid=tenant_uuid, customer_id=origin.customer_id,
                            object_kind="interaction", object_id=joined.id,
                            reason="resolved", linked_by=row.redeemed_by)
    return row, origin, joined


async def revoke(session: AsyncSession, *, row: CustomerInteractionJoinModel, actor: Any) -> None:
    origin = await session.get(CustomerInteractionModel, row.interaction_id)
    if not may_manage(actor, origin, row.created_by):
        raise JoinError("only the rep who made this link, the rep on the contact, "
                        "or a supervisor can revoke it", 403)
    if row.redeemed_at is not None:
        raise JoinError("this link was already used", 409)
    if row.revoked_at is None:
        row.revoked_at = _utcnow()
        row.revoked_by = str(getattr(actor, "user_id", None) or "unknown")
        await session.flush()


def link_json(row: CustomerInteractionJoinModel) -> dict[str, Any]:
    """Never the token — it existed once, in the create response."""
    from case_service.core.timestamps import iso_utc

    now = _utcnow()
    state = ("used" if row.redeemed_at else "revoked" if row.revoked_at
             else "expired" if _aware(row.expires_at) <= now else "open")
    return {
        "id": str(row.id), "interaction_id": str(row.interaction_id),
        "mode": row.mode, "for_user_id": row.for_user_id, "for_queue": row.for_queue,
        "created_by": row.created_by, "created_at": iso_utc(row.created_at),
        "expires_at": iso_utc(row.expires_at), "state": state,
        "redeemed_by": row.redeemed_by, "redeemed_at": iso_utc(row.redeemed_at),
        "joined_interaction_id": (str(row.joined_interaction_id)
                                  if row.joined_interaction_id else None),
    }
