"""Bot definitions, the job queue, and the result path.

Three things here carry the security of the feature, and each is a refusal
rather than a convention:

* **approval** — a definition is `draft` until an admin who is *not* the creator
  approves it. One person cannot both write a bot and put it into production;
* **wildcard egress with credentials** — a bot holding a supplier password and
  `granted_domains: ["*"]` is a data-exfiltration tool, so that combination is
  refused at approval, not warned about;
* **the result path** — a per-job, single-use token over the job's own row, an
  output validated against the bot's declared `output_schema`, and the case
  write performed by the platform. The container never holds mutation authority
  over anything else, and the general broker stays read-only.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from case_service.bots import tokens
from velaris_shared.db.models import (
    BotDefinitionModel,
    BotJobModel,
    BotRunEventModel,
)

log = logging.getLogger(__name__)

#: `light` keeps the Layer-2 pid cap. `browser` raises it because a Chromium
#: process tree exceeds 64 immediately — and gets a larger tmpfs, because
#: Playwright needs writable directories. **Never `read_only=False`**: a browser
#: needs writable dirs, not a writable root filesystem.
#: `mem_limit` is per profile because **tmpfs counts against the container's own
#: memory cgroup**. A browser profile is given 768m of scratch (512m + 256m), so
#: under the Layer-2 default of 512m it could be OOM-killed by filling the
#: directories it was given to fill — a failure that reads as a flaky website
#: rather than as a misconfiguration. `None` means "the platform default".
RESOURCE_PROFILES: dict[str, dict[str, Any]] = {
    "light": {"pids_limit": 64, "tmpfs": {"/tmp": "size=64m"}, "mem_limit": None},
    "browser": {
        "pids_limit": 512,
        "tmpfs": {"/tmp": "size=512m", "/home/pwuser/.cache": "size=256m"},
        "mem_limit": "browser",     # resolved from settings at provision time
    },
}


def tmpfs_megabytes(profile: dict) -> int:
    """Total scratch a profile hands out, in MB — which is also memory it may
    consume. Used to prove a profile cannot be sized to kill itself."""
    total = 0
    for spec in (profile.get("tmpfs") or {}).values():
        digits = "".join(c for c in str(spec) if c.isdigit())
        if digits:
            total += int(digits)
    return total


def profile_mem_limit(name: str) -> str | None:
    """The memory ceiling for a profile, or None to use the platform default."""
    from velaris_shared.config import get_settings

    value = (RESOURCE_PROFILES.get(name) or {}).get("mem_limit")
    if value == "browser":
        return getattr(get_settings(), "bot_browser_mem_limit", "1536m")
    return value

TERMINAL_STATUSES = ("succeeded", "failed", "timed_out", "cancelled")

#: The connector path's backoff, reused rather than reinvented.
RETRY_BACKOFF_SECONDS = [60, 300, 1800]
MAX_ATTEMPTS = len(RETRY_BACKOFF_SECONDS)

#: A bot result is a JSON document, not an upload.
MAX_RESULT_BYTES = 256 * 1024


class BotError(Exception):
    def __init__(self, message: str, *, status: int = 400):
        super().__init__(message)
        self.status = status


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


async def record_event(
    session: AsyncSession, job_id, kind: str, detail: dict | None = None,
) -> None:
    session.add(BotRunEventModel(job_id=job_id, kind=kind, detail=detail or {}))
    await session.flush()


# ── definitions ───────────────────────────────────────────────────────────

def validate_definition(bot: BotDefinitionModel) -> None:
    """The checks that must hold before a bot may ever run."""
    if not str(bot.digest or "").startswith("sha256:"):
        raise BotError(
            "A bot image must be pinned by digest (sha256:…). A tag can be "
            "repointed at different content after review, which would make the "
            "approval meaningless.")
    if bot.resource_profile not in RESOURCE_PROFILES:
        raise BotError(
            f"resource_profile must be one of: {', '.join(RESOURCE_PROFILES)}")
    if bot.timeout_seconds <= 0 or bot.timeout_seconds > 3600:
        raise BotError("timeout_seconds must be between 1 and 3600.")
    _assert_egress_safe(bot)


def _assert_egress_safe(bot: BotDefinitionModel) -> None:
    """A credential-holding bot may not have wildcard egress.

    Refused rather than warned about: a bot with a supplier password and
    permission to reach anywhere is a data-exfiltration tool, and the two halves
    are individually reasonable, which is exactly why the combination has to be
    checked in one place.
    """
    domains = [str(d).strip() for d in (bot.granted_domains or [])]
    if not bot.credential_refs:
        return
    if any(d in ("*", "*.*") or d.startswith("*") for d in domains):
        raise BotError(
            "A bot holding credentials cannot be granted wildcard egress: with "
            "a live password and permission to reach anywhere, it is an "
            "exfiltration tool. Name the hosts it needs.")


async def approve(
    session: AsyncSession, bot: BotDefinitionModel, *, approver: str,
) -> BotDefinitionModel:
    """Move a definition to `active`.

    Separate from create, and **not by the creator** — the same separation the
    Layer-2 grant flow already enforces. One person writing a container image
    and putting it into production unreviewed is the whole risk here.
    """
    if bot.status == "active":
        raise BotError("This bot is already active.", status=409)

    # The author AND the last editor, not just the author. `created_by` never
    # changes, so comparing against it alone let the reviewer approve a bot, then
    # repoint it at a different image with a wider egress allowlist and approve
    # their own change — the author's name was still on the row, so the check
    # passed. Proven against a running service on 2026-08-20.
    me = (approver or "").strip().lower()
    for name, whose in ((bot.created_by, "its author"),
                        (bot.last_edited_by, "the person who last changed it")):
        if (name or "").strip().lower() == me:
            raise BotError(
                f"A bot must be approved by somebody other than {whose}. "
                f"Approval is the review, and reviewing your own container image "
                f"is not one. If you are the reviewer, ask the author to make "
                f"the change rather than making it yourself — the same "
                f"discipline as code review.", status=409)

    validate_definition(bot)
    bot.status = "active"
    bot.approved_by = approver
    bot.approved_at = _utcnow()
    await session.flush()
    return bot


# ── queue ─────────────────────────────────────────────────────────────────

async def enqueue(
    session: AsyncSession,
    *,
    bot: BotDefinitionModel,
    input_data: dict,
    trigger: str,
    requested_by: str | None = None,
    case_id: uuid.UUID | None = None,
    step_id: str | None = None,
    process_instance_id: uuid.UUID | None = None,
    node_id: str | None = None,
    run_after: datetime | None = None,
    batch_id: uuid.UUID | None = None,
    batch_index: int | None = None,
    batch_size: int | None = None,
) -> BotJobModel:
    """Queue one execution. Does not run it — the runner leases it."""
    if bot.status != "active":
        raise BotError(
            f"Bot '{bot.name}' is {bot.status}, so it cannot be run. An "
            f"unapproved bot has not been reviewed.", status=409)

    job = BotJobModel(
        tenant_id=bot.tenant_id, bot_id=bot.id, case_id=case_id, step_id=step_id,
        process_instance_id=process_instance_id, node_id=node_id,
        trigger=trigger, requested_by=requested_by,
        input=input_data or {}, artifacts=[], status="queued",
        run_after=run_after, batch_id=batch_id,
        batch_index=batch_index, batch_size=batch_size,
    )
    session.add(job)
    await session.flush()
    await record_event(session, job.id, "queued", {"trigger": trigger})
    return job


async def enqueue_run(
    session: AsyncSession,
    *,
    bot: BotDefinitionModel,
    input_data: dict,
    trigger: str,
    **kwargs,
) -> list[BotJobModel]:
    """Queue one job, or a fan-out batch when the definition asks for one.

    Fan-out is **N ordinary jobs sharing a `batch_id`** — one queue, one lease
    path, one set of limits. A second execution mechanism would need its own
    concurrency ceiling, its own retry ladder and its own failure semantics, and
    would get one of them wrong.

    The collection key lives on the *definition* because approval is the review:
    somebody approving a bot has to be able to see that one call can start
    `fanout_max` containers holding leased credentials. It is bounded rather than
    truncated — quietly processing the first 25 of 400 rows and reporting success
    is worse than refusing.
    """
    key = (bot.fanout_input_key or "").strip()
    if not key:
        return [await enqueue(
            session, bot=bot, input_data=input_data, trigger=trigger, **kwargs)]

    if key not in (input_data or {}):
        raise BotError(
            f"This bot fans out over '{key}', which is not in the input.")
    items = (input_data or {})[key]
    if not isinstance(items, list):
        raise BotError(f"Input '{key}' must be a list to fan out over.")
    if not items:
        raise BotError(f"Input '{key}' is empty — there is nothing to run.")
    limit = max(1, bot.fanout_max or 25)
    if len(items) > limit:
        raise BotError(
            f"This run would start {len(items)} jobs and the bot's fan-out limit "
            f"is {limit}. Raise the limit deliberately — and have it re-approved "
            f"— rather than running a batch nobody reviewed.")

    batch_id = uuid.uuid4()
    jobs: list[BotJobModel] = []
    for index, item in enumerate(items):
        child = dict(input_data or {})
        child[key] = item
        jobs.append(await enqueue(
            session, bot=bot, input_data=child, trigger=trigger,
            batch_id=batch_id, batch_index=index, batch_size=len(items),
            **kwargs))
    return jobs


async def batch_summary(session: AsyncSession, batch_id: uuid.UUID) -> dict:
    """How a fan-out batch is doing, and whether it is finished.

    `finished` is every child in a terminal status — the condition a suspended
    `botTask` resumes on, so it is computed in one place rather than in the
    engine and the API separately.
    """
    rows = (await session.execute(
        select(BotJobModel).where(BotJobModel.batch_id == batch_id)
        .order_by(BotJobModel.batch_index)
    )).scalars().all()
    if not rows:
        raise BotError("Batch not found.", status=404)

    counts: dict[str, int] = {}
    for job in rows:
        counts[job.status] = counts.get(job.status, 0) + 1
    return {
        "batch_id": str(batch_id),
        "bot_id": str(rows[0].bot_id),
        "size": rows[0].batch_size or len(rows),
        "counts": counts,
        "finished": all(j.status in TERMINAL_STATUSES for j in rows),
        "succeeded": counts.get("succeeded", 0),
        "jobs": [str(j.id) for j in rows],
    }


async def lease_next(
    session: AsyncSession, *, lease_seconds: int = 600, now: datetime | None = None,
) -> BotJobModel | None:
    """Claim one runnable job, atomically.

    The claim is a conditional UPDATE whose WHERE still names the old status, so
    two runners racing for the same row cannot both win: the loser's rowcount is
    zero. That, plus `lease_expires_at`, is what makes a second runner safe
    without a distributed lock — and what makes a crashed runner's job come back
    rather than being lost.

    **The limits are checked here, before the claim, not by the caller after it.**
    They used to be checked afterwards, against a table that already contained
    the job the caller had just leased — so a definition at the default
    `max_concurrency=1` counted itself, declared the bot full, and put the job
    back. Every job, forever. Checking first also means a blocked bot costs the
    job nothing: no lease, no `attempt` spent, and the scan moves on to the next
    candidate instead of stalling behind it.

    `run_after` is honoured here too, which is what makes the retry backoff and
    a scheduled run real rather than advisory.
    """
    now = now or _utcnow()

    candidates = (await session.execute(
        select(BotJobModel).where(
            BotJobModel.status.in_(("queued", "leased")),
            or_(BotJobModel.run_after.is_(None), BotJobModel.run_after <= now),
        ).order_by(BotJobModel.queued_at).limit(50)
    )).scalars().all()

    for job in candidates:
        expired = job.status == "leased" and (
            job.lease_expires_at is None or _aware(job.lease_expires_at) <= now)
        if job.status != "queued" and not expired:
            continue

        bot = await _locked_definition(session, job.bot_id)
        # A job whose definition is gone is still leased: the runner's own
        # failure path is what reports that, and skipping it here would leave the
        # job queued forever with nobody explaining why.
        if bot is not None and await at_capacity(
                session, bot, exclude_job_id=job.id, now=now):
            continue

        result = await session.execute(
            update(BotJobModel)
            .where(BotJobModel.id == job.id, BotJobModel.status == job.status)
            .values(status="leased",
                    lease_expires_at=now + timedelta(seconds=lease_seconds),
                    attempt=BotJobModel.attempt + 1)
        )
        if (result.rowcount or 0) == 1:
            await session.flush()
            await session.refresh(job)
            await record_event(session, job.id, "leased", {"attempt": job.attempt})
            return job

    return None


async def _locked_definition(session: AsyncSession, bot_id):
    """Fetch the definition, holding a row lock where the database has one.

    The capacity check and the claim have to be one decision. Inside a process an
    asyncio lock in the runner does that; across processes — two case-service
    containers on the same database — only the database can, so the definition
    row is locked for the duration. SQLite has no row locks and is single-writer
    anyway, so it falls back to a plain read rather than failing.
    """
    dialect = getattr(getattr(session, "bind", None), "dialect", None)
    name = getattr(dialect, "name", "")
    if name in ("postgresql", "mysql", "mariadb"):
        try:
            return (await session.execute(
                select(BotDefinitionModel)
                .where(BotDefinitionModel.id == bot_id)
                .with_for_update()
            )).scalars().first()
        except Exception:
            log.debug("row lock unavailable; falling back", exc_info=True)
    return await session.get(BotDefinitionModel, bot_id)


async def _live_jobs(
    session: AsyncSession, *, bot_id=None, tenant_id: str | None = None,
    exclude_job_id=None,
) -> int:
    """Count jobs holding a container slot right now."""
    clauses = [BotJobModel.status.in_(("leased", "running"))]
    if bot_id is not None:
        clauses.append(BotJobModel.bot_id == bot_id)
    if tenant_id is not None:
        clauses.append(BotJobModel.tenant_id == tenant_id)
    if exclude_job_id is not None:
        clauses.append(BotJobModel.id != exclude_job_id)
    rows = (await session.execute(
        select(BotJobModel.id).where(*clauses)
    )).scalars().all()
    return len(rows)


async def tenant_limit(session: AsyncSession, tenant_id: str) -> int:
    """The tenant's ceiling: its own row, else the platform default.

    Absent a row the default applies, so the cap is never *off* — a missing
    override must not read as "unlimited", which is the failure mode a
    row-means-enabled design has.
    """
    from velaris_shared.config import get_settings
    from velaris_shared.db.models import BotTenantLimitModel

    row = await session.get(BotTenantLimitModel, tenant_id)
    if row is not None and row.max_concurrent_jobs > 0:
        return row.max_concurrent_jobs
    return max(1, int(getattr(get_settings(), "bot_tenant_max_concurrent", 4)))


async def at_capacity(
    session: AsyncSession, bot: BotDefinitionModel, *,
    exclude_job_id=None, now: datetime | None = None,
) -> bool:
    """True when this bot, or its tenant, has no slot free.

    Two ceilings in one place: `max_concurrency` bounds the definition — the
    limit exists because a bot drives a third party's system — and the tenant
    pool bounds all of that tenant's bots together, because they land as
    containers on one host.
    """
    per_bot = await _live_jobs(
        session, bot_id=bot.id, exclude_job_id=exclude_job_id)
    if per_bot >= max(1, bot.max_concurrency):
        return True

    per_tenant = await _live_jobs(
        session, tenant_id=bot.tenant_id, exclude_job_id=exclude_job_id)
    return per_tenant >= await tenant_limit(session, bot.tenant_id)


async def at_concurrency_limit(
    session: AsyncSession, bot: BotDefinitionModel, *, exclude_job_id=None,
) -> bool:
    """Kept for callers that mean the per-definition limit specifically."""
    per_bot = await _live_jobs(
        session, bot_id=bot.id, exclude_job_id=exclude_job_id)
    return per_bot >= max(1, bot.max_concurrency)


# ── result ────────────────────────────────────────────────────────────────

def validate_output(output: Any, schema: dict) -> None:
    """Check a bot's output against its declared schema.

    A deliberately small subset of JSON Schema — `type`, `required`,
    `properties` with their types. The alternative was a dependency whose full
    grammar runs on input from a container, and the properties worth enforcing
    here are the ones a case write depends on.
    """
    if not schema:
        return
    expected = schema.get("type")
    if expected == "object" and not isinstance(output, dict):
        raise BotError("Bot output must be a JSON object.")
    if expected == "array" and not isinstance(output, list):
        raise BotError("Bot output must be a JSON array.")
    if not isinstance(output, dict):
        return

    for field in schema.get("required") or []:
        if field not in output:
            raise BotError(f"Bot output is missing the required field '{field}'.")

    types = {
        "string": str, "number": (int, float), "integer": int,
        "boolean": bool, "object": dict, "array": list,
    }
    for field, spec in (schema.get("properties") or {}).items():
        if field not in output or not isinstance(spec, dict):
            continue
        wanted = types.get(spec.get("type"))
        if wanted and not isinstance(output[field], wanted):
            raise BotError(
                f"Bot output field '{field}' should be {spec.get('type')}.")


async def redeem_result(
    session: AsyncSession,
    *,
    job_id: uuid.UUID,
    presented_token: str | None,
    output: Any,
    artifacts: list | None = None,
    now: datetime | None = None,
) -> BotJobModel:
    """Accept a bot's result, once.

    Every refusal below raises the *same* 404, because the caller is a container
    on the apps network and a distinct "already used" would tell whoever holds a
    stolen token that it was genuine.
    """
    now = now or _utcnow()
    refusal = BotError("Not found.", status=404)

    job = await session.get(BotJobModel, job_id)
    if job is None:
        raise refusal
    if not tokens.matches(presented_token, job.result_token_hash):
        raise refusal
    if job.result_token_used_at is not None:
        raise refusal
    if job.status in TERMINAL_STATUSES:
        raise refusal

    # Burn the token before anything else can fail. A validation error must not
    # leave the token spendable for a second attempt with different content.
    job.result_token_used_at = now

    bot = await session.get(BotDefinitionModel, job.bot_id)
    try:
        validate_output(output, (bot.output_schema if bot else {}) or {})
    except BotError as exc:
        job.status = "failed"
        job.error = str(exc)
        job.ended_at = now
        await record_event(session, job.id, "error", {"reason": "output_schema"})
        await session.flush()
        raise BotError(str(exc), status=422) from exc

    job.output = output if isinstance(output, dict) else {"value": output}
    job.artifacts = list(artifacts or [])
    job.status = "succeeded"
    job.ended_at = now
    await record_event(session, job.id, "result", {"keys": list(job.output.keys())})
    await session.flush()
    return job


async def apply_to_case(session: AsyncSession, job: BotJobModel) -> bool:
    """Write a succeeded job's output onto its case, under the platform's authority.

    **Case variables only.** A bot cannot change status, assignment or
    permissions — those are decisions the workflow and the operator own, and a
    container that could make them would be a privilege-escalation path dressed
    up as an integration.

    An attended job is not applied here: its output is a *proposal* the operator
    accepts, which is what makes an agent-triggered bot safe.
    """
    from velaris_shared.db.models import CaseInstanceModel

    if job.case_id is None or job.status != "succeeded" or job.trigger == "agent":
        return False

    case = await session.get(CaseInstanceModel, job.case_id)
    if case is None:
        return False

    data = dict(case.data or {})
    for key, value in (job.output or {}).items():
        if str(key).startswith("_"):
            continue                        # platform-internal keys are ours
        data[str(key)] = value
    case.data = data
    await session.flush()
    return True


async def fail(
    session: AsyncSession, job: BotJobModel, *, error: str,
    status: str = "failed", now: datetime | None = None,
) -> None:
    now = now or _utcnow()
    job.status = status
    job.error = (error or "")[:2000]
    job.ended_at = now
    await record_event(session, job.id, "error", {"status": status})
    await session.flush()


async def requeue_or_fail(
    session: AsyncSession, job: BotJobModel, *, error: str,
) -> str:
    """Retry with the connector path's backoff, then give up visibly.

    Reusing `[60, 300, 1800]` rather than inventing a schedule keeps one answer
    to "how does an integration failure behave here".
    """
    if job.attempt >= MAX_ATTEMPTS:
        await fail(session, job, error=error)
        return "failed"

    delay = RETRY_BACKOFF_SECONDS[min(job.attempt, MAX_ATTEMPTS - 1) - 1]
    job.status = "queued"
    job.lease_expires_at = None
    job.container_id = None
    # The delay has to land on the ROW. It used to be written only into the run
    # event below, where nothing could read it: `lease_next` had no time filter,
    # so a failed job came back on the next five-second cycle and the whole
    # [60, 300, 1800] ladder was spent in fifteen seconds.
    job.run_after = _utcnow() + timedelta(seconds=delay)
    job.error = (error or "")[:2000]
    await record_event(session, job.id, "error",
                       {"retry_in_seconds": delay, "attempt": job.attempt})
    await session.flush()
    return "requeued"


async def mint_result_token(session: AsyncSession, job: BotJobModel) -> str:
    plaintext, digest = tokens.mint()
    job.result_token_hash = digest
    job.result_token_used_at = None
    await session.flush()
    return plaintext
