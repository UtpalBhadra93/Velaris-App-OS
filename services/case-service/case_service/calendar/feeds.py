"""Subscribing to a calendar that lives somewhere else.

Migration 175 gave the calendar a reader for an uploaded `.ics`. A file is a
snapshot: it is stale the moment somebody books leave in Outlook. This is the
same reader pointed at a URL and run again on a timetable — "add a calendar by
URL", the way Google and Outlook offer it.

**Read-only, deliberately.** That is what makes the feature cheap and safe: with
no write-back there is no question of who wins when both sides change, and no
way for a bug here to alter somebody's real calendar.

**The fetch is the whole security surface.** A user supplies an address and this
server requests it from inside the network — the exact shape of SSRF. So:

* an operator-set **allowlist** of hosts, unset meaning no subscription at all;
* the resolved **address** is checked, not just the name, because a hostname an
  operator allowed can still resolve to 127.0.0.1 or 169.254.169.254;
* **redirects are refused, never followed** — a redirect is how an allowlisted
  host hands you an address nobody allowed;
* a byte cap enforced **while streaming**, so a host that never stops sending
  cannot fill memory or hold a worker open;
* and any credentials in the URL are stripped before it is used.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import asyncio
import ipaddress
import logging
import socket
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit, urlunsplit

from velaris_shared.config import get_settings

from case_service.calendar.ics_import import IcsError, apply_import, parse_ics

logger = logging.getLogger(__name__)

#: A subscription that has failed this many times in a row stops being polled by
#: the scheduled refresh. It is not disabled and not deleted: a manual refresh
#: still runs, and the row says why it stopped. Retrying a dead URL every hour
#: forever is how a log becomes unreadable.
MAX_CONSECUTIVE_ERRORS = 20

#: Bounds on how often a feed may be re-read. Below five minutes is polling
#: somebody else's server hard enough to be noticed; a week is the point at which
#: the answer is a file upload instead.
MIN_INTERVAL_MINUTES = 5
MAX_INTERVAL_MINUTES = 10_080


class FeedError(Exception):
    """The feed cannot be fetched. Nothing is written."""


def allowed_hosts() -> list[str]:
    raw = get_settings().calendar_feed_hosts or ""
    return [h.strip().lower() for h in raw.split(",") if h.strip()]


def _host_allowed(host: str, allowlist: list[str]) -> bool:
    """Exact host, or a leading dot for a domain and its subdomains.

    `.google.com` allows `calendar.google.com` and `google.com`, and does **not**
    allow `notgoogle.com` — a suffix match without the dot would.
    """
    host = host.lower().rstrip(".")
    for entry in allowlist:
        if entry.startswith("."):
            if host == entry[1:] or host.endswith(entry):
                return True
        elif host == entry:
            return True
    return False


def _refuse_internal(host: str) -> list[str]:
    """Resolve the name and refuse anything that is not a public address.

    **Allowlisting the name is not enough.** A host an operator allowed can
    resolve to a loopback, a private range, or the cloud metadata address —
    either by mistake or because whoever controls the DNS record wants it to.
    """
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror as exc:
        raise FeedError(f"`{host}` does not resolve") from exc

    addresses = sorted({info[4][0] for info in infos})
    if not addresses:
        raise FeedError(f"`{host}` does not resolve")
    for address in addresses:
        ip = ipaddress.ip_address(address)
        if (ip.is_private or ip.is_loopback or ip.is_link_local
                or ip.is_multicast or ip.is_reserved or ip.is_unspecified):
            raise FeedError(
                f"`{host}` resolves to {address}, which is not a public address")
    return addresses


def _validate_sync(url: str) -> str:
    """The check itself. Blocking, because `getaddrinfo` is — see `validate_url`."""
    allowlist = allowed_hosts()
    if not allowlist:
        raise FeedError(
            "No calendar feed hosts are allowed on this server. An administrator "
            "sets VELARIS_CASE_CALENDAR_FEED_HOSTS before a calendar can be "
            "subscribed to.")

    # `webcal://` is what Apple and Outlook put on the clipboard. It is https by
    # another name, and rewriting it saves every user the same edit.
    cleaned = url.strip()
    if cleaned.lower().startswith("webcal://"):
        cleaned = "https://" + cleaned[len("webcal://"):]

    parts = urlsplit(cleaned)
    if parts.scheme.lower() not in ("http", "https"):
        raise FeedError("A calendar URL must be http, https or webcal.")
    if not parts.hostname:
        raise FeedError("That URL names no host.")
    if not _host_allowed(parts.hostname, allowlist):
        raise FeedError(
            f"`{parts.hostname}` is not an allowed calendar host on this server.")
    _refuse_internal(parts.hostname)

    # Strip userinfo: a `https://user:pass@host/` subscription would otherwise
    # store a credential in a column that is read back to a screen.
    netloc = parts.hostname + (f":{parts.port}" if parts.port else "")
    return urlunsplit((parts.scheme.lower(), netloc, parts.path,
                       parts.query, ""))


async def validate_url(url: str) -> str:
    """Check a subscription URL and return it with any credentials stripped.

    Called when the subscription is **created** as well as before every fetch:
    a check that only runs at write time is not a control, because DNS moves
    afterwards.

    **Off the event loop.** `socket.getaddrinfo` blocks, and this service runs
    one asyncio loop: a resolver that takes five seconds to answer would stall
    every other request in the process, not just this one. A name that never
    resolves is exactly what a hostile URL would be built from.
    """
    return await asyncio.to_thread(_validate_sync, url)


#: DNS is resolved for the check and again by the client when it connects, so a
#: hostile record could answer publicly the first time and privately the second
#: — DNS rebinding. What bounds it here is the **allowlist**: rebinding requires
#: an operator to have approved the attacker's own domain first. Recorded rather
#: than claimed closed; pinning the resolved address through the connection is
#: the real fix and needs a custom transport.


async def fetch(url: str, *, etag: str | None = None,
                last_modified: str | None = None) -> tuple[str | None, dict]:
    """→ (ics text or None when unchanged, response metadata).

    `None` is the 304 case and is a success, not a failure: an unchanged feed
    costs both sides nothing, which is what makes a short interval reasonable.
    """
    import httpx

    settings = get_settings()
    safe = await validate_url(url)
    # Never read more than the parser will accept: a 1.5 MB feed that downloads
    # fine and then fails with "file is larger than 1024 KB" wastes the transfer
    # and reports the wrong stage as the failure.
    from case_service.calendar.ics_import import MAX_BYTES as PARSE_MAX

    cap = min(settings.calendar_feed_max_bytes, PARSE_MAX)

    headers = {"Accept": "text/calendar, text/plain;q=0.5",
               "User-Agent": "Velaris-Calendar/1.0"}
    if etag:
        headers["If-None-Match"] = etag
    if last_modified:
        headers["If-Modified-Since"] = last_modified

    try:
        async with httpx.AsyncClient(
                timeout=settings.calendar_feed_timeout_seconds,
                # A redirect is how an allowlisted host hands you an address
                # nobody allowed. Refused, and named, rather than followed.
                follow_redirects=False) as client:
            async with client.stream("GET", safe, headers=headers) as response:
                if response.status_code == 304:
                    return None, {"status": response.status_code}
                if response.is_redirect:
                    raise FeedError(
                        f"That URL redirects to "
                        f"`{response.headers.get('location', 'somewhere else')}`. "
                        f"Subscribe to the final address instead.")
                if response.status_code >= 400:
                    raise FeedError(f"The feed answered {response.status_code}.")

                chunks, total = [], 0
                async for chunk in response.aiter_bytes():
                    total += len(chunk)
                    if total > cap:
                        raise FeedError(
                            f"That feed is larger than {cap // 1024} KB.")
                    chunks.append(chunk)
                body = b"".join(chunks)
                meta = {
                    "status": response.status_code,
                    "etag": response.headers.get("etag"),
                    "last_modified": response.headers.get("last-modified"),
                    "bytes": total,
                }
    except httpx.HTTPError as exc:
        raise FeedError(f"Could not reach that calendar: {exc}") from exc

    return body.decode("utf-8", "replace"), meta


def due(subscription, *, now: datetime | None = None) -> bool:
    now = now or datetime.now(timezone.utc)
    if not subscription.enabled:
        return False
    if subscription.consecutive_errors >= MAX_CONSECUTIVE_ERRORS:
        return False
    if subscription.last_fetched_at is None:
        return True
    last = subscription.last_fetched_at
    if last.tzinfo is None:
        last = last.replace(tzinfo=timezone.utc)
    return now - last >= timedelta(minutes=subscription.interval_minutes)


async def refresh(session, subscription, *, actor: str) -> dict:
    """Re-read one feed and reconcile the rows it owns.

    Never raises for a feed-side problem: the failure is recorded on the
    subscription and reported. A calendar that has been failing for a fortnight
    must say so on the screen — a sync that silently stops is worse than one
    that never started, because the calendar still looks full.
    """
    now = datetime.now(timezone.utc)
    subscription.last_fetched_at = now

    try:
        text, meta = await fetch(
            subscription.url, etag=subscription.etag,
            last_modified=subscription.last_modified)
    except FeedError as exc:
        subscription.last_status = "error"
        subscription.last_error = str(exc)[:2000]
        subscription.consecutive_errors += 1
        return {"status": "error", "error": str(exc),
                "stopped": subscription.consecutive_errors >= MAX_CONSECUTIVE_ERRORS}

    if text is None:
        subscription.last_status = "unchanged"
        subscription.last_error = ""
        subscription.consecutive_errors = 0
        subscription.last_success_at = now
        return {"status": "unchanged"}

    try:
        parsed = parse_ics(text)
    except IcsError as exc:
        # The feed answered, and what it sent is not a calendar. That is the
        # feed's problem, recorded — not an exception out of a scheduled run.
        subscription.last_status = "error"
        subscription.last_error = f"That feed is not readable: {exc}"[:2000]
        subscription.consecutive_errors += 1
        return {"status": "error", "error": subscription.last_error}

    outcome = await apply_import(
        session, parsed, tenant_id=subscription.tenant_id,
        user_id=subscription.user_id, actor=actor, dry_run=False,
        subscription_id=subscription.id)

    withdrawn = await _withdraw_missing(
        session, subscription, {event.uid for event in parsed.events})

    subscription.etag = meta.get("etag")
    subscription.last_modified = meta.get("last_modified")
    subscription.last_status = "ok"
    subscription.last_error = ""
    subscription.consecutive_errors = 0
    subscription.last_success_at = now
    subscription.last_event_count = len(parsed.events)
    return {"status": "ok", "withdrawn": withdrawn, **outcome}


async def _withdraw_missing(session, subscription, seen: set[str]) -> int:
    """An entry that has left the feed is **cancelled, never deleted**.

    A subscription is authoritative for the rows it created, so unlike a file
    upload it does act on absence — otherwise a cancelled meeting would sit in
    somebody's calendar forever. But cancelling keeps the row visible and
    reversible, and it keeps the history a deletion would take with it.

    Requested leave is withdrawn the same way. **Approved leave is not touched**:
    a human approved that, and a feed must not be able to undo a human decision
    or to put somebody back on the rota they were taken off.
    """
    from sqlalchemy import select
    from velaris_shared.db.models import CalendarEventModel, UserAbsenceModel

    count = 0
    events = (await session.execute(
        select(CalendarEventModel).where(
            CalendarEventModel.subscription_id == subscription.id,
            CalendarEventModel.status != "cancelled",
        ))).scalars().all()
    for row in events:
        if row.external_uid not in seen:
            row.status = "cancelled"
            count += 1

    absences = (await session.execute(
        select(UserAbsenceModel).where(
            UserAbsenceModel.subscription_id == subscription.id,
            UserAbsenceModel.status == "requested",
        ))).scalars().all()
    for row in absences:
        if row.external_uid not in seen:
            row.status = "cancelled"
            count += 1
    return count
