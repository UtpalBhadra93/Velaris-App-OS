"""The bot runner loop — an asyncio loop in the shape of the others.

Same shape as `OutboxRelay`, `RetentionRunner` and `EmailPollLoop`, started from
`main.py` alongside them, so there is one answer to "where do background loops
live in this service".

**Default off.** This loop starts containers on the host, so it is opt-in per
deployment (`VELARIS_CASE_BOT_RUNNER_ENABLED`). A deployment without Docker
access must see a clear refusal, never a traceback.

The cycle:

    lease a queued job         (conditional UPDATE — two runners are safe)
    resolve credential leases  (per job, TTL-bounded, revoked at teardown)
    mint a single-use result token
    provision the container    (marketplace/runtime.py, unchanged posture)
    wait for the result POST, up to timeout_seconds
    stop the container         (always — including on every failure path)
    on failure: the connector path's backoff, then a visible failure

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from sqlalchemy import select

from case_service.bots import credentials, service
from case_service.db.models import BotDefinitionModel, BotJobModel

log = logging.getLogger(__name__)

#: How often to look for work when the queue was empty.
IDLE_SLEEP_SECONDS = 5.0

#: How often to check whether a running job has posted its result.
POLL_INTERVAL_SECONDS = 2.0


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class BotRunner:
    """Leases and executes bot jobs until stopped."""

    def __init__(self) -> None:
        self._task: asyncio.Task | None = None
        self._stopping = asyncio.Event()

    def start(self) -> None:
        from case_service.config import get_settings

        if not getattr(get_settings(), "bot_runner_enabled", False):
            log.info("bot runner disabled (VELARIS_CASE_BOT_RUNNER_ENABLED)")
            return
        if self._task is not None:
            return
        self._task = asyncio.create_task(self._loop(), name="bot-runner")
        log.info("bot runner started")

    async def stop(self) -> None:
        self._stopping.set()
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None

    async def _loop(self) -> None:
        while not self._stopping.is_set():
            try:
                ran = await self._cycle()
            except asyncio.CancelledError:
                raise
            except Exception:
                # One bad job must not stop the loop; the job itself is failed
                # or requeued inside `_cycle`.
                log.exception("bot runner cycle failed")
                ran = False
            if not ran:
                await asyncio.sleep(IDLE_SLEEP_SECONDS)

    async def _cycle(self) -> bool:
        from case_service.db.session import get_session_factory

        async with get_session_factory()() as session:
            job = await service.lease_next(session)
            if job is None:
                await session.commit()
                return False

            bot = await session.get(BotDefinitionModel, job.bot_id)
            if bot is None or bot.status != "active":
                await service.fail(
                    session, job,
                    error="The bot definition is missing or no longer active.")
                await session.commit()
                return True

            if await service.at_concurrency_limit(session, bot):
                # Put it back rather than running past the limit: the limit
                # exists because a bot drives a third party's system.
                job.status = "queued"
                job.lease_expires_at = None
                await session.commit()
                return False

            await session.commit()

        await self._execute(job.id)
        return True

    async def _execute(self, job_id) -> None:
        from case_service.db.session import get_session_factory

        container_id: str | None = None
        secrets_used: list[str] = []

        async with get_session_factory()() as session:
            job = await session.get(BotJobModel, job_id)
            bot = await session.get(BotDefinitionModel, job.bot_id) if job else None
            if job is None or bot is None:
                return

            try:
                env = dict(bot.declared_env or {})
                leased = await credentials.resolve_leases(
                    session, job=job, refs=list(bot.credential_refs or []),
                    ttl_seconds=bot.timeout_seconds + 60,
                )
                secrets_used = list(leased.values())
                env.update(leased)

                token = await service.mint_result_token(session, job)
                env["VELARIS_JOB_URL"] = self._job_url(job.id)
                env["VELARIS_JOB_TOKEN"] = token
                env["VELARIS_JOB_ID"] = str(job.id)

                job.status = "running"
                job.started_at = _utcnow()
                await service.record_event(session, job.id, "started", {})
                await session.commit()

                container_id = await asyncio.to_thread(
                    self._provision, bot, job, env,
                )
            except credentials.CredentialError as exc:
                await service.fail(session, job, error=str(exc))
                await session.commit()
                return
            except Exception as exc:
                log.exception("bot job %s could not start", job_id)
                await service.requeue_or_fail(
                    session, job,
                    error=credentials.redact(str(exc), secrets_used))
                await credentials.revoke_leases(session, job_id=job.id)
                await session.commit()
                return

            job.container_id = container_id
            await session.commit()

        timed_out = await self._await_result(job_id, bot.timeout_seconds)

        if container_id:
            await asyncio.to_thread(self._teardown, container_id)

        async with get_session_factory()() as session:
            job = await session.get(BotJobModel, job_id)
            if job is None:
                return
            await credentials.revoke_leases(session, job_id=job.id)

            if timed_out and job.status not in service.TERMINAL_STATUSES:
                await service.record_event(session, job.id, "timeout", {})
                await service.requeue_or_fail(
                    session, job, error="The bot did not return within its timeout.")
            elif job.status == "succeeded":
                await service.apply_to_case(session, job)
            await session.commit()

    def _job_url(self, job_id) -> str:
        from case_service.config import get_settings

        base = (getattr(get_settings(), "bot_result_base_url", "") or "").rstrip("/")
        return f"{base}/api/v1/bots/jobs/{job_id}/result"

    def _provision(self, bot: BotDefinitionModel, job: BotJobModel, env: dict) -> str:
        """Start the container. Runs in a thread — the docker SDK is blocking."""
        from case_service.marketplace.runtime import provision_app_container

        profile = service.RESOURCE_PROFILES.get(
            bot.resource_profile, service.RESOURCE_PROFILES["light"])
        return provision_app_container(
            row_id=str(job.id),
            tenant_id=bot.tenant_id,
            package_id=f"bot:{bot.name}",
            image=bot.image,
            digest=bot.digest,
            granted_domains=list(bot.granted_domains or []),
            declared_env=env,
            declared_command=list(bot.declared_command or []) or None,
            pids_limit=profile["pids_limit"],
            tmpfs=profile["tmpfs"],
            labels={"velaris.bot": bot.name, "velaris.job": str(job.id)},
        )

    def _teardown(self, container_id: str) -> None:
        from case_service.marketplace.runtime import stop_app_container

        stop_app_container(container_id)

    async def _await_result(self, job_id, timeout_seconds: int) -> bool:
        """Poll for the bot's result POST. Returns True if it timed out."""
        from case_service.db.session import get_session_factory

        deadline = asyncio.get_running_loop().time() + max(1, timeout_seconds)
        while asyncio.get_running_loop().time() < deadline:
            if self._stopping.is_set():
                return False
            await asyncio.sleep(POLL_INTERVAL_SECONDS)
            async with get_session_factory()() as session:
                job = await session.get(BotJobModel, job_id)
                if job is None or job.status in service.TERMINAL_STATUSES:
                    return False
        return True


#: The process-wide instance, started from `main.py` beside the other loops.
runner = BotRunner()


async def sweep_expired_leases(session) -> int:
    """Revoke credential leases whose job has finished or whose TTL has passed.

    A lease that outlives its job is a live credential nobody is watching, and a
    crashed runner is exactly when that happens.
    """
    from case_service.db.models import BotCredentialLeaseModel

    now = _utcnow()
    rows = (await session.execute(
        select(BotCredentialLeaseModel).where(
            BotCredentialLeaseModel.revoked_at.is_(None))
    )).scalars().all()

    revoked = 0
    for lease in rows:
        expires = lease.expires_at
        if expires is not None and expires.tzinfo is None:
            expires = expires.replace(tzinfo=timezone.utc)
        job = await session.get(BotJobModel, lease.job_id)
        if (expires is not None and expires <= now) or (
                job is None or job.status in service.TERMINAL_STATUSES):
            lease.revoked_at = now
            revoked += 1
    await session.flush()
    return revoked
