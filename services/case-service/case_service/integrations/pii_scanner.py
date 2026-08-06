"""Scheduled PII scan — keeps the HxPrivacy inventory current (§8.10).

`POST /compliance/privacy/scan` ran only when someone asked, so the inventory
was as fresh as the last time a human remembered. That is worse than it sounds:
an inventory nobody refreshes still *looks* authoritative, and a form field
added last month is simply absent from it — indistinguishable, on screen, from
a form with no personal data in it.

The scan is already idempotent and already refuses to overwrite a human verdict
(`pii_scan._upsert`), which is what makes running it unattended safe: a
scheduled scan cannot re-open a dismissal or undo a confirmation. All it can do
is find new fields and mark vanished ones stale.

**Daily by default, not hourly.** Forms and data models are edited by people,
not by traffic. A scan reads every form and data model definition, so running
it often costs real queries to discover nothing changed.

**Never raises into the app**, same rule as the health sampler: a background
job that can take down the service it audits is worse than no background job.

**First run is delayed.** Scanning during startup competes with migrations and
the first requests for the same pool, on the one occasion the service is least
able to spare it.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import asyncio
import logging

logger = logging.getLogger(__name__)

#: Wait this long after boot before the first scan.
STARTUP_DELAY_SECONDS = 300

#: Floor on the interval. A scan reads every form definition; letting an
#: operator set 60s would turn an audit tool into a load generator.
MIN_INTERVAL_SECONDS = 3600


class PIIScanner:
    """Background task running the HxPrivacy scan on an interval."""

    def __init__(self, session_factory, *, interval_seconds: int = 86_400) -> None:
        self._factory = session_factory
        self._interval = max(MIN_INTERVAL_SECONDS, int(interval_seconds))
        self._task: asyncio.Task | None = None
        self._running = False

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._run())
        logger.info("PII scanner started (every %ss)", self._interval)

    def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()

    async def _run(self) -> None:
        # Delay before the first scan, not after it — otherwise the first run
        # lands during startup, which is exactly when it is least welcome.
        try:
            await asyncio.sleep(STARTUP_DELAY_SECONDS)
        except asyncio.CancelledError:
            return

        while self._running:
            try:
                from case_service.compliance import pii_scan

                async with self._factory() as session:
                    out = await pii_scan.scan_all(session)
                    await session.commit()

                # Logged only when something moved. A daily "0 new, 0 stale"
                # line teaches operators to filter these out, and then they
                # miss the one that matters.
                if out.get("findings_created") or out.get("findings_marked_stale"):
                    logger.info(
                        "PII scan: %d new, %d refreshed, %d stale across %d forms "
                        "and %d data models",
                        out.get("findings_created", 0),
                        out.get("findings_updated", 0),
                        out.get("findings_marked_stale", 0),
                        out.get("scanned_forms", 0),
                        out.get("scanned_data_models", 0),
                    )
            except asyncio.CancelledError:
                return
            except Exception as exc:
                logger.warning("scheduled PII scan failed: %s", exc)

            try:
                await asyncio.sleep(self._interval)
            except asyncio.CancelledError:
                return
