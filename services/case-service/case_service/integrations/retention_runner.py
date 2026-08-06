"""Scheduled retention — the unattended half, built with the brakes on.

Retention runs were manual: an operator had to call the endpoint. That is fine
for a first release of an irreversible operation and wrong as a permanent
answer, because a retention policy nobody remembers to run retains everything,
which is the state the policy exists to prevent.

This is the scheduler, and it is deliberately the most conservative background
task on the platform.

**OFF by default.** Every other background job here defaults on when it is
cheap and additive. This one deletes. It stays off until somebody decides,
in writing, that unattended deletion is what they want.

**Dry-run by default even when enabled.** Turning the scheduler on gets you a
log line telling you what *would* go. Actually deleting needs a second,
separate flag. Two switches, because the first one is easy to flip while
skim-reading a config file and the second is not.

**Refuses to start unless both are explicit.** There is no "enabled implies
apply" convenience. A convenience that deletes customer data is not one.

**Never raises into the app**, like the other samplers — but note the
asymmetry: a health sampler that dies costs a gap in a chart. This one dying
costs nothing at all, which is the safe direction, and it is why failure is
logged loudly rather than retried aggressively.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import asyncio
import logging

logger = logging.getLogger(__name__)

#: Long, because retention is measured in years. Anything that needs deleting
#: today needed deleting yesterday too — there is no urgency argument for a
#: tight interval, and every run scans several large tables.
DEFAULT_INTERVAL_SECONDS = 86_400

#: A day. Deletion should never be the first thing a freshly-started service
#: does: if a bad policy has just been deployed, this is the window in which
#: somebody notices and stops it.
STARTUP_DELAY_SECONDS = 86_400

MIN_INTERVAL_SECONDS = 3_600


class RetentionRunner:
    """Background task applying retention policies on an interval."""

    def __init__(
        self, session_factory, *, interval_seconds: int = DEFAULT_INTERVAL_SECONDS,
        apply: bool = False, startup_delay_seconds: int = STARTUP_DELAY_SECONDS,
    ) -> None:
        self._factory = session_factory
        self._interval = max(MIN_INTERVAL_SECONDS, int(interval_seconds))
        self._apply = bool(apply)
        self._startup_delay = max(0, int(startup_delay_seconds))
        self._task: asyncio.Task | None = None
        self._running = False

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._run())
        # Logged at WARNING when it will actually delete. An operator reading
        # startup logs should not have to infer this from an INFO line.
        (logger.warning if self._apply else logger.info)(
            "retention runner started (every %ss, mode=%s)",
            self._interval, "APPLY — deletes data" if self._apply else "dry-run",
        )

    def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()

    async def _run(self) -> None:
        try:
            await asyncio.sleep(self._startup_delay)
        except asyncio.CancelledError:
            return

        while self._running:
            try:
                from case_service.compliance import retention

                async with self._factory() as session:
                    out = await retention.run_all(session, apply=self._apply)
                    if self._apply:
                        await session.commit()

                rows = out.get("total_rows", 0)
                if rows:
                    (logger.warning if self._apply else logger.info)(
                        "retention %s: %d row(s) across %d policies",
                        "APPLIED" if self._apply else "would remove",
                        rows, len(out.get("policies", [])),
                    )
                for err in out.get("errors", []):
                    # A refused policy is worth a line every cycle: it is a
                    # policy an operator believes is enforcing and is not.
                    logger.warning("retention policy refused — %s", err)
            except asyncio.CancelledError:
                return
            except Exception as exc:
                logger.warning("scheduled retention run failed: %s", exc)

            try:
                await asyncio.sleep(self._interval)
            except asyncio.CancelledError:
                return
