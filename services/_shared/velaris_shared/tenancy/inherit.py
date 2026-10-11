"""Inherited defaults with copy-on-write tenant forks (Oct 8e part a).

The rule agreed for case types in 8i (2026-09-30), applied to forms, rules,
data models and business calendars (2026-10-01):

* A row with no tenant is a **default** — one platform row every tenant reads.
* A tenant's own row is invisible to every other tenant.
* A tenant that edits a default gets its **fork** — a copy recorded with
  `forked_from_id` — and the fork wins for that tenant from then on: the
  default no longer lists for it, and a read of the default's id *by that
  tenant* resolves to the fork (`resolve`). Rules, forms and calendars are
  referenced BY ID from case-type definitions, so without this a fork would
  sit beside the default and never be used.
* Only the platform operator changes a default; its later change is offered
  to forks (`updates_available`), never applied over them.
* The operator creates exactly as before (as sent, NULL = default), so a
  single-tenant install sees no change.

Each model must carry `tenant_id`, `tenant_key`, `forked_from_id`, `forked_at`
(migration 214). Case types keep their own module (`case_service/case_types/
ownership.py`) because their fork also copies stages, steps and statuses.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import and_, exists, inspect, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from velaris_shared.tenancy.identity import (
    caller_tenant_uuid,
    is_platform_operator,
    is_superadmin,
)


class InheritError(Exception):
    """Refused. `status` is the HTTP answer a route should give."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status


def tenant_key_of(tenant_id: Any) -> str:
    return str(tenant_id) if tenant_id else "platform"


def visible_to(model, tenant_id: uuid.UUID):
    """SQL: the rows `tenant_id` can use — its own, and every default it has
    not forked."""
    own = aliased(model)
    forked = exists(select(own.id).where(
        own.tenant_id == tenant_id, own.forked_from_id == model.id))
    return or_(model.tenant_id == tenant_id,
               and_(model.tenant_id.is_(None), ~forked))


async def scope_list(stmt, model, session: AsyncSession, user: Any):
    """Narrow a list to what the caller can use. superadmin is unscoped."""
    if is_superadmin(user):
        return stmt
    mine = await caller_tenant_uuid(session, user)
    # A tenant that does not resolve owns nothing: the defaults alone.
    return stmt.where(visible_to(model, mine or uuid.UUID(int=0)))


async def owner_for_create(session: AsyncSession, user: Any,
                           requested: uuid.UUID | None = None) -> uuid.UUID | None:
    """The operator keeps the value as sent (NULL = a default, as before);
    everybody else creates in their own tenant."""
    if await is_platform_operator(session, user):
        return requested
    mine = await caller_tenant_uuid(session, user)
    if mine is None:
        raise InheritError(403, "Your tenant could not be resolved.")
    if requested is not None and requested != mine:
        raise InheritError(403, "This may only be created in your own tenant.")
    return mine


async def name_taken(session: AsyncSession, model, owner: uuid.UUID | None,
                     **identity: Any) -> bool:
    """Is this name (+ version) already used in the owner's namespace?"""
    stmt = select(model.id).where(model.tenant_key == tenant_key_of(owner))
    for col, val in identity.items():
        stmt = stmt.where(getattr(model, col) == val)
    return (await session.execute(stmt.limit(1))).first() is not None


async def may_write_in_place(session: AsyncSession, user: Any, row) -> bool:
    if row.tenant_id is None:
        return await is_platform_operator(session, user)
    if is_superadmin(user):
        return True
    return row.tenant_id == await caller_tenant_uuid(session, user)


async def find_fork(session: AsyncSession, model, default, tenant_id: uuid.UUID):
    return (await session.execute(
        select(model).where(model.tenant_id == tenant_id,
                            model.forked_from_id == default.id)
        .order_by(model.created_at.desc()).limit(1)
    )).scalars().first()


async def fork(session: AsyncSession, model, default, tenant_id: uuid.UUID):
    """Copy a default into `tenant_id`; the existing fork if there is one."""
    existing = await find_fork(session, model, default, tenant_id)
    if existing is not None:
        return existing
    ident = {c: getattr(default, c) for c in ("name", "version") if hasattr(default, c)}
    if await name_taken(session, model, tenant_id, **ident):
        raise InheritError(409, f"Your tenant already has its own '{default.name}'; "
                                "edit that one instead.")
    now = datetime.now(timezone.utc)
    values = {c.key: getattr(default, c.key) for c in inspect(model).column_attrs
              if c.key != "id"}
    values.update(id=uuid.uuid4(), tenant_id=tenant_id, tenant_key=str(tenant_id),
                  forked_from_id=default.id, forked_at=now)
    for ts in ("created_at", "updated_at"):
        if ts in values:
            values[ts] = now
    for k, v in list(values.items()):
        if isinstance(v, dict):
            values[k] = dict(v)
        elif isinstance(v, list):
            values[k] = list(v)
    row = model(**values)
    session.add(row)
    await session.flush()
    return row


async def target_for_write(session: AsyncSession, user: Any, model, row):
    """The row an edit lands on: this one, or the caller's fork of a default.
    Another tenant's row is 404 (as a read of it is)."""
    if await may_write_in_place(session, user, row):
        return row
    if row.tenant_id is not None:
        raise InheritError(404, "Not found")
    mine = await caller_tenant_uuid(session, user)
    if mine is None:
        raise InheritError(403, "Your tenant could not be resolved.")
    return await fork(session, model, row, mine)


async def assert_may_delete(session: AsyncSession, user: Any, row) -> None:
    """Delete acts on the row itself: the caller's own, or — for a default —
    the platform operator only."""
    if await may_write_in_place(session, user, row):
        return
    if row.tenant_id is not None:
        raise InheritError(404, "Not found")
    raise InheritError(403, "A default is shared by every tenant; only the "
                            "platform operator may delete it.")


async def resolve(session: AsyncSession, model, row, tenant_id: uuid.UUID | None):
    """What `tenant_id` gets when it reads `row` by id: its fork of a default,
    if it has one, else the row."""
    if row is None or row.tenant_id is not None or tenant_id is None:
        return row
    return await find_fork(session, model, row, tenant_id) or row


async def resolve_for_caller(session: AsyncSession, user: Any, model, row):
    if row is None or is_superadmin(user):
        return row
    return await resolve(session, model, row, await caller_tenant_uuid(session, user))


async def updates_available(session: AsyncSession, model, rows: list) -> set:
    forks = [r for r in rows if r.forked_from_id is not None and r.forked_at is not None]
    if not forks:
        return set()
    parents = {p.id: p for p in (await session.execute(
        select(model).where(model.id.in_({r.forked_from_id for r in forks}))
    )).scalars().all()}
    out = set()
    for r in forks:
        p = parents.get(r.forked_from_id)
        if p is not None and p.updated_at is not None and _aware(p.updated_at) > _aware(r.forked_at):
            out.add(r.id)
    return out


async def flag_updates(session: AsyncSession, model, rows: list) -> list:
    """Set `update_available` on each row (8k): a fork whose default changed
    after it was taken. Not a column — read by the response schemas."""
    stale = await updates_available(session, model, rows)
    for r in rows:
        r.update_available = r.id in stale
    return rows


def _aware(ts: datetime) -> datetime:
    return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)
