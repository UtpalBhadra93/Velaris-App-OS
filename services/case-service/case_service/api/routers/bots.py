"""RPA bots — registry, queue, results, and the attended review gate.

Two kinds of caller, authenticated differently on purpose:

* **operators and admins** use a normal Velaris JWT, gated by HxGuard;
* **the bot container** holds a **per-job, single-use token** and can do exactly
  one thing with it: POST that job's result. It has no other authority, which is
  what let the scoped broker stay read-only instead of gaining write scopes that
  every container on the apps network would then carry, permanently, to serve
  this one feature.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from case_service import hxguard
from case_service.auth.models import AuthenticatedUser
from case_service.bots import credentials as bot_credentials
from case_service.bots import service
from case_service.db.models import (
    BotCredentialModel,
    BotDefinitionModel,
    BotJobModel,
    BotRunEventModel,
)
from case_service.db.session import get_session
from case_service.middleware.endpoint_rate_limit import rate_limit

log = logging.getLogger(__name__)

router = APIRouter(prefix="/bots", tags=["bots"])

#: The result and progress endpoints are reachable from the apps network and
#: authenticate on a per-job token, so they get their own bound.
_result_rl = rate_limit(max_calls=120, window_seconds=60, name="bot-result")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _bot_dict(b: BotDefinitionModel) -> dict:
    return {
        "id": str(b.id), "tenant_id": b.tenant_id, "name": b.name,
        "description": b.description, "image": b.image, "digest": b.digest,
        "signature_verified": b.signature_verified,
        "declared_command": b.declared_command or [],
        "declared_env": b.declared_env or {},
        "credential_refs": b.credential_refs or [],
        "input_schema": b.input_schema or {},
        "output_schema": b.output_schema or {},
        "granted_domains": b.granted_domains or [],
        "timeout_seconds": b.timeout_seconds,
        "max_concurrency": b.max_concurrency,
        "resource_profile": b.resource_profile,
        "status": b.status, "created_by": b.created_by,
        "approved_by": b.approved_by,
        "approved_at": b.approved_at.isoformat() if b.approved_at else None,
    }


def _job_dict(j: BotJobModel) -> dict:
    return {
        "id": str(j.id), "bot_id": str(j.bot_id), "tenant_id": j.tenant_id,
        "case_id": str(j.case_id) if j.case_id else None,
        "step_id": j.step_id, "node_id": j.node_id,
        "process_instance_id": str(j.process_instance_id) if j.process_instance_id else None,
        "trigger": j.trigger, "requested_by": j.requested_by,
        "input": j.input or {}, "output": j.output,
        "artifacts": j.artifacts or [],
        "status": j.status, "attempt": j.attempt, "error": j.error,
        "queued_at": j.queued_at.isoformat() if j.queued_at else None,
        "started_at": j.started_at.isoformat() if j.started_at else None,
        "ended_at": j.ended_at.isoformat() if j.ended_at else None,
        # The token hash is never returned in any shape — it is the credential.
    }


# ── definitions ───────────────────────────────────────────────────────────

class BotIn(BaseModel):
    tenant_id: str = "default"
    name: str = Field(..., max_length=255)
    description: str = ""
    image: str = Field(..., max_length=512)
    digest: str = Field(..., max_length=80)
    declared_command: list[str] = []
    declared_env: dict = {}
    credential_refs: list[str] = []
    input_schema: dict = {}
    output_schema: dict = {}
    granted_domains: list[str] = []
    timeout_seconds: int = Field(300, ge=1, le=3600)
    max_concurrency: int = Field(1, ge=1, le=32)
    resource_profile: str = "light"


@router.get("")
async def list_bots(
    session: AsyncSession = Depends(get_session),
    _user: AuthenticatedUser = Depends(hxguard.guard("bots.definitions.read")),
):
    rows = (await session.execute(
        select(BotDefinitionModel).order_by(BotDefinitionModel.name)
    )).scalars().all()
    return {"bots": [_bot_dict(b) for b in rows]}


@router.post("", status_code=201)
async def create_bot(
    body: BotIn,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(hxguard.guard("bots.definitions.write")),
):
    """Register a bot. It is created **draft** and cannot run until approved."""
    bot = BotDefinitionModel(
        tenant_id=body.tenant_id, name=body.name, description=body.description,
        image=body.image, digest=body.digest,
        declared_command=body.declared_command, declared_env=body.declared_env,
        credential_refs=body.credential_refs,
        input_schema=body.input_schema, output_schema=body.output_schema,
        granted_domains=body.granted_domains,
        timeout_seconds=body.timeout_seconds,
        max_concurrency=body.max_concurrency,
        resource_profile=body.resource_profile,
        status="draft", created_by=user.username,
    )
    try:
        service.validate_definition(bot)
    except service.BotError as exc:
        raise HTTPException(exc.status, str(exc)) from exc

    session.add(bot)
    await session.commit()
    await session.refresh(bot)
    return _bot_dict(bot)


@router.post("/{bot_id}/approve")
async def approve_bot(
    bot_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(hxguard.guard("bots.definitions.approve")),
):
    """Put a bot into production. Deliberately separate from create, and never
    by its author — approval *is* the review."""
    bot = await session.get(BotDefinitionModel, bot_id)
    if bot is None:
        raise HTTPException(404, "Bot not found.")
    try:
        await service.approve(session, bot, approver=user.username)
    except service.BotError as exc:
        raise HTTPException(exc.status, str(exc)) from exc
    await session.commit()

    from case_service.enterprise.security_events import log_security_event
    await log_security_event(
        session, event_type="bot_approved", severity="high",
        user_id=user.user_id, resource_type="bot_definition", resource_id=str(bot.id),
        action="approve", outcome="success",
        details={"name": bot.name, "image": bot.image, "digest": bot.digest,
                 "granted_domains": bot.granted_domains or [],
                 "credential_refs": bot.credential_refs or []},
    )
    await session.commit()
    await session.refresh(bot)
    return _bot_dict(bot)


@router.post("/{bot_id}/disable")
async def disable_bot(
    bot_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(hxguard.guard("bots.definitions.write")),
):
    """The kill switch: disable, then cancel everything in flight.

    Identical in spirit to revoking a Layer-2 grant — a bot that has started
    misbehaving must stop now, not at the end of its queue.
    """
    bot = await session.get(BotDefinitionModel, bot_id)
    if bot is None:
        raise HTTPException(404, "Bot not found.")
    bot.status = "disabled"

    live = (await session.execute(
        select(BotJobModel).where(
            BotJobModel.bot_id == bot.id,
            BotJobModel.status.in_(("queued", "leased", "running")),
        )
    )).scalars().all()
    for job in live:
        await service.fail(session, job, error="Bot disabled.", status="cancelled")
        if job.container_id:
            try:
                from case_service.marketplace.runtime import stop_app_container

                stop_app_container(job.container_id)
            except Exception:
                log.exception("could not stop container for cancelled job %s", job.id)
    await session.commit()
    return {"disabled": True, "cancelled_jobs": len(live)}


# ── credentials ───────────────────────────────────────────────────────────

class CredentialIn(BaseModel):
    tenant_id: str = "default"
    ref: str = Field(..., max_length=512)
    secret: str = Field(..., max_length=4096)


@router.get("/credentials")
async def list_credentials(
    session: AsyncSession = Depends(get_session),
    _user: AuthenticatedUser = Depends(hxguard.guard("bots.credentials.write")),
):
    """Refs and metadata only. The value is never returned, to anyone."""
    rows = (await session.execute(
        select(BotCredentialModel).order_by(BotCredentialModel.ref)
    )).scalars().all()
    return {"credentials": [{
        "id": str(r.id), "tenant_id": r.tenant_id, "ref": r.ref,
        "version": r.version, "created_by": r.created_by,
        "env_name": bot_credentials.env_name_for(r.ref),
        "updated_at": r.updated_at.isoformat() if r.updated_at else None,
    } for r in rows]}


@router.put("/credentials")
async def upsert_credential(
    body: CredentialIn,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(hxguard.guard("bots.credentials.write")),
):
    """Store or rotate a bot credential.

    Encrypted under the tenant's HxVault DEK, so a database dump yields nothing
    and crypto-shredding the tenant destroys it with everything else of theirs.
    Rotating bumps `version`, which is what makes outstanding leases dead rather
    than merely stale.
    """
    from case_service.hxbridge.encryption import encrypt_credentials
    from case_service.hxvault.keyring import ensure_dek
    from case_service.tenancy.identity import resolve_tenant

    env_name = bot_credentials.env_name_for(body.ref)
    if env_name in bot_credentials.RESERVED_ENV:
        raise HTTPException(
            400, f"'{body.ref}' maps to the reserved environment variable "
                 f"'{env_name}'. Rename the ref.")

    resolved = await resolve_tenant(session, body.tenant_id)
    vault = False
    if resolved is not None:
        try:
            await ensure_dek(session, resolved[0])
            vault = True
        except Exception:
            # A tenant without a usable DEK falls back to the platform key
            # rather than storing plaintext.
            log.warning("no HxVault DEK for %s — using the platform key", body.tenant_id)

    row = (await session.execute(
        select(BotCredentialModel).where(
            BotCredentialModel.tenant_id == body.tenant_id,
            BotCredentialModel.ref == body.ref,
        )
    )).scalar_one_or_none()

    value_enc = encrypt_credentials(
        {"secret": body.secret},
        tenant_id=resolved[0] if resolved else None, vault=vault,
    )
    if row is None:
        row = BotCredentialModel(
            tenant_id=body.tenant_id, ref=body.ref, value_enc=value_enc,
            created_by=user.username, version=1,
        )
        session.add(row)
    else:
        row.value_enc = value_enc
        row.version = (row.version or 1) + 1

    await session.commit()
    await session.refresh(row)
    return {"ref": row.ref, "version": row.version,
            "env_name": bot_credentials.env_name_for(row.ref)}


# ── jobs ──────────────────────────────────────────────────────────────────

class RunRequest(BaseModel):
    input: dict = {}
    case_id: Optional[uuid.UUID] = None
    step_id: Optional[str] = None


@router.post("/{bot_id}/run", status_code=202)
async def run_bot(
    bot_id: uuid.UUID,
    body: RunRequest,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(hxguard.guard("bots.jobs.run")),
):
    """Queue a job. 202, because the runner leases it — this does not block."""
    bot = await session.get(BotDefinitionModel, bot_id)
    if bot is None:
        raise HTTPException(404, "Bot not found.")
    try:
        job = await service.enqueue(
            session, bot=bot, input_data=body.input, trigger="agent",
            requested_by=user.username, case_id=body.case_id, step_id=body.step_id,
        )
    except service.BotError as exc:
        raise HTTPException(exc.status, str(exc)) from exc
    await session.commit()
    return _job_dict(job)


@router.get("/jobs")
async def list_jobs(
    status: Optional[str] = Query(None),
    case_id: Optional[uuid.UUID] = Query(None),
    bot_id: Optional[uuid.UUID] = Query(None),
    limit: int = Query(50, ge=1, le=200),
    session: AsyncSession = Depends(get_session),
    _user: AuthenticatedUser = Depends(hxguard.guard("bots.jobs.read")),
):
    stmt = select(BotJobModel)
    if status:
        stmt = stmt.where(BotJobModel.status == status)
    if case_id:
        stmt = stmt.where(BotJobModel.case_id == case_id)
    if bot_id:
        stmt = stmt.where(BotJobModel.bot_id == bot_id)
    rows = (await session.execute(
        stmt.order_by(desc(BotJobModel.queued_at)).limit(limit)
    )).scalars().all()
    return {"jobs": [_job_dict(j) for j in rows]}


@router.get("/jobs/{job_id}")
async def get_job(
    job_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    _user: AuthenticatedUser = Depends(hxguard.guard("bots.jobs.read")),
):
    job = await session.get(BotJobModel, job_id)
    if job is None:
        raise HTTPException(404, "Job not found.")
    events = (await session.execute(
        select(BotRunEventModel).where(BotRunEventModel.job_id == job.id)
        .order_by(BotRunEventModel.created_at)
    )).scalars().all()
    return {
        "job": _job_dict(job),
        "events": [{"kind": e.kind, "detail": e.detail or {},
                    "at": e.created_at.isoformat() if e.created_at else None}
                   for e in events],
    }


@router.post("/jobs/{job_id}/cancel")
async def cancel_job(
    job_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    _user: AuthenticatedUser = Depends(hxguard.guard("bots.jobs.run")),
):
    job = await session.get(BotJobModel, job_id)
    if job is None:
        raise HTTPException(404, "Job not found.")
    if job.status in service.TERMINAL_STATUSES:
        raise HTTPException(409, f"This job already {job.status}.")

    await service.fail(session, job, error="Cancelled by an operator.",
                       status="cancelled")
    if job.container_id:
        try:
            from case_service.marketplace.runtime import stop_app_container

            stop_app_container(job.container_id)
        except Exception:
            log.exception("could not stop container for cancelled job %s", job.id)
    await session.commit()
    return _job_dict(job)


# ── the result path (per-job token) ───────────────────────────────────────

def _presented_token(request: Request) -> str | None:
    header = request.headers.get("authorization") or ""
    scheme, _, value = header.partition(" ")
    if scheme.lower() == "bearer" and value.strip():
        return value.strip()
    return request.headers.get("x-velaris-job-token")


class ResultIn(BaseModel):
    output: Any = None
    artifacts: list = []


@router.post("/jobs/{job_id}/result", dependencies=[Depends(_result_rl)])
async def submit_result(
    job_id: uuid.UUID,
    body: ResultIn,
    request: Request,
    session: AsyncSession = Depends(get_session),
):
    """The bot returns its work. Once.

    Authenticated by the per-job token and nothing else — this is called from a
    container that holds no session and no user. Every refusal is the same 404:
    a distinct "already used" would tell whoever holds a stolen token that it
    was genuine.
    """
    raw = await request.body()
    if len(raw) > service.MAX_RESULT_BYTES:
        raise HTTPException(413, "Result too large.")

    try:
        job = await service.redeem_result(
            session, job_id=job_id, presented_token=_presented_token(request),
            output=body.output, artifacts=body.artifacts,
        )
    except service.BotError as exc:
        await session.commit()          # keep the burned token and the event
        raise HTTPException(exc.status, str(exc)) from exc

    # An unattended job's output is applied by the platform, under the
    # workflow's authority. An attended one is a proposal the operator accepts.
    await service.apply_to_case(session, job)
    await session.commit()
    return {"accepted": True, "job_id": str(job.id)}


class ProgressIn(BaseModel):
    message: str = Field("", max_length=1000)
    percent: Optional[int] = Field(None, ge=0, le=100)


@router.post("/jobs/{job_id}/progress", dependencies=[Depends(_result_rl)])
async def submit_progress(
    job_id: uuid.UUID,
    body: ProgressIn,
    request: Request,
    session: AsyncSession = Depends(get_session),
):
    """Live progress, so a 90-second bot is not a frozen spinner.

    The **same** per-job token, but NOT single-use — progress is many, results
    are one. It cannot complete the job, so replaying it achieves nothing.
    """
    from case_service.bots import tokens

    job = await session.get(BotJobModel, job_id)
    if job is None or not tokens.matches(_presented_token(request), job.result_token_hash):
        raise HTTPException(404, "Not found.")
    if job.status in service.TERMINAL_STATUSES:
        raise HTTPException(404, "Not found.")

    await service.record_event(session, job.id, "progress",
                               {"message": body.message, "percent": body.percent})
    await session.commit()

    try:
        from case_service.realtime.publisher import publish_case_event

        if job.case_id:
            await publish_case_event(job.case_id, "bot_progress", {
                "job_id": str(job.id), "message": body.message,
                "percent": body.percent,
            })
    except Exception:
        log.debug("bot progress publish failed", exc_info=True)
    return {"recorded": True}


# ── attended: propose, then accept ────────────────────────────────────────

class AcceptRequest(BaseModel):
    #: The case's data checksum as the operator saw it. A mismatch is a 409
    #: rather than a silent overwrite — the same rule HxDraft already applies.
    expected_checksum: Optional[str] = None
    fields: Optional[list[str]] = None


def _checksum(data: dict) -> str:
    import hashlib
    import json

    return hashlib.sha256(
        json.dumps(data or {}, sort_keys=True, default=str).encode()).hexdigest()


@router.get("/jobs/{job_id}/proposal")
async def job_proposal(
    job_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    _user: AuthenticatedUser = Depends(hxguard.guard("bots.jobs.read")),
):
    """What an attended job wants to change, as a diff against the case.

    The human is the commit point. That is the whole difference between an
    attended bot and an unattended one, and it is why the output is shown rather
    than applied.
    """
    from case_service.db.models import CaseInstanceModel

    job = await session.get(BotJobModel, job_id)
    if job is None or job.status != "succeeded":
        raise HTTPException(404, "No completed job here.")
    case = await session.get(CaseInstanceModel, job.case_id) if job.case_id else None
    current = dict(case.data or {}) if case else {}

    changes = [
        {"field": key, "current": current.get(key), "proposed": value}
        for key, value in (job.output or {}).items()
        if not str(key).startswith("_") and current.get(key) != value
    ]
    return {"job_id": str(job.id), "case_id": str(job.case_id) if job.case_id else None,
            "checksum": _checksum(current), "changes": changes}


@router.post("/jobs/{job_id}/accept")
async def accept_proposal(
    job_id: uuid.UUID,
    body: AcceptRequest,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(hxguard.guard("bots.jobs.run")),
):
    """Apply an attended job's output, under the operator's authority."""
    from case_service.db.models import CaseInstanceModel

    job = await session.get(BotJobModel, job_id)
    if job is None or job.status != "succeeded" or job.case_id is None:
        raise HTTPException(404, "No completed job here.")

    case = await session.get(CaseInstanceModel, job.case_id)
    if case is None:
        raise HTTPException(404, "Case not found.")

    current = dict(case.data or {})
    if body.expected_checksum and body.expected_checksum != _checksum(current):
        raise HTTPException(
            409, "The case changed while this proposal was open. Reload the diff "
                 "and review it again.")

    wanted = set(body.fields) if body.fields else None
    applied = []
    for key, value in (job.output or {}).items():
        if str(key).startswith("_") or (wanted is not None and key not in wanted):
            continue
        current[key] = value
        applied.append(key)
    case.data = current

    await service.record_event(session, job.id, "accepted",
                               {"by": user.username, "fields": applied})
    await session.commit()
    return {"applied": applied}
