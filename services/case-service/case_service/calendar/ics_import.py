"""Reading an iCalendar file somebody exported from Outlook, Google or Apple.

`invites.py` writes ICS; this reads it. They are deliberately a pair, and the
round trip is a test: anything this platform emits, it can take back.

**Why a file and not a URL.** Outlook, Google and Apple all publish a calendar
as an `.ics` you can download, so a file upload reaches every one of them. A
subscription URL would mean this service fetching an address a user supplies,
which is an SSRF surface aimed straight at the internal network — the platform
already carries one such fetch as an accepted risk and does not need a second.
Somebody downloading their own calendar and uploading it costs them one step.

**What an import may not do.** It writes busy time and it writes out-of-office
as a *requested* absence. It never approves one, because only an approved
absence reroutes a team's work (`docs/components/calendar.md`), and a file
should not be able to empty somebody's queue. It never deletes: an event that
has left the feed stays, since "gone from an export" and "cancelled" are not
the same fact. And it never fills `external_emails`, because that list is what
`send_invitations` mails — importing a supplier's address into a row that can
send would turn a file into a mailing list.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import logging
import re
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone

try:                                     # 3.9+; present everywhere this runs
    from zoneinfo import ZoneInfo
except ImportError:                      # pragma: no cover - defensive
    ZoneInfo = None                      # type: ignore[assignment]

logger = logging.getLogger(__name__)


class IcsError(Exception):
    """The file cannot be read at all. Nothing is written."""


#: A calendar export, not a data warehouse. Both bounds exist so one upload
#: cannot become an unbounded parse: the byte cap stops the read, and the event
#: cap stops a small file that expands into a very large number of rows.
MAX_BYTES = 1_048_576
MAX_EVENTS = 500

#: Mirrors the API's own limits so an imported row can never hold something a
#: typed one could not. Oversize text is truncated and **reported**, not
#: silently trimmed and not skipped — a 300-character meeting title is a real
#: meeting.
MAX_TITLE = 255
MAX_DESCRIPTION = 4000

#: Our recurrence vocabulary (`CalendarEventModel.recurrence`). An ICS may carry
#: FREQ values this platform cannot express; those events are skipped by name
#: rather than flattened into a single occurrence, which would silently turn a
#: yearly reminder into a one-off.
_FREQ = {"DAILY": "daily", "WEEKLY": "weekly", "MONTHLY": "monthly"}

#: Microsoft writes out-of-office here; everyone else uses TRANSP. Both are
#: read, because the property that marks leave is the one that matters most.
_OOF = "OOF"


@dataclass
class ImportedEvent:
    """One VEVENT, normalised to the shape `calendar_events` holds."""

    uid: str
    title: str
    description: str
    starts_at: datetime
    ends_at: datetime
    all_day: bool = False
    location: str = ""
    cancelled: bool = False
    out_of_office: bool = False
    recurrence: dict | None = None
    excluded_dates: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


@dataclass
class ParseResult:
    events: list[ImportedEvent]
    #: `(uid_or_summary, reason)` for everything deliberately not imported. The
    #: caller reports this: an import that quietly drops half a file is worse
    #: than one that refuses it.
    skipped: list[tuple[str, str]]


# ── lexing ────────────────────────────────────────────────────────────────

def _unfold(text: str) -> list[str]:
    """Undo RFC 5545 line folding.

    A continuation line begins with a space or a tab and belongs to the line
    before it. Long values — a description, a URL — arrive folded from every
    real client, so a parser that skips this step sees truncated garbage.
    """
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    out: list[str] = []
    for line in text.split("\n"):
        if line[:1] in (" ", "\t") and out:
            out[-1] += line[1:]
        else:
            out.append(line)
    return out


def _split(line: str) -> tuple[str, dict[str, str], str]:
    """`NAME;PARAM=value:VALUE` → (name, params, value).

    The colon that separates name from value is the first one **outside** a
    quoted parameter — `TZID="Europe/London: winter"` is legal, and splitting on
    the first colon found would cut it in half.
    """
    in_quotes = False
    for i, ch in enumerate(line):
        if ch == '"':
            in_quotes = not in_quotes
        elif ch == ":" and not in_quotes:
            head, value = line[:i], line[i + 1:]
            break
    else:
        return "", {}, ""

    parts, buf, quoted = [], "", False
    for ch in head:
        if ch == '"':
            quoted = not quoted
            continue
        if ch == ";" and not quoted:
            parts.append(buf)
            buf = ""
            continue
        buf += ch
    parts.append(buf)

    name = parts[0].strip().upper()
    params: dict[str, str] = {}
    for part in parts[1:]:
        if "=" in part:
            key, _, val = part.partition("=")
            params[key.strip().upper()] = val.strip()
    return name, params, value


def _unescape(value: str) -> str:
    """Inverse of `invites._escape` (RFC 5545 §3.3.11).

    Order matters: `\\\\n` is a literal backslash followed by `n`, not a newline,
    so the backslash escape is resolved last by walking the string once rather
    than by chained replaces.
    """
    out, i = [], 0
    while i < len(value):
        ch = value[i]
        if ch == "\\" and i + 1 < len(value):
            nxt = value[i + 1]
            out.append({"n": "\n", "N": "\n", ",": ",", ";": ";", "\\": "\\"}.get(nxt, nxt))
            i += 2
            continue
        out.append(ch)
        i += 1
    return "".join(out)


# ── values ────────────────────────────────────────────────────────────────

_DT_RE = re.compile(r"^(\d{4})(\d{2})(\d{2})(?:T(\d{2})(\d{2})(\d{2})(Z?))?$")


def _zone(tzid: str | None):
    """Resolve a TZID, or raise.

    **An unknown zone is refused, never assumed.** Defaulting to UTC would move
    a 09:00 meeting by up to a day, and the row would look perfectly ordinary
    afterwards — the worst kind of wrong.
    """
    if not tzid:
        return None
    if ZoneInfo is None:                              # pragma: no cover
        raise IcsError("timezone support unavailable")
    cleaned = tzid.strip().strip('"')
    # Windows/Exchange writes its own names ("GMT Standard Time"). Map the ones
    # that are unambiguous and refuse the rest by name.
    windows = {
        "GMT STANDARD TIME": "Europe/London",
        "UTC": "UTC",
        "W. EUROPE STANDARD TIME": "Europe/Berlin",
        "CENTRAL EUROPE STANDARD TIME": "Europe/Budapest",
        "ROMANCE STANDARD TIME": "Europe/Paris",
        "EASTERN STANDARD TIME": "America/New_York",
        "CENTRAL STANDARD TIME": "America/Chicago",
        "MOUNTAIN STANDARD TIME": "America/Denver",
        "PACIFIC STANDARD TIME": "America/Los_Angeles",
        "INDIA STANDARD TIME": "Asia/Kolkata",
        "SINGAPORE STANDARD TIME": "Asia/Singapore",
        "AUS EASTERN STANDARD TIME": "Australia/Sydney",
    }
    name = windows.get(cleaned.upper(), cleaned)
    try:
        return ZoneInfo(name)
    except Exception as exc:                          # unknown zone
        raise IcsError(f"unknown timezone `{cleaned}`") from exc


def _parse_dt(value: str, params: dict[str, str]) -> tuple[datetime, bool]:
    """→ (aware UTC datetime, all_day).

    Three forms exist and all three appear in the wild: a UTC stamp ending `Z`,
    a local stamp with a `TZID`, and a **floating** stamp with neither. Floating
    means "whatever the reader's local time is", which no server can know, so it
    is read as UTC and that assumption is reported on the event.
    """
    raw = value.strip()
    match = _DT_RE.match(raw)
    if not match:
        raise IcsError(f"unreadable date `{raw[:32]}`")
    y, mo, d, hh, mm, ss, zulu = match.groups()

    if params.get("VALUE", "").upper() == "DATE" or hh is None:
        start = datetime(int(y), int(mo), int(d), tzinfo=timezone.utc)
        return start, True

    naive = datetime(int(y), int(mo), int(d), int(hh), int(mm), int(ss))
    if zulu == "Z":
        return naive.replace(tzinfo=timezone.utc), False
    zone = _zone(params.get("TZID"))
    if zone is None:
        return naive.replace(tzinfo=timezone.utc), False
    return naive.replace(tzinfo=zone).astimezone(timezone.utc), False


_DUR_RE = re.compile(
    r"^(?P<sign>[+-])?P(?:(?P<w>\d+)W)?(?:(?P<d>\d+)D)?"
    r"(?:T(?:(?P<h>\d+)H)?(?:(?P<m>\d+)M)?(?:(?P<s>\d+)S)?)?$")


def _parse_duration(value: str) -> timedelta:
    match = _DUR_RE.match(value.strip())
    if not match:
        raise IcsError(f"unreadable duration `{value[:32]}`")
    g = match.groupdict()
    delta = timedelta(weeks=int(g["w"] or 0), days=int(g["d"] or 0),
                      hours=int(g["h"] or 0), minutes=int(g["m"] or 0),
                      seconds=int(g["s"] or 0))
    return -delta if g["sign"] == "-" else delta


def _parse_rrule(value: str) -> dict:
    """→ our `recurrence` dict, or raise for a rule we cannot express.

    Raising is the point. `CalendarEventModel.recurrence` holds freq, interval,
    count and until and nothing else; a BYDAY or a yearly rule imported without
    them would describe a *different* series from the one in the file.
    """
    parts = {}
    for chunk in value.split(";"):
        key, _, val = chunk.partition("=")
        parts[key.strip().upper()] = val.strip()

    freq = _FREQ.get(parts.get("FREQ", "").upper())
    if not freq:
        raise IcsError(f"unsupported recurrence FREQ={parts.get('FREQ', '?')}")
    for unsupported in ("BYDAY", "BYMONTHDAY", "BYMONTH", "BYSETPOS", "WKST"):
        if unsupported in parts:
            raise IcsError(f"unsupported recurrence part {unsupported}")

    rule: dict = {"freq": freq, "interval": max(1, int(parts.get("INTERVAL", 1) or 1))}
    if parts.get("COUNT"):
        rule["count"] = int(parts["COUNT"])
    if parts.get("UNTIL"):
        until, _ = _parse_dt(parts["UNTIL"], {})
        rule["until"] = until.isoformat()
    return rule


# ── the parse ─────────────────────────────────────────────────────────────

def parse_ics(text: str) -> ParseResult:
    """Read a calendar file. Raises `IcsError` only when nothing can be read.

    A single unreadable VEVENT is skipped with a reason rather than failing the
    file: a year of somebody's calendar containing one rule we cannot express
    should still import the other 364 days.
    """
    if len(text.encode("utf-8", "ignore")) > MAX_BYTES:
        raise IcsError(f"file is larger than {MAX_BYTES // 1024} KB")
    lines = _unfold(text)
    if not any(line.strip().upper() == "BEGIN:VCALENDAR" for line in lines):
        raise IcsError("not an iCalendar file (no BEGIN:VCALENDAR)")

    events: list[ImportedEvent] = []
    skipped: list[tuple[str, str]] = []
    current: list[tuple[str, dict[str, str], str]] | None = None
    depth_other = 0

    for line in lines:
        bare = line.strip()
        upper = bare.upper()
        # VTIMEZONE and VALARM carry BEGIN/END too. Their properties must not be
        # read as the event's — a VALARM has its own DURATION.
        if upper.startswith("BEGIN:") and upper != "BEGIN:VEVENT":
            if current is not None:
                depth_other += 1
            continue
        if upper.startswith("END:") and upper != "END:VEVENT":
            if depth_other:
                depth_other -= 1
            continue
        if upper == "BEGIN:VEVENT":
            current = []
            continue
        if upper == "END:VEVENT":
            if current is None:
                continue
            if len(events) + len(skipped) >= MAX_EVENTS:
                raise IcsError(f"file holds more than {MAX_EVENTS} events")
            try:
                events.append(_build(current))
            except IcsError as exc:
                skipped.append((_label(current), str(exc)))
            current = None
            continue
        if current is not None and not depth_other and bare:
            current.append(_split(bare))

    if current is not None:
        raise IcsError("unterminated VEVENT")
    return ParseResult(events=events, skipped=skipped)


def _label(props: list[tuple[str, dict[str, str], str]]) -> str:
    for name, _params, value in props:
        if name == "SUMMARY":
            return _unescape(value)[:80]
    for name, _params, value in props:
        if name == "UID":
            return value[:80]
    return "(unnamed event)"


def _build(props: list[tuple[str, dict[str, str], str]]) -> ImportedEvent:
    got: dict[str, tuple[dict[str, str], str]] = {}
    excluded: list[str] = []
    for name, params, value in props:
        if name == "EXDATE":
            for one in value.split(","):
                try:
                    when, _ = _parse_dt(one, params)
                except IcsError:
                    continue
                excluded.append(when.date().isoformat())
            continue
        got.setdefault(name, (params, value))

    if "DTSTART" not in got:
        raise IcsError("no DTSTART")
    start_params, start_value = got["DTSTART"]
    starts_at, all_day = _parse_dt(start_value, start_params)

    notes: list[str] = []
    if not all_day and "Z" not in start_value.upper() and "TZID" not in start_params:
        notes.append("floating time read as UTC")

    if "DTEND" in got:
        end_params, end_value = got["DTEND"]
        ends_at, _ = _parse_dt(end_value, end_params)
    elif "DURATION" in got:
        ends_at = starts_at + _parse_duration(got["DURATION"][1])
    elif all_day:
        ends_at = starts_at + timedelta(days=1)
    else:
        # RFC 5545: a timed event with neither is instantaneous. The schema's
        # CHECK requires ends_at > starts_at, so it gets the default meeting
        # length rather than being refused over a missing property.
        ends_at = starts_at + timedelta(hours=1)
        notes.append("no end time; assumed 1 hour")

    if ends_at <= starts_at:
        raise IcsError("ends before it starts")

    title = _unescape(got.get("SUMMARY", ({}, ""))[1]).strip() or "(no title)"
    if len(title) > MAX_TITLE:
        title = title[:MAX_TITLE]
        notes.append("title truncated")
    description = _unescape(got.get("DESCRIPTION", ({}, ""))[1]).strip()
    if len(description) > MAX_DESCRIPTION:
        description = description[:MAX_DESCRIPTION]
        notes.append("description truncated")

    recurrence = _parse_rrule(got["RRULE"][1]) if "RRULE" in got else None

    transp = got.get("TRANSP", ({}, ""))[1].strip().upper()
    busystatus = got.get("X-MICROSOFT-CDO-BUSYSTATUS", ({}, ""))[1].strip().upper()
    if transp == "TRANSPARENT" and busystatus != _OOF:
        raise IcsError("marked free, not busy")

    uid = got.get("UID", ({}, ""))[1].strip()
    if not uid:
        raise IcsError("no UID")

    return ImportedEvent(
        uid=uid[:255],
        title=title,
        description=description,
        starts_at=starts_at,
        ends_at=ends_at,
        all_day=all_day,
        location=_unescape(got.get("LOCATION", ({}, ""))[1]).strip(),
        cancelled=got.get("STATUS", ({}, ""))[1].strip().upper() == "CANCELLED",
        out_of_office=busystatus == _OOF,
        recurrence=recurrence,
        excluded_dates=sorted(set(excluded)),
        notes=notes,
    )


# ── writing ───────────────────────────────────────────────────────────────

#: Same allowlist the API applies to a typed link (`_check_meeting_url`). A
#: LOCATION is attacker-supplied text once a file can be uploaded, and Studio
#: renders the link as an anchor — `javascript:` there is script execution one
#: click away. A location that is not a safe URL is not lost: it is kept as a
#: line of the description, which is rendered as text.
_URL_SCHEMES = ("http", "https")


def _location_split(location: str) -> tuple[str | None, str]:
    from urllib.parse import urlsplit

    if not location:
        return None, ""
    if len(location) <= 2000 and urlsplit(location).scheme.lower() in _URL_SCHEMES:
        return location, ""
    return None, f"Location: {location}"[:MAX_DESCRIPTION]


async def apply_import(session, result: ParseResult, *, tenant_id: str,
                       user_id: str, actor: str, dry_run: bool = True,
                       subscription_id=None) -> dict:
    """Write a parsed calendar into one person's rows.

    Matching is on `(tenant_id, source='import', external_uid, organiser)`, so a
    second import of the same feed updates. **`source='manual'` is never
    matched**: a row somebody typed is theirs, and an upload must not be able to
    rewrite it by claiming its UID.

    Nothing is deleted. An event that has disappeared from an export has not
    necessarily been cancelled — the export may simply cover a shorter window —
    and deleting on absence of evidence would empty a calendar from a partial
    file.
    """
    from sqlalchemy import select
    from velaris_shared.db.models import CalendarEventModel, UserAbsenceModel

    created = updated = absences = 0
    reports: list[dict] = []

    #: A subscription owns only the rows it created; an upload owns only rows
    #: no subscription created. Two feeds may legitimately carry the same UID —
    #: a meeting a person and their team are both invited to — so the UID alone
    #: cannot identify a row, and without this an upload would adopt a feed's
    #: rows and then the next refresh would fight it back.
    scope = (CalendarEventModel.subscription_id == subscription_id
             if subscription_id is not None
             else CalendarEventModel.subscription_id.is_(None))
    existing_rows = (await session.execute(
        select(CalendarEventModel).where(
            CalendarEventModel.tenant_id == tenant_id,
            CalendarEventModel.organiser_user_id == user_id,
            CalendarEventModel.source == "import",
            scope,
        )
    )).scalars().all()
    by_uid = {row.external_uid: row for row in existing_rows if row.external_uid}

    absence_scope = (UserAbsenceModel.subscription_id == subscription_id
                     if subscription_id is not None
                     else UserAbsenceModel.subscription_id.is_(None))
    known_absences = (await session.execute(
        select(UserAbsenceModel).where(
            UserAbsenceModel.tenant_id == tenant_id,
            UserAbsenceModel.user_id == user_id,
            UserAbsenceModel.source == "import",
            absence_scope,
        )
    )).scalars().all()
    absence_uids = {row.external_uid for row in known_absences if row.external_uid}

    for event in result.events:
        meeting_url, extra = _location_split(event.location)
        description = "\n\n".join(part for part in (event.description, extra) if part)
        row = by_uid.get(event.uid)
        action = "update" if row is not None else "create"

        if not dry_run:
            if row is None:
                row = CalendarEventModel(
                    id=uuid.uuid4(),
                    tenant_id=tenant_id,
                    organiser_user_id=user_id,
                    #: The importer is the only attendee. Mapping ATTENDEE
                    #: addresses onto directory rows would be guessing at
                    #: identity from an email a stranger controls.
                    attendee_user_ids=[user_id],
                    external_emails=[],
                    source="import",
                    external_uid=event.uid,
                    subscription_id=subscription_id,
                    #: Somebody's own calendar, not the team's. A team-visible
                    #: default would publish every meeting title they hold.
                    visibility="private",
                    created_by=actor,
                )
                session.add(row)
            row.title = event.title
            row.description = description[:MAX_DESCRIPTION]
            row.starts_at = event.starts_at
            row.ends_at = event.ends_at
            row.recurrence = event.recurrence
            row.excluded_dates = event.excluded_dates
            row.meeting_url = meeting_url
            # A row cancelled because it left the feed must come back if the
            # feed carries it again — otherwise a reinstated meeting stays
            # struck through forever.
            row.status = "cancelled" if event.cancelled else "scheduled"
            row.updated_by = actor
        created += action == "create"
        updated += action == "update"

        if event.out_of_office and event.uid not in absence_uids:
            absences += 1
            if not dry_run:
                leave = UserAbsenceModel(
                    id=uuid.uuid4(),
                    tenant_id=tenant_id,
                    user_id=user_id,
                    kind="other",
                    starts_at=event.starts_at,
                    ends_at=event.ends_at,
                    all_day=event.all_day,
                    #: NEVER `approved`. Only an approved absence reroutes work,
                    #: and a file must not be able to empty a queue.
                    status="requested",
                    source="import",
                    external_uid=event.uid,
                    subscription_id=subscription_id,
                    note="",
                    created_by=actor,
                )
                # The same validation the typed path runs. A feed is somebody
                # else's data: a range this schema's CHECK constraint would
                # refuse should be skipped here, not turned into a 500 at commit
                # time that loses the whole refresh.
                from case_service.calendar.service import (
                    CalendarError, validate_absence,
                )
                try:
                    validate_absence(leave)
                except CalendarError as exc:
                    absences -= 1
                    result.skipped.append((event.title, f"leave refused: {exc}"))
                else:
                    session.add(leave)

        reports.append({
            "uid": event.uid, "title": event.title, "action": action,
            "out_of_office": event.out_of_office, "notes": event.notes,
        })

    return {
        "dry_run": dry_run,
        "created": created,
        "updated": updated,
        "absences_requested": absences,
        "skipped": [{"event": name, "reason": reason} for name, reason in result.skipped],
        "events": reports,
    }
