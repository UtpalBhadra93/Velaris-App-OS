"""Shared date anchors for the calendar tests.

**Why this module exists.** `test_calendar_p1`, `test_calendar_p4_events` and
`test_calendar_p5_full` each pinned their own `datetime(2026, 9, 2)` — a
Wednesday, chosen because it sits inside every default working pattern. That
date passed on 2026-09-02, and twenty-eight tests went red in a body: the
scheduling routes correctly refuse *"That start time has already passed"*, and
the absence listing correctly omits an absence that is over. **The code was
right and the tests were wrong**, which is the worst kind of red — it is
indistinguishable at a glance from a regression, and after a week of seeing it
nobody reads the list any more.

A calendar test needs three things from its anchor and only three: that it is a
**Wednesday** (a working day in every pattern these tests set up), that a
**Saturday** is reachable from it (the non-working day the availability tests
turn on), and that it is far enough **ahead of now** that creating a meeting on
it is creating a future meeting. None of those require a fixed date, so none of
them is pinned to one any more.

Deliberately **not** applied to `test_calendar_feeds.py` and
`test_calendar_ics_import.py`, which also hold 2026-09 dates and are green:
those parse and round-trip an `.ics`, where the date is data rather than a
schedule, and a past date proves the parser just as well as a future one.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

#: How far ahead the anchor sits. A week, so a test that walks a series forward
#: by a few days — or one run just before midnight — is still comfortably in the
#: future, and short enough that the anchor stays in a recognisable week.
LEAD_DAYS = 7

#: Python's Monday-is-0 weekday for Wednesday.
_WEDNESDAY = 2


def next_wednesday(hour: int = 0) -> datetime:
    """The first Wednesday at least `LEAD_DAYS` from today, at `hour` UTC.

    Between 7 and 13 days out, always a Wednesday, always in the future.
    """
    day = (datetime.now(timezone.utc) + timedelta(days=LEAD_DAYS)).date()
    day += timedelta(days=(_WEDNESDAY - day.weekday()) % 7)
    return datetime(day.year, day.month, day.day, hour, tzinfo=timezone.utc)


def saturday_after(wednesday: datetime, hour: int | None = None) -> datetime:
    """The Saturday of the same week — three days on from the anchor.

    Derived rather than pinned so the two can never drift apart: a "weekend"
    date that had quietly become a Thursday would make the availability tests
    assert the opposite of what they read.
    """
    out = wednesday + timedelta(days=3)
    assert out.weekday() == 5, "the anchor moved off Wednesday"
    return out if hour is None else out.replace(hour=hour)
