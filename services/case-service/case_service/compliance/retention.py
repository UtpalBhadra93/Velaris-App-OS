"""Retention enforcement — the half `retention_policies` never had.

The table has existed for a long time and is **CRUD only**: policies can be
seeded, listed and edited, and nothing has ever applied one. `last_run_at` was
never written by anything. A retention policy that is displayed but never
enforced is worse than none — it is a compliance claim the platform cannot
back, and it looks identical on screen to one that works.

Three rules this module will not bend:

**The audit chain is never touched.** `case_audit_log_chain` carries DB-level
triggers that raise on UPDATE and DELETE (migration 082) — deliberately, so the
chain is tamper-evident even against direct database access. A retention job
that tried to prune it would not merely fail, it would be arguing with the
platform's own integrity guarantee. It is excluded by name, not by luck.

**`anonymize` keeps the row and removes the identity.** It is not a softer
delete — it is a different operation with a different purpose. Deleting a
resolved case destroys a labelled training example: `sentinel/model.py` walks
`CaseOutcomeModel` and skips any case whose row has gone
(`if case is None: continue`), so a delete policy silently degrades the risk
model and nobody sees it happen.

Anonymising is safe for that model specifically because **its features are
already PII-free**: identity signals are stored hashed (`hashed=True`, with
`value={"kind": …}`), and `amount` comes from numeric case variables. Scrubbing
the payload therefore removes personal data without touching a single model
input. The outcome label survives, so the case keeps teaching.

Which keys get scrubbed is the union of `ALWAYS_STRIP` (structural, always
present on portal cases) and **HxPrivacy's confirmed findings** — only
`confirmed`, never `detected`, because a heuristic guess must not silently
destroy a field an operator still needs.

**`archive` means delete, on the policy's own clock.** There is no archive
table and no archived state, by decision: archiving here is retention with a
longer period, not relocation. Both actions therefore use the policy's own
`retention_days` — one number per policy, editable by an admin in
Enterprise → Retention.

An earlier draft made `archive` read a platform-wide env var instead. That was
wrong: it meant the days shown on the admin screen were not the days that ran,
so a policy reading "3 years" would delete at 7 and the screen could not say
so. Every policy already owns its period and the panel already edits it; a
second source of truth for the same number could only ever disagree with the
first.

**Dry-run is the default.** The first thing an operator should be able to do
with a deletion job is find out what it *would* delete. `apply=False` counts
and reports without touching a row.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import logging
from collections.abc import Sequence
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import delete, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from velaris_shared.db.models import RetentionPolicyModel

logger = logging.getLogger(__name__)

ARCHIVE = "archive"
DELETE = "delete"
ANONYMIZE = "anonymize"

#: Marker written into `case.data` so anonymisation is idempotent and visible.
#: A re-run must not re-scrub an already-scrubbed case, and an auditor asking
#: "why is this case empty" deserves an answer in the row itself.
ANON_MARKER = "_anonymized_at"
#: Second marker, for the free-text sweep (Oct 11f) — see `_anonymize_cases`.
ANON_TEXT_MARKER = "_anonymized_text_at"

#: Payload keys stripped regardless of what HxPrivacy has confirmed. These are
#: structural — the intake service writes them on every portal-originated case,
#: so they exist whether or not anyone has run a scan.
ALWAYS_STRIP = frozenset({
    "_raw_payload", "submitter_email", "portal_submitter_email",
    "submitter_name", "email", "phone",
})

#: What a newly-seeded `archive` policy gets — seven years, the common
#: statutory period. Only a seed default: once the row exists, the admin panel
#: owns the number.
DEFAULT_ARCHIVE_DAYS = 2555

#: Tables a retention policy may never touch, whatever a policy row says.
#: `case_audit_log_chain` is append-only at the database level (migration 082);
#: pruning it would break the guarantee the chain exists to provide.
FORBIDDEN_TABLES = frozenset({"case_audit_log_chain", "audit_anchors"})


class RetentionError(RuntimeError):
    """The policy cannot be applied as written."""


def _cutoff(days: int) -> datetime:
    return datetime.now(timezone.utc) - timedelta(days=max(1, int(days)))


async def _anonymize_cases(session: AsyncSession, cutoff: datetime, apply: bool,
                    tenant_id: str | None = None,
                    exclude_tenants: Sequence[str] | None = None) -> int:
    """Strip identity from old resolved cases, keeping the row.

    Idempotent: a case already carrying `ANON_MARKER` is skipped, so re-running
    is free and cannot double-scrub.
    """
    from velaris_shared.db.models import CaseInstanceModel

    terminal = ("closed", "resolved", "cancelled", "rejected")
    q = select(CaseInstanceModel).where(
        CaseInstanceModel.status.in_(terminal),
        CaseInstanceModel.updated_at < cutoff,
    )
    if tenant_id:
        q = q.where(CaseInstanceModel.tenant_id == tenant_id)
    elif exclude_tenants:
        q = q.where(_not_overridden(CaseInstanceModel, exclude_tenants))
    # A case under a legal hold is evidence: never anonymized, however old.
    free = await _not_held(session, CaseInstanceModel.id)
    if free is not None:
        q = q.where(free)
    rows = (await session.execute(q)).scalars().all()

    pending = [c for c in rows if not (c.data or {}).get(ANON_MARKER)]
    # Oct 11f, off unless switched on: the case's free text. A case anonymized
    # before the switch existed carries `ANON_MARKER` and is never revisited by
    # the line above, so it gets its own marker and its own sweep.
    text_on = _case_text_enabled()
    text_due = [c for c in rows
                if text_on and not (c.data or {}).get(ANON_TEXT_MARKER)]
    # Already-marked cases were scrubbed before the submitter COLUMNS were part
    # of this routine, so they still carry the customer's name and address. The
    # marker check above means they are never revisited — they have to be swept
    # up explicitly or they stay un-erased forever.
    backfill = [c for c in rows
                if (c.data or {}).get(ANON_MARKER)
                and (c.portal_submitter_email or c.portal_submitter_name
                     or c.portal_tracking_token)]
    touched = {c.id for c in pending + backfill + text_due}
    if not apply:
        return len(touched)

    strip = set(ALWAYS_STRIP) | await _confirmed_pii_keys(session)
    now = datetime.now(timezone.utc)
    for case in pending:
        data = dict(case.data or {})
        # Replaced, not popped: a missing key and a removed key look identical
        # to anyone reading the case later, and only one of them is a fact.
        for key in list(data):
            if str(key).lower() in strip:
                data[key] = "[anonymized]"
        data[ANON_MARKER] = now.isoformat()
        case.data = data
        case.created_by = "anonymized"
        _strip_submitter_columns(case)
        session.add(case)
    for case in backfill:
        _strip_submitter_columns(case)
        session.add(case)

    # The DERIVED copy of the documents, which anonymization never reached.
    #
    # `hxnexus_document_chunks` holds the extracted TEXT of a case's documents —
    # a name, an address, a bank detail, whatever the PDF actually said — so an
    # anonymized case went on carrying its own contents in a table nothing here
    # touched, readable by anyone with case access and quotable by HxNexus. It
    # is the same shape as the submitter columns above: the scrub was written
    # when `case.data` was the only place personal data lived.
    #
    # Deleted, not blanked: a chunk is a searchable copy, and an empty one is
    # noise in a vector index. The documents themselves are the document
    # retention policy's business, not this one's — what goes here is only the
    # copy this platform made for the AI.
    await _delete_chunks_for(session, [c.id for c in pending + backfill])
    # And the uploads still WAITING to become chunks (mig 206): a background
    # upload's bytes sit in storage until the drain runs, which may be never.
    await _discard_queued_uploads_for(session, [c.id for c in pending + backfill])

    if text_due:
        await _anonymize_case_text(session, [c.id for c in text_due])
        for case in text_due:
            data = dict(case.data or {})
            data[ANON_TEXT_MARKER] = now.isoformat()
            case.data = data
            session.add(case)

    return len(touched)


def _case_text_enabled() -> bool:
    try:
        from velaris_shared.config import get_settings
        return bool(get_settings().retention_anonymize_case_text)
    except Exception:  # noqa: BLE001 — unreadable config is "off", as before
        return False


async def _anonymize_case_text(session: AsyncSession, case_ids: Sequence) -> None:
    """Blank what was WRITTEN about a case, not only what was filled in.

    `case.data` is the form. The customer's name, address and account number
    are just as present in the messages exchanged on the case, in the SMS and
    WhatsApp thread, and in what somebody asked HxNexus about it — and none of
    those were reached, so an "anonymized" case could still be read in full.

    Rows are kept and their text replaced: that a message was sent, when, and
    in which direction is the case's history; what it said is the identity.

    **Not reached, deliberately:** `case_events`. It is the append-only stream
    the case row is a projection of, and nothing in the codebase updates it;
    rewriting it here would trade one guarantee for another without anyone
    having decided to. Recorded as open in `Oct_TODO.md` §11. The conversation's
    `customer_id` is left too: it is a link, and the customer record has its
    own erasure.
    """
    if not case_ids:
        return
    from sqlalchemy import update
    from velaris_shared.db.models import (
        CaseMessageModel, ChannelConversationModel, ChannelMessageModel,
        CopilotConversationModel, CopilotMessageModel,
    )

    ids = list(case_ids)
    blank = "[anonymized]"
    await session.execute(
        update(CaseMessageModel).where(CaseMessageModel.case_id.in_(ids))
        .values(body=blank, author_name=None)
        .execution_options(synchronize_session=False))
    await session.execute(
        update(ChannelMessageModel).where(ChannelMessageModel.case_id.in_(ids))
        .values(body=blank, peer_address=blank, media=[], raw_payload={})
        .execution_options(synchronize_session=False))
    # The thread itself holds the customer's number, and it is what inbound
    # routing matches on. Blanked whole, not to its last four digits: four
    # digits beside a date, a channel and a tenant still point at one person.
    # Closed as well, so the next message from that number opens a NEW
    # conversation rather than being appended to a case that was anonymized.
    await session.execute(
        update(ChannelConversationModel).where(ChannelConversationModel.case_id.in_(ids))
        .values(peer_address=blank, status="closed", metadata_json={})
        .execution_options(synchronize_session=False))
    conversations = select(CopilotConversationModel.id).where(
        CopilotConversationModel.case_id.in_(ids))
    await session.execute(
        update(CopilotMessageModel)
        .where(CopilotMessageModel.conversation_id.in_(conversations))
        .values(content=blank)
        .execution_options(synchronize_session=False))


async def _delete_chunks_for(session: AsyncSession, case_ids: Sequence) -> int:
    """Remove the AI's derived copy of a case's document text."""
    from velaris_shared.db.models import DocumentChunkModel

    if not case_ids:
        return 0
    result = await session.execute(
        delete(DocumentChunkModel).where(DocumentChunkModel.case_id.in_(list(case_ids)))
        .execution_options(synchronize_session=False)
    )
    return int(result.rowcount or 0)


async def _discard_queued_uploads_for(session: AsyncSession, case_ids: Sequence) -> int:
    """Cancel pending background uploads for these cases and delete their bytes."""
    from velaris_shared.db.models import DocumentIndexJobModel
    from case_service.hxnexus import index_queue

    if not case_ids:
        return 0
    jobs = (await session.execute(
        select(DocumentIndexJobModel).where(
            DocumentIndexJobModel.case_id.in_(list(case_ids)),
            DocumentIndexJobModel.status == "pending",
            DocumentIndexJobModel.staged_storage_key.is_not(None),
        )
    )).scalars().all()
    for job in jobs:
        await index_queue.discard_staged(job.staged_storage_key)
        job.status = "cancelled"
        job.active_key = None
        job.finished_at = datetime.now(timezone.utc)
        job.last_error = "cancelled: the case was anonymized before indexing"
        session.add(job)
    return len(jobs)


def _strip_submitter_columns(case) -> None:
    """Clear the submitter identity that does NOT live in ``case.data``.

    ``ALWAYS_STRIP`` names ``portal_submitter_email``, but that only ever matched
    a *data key*; the real values are dedicated columns on ``case_instances`` and
    survived anonymization untouched — so a case reported as anonymized still
    held the customer's address and name, readable by anyone with case access.

    ``portal_tracking_token`` goes too. It is not personal data itself, but it is
    a **bearer credential**: ``GET /portal/{slug}/track/{token}`` returns the case
    to whoever holds the link, with no other authentication. Leaving it live means
    an old confirmation email still opens an erased case, so the erasure only held
    against people who came in through the front door.

    NULLed rather than replaced with a placeholder, unlike the data keys. All
    three are matched with ``==`` across the portal (login, case list, tracking,
    mail threading); a placeholder would be a value every erased case shares,
    which is how they would start matching each other. NULL is safe for the
    unique index on the token — SQL does not treat two NULLs as equal — and the
    tracking route takes a ``uuid.UUID`` path param, so no request can arrive
    carrying NULL and match these rows.
    """
    case.portal_submitter_email = None
    case.portal_submitter_name = None
    case.portal_tracking_token = None


async def _confirmed_pii_keys(session: AsyncSession) -> set[str]:
    """HxPrivacy findings a human has confirmed. Never `detected` guesses."""
    try:
        from case_service.compliance import pii_scan

        return await pii_scan.confirmed_field_paths(session)
    except Exception as exc:                       # noqa: BLE001
        # A privacy-scan problem must not stop structural scrubbing, which is
        # the part that always applies.
        logger.warning("could not load confirmed PII fields: %s", exc)
        return set()


async def _resolved_cases(session: AsyncSession, cutoff: datetime, apply: bool,
                    tenant_id: str | None = None,
                    exclude_tenants: Sequence[str] | None = None) -> int:
    """Closed/resolved cases older than the cutoff.

    Scoped to terminal states only. A retention job that could delete an open
    case would destroy live work, and no retention period justifies that.
    """
    from velaris_shared.db.models import CaseInstanceModel

    terminal = ("closed", "resolved", "cancelled", "rejected")
    where = [
        CaseInstanceModel.status.in_(terminal),
        CaseInstanceModel.updated_at < cutoff,
    ]
    if tenant_id:
        where.append(CaseInstanceModel.tenant_id == tenant_id)
    elif exclude_tenants:
        where.append(_not_overridden(CaseInstanceModel, exclude_tenants))
    # A case under a legal hold is evidence: never deleted, however old.
    free = await _not_held(session, CaseInstanceModel.id)
    if free is not None:
        where.append(free)
    where = tuple(where)
    if not apply:
        return int((await session.execute(
            select(func.count()).select_from(CaseInstanceModel).where(*where)
        )).scalar_one() or 0)
    return int((await session.execute(
        delete(CaseInstanceModel).where(*where)
    )).rowcount or 0)


async def _audit_log(session: AsyncSession, cutoff: datetime, apply: bool,
                    tenant_id: str | None = None,
                    exclude_tenants: Sequence[str] | None = None) -> int:
    """`case_audit_log` — the mutable operational log, NOT the sealed chain.

    The two are separate tables and only one is prunable. The chain keeps its
    own hashed rows, and stays verifiable because the delete goes through
    `audit_chain.prune_audit_rows`, which seals a prune record naming the rows
    it removed (mig 216). Until 2026-10-11 this was a bare DELETE and every
    sealed row it removed verified as `missing_audit_row` — Oct_TODO §11h.

    The audit rows of a case under a legal hold are not pruned.
    """
    from velaris_shared.db.models import CaseAuditLogModel

    where = [CaseAuditLogModel.timestamp < cutoff]
    if tenant_id and hasattr(CaseAuditLogModel, "tenant_id"):
        where.append(CaseAuditLogModel.tenant_id == tenant_id)
    free = await _not_held(session, CaseAuditLogModel.case_id)
    if free is not None:
        where.append(free)
    where = tuple(where)
    if not apply:
        return int((await session.execute(
            select(func.count()).select_from(CaseAuditLogModel).where(*where)
        )).scalar_one() or 0)
    from case_service.compliance import audit_chain
    return await audit_chain.prune_audit_rows(
        session, where, reason="retention", actor="system:retention", cutoff=cutoff)


async def _event_log(session: AsyncSession, cutoff: datetime, apply: bool,
                    tenant_id: str | None = None,
                    exclude_tenants: Sequence[str] | None = None) -> int:
    """Process-mining event log — high volume, lowest sensitivity."""
    from velaris_shared.db.models import CaseEventLogModel

    where = [CaseEventLogModel.timestamp < cutoff]
    if hasattr(CaseEventLogModel, "tenant_id"):
        if tenant_id:
            where.append(CaseEventLogModel.tenant_id == tenant_id)
        elif exclude_tenants:
            where.append(_not_overridden(CaseEventLogModel, exclude_tenants))
    free = await _not_held(session, CaseEventLogModel.case_id)
    if free is not None:
        where.append(free)
    where = tuple(where)
    if not apply:
        return int((await session.execute(
            select(func.count()).select_from(CaseEventLogModel).where(*where)
        )).scalar_one() or 0)
    return int((await session.execute(
        delete(CaseEventLogModel).where(*where)
    )).rowcount or 0)


async def _security_events(session: AsyncSession, cutoff: datetime, apply: bool,
                    tenant_id: str | None = None,
                    exclude_tenants: Sequence[str] | None = None) -> int:
    from velaris_shared.db.models import SecurityEventModel

    where = [SecurityEventModel.timestamp < cutoff]
    if tenant_id and hasattr(SecurityEventModel, "tenant_id"):
        where.append(SecurityEventModel.tenant_id == tenant_id)
    where = tuple(where)
    if not apply:
        return int((await session.execute(
            select(func.count()).select_from(SecurityEventModel).where(*where)
        )).scalar_one() or 0)
    return int((await session.execute(
        delete(SecurityEventModel).where(*where)
    )).rowcount or 0)


async def _not_held(session: AsyncSession, case_id_column):
    """Leave out cases under a legal hold (Oct item 13). None = nothing is held."""
    from case_service.legal_hold import service as legal_hold

    return await legal_hold.not_held(session, case_id_column)


async def _held_due(session: AsyncSession, cutoff: datetime, tenant_id: str | None,
                    exclude_tenants: Sequence[str] | None) -> int:
    """Cases this policy WOULD have reached, kept because of a legal hold.

    Reported, so a run that skips a case says so — a policy that quietly leaves
    rows behind looks, to whoever reads the report, like one that failed.
    """
    from velaris_shared.db.models import CaseInstanceModel
    from case_service.legal_hold import service as legal_hold

    q = select(CaseInstanceModel.id).where(
        CaseInstanceModel.status.in_(("closed", "resolved", "cancelled", "rejected")),
        CaseInstanceModel.updated_at < cutoff)
    if tenant_id:
        q = q.where(CaseInstanceModel.tenant_id == tenant_id)
    elif exclude_tenants:
        q = q.where(_not_overridden(CaseInstanceModel, exclude_tenants))
    return await legal_hold.count_held(session, q)


def _not_overridden(model, exclude_tenants: Sequence[str]):
    """"Every tenant except these" — the filter a platform-default policy needs.

    A default policy exists to cover tenants that have not set their own. Before
    2026-08-06 it ran completely unscoped, so on a multi-tenant install a
    365-day default would delete rows belonging to a tenant whose own policy
    said 2555 days: the override was stored, shown in the admin screen, and
    ignored at the only moment it mattered.

    `tenant_id IS NULL` must stay INCLUDED. Tenant-less rows belong to no tenant
    and are therefore covered by nothing but the default; a bare `NOT IN` drops
    them, because SQL NULL comparisons are never true — and they would then be
    retained forever by a policy that reported success.
    """
    col = model.tenant_id
    return or_(col.is_(None), col.notin_(list(exclude_tenants)))


#: Resource types whose table carries no tenant column at all. A tenant-scoped
#: policy on one of these CANNOT be honoured — and silently applying it
#: platform-wide would delete every tenant's rows while the policy claims to be
#: scoped to one. Refused instead.
UNSCOPABLE = frozenset({"audit_log", "security_events"})


async def _resolve_tenant_key(session: AsyncSession, token: str | None):
    """Map a policy's tenant token to what `case_instances.tenant_id` holds.

    `retention_policies.tenant_id` is a VARCHAR (a slug, usually) while
    `case_instances.tenant_id` is a GUID. Comparing the raw values makes the
    driver try to parse a slug as a UUID and blow up — or, on a looser
    database, silently match nothing and report zero rows for a policy the
    operator believes is running.
    """
    from case_service.tenancy import identity

    resolved = await identity.resolve_tenant(session, token)
    if resolved is None:
        # Unknown tenant means DENY, never "any tenant" — a retention run is
        # the last place to widen a filter that failed to resolve.
        raise RetentionError(
            f"policy names tenant {token!r}, which resolves to no tenant. "
            f"Refusing rather than running unscoped."
        )
    return resolved[0]


#: Resource types this module knows how to enforce. A policy naming anything
#: else is refused rather than ignored — an unknown resource type silently
#: doing nothing is how a policy comes to be trusted without ever running.
HANDLERS = {
    "resolved_cases": _resolved_cases,
    "audit_log": _audit_log,
    "event_log": _event_log,
    "security_events": _security_events,
}


async def apply_policy(
    session: AsyncSession, policy: RetentionPolicyModel, *, apply: bool = False,
    exclude_tenants: Sequence[str] | None = None,
) -> dict[str, Any]:
    """Evaluate one policy. Counts by default; deletes only when `apply`.

    `exclude_tenants` is meaningful only for a **platform-default** policy
    (`tenant_id` NULL): it names the tenants that have their own policy for this
    resource type and must therefore be left alone. `run_all` computes it;
    calling `apply_policy` directly on a default policy without it reproduces
    the pre-2026-08-06 behaviour of overriding every tenant's own retention
    period, so the caller is trusted here and `run_all` is the supported entry
    point.
    """
    result: dict[str, Any] = {
        "resource_type": policy.resource_type,
        "tenant_id": policy.tenant_id,
        "action": policy.action,
        "retention_days": policy.retention_days,
        "applied": False,
        "rows": 0,
    }

    if not policy.enabled:
        result["skipped"] = "policy disabled"
        return result

    if policy.action not in (ARCHIVE, DELETE, ANONYMIZE):
        raise RetentionError(f"unknown retention action {policy.action!r}")

    if policy.action == ANONYMIZE:
        if policy.resource_type != "resolved_cases":
            raise RetentionError(
                f"'anonymize' applies to resolved_cases only — {policy.resource_type!r} "
                f"has no identity to strip, so anonymising it would be a no-op "
                f"dressed as an action"
            )
        cutoff = _cutoff(int(policy.retention_days))
        result["effective_days"] = int(policy.retention_days)
        result["cutoff"] = cutoff.isoformat()
        scope = await _resolve_tenant_key(session, policy.tenant_id) if policy.tenant_id else None
        if scope is None:
            result["excluded_tenants"] = len(exclude_tenants or ())
        result["rows"] = await _anonymize_cases(
            session, cutoff, apply, scope, exclude_tenants)
        result["held_by_legal_hold"] = await _held_due(session, cutoff, scope, exclude_tenants)
        result["applied"] = apply
        if apply:
            policy.last_run_at = datetime.now(timezone.utc)
            session.add(policy)
        return result

    if policy.tenant_id and policy.resource_type in UNSCOPABLE:
        raise RetentionError(
            f"{policy.resource_type!r} has no tenant column, so a policy scoped "
            f"to {policy.tenant_id!r} cannot be honoured. Running it anyway would "
            f"delete every tenant's rows while claiming to be scoped — refused."
        )

    handler = HANDLERS.get(policy.resource_type)
    if handler is None:
        raise RetentionError(
            f"no handler for resource type {policy.resource_type!r} — refusing, "
            f"because a policy that quietly does nothing still looks enforced"
        )

    # Both actions use the policy's own period. What differs between them is
    # intent, not arithmetic — and the admin panel edits exactly this number.
    days = int(policy.retention_days)
    result["effective_days"] = days
    cutoff = _cutoff(days)
    result["cutoff"] = cutoff.isoformat()
    scope = await _resolve_tenant_key(session, policy.tenant_id) if policy.tenant_id else None
    if scope is None:
        # Recorded even when zero, so a dry-run report shows plainly whether the
        # default was narrowed and by how much.
        result["excluded_tenants"] = len(exclude_tenants or ())
    result["rows"] = await handler(session, cutoff, apply, scope, exclude_tenants)
    if policy.resource_type == "resolved_cases":
        result["held_by_legal_hold"] = await _held_due(session, cutoff, scope, exclude_tenants)
    result["applied"] = apply

    if apply:
        policy.last_run_at = datetime.now(timezone.utc)
        session.add(policy)

    return result


async def run_all(
    session: AsyncSession, *, apply: bool = False, tenant_id: str | None = None,
) -> dict[str, Any]:
    """Evaluate every enabled policy.

    One failing policy does not stop the others — a policy with an unknown
    resource type should not prevent the valid ones from running.
    """
    q = select(RetentionPolicyModel)
    if tenant_id is not None:
        q = q.where(RetentionPolicyModel.tenant_id == tenant_id)
    policies = (await session.execute(q)).scalars().all()

    # Which tenants have overridden each resource type. A platform-default
    # policy must skip them, or the override it is meant to defer to is
    # silently overruled — the number in the admin screen would be shown,
    # stored, and then ignored at the one moment it matters.
    #
    # Computed across ALL enabled policies, not just this run's selection: a
    # tenant's override exists whether or not the caller happened to ask for it.
    overrides: dict[str, list[str]] = {}
    if tenant_id is None:
        every = (await session.execute(select(RetentionPolicyModel))).scalars().all()
        for p in every:
            if not p.tenant_id or not p.enabled:
                continue
            try:
                key = await _resolve_tenant_key(session, p.tenant_id)
            except RetentionError:
                # An override naming an unresolvable tenant cannot be honoured,
                # and it must NOT quietly widen the default back to everyone —
                # `apply_policy` refuses that policy on its own turn. Skipping
                # it here only means the default keeps its normal scope.
                continue
            overrides.setdefault(p.resource_type, []).append(key)

    results: list[dict[str, Any]] = []
    errors: list[str] = []
    for policy in policies:
        try:
            results.append(await apply_policy(
                session, policy, apply=apply,
                exclude_tenants=overrides.get(policy.resource_type),
            ))
        except RetentionError as exc:
            errors.append(f"{policy.resource_type}: {exc}")
            results.append({
                "resource_type": policy.resource_type,
                "action": policy.action,
                "error": str(exc),
                "rows": 0,
                "applied": False,
            })

    return {
        "dry_run": not apply,
        "tenant_id": tenant_id,
        "policies": results,
        "total_rows": sum(int(r.get("rows") or 0) for r in results),
        "errors": errors,
    }
