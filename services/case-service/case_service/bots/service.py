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

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from case_service.bots import tokens
from case_service.db.models import (
    BotDefinitionModel,
    BotJobModel,
    BotRunEventModel,
)

log = logging.getLogger(__name__)

#: `light` keeps the Layer-2 pid cap. `browser` raises it because a Chromium
#: process tree exceeds 64 immediately — and gets a larger tmpfs, because
#: Playwright needs writable directories. **Never `read_only=False`**: a browser
#: needs writable dirs, not a writable root filesystem.
RESOURCE_PROFILES: dict[str, dict[str, Any]] = {
    "light": {"pids_limit": 64, "tmpfs": {"/tmp": "size=64m"}},
    "browser": {
        "pids_limit": 512,
        "tmpfs": {"/tmp": "size=512m", "/home/pwuser/.cache": "size=256m"},
    },
}

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
    if (bot.created_by or "").strip().lower() == (approver or "").strip().lower():
        raise BotError(
            "A bot must be approved by somebody other than its author. "
            "Approval is the review, and reviewing your own container image is "
            "not one.", status=409)

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
    )
    session.add(job)
    await session.flush()
    await record_event(session, job.id, "queued", {"trigger": trigger})
    return job


async def lease_next(
    session: AsyncSession, *, lease_seconds: int = 600, now: datetime | None = None,
) -> BotJobModel | None:
    """Claim one queued job, atomically.

    The claim is a conditional UPDATE whose WHERE still names the old status, so
    two runners racing for the same row cannot both win: the loser's rowcount is
    zero. That, plus `lease_expires_at`, is what makes a second runner safe
    without a distributed lock — and what makes a crashed runner's job come back
    rather than being lost.
    """
    now = now or _utcnow()

    candidates = (await session.execute(
        select(BotJobModel).where(BotJobModel.status.in_(("queued", "leased")))
        .order_by(BotJobModel.queued_at).limit(20)
    )).scalars().all()

    for job in candidates:
        expired = job.status == "leased" and (
            job.lease_expires_at is None or _aware(job.lease_expires_at) <= now)
        if job.status != "queued" and not expired:
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


async def at_concurrency_limit(
    session: AsyncSession, bot: BotDefinitionModel,
) -> bool:
    running = (await session.execute(
        select(BotJobModel).where(
            BotJobModel.bot_id == bot.id,
            BotJobModel.status.in_(("leased", "running")),
        )
    )).scalars().all()
    return len(running) >= max(1, bot.max_concurrency)


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
    from case_service.db.models import CaseInstanceModel

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
