"""Inviting people who have no account here.

A guest — a supplier, a customer's solicitor — cannot be an `attendee_user_id`:
there is no directory row to look up, no working pattern, no availability. They
are invited by **email**, and what makes that an invitation rather than a
paragraph is the `text/calendar` part, which the guest's own calendar reads.

Sent explicitly, never automatically on save: an invitation is a message this
platform sends in somebody's name, and a half-finished meeting that mails twenty
people because the organiser was still typing is a worse failure than one that
needs a second click.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from case_service.calendar.service import CalendarError, occurrences

logger = logging.getLogger(__name__)

#: Line endings are CRLF and lines fold at 75 octets — RFC 5545 is picky, and a
#: client that cannot parse the file shows the guest nothing at all.
_CRLF = "\r\n"


def _stamp(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _escape(text: str) -> str:
    """RFC 5545 §3.3.11.

    A raw comma or semicolon ends the value early, which truncates the summary in
    the guest's calendar rather than failing loudly. **A raw CR or LF is worse
    than that**: it ends the *line*, and everything after it is read as another
    iCalendar property — a title could otherwise add an ORGANIZER, an ATTENDEE
    or a URL to the invitation somebody else receives.
    """
    return (str(text or "")
            .replace("\\", "\\\\").replace(";", r"\;")
            .replace(",", r"\,")
            .replace("\r\n", r"\n").replace("\r", r"\n").replace("\n", r"\n"))


def _fold(line: str) -> str:
    """Fold at 75 octets, continuation lines starting with a space."""
    raw = line.encode("utf-8")
    if len(raw) <= 75:
        return line
    out, chunk = [], raw
    out.append(chunk[:75].decode("utf-8", "ignore"))
    chunk = chunk[75:]
    while chunk:
        out.append(" " + chunk[:74].decode("utf-8", "ignore"))
        chunk = chunk[74:]
    return _CRLF.join(out)


def build_ics(event, *, organiser_email: str | None = None,
              method: str = "REQUEST") -> str:
    """One VEVENT for this meeting, repeating if it repeats.

    The recurrence travels as an RRULE rather than as expanded copies: the
    guest's calendar then owns the series the same way this one does, and moving
    it later means one update rather than fifty.
    """
    uid = f"{event.id}@velaris"
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//Velaris//Calendar//EN",
        f"METHOD:{method}",
        "BEGIN:VEVENT",
        f"UID:{uid}",
        f"DTSTAMP:{_stamp(datetime.now(timezone.utc))}",
        f"DTSTART:{_stamp(event.starts_at)}",
        f"DTEND:{_stamp(event.ends_at)}",
        f"SUMMARY:{_escape(event.title)}",
        f"SEQUENCE:{len(event.excluded_dates or [])}",
        f"STATUS:{'CANCELLED' if event.status == 'cancelled' else 'CONFIRMED'}",
    ]
    if event.description:
        lines.append(f"DESCRIPTION:{_escape(event.description)}")

    location = event.meeting_url or ""
    if location:
        lines.append(f"LOCATION:{_escape(location)}")
    if organiser_email:
        lines.append(f"ORGANIZER:mailto:{organiser_email}")
    for address in (event.external_emails or []):
        lines.append(f"ATTENDEE;ROLE=REQ-PARTICIPANT;RSVP=TRUE:mailto:{address}")

    rule = event.recurrence or None
    if rule:
        freq = {"daily": "DAILY", "weekly": "WEEKLY", "monthly": "MONTHLY"}[rule["freq"]]
        rrule = f"RRULE:FREQ={freq};INTERVAL={int(rule.get('interval', 1) or 1)}"
        if rule.get("count"):
            rrule += f";COUNT={int(rule['count'])}"
        if rule.get("until"):
            rrule += f";UNTIL={_stamp(datetime.fromisoformat(str(rule['until']).replace('Z', '+00:00')))}"
        lines.append(rrule)
        for skipped in (event.excluded_dates or []):
            # EXDATE carries the *start time* of the skipped occurrence, not the
            # date alone: a client matching on date alone would drop the wrong one
            # for a meeting that crosses midnight in the guest's zone.
            start = event.starts_at
            lines.append(
                f"EXDATE:{skipped.replace('-', '')}T{_stamp(start)[9:]}")

    lines += ["END:VEVENT", "END:VCALENDAR"]
    return _CRLF.join(_fold(line) for line in lines) + _CRLF


async def _join_link_for(session: AsyncSession, event, address: str, *,
                         base_url: str, invited_by: str) -> str | None:
    """The link **this guest** can actually use.

    A pasted `meeting_url` is the same for everybody, so it is returned as is. A
    room this platform created is not: an embedded room admits a guest only
    through a **single-use invite token**, so each address gets its own, and the
    email carries an absolute URL because a path is meaningless in somebody
    else's inbox.

    The token's life is stretched to cover the meeting. The platform default is
    fifteen minutes — right for a link read out during a call, useless in an
    invitation sent a week ahead, where the guest would receive something that
    expired before they opened it.
    """
    if event.meeting_url:
        return event.meeting_url
    if not event.meet_session_id:
        return None
    if not base_url:
        # A room exists but nothing can be said about how to reach it. Refusing
        # is the honest answer: an invitation carrying `http://127.0.0.1/…`
        # looks fine to the sender and is useless to everybody who receives it.
        raise CalendarError(
            "This meeting has a room, but no public address is configured for "
            "the guest to open. Set VELARIS_CASE_PUBLIC_BASE_URL to where Studio "
            "is reachable (for example https://velaris.example.com) and invite "
            "again.", status=409)

    from velaris_shared.db.models import CaseSessionModel
    from case_service.meet import service as meet

    room = await session.get(CaseSessionModel, event.meet_session_id)
    if room is None or room.status != "active":
        return None
    if room.join_url:                       # an off-platform provider's own link
        return room.join_url

    ends = _aware(event.ends_at)
    # Until the meeting ends, plus an hour for late arrivals — and never less
    # than the platform default, so a meeting already under way still works.
    ttl = max(int((ends - datetime.now(timezone.utc)).total_seconds()) + 3600, 900)
    try:
        _participant, raw = await meet.create_guest_invite(
            session, row=room, invited_by=invited_by, email=address,
            display_name=address.split("@")[0], ttl_seconds=ttl,
        )
    except Exception as exc:                                  # pragma: no cover
        logger.warning("guest invite for %s failed: %s", address, exc)
        return None
    return f"{base_url.rstrip('/')}/meet/join?token={raw}"


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _ics_with(event, base_ics: str, link: str | None) -> str:
    """The same invitation, with this guest's link as the LOCATION.

    Rebuilt rather than string-patched: LOCATION may not be present in the base,
    and a calendar file assembled by find-and-replace is a calendar file that
    breaks the first time a title contains the word it looked for.
    """
    if not link or event.meeting_url:
        return base_ics

    class _View:                      # a shallow view, so the row is untouched
        def __init__(self, ev, url):
            self._ev, self.meeting_url = ev, url
        def __getattr__(self, name):
            return getattr(self._ev, name)

    return build_ics(_View(event, link))



async def send_invitations(session: AsyncSession, event, *, tenant_id: str,
                           sender: str, base_url: str = "") -> dict:
    """Email every external guest, with the `.ics` inline and their own link.

    One message per guest rather than one message to all of them: a guest list
    is not something to disclose to the guests, and a supplier learning which
    other supplier is in the meeting is a leak the organiser never chose. It is
    also what lets each guest have a link of their own.
    """
    from velaris_shared.db.models import EmailAccountModel
    from case_service.mail.service import EmailService

    account = (await session.execute(
        select(EmailAccountModel).where(
            EmailAccountModel.is_default_outbound.is_(True),
            EmailAccountModel.is_active.is_(True),
        ).limit(1)
    )).scalar_one_or_none()
    if account is None:
        raise CalendarError(
            "No outbound email account is configured, so an invitation cannot be "
            "sent. Set one up in Email Admin.", status=409)

    ics = build_ics(event, organiser_email=account.address)
    when = occurrences(event, event.starts_at, event.ends_at) or [(event.starts_at, event.ends_at)]
    first = when[0][0]

    body = [
        f"You are invited to: {event.title}",
        "",
        f"When: {first.strftime('%A %d %B %Y, %H:%M UTC')}",
    ]
    if event.recurrence:
        body.append(f"Repeats: {event.recurrence.get('freq')} "
                    f"(every {event.recurrence.get('interval', 1)})")
    if event.description:
        body += ["", event.description]
    # The Join line is added per guest, below: a pasted link is the same for
    # everybody but a created room is not, and printing both would give the
    # reader two links and no way to tell which one is theirs.

    # Header injection: a newline in the subject would end the header and let
    # the rest be read as another one (Bcc:, Reply-To:). The title is the
    # organiser's text, so it is sanitised rather than trusted.
    subject = f"Invitation: {event.title}".replace("\r", " ").replace("\n", " ")[:200]

    sent, failed = [], []
    for address in (event.external_emails or []):
        try:
            link = await _join_link_for(session, event, address,
                                        base_url=base_url, invited_by=sender)
            lines = list(body)
            if link:
                lines += ["", f"Join: {link}"]
            elif event.meet_session_id:
                lines += ["", "A meeting room is set up; ask the organiser for the "
                              "link if you cannot see one here."]

            row = await EmailService().send(
                session, case_id=event.case_id, account=account,
                to_addresses=[address], subject=subject,
                body_text="\n".join(lines), calendar_ics=_ics_with(event, ics, link),
                tenant_id=tenant_id, purpose="case_processing",
            )
            # `EmailService.send` does not raise when SMTP does: it records the
            # failure on the row and returns. Reporting "sent" on that basis is
            # how an invitation nobody received looks like a success.
            if getattr(row, "status", None) == "sent":
                sent.append(address)
            else:
                failed.append({"email": address,
                               "error": getattr(row, "error_message", None)
                               or f"the mail server did not accept it ({getattr(row, 'status', 'unknown')})"})
        except Exception as exc:
            # One bad address must not stop the rest — and the organiser needs
            # to know which one, or they will resend to everybody.
            logger.warning("invitation to %s failed: %s", address, exc)
            failed.append({"email": address, "error": str(exc)[:200]})

    return {"sent": sent, "failed": failed, "guests": len(event.external_emails or []),
            "from": account.address}
