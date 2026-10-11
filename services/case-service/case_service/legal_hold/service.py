"""Legal holds — freezing a case's records while a dispute is open.

Oct item 13. Until 2026-10-11 the platform had no legal hold: retention deleted
or anonymized a closed case on its clock, a data subject's erasure went through,
a case could be hidden or moved to another tenant, and nothing could say "not
this one — it is evidence". Moving a case between tenants borrowed an open
HxSentinel risk hold as the stand-in, which is a different thing.

**What a hold is.** One row per (case, matter) in `legal_holds` (migration
215). A case is held while ANY row for it is `active`; it can be under several
matters and stays frozen until the last one is released. Rows are never
deleted — a released hold is the record that the freeze existed, who placed it
and who lifted it.

**What a hold freezes** — destruction, concealment and removal, each refused at
the place it happens (`FROZEN` below is the same list, shown to operators):

* retention — a held case is neither deleted nor anonymized, and its audit and
  event rows are not pruned (`compliance/retention.py`);
* the `delete` case action — hiding evidence from every read is not preserving
  it; `restore` still works (`case_actions/dispatcher.py`);
* deleting what is attached to the case — a document or one of its versions,
  a time entry, an HxNexus conversation about it, an HxCanvas board on it
  (`documents.py`, `costing.py`, `hxnexus.py`, `hxcanvas.py`);
* erasing a customer who has a held case — the portal's self-service and admin
  erasure and the rep desk's (`engage/erasure.forget`). GDPR Art. 17(3)(e)
  exempts data needed for legal claims; the request is refused, not lost;
* anonymizing a staff user who created, worked or is recorded on a held case
  (`enterprise/gdpr.anonymize_user_data`) — it rewrites who did what;
* moving the case to another tenant (`case_actions/tenant_transfer.blockers`);
* permanently deleting a tenant, or shredding its encryption key, while any of
  its cases is held.

**What it does not freeze.** Ordinary work. A held case can still be worked,
noted, re-prioritised and closed — a hold preserves the record, it does not
stop the business. And it cannot stop somebody with direct database access
(`HxDBManager`'s raw statements included): that is the audit chain's job to
detect, not this module's to prevent.

**A missing table means "nothing is held", never an error.** The checks below
sit in front of retention, erasure and the case action dispatcher — functions
every install already uses. If migration 215 has not been applied, those must
keep working exactly as they did; no hold can exist without the table anyway.
Any OTHER database error still propagates: a hold check that cannot be answered
must not be read as "not held".

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Iterable

from sqlalchemy import func, select
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from velaris_shared.db.models import CaseInstanceModel, LegalHoldModel

log = logging.getLogger(__name__)

PRIV_READ = "legal.holds.read"
PRIV_PLACE = "legal.holds.place"
PRIV_RELEASE = "legal.holds.release"

CODE_HELD = "VEL-LEGALHOLD-HELD-409"
CODE_STATE = "VEL-LEGALHOLD-STATE-409"
CODE_INVALID = "VEL-LEGALHOLD-INVALID-400"
CODE_NOTFOUND = "VEL-LEGALHOLD-NOTFOUND-404"

ACTIVE = "active"
RELEASED = "released"

MATTER_MAX = 120
REASON_MIN = 10
REASON_MAX = 4000

#: What an operator is told a hold freezes. Kept beside the docstring above so
#: the two cannot drift far; each line has an enforcement point and a test.
FROZEN: tuple[str, ...] = (
    "Retention will not delete or anonymize the case, or prune its audit and event history.",
    "The case cannot be deleted (hidden). Restore still works.",
    "Documents and their versions, time entries, AI conversations and boards attached "
    "to the case cannot be deleted.",
    "A customer linked to the case cannot be erased.",
    "A staff user recorded on the case cannot be anonymized.",
    "The case cannot be moved to another tenant.",
    "The tenant cannot be permanently deleted, and its encryption key cannot be shredded.",
)


class LegalHoldError(Exception):
    """A legal hold refuses this, or the hold itself is in the wrong state."""

    def __init__(self, message: str, *, code: str = CODE_HELD, status_code: int = 409) -> None:
        super().__init__(message)
        self.code = code
        self.status_code = status_code


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def normalise_matter(matter_ref: str) -> str:
    """One spelling per matter: trimmed, inner whitespace collapsed."""
    return " ".join(str(matter_ref or "").split())


ALREADY_HELD = ("This case is already under a legal hold. "
                "Release that hold before placing another.")


def _active_key(case_id: uuid.UUID) -> str:
    # One active hold per case (decided 2026-10-11): the key is the case alone,
    # so the unique index refuses a second one even when two operators race.
    return str(case_id)


# ── is the table there ───────────────────────────────────────────────────────

_table_seen = False
_missing_logged = False


def _is_missing_table(exc: BaseException) -> bool:
    text = str(exc).lower()
    return "legal_holds" in text and any(
        s in text for s in ("does not exist", "doesn't exist", "no such table"))


async def available(session: AsyncSession) -> bool:
    """False only when `legal_holds` does not exist (migration 215 not applied)."""
    global _table_seen, _missing_logged  # noqa: PLW0603
    if _table_seen:
        return True
    try:
        async with session.begin_nested():
            await session.execute(select(LegalHoldModel.id).limit(1))
    except DBAPIError as exc:
        if _is_missing_table(exc):
            if not _missing_logged:          # once per process, not once per case
                _missing_logged = True
                log.warning("legal_holds table is missing (migration 215 not applied): "
                            "no case is treated as held")
            return False
        raise
    _table_seen = True
    return True


def _reset_cache_for_tests() -> None:
    global _table_seen  # noqa: PLW0603
    _table_seen = False


# ── asking ───────────────────────────────────────────────────────────────────

def held_case_ids():
    """A subquery of every held case id, for `NOT IN` on a destructive query.

    Call `available()` first — this is SQL, it cannot tolerate a missing table.
    """
    return select(LegalHoldModel.case_id).where(LegalHoldModel.status == ACTIVE)


async def not_held(session: AsyncSession, case_id_column):
    """A filter that leaves held cases out, or None when nothing can be held."""
    if not await available(session):
        return None
    return case_id_column.notin_(held_case_ids())


async def held_among(session: AsyncSession, case_ids: Iterable[Any]) -> set[uuid.UUID]:
    ids = [i for i in {_as_uuid(c) for c in case_ids} if i is not None]
    if not ids or not await available(session):
        return set()
    return set((await session.execute(
        select(LegalHoldModel.case_id).where(
            LegalHoldModel.status == ACTIVE,
            LegalHoldModel.case_id.in_(ids)))).scalars().all())


async def is_held(session: AsyncSession, case_id: Any) -> bool:
    return bool(await held_among(session, [case_id]))


async def count_held(session: AsyncSession, case_ids_query) -> int:
    """How many of the cases a query selects are held."""
    if not await available(session):
        return 0
    return int((await session.execute(
        select(func.count(func.distinct(LegalHoldModel.case_id))).where(
            LegalHoldModel.status == ACTIVE,
            LegalHoldModel.case_id.in_(case_ids_query)))).scalar() or 0)


async def require_not_held(session: AsyncSession, case_id: Any, doing: str) -> None:
    """Raise unless the case is free. `doing` completes "… cannot be <doing>"."""
    if case_id is not None and await is_held(session, case_id):
        raise LegalHoldError(
            f"This case is under a legal hold and cannot be {doing} until the hold is released.")


async def tenant_active_count(session: AsyncSession, tenant_uuid: Any) -> int:
    """Active holds on a tenant's cases — by the hold's tenant or the case's."""
    tid = _as_uuid(tenant_uuid)
    if tid is None or not await available(session):
        return 0
    from sqlalchemy import or_

    return int((await session.execute(
        select(func.count()).select_from(LegalHoldModel).where(
            LegalHoldModel.status == ACTIVE,
            or_(LegalHoldModel.tenant_id == tid,
                LegalHoldModel.case_id.in_(
                    select(CaseInstanceModel.id).where(CaseInstanceModel.tenant_id == tid)))
        ))).scalar() or 0)


async def require_tenant_free(session: AsyncSession, tenant_uuid: Any, doing: str) -> None:
    n = await tenant_active_count(session, tenant_uuid)
    if n:
        raise LegalHoldError(
            f"{n} legal hold(s) are active on this tenant's cases; it cannot be {doing} "
            "until they are released.")


async def customer_held_cases(
    session: AsyncSession, *, tenant: Any, emails: Iterable[str | None] = (),
    phones: Iterable[str | None] = (), portal_customer_ids: Iterable[Any] = (),
    profile_ids: Iterable[Any] = (),
) -> set[uuid.UUID]:
    """The held cases of one data subject.

    "The subject's cases" is whatever the Art. 15 export would hand them — one
    definition, in `privacy/customer_export.py`, so an erasure cannot be refused
    for a case the export does not consider theirs, or allowed for one it does.
    """
    if not await available(session):
        return set()
    from case_service.privacy import customer_export

    try:
        data = await customer_export.build(
            session, tenant=tenant, emails=emails, phones=phones,
            portal_customer_ids=portal_customer_ids, customer_ids=profile_ids,
            include_internal=False)
    except customer_export.ExportError:
        return set()            # no identifier at all: nothing to find, nothing held
    return await held_among(session, [c.get("id") for c in data.get("cases") or []])


async def require_customer_erasable(session: AsyncSession, **who: Any) -> None:
    held = await customer_held_cases(session, **who)
    if held:
        raise LegalHoldError(
            f"This customer has {len(held)} case(s) under a legal hold. Their data cannot "
            "be erased until the hold is released.")


async def user_held_cases(session: AsyncSession, user_id: str) -> int:
    """Held cases a STAFF user is recorded on — creator, actor or assignee."""
    if not user_id or not await available(session):
        return 0
    from velaris_shared.db.models import CaseAssignmentModel, CaseAuditLogModel

    ids: set[uuid.UUID] = set()
    for stmt in (
        select(CaseInstanceModel.id).where(CaseInstanceModel.created_by == user_id),
        select(CaseAuditLogModel.case_id).where(CaseAuditLogModel.actor_id == user_id),
        select(CaseAssignmentModel.case_id).where(CaseAssignmentModel.assignee_id == user_id),
    ):
        ids |= set((await session.execute(
            stmt.where(stmt.selected_columns[0].in_(held_case_ids())).distinct()
        )).scalars().all())
    return len(ids)


# ── placing and releasing ────────────────────────────────────────────────────

def _as_uuid(value: Any) -> uuid.UUID | None:
    if isinstance(value, uuid.UUID):
        return value
    try:
        return uuid.UUID(str(value))
    except (ValueError, TypeError, AttributeError):
        return None


def _clean(matter_ref: str, reason: str) -> tuple[str, str]:
    matter = normalise_matter(matter_ref)
    why = str(reason or "").strip()
    if not matter or len(matter) > MATTER_MAX:
        raise LegalHoldError(f"Name the matter (1–{MATTER_MAX} characters).",
                             code=CODE_INVALID, status_code=400)
    if not REASON_MIN <= len(why) <= REASON_MAX:
        raise LegalHoldError(
            f"Give a reason of {REASON_MIN}–{REASON_MAX} characters. It is the record of "
            "why the case was frozen, or freed.", code=CODE_INVALID, status_code=400)
    return matter, why


async def _record(session: AsyncSession, row: LegalHoldModel, *, action: str, actor: str,
                  case_exists: bool) -> None:
    """The case's own timeline, and the security log. Neither carries the reason."""
    from velaris_shared.db import repository as repo
    from velaris_shared.db import security_events

    details = {"hold_id": str(row.id), "matter_ref": row.matter_ref}
    if case_exists:
        await repo.append_audit_entry(session, data={
            "case_id": row.case_id, "action": f"legal_hold_{action}",
            "actor_id": actor, "actor_type": "user", "details": details})
    await security_events.log_security_event(
        session, event_type=f"legal_hold.{action}", severity="warning", user_id=actor,
        resource_type="case", resource_id=str(row.case_id),
        action=("place" if action == "placed" else "release"), outcome="success",
        details=details)


async def place(session: AsyncSession, *, case: CaseInstanceModel, matter_ref: str,
                reason: str, actor: str, tenant_uuid: uuid.UUID | None) -> LegalHoldModel:
    """Put `case` under hold for `matter_ref`. One active hold per case: a held
    case takes no second hold until the first is released."""
    matter, why = _clean(matter_ref, reason)
    key = _active_key(case.id)
    # By status, not by key: a hold placed before 2026-10-11 carries the older
    # per-matter key and must still count.
    if (await session.execute(select(LegalHoldModel.id).where(
            LegalHoldModel.case_id == case.id,
            LegalHoldModel.status == ACTIVE))).first() is not None:
        raise LegalHoldError(ALREADY_HELD, code=CODE_STATE)
    row = LegalHoldModel(
        id=uuid.uuid4(), tenant_id=case.tenant_id or tenant_uuid, case_id=case.id,
        case_number=case.case_number, matter_ref=matter, reason=why, status=ACTIVE,
        active_key=key, placed_by=str(actor), placed_at=_utcnow())
    try:
        async with session.begin_nested():
            session.add(row)
            await session.flush()
    except IntegrityError:
        # Two operators at once: the unique key let one in.
        raise LegalHoldError(ALREADY_HELD, code=CODE_STATE) from None
    await _record(session, row, action="placed", actor=str(actor), case_exists=True)
    return row


async def release(session: AsyncSession, *, hold: LegalHoldModel, reason: str,
                  actor: str) -> LegalHoldModel:
    """Lift one hold. The case stays frozen if an older install left it a second one."""
    _, why = _clean(hold.matter_ref, reason)
    if hold.status != ACTIVE:
        raise LegalHoldError("This hold has already been released.", code=CODE_STATE)
    hold.status = RELEASED
    hold.active_key = None
    hold.released_by = str(actor)
    hold.released_at = _utcnow()
    hold.release_reason = why
    session.add(hold)
    await session.flush()
    exists = (await session.execute(select(CaseInstanceModel.id).where(
        CaseInstanceModel.id == hold.case_id))).first() is not None
    await _record(session, hold, action="released", actor=str(actor), case_exists=exists)
    return hold


def as_dict(row: LegalHoldModel) -> dict[str, Any]:
    def iso(v):
        return v.isoformat() if v else None

    return {
        "id": str(row.id), "case_id": str(row.case_id), "case_number": row.case_number,
        "tenant_id": str(row.tenant_id) if row.tenant_id else None,
        "matter_ref": row.matter_ref, "reason": row.reason, "status": row.status,
        "placed_by": row.placed_by, "placed_at": iso(row.placed_at),
        "released_by": row.released_by, "released_at": iso(row.released_at),
        "release_reason": row.release_reason,
    }
