"""A five-field cron parser and "when does this next fire?".

Written rather than depended on: nothing in this service parses cron today (the
existing `*_cron` coroutines are fixed-interval loops), and the alternative was a
new runtime dependency whose grammar we would then have to constrain anyway.
Five fields, the Vixie semantics people expect, and nothing else:

    minute hour day-of-month month day-of-week
    0 2 * * *        02:00 every day
    */15 9-17 * * 1-5  every 15 minutes, 9am-5pm, weekdays
    0 6 1 * *        06:00 on the 1st

**Day-of-month and day-of-week are OR-ed when both are restricted**, which is the
rule real cron uses and the one a schedule silently gets wrong otherwise: `0 0 1 *
1` is "the 1st *or* any Monday", not "Mondays that fall on the 1st".

**Time is computed on the local wall clock**, then converted, which is what makes
a daylight-saving change behave: the next occurrence after a fire is the next
*wall-clock* match, so the hour that repeats in autumn fires once rather than
twice, and a schedule set for 02:00 does not silently drift to 01:00 in summer.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

#: Field bounds, in order.
_BOUNDS = ((0, 59), (0, 23), (1, 31), (1, 12), (0, 6))

_MONTHS = {m: i + 1 for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun",
     "jul", "aug", "sep", "oct", "nov", "dec"])}
_DAYS = {d: i for i, d in enumerate(
    ["sun", "mon", "tue", "wed", "thu", "fri", "sat"])}

MACROS = {
    "@yearly": "0 0 1 1 *", "@annually": "0 0 1 1 *",
    "@monthly": "0 0 1 * *", "@weekly": "0 0 * * 0",
    "@daily": "0 0 * * *", "@midnight": "0 0 * * *",
    "@hourly": "0 * * * *",
}

#: How far ahead to look before giving up. A parsed expression can still be
#: unsatisfiable — `0 0 30 2 *` is a 30th of February — and an unbounded search
#: for it would hang the scheduler loop rather than reject the schedule.
_SEARCH_DAYS = 1500


class CronError(ValueError):
    """An expression that cannot be parsed, or can never fire."""


def _parse_field(raw: str, index: int) -> set[int]:
    low, high = _BOUNDS[index]
    names = _MONTHS if index == 3 else (_DAYS if index == 4 else {})
    out: set[int] = set()

    for part in str(raw).split(","):
        part = part.strip().lower()
        if not part:
            raise CronError(f"Empty value in field {index + 1}.")

        step = 1
        if "/" in part:
            part, _, step_raw = part.partition("/")
            if not step_raw.isdigit() or int(step_raw) < 1:
                raise CronError(f"'{step_raw}' is not a step.")
            step = int(step_raw)

        if part in ("*", ""):
            start, end = low, high
        elif "-" in part[1:] or (part.startswith("-") is False and "-" in part):
            a, _, b = part.partition("-")
            start, end = _value(a, names, low, high), _value(b, names, low, high)
            if start > end:
                raise CronError(f"'{raw}' has a range that runs backwards.")
        else:
            start = end = _value(part, names, low, high)

        out.update(range(start, end + 1, step))

    if index == 4:
        # Both 0 and 7 mean Sunday, as everywhere else.
        if 7 in out:
            out.discard(7)
            out.add(0)
    if not out:
        raise CronError(f"Field {index + 1} matches nothing.")
    return out


def _value(token: str, names: dict, low: int, high: int) -> int:
    token = token.strip().lower()
    if token in names:
        return names[token]
    if token == "7" and high == 6:
        return 0
    if not token.lstrip("-").isdigit():
        raise CronError(f"'{token}' is not a number.")
    value = int(token)
    if not low <= value <= high:
        raise CronError(f"'{token}' is outside {low}-{high}.")
    return value


class CronExpression:
    """A parsed expression. Construct it to validate; call `next_after` to use it."""

    __slots__ = ("raw", "minutes", "hours", "doms", "months", "dows",
                 "_dom_restricted", "_dow_restricted")

    def __init__(self, expression: str):
        raw = (expression or "").strip()
        raw = MACROS.get(raw.lower(), raw)
        fields = raw.split()
        if len(fields) != 5:
            raise CronError(
                "A schedule needs five fields — minute hour day-of-month month "
                "day-of-week — or a macro such as @daily. "
                f"Got {len(fields)}: {expression!r}")

        self.raw = raw
        self.minutes = _parse_field(fields[0], 0)
        self.hours = _parse_field(fields[1], 1)
        self.doms = _parse_field(fields[2], 2)
        self.months = _parse_field(fields[3], 3)
        self.dows = _parse_field(fields[4], 4)
        self._dom_restricted = fields[2].strip() != "*"
        self._dow_restricted = fields[4].strip() != "*"

    def _day_matches(self, day) -> bool:
        if day.month not in self.months:
            return False
        # Python: Monday=0. Cron: Sunday=0.
        dow = (day.weekday() + 1) % 7
        in_dom = day.day in self.doms
        in_dow = dow in self.dows
        if self._dom_restricted and self._dow_restricted:
            return in_dom or in_dow      # the Vixie rule
        if self._dom_restricted:
            return in_dom
        if self._dow_restricted:
            return in_dow
        return True

    def next_after(self, after: datetime, tz: str = "UTC") -> datetime:
        """The first firing strictly after `after`, as an aware UTC datetime."""
        zone = load_timezone(tz)
        local = after.astimezone(zone) if after.tzinfo else after.replace(
            tzinfo=timezone.utc).astimezone(zone)
        # Compare on the naive wall clock: an ambiguous hour (autumn) has two
        # instants with the same wall time, and only the first should fire.
        cursor = local.replace(second=0, microsecond=0, tzinfo=None) + timedelta(minutes=1)

        for offset in range(_SEARCH_DAYS):
            day = (cursor + timedelta(days=offset)).date()
            if not self._day_matches(day):
                continue
            for hour in sorted(self.hours):
                for minute in sorted(self.minutes):
                    candidate = datetime(day.year, day.month, day.day, hour, minute)
                    if candidate < cursor:
                        continue
                    return candidate.replace(tzinfo=zone).astimezone(timezone.utc)

        raise CronError(
            f"'{self.raw}' has no firing time in the next four years — check the "
            f"day-of-month and month together (there is no 30th of February).")


def load_timezone(name: str):
    try:
        return ZoneInfo(name or "UTC")
    except (ZoneInfoNotFoundError, ValueError, KeyError) as exc:
        raise CronError(f"Unknown timezone '{name}'.") from exc


def validate(expression: str, tz: str = "UTC") -> datetime:
    """Parse and prove it fires. Returns the next firing time."""
    return CronExpression(expression).next_after(
        datetime.now(timezone.utc), tz)


def describe(expression: str, tz: str = "UTC", count: int = 5) -> list[str]:
    """The next few firing times, for the UI to show before anyone saves."""
    expr = CronExpression(expression)
    out: list[str] = []
    cursor = datetime.now(timezone.utc)
    for _ in range(max(1, count)):
        cursor = expr.next_after(cursor, tz)
        out.append(cursor.isoformat())
    return out
