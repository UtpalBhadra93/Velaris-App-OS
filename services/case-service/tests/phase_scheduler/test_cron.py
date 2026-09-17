"""The cron parser — what it accepts, and what it refuses to store.

A schedule is written once and then nobody looks at it again, so an expression
that parses but never fires is worse than one that fails: the failure is visible
and the silence is not. These tests are mostly about the second kind.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from case_service.scheduler.cron import CronError, CronExpression, describe

UTC = timezone.utc


def _next(expr: str, after: datetime, tz: str = "UTC") -> datetime:
    return CronExpression(expr).next_after(after, tz)


class TestParsing:
    def test_the_shapes_people_actually_write(self):
        base = datetime(2026, 8, 20, 12, 0, tzinfo=UTC)
        assert _next("0 2 * * *", base) == datetime(2026, 8, 21, 2, 0, tzinfo=UTC)
        assert _next("*/15 * * * *", base) == datetime(2026, 8, 20, 12, 15, tzinfo=UTC)
        assert _next("0 9-17 * * 1-5", base) == datetime(2026, 8, 20, 13, 0, tzinfo=UTC)
        assert _next("0 6 1 * *", base) == datetime(2026, 9, 1, 6, 0, tzinfo=UTC)
        assert _next("@daily", base) == datetime(2026, 8, 21, 0, 0, tzinfo=UTC)

    def test_names_are_accepted_for_months_and_days(self):
        base = datetime(2026, 8, 20, 12, 0, tzinfo=UTC)
        assert _next("0 0 * * sun", base) == datetime(2026, 8, 23, 0, 0, tzinfo=UTC)
        assert _next("0 0 1 jan *", base) == datetime(2027, 1, 1, 0, 0, tzinfo=UTC)

    def test_sunday_is_both_zero_and_seven(self):
        base = datetime(2026, 8, 20, 12, 0, tzinfo=UTC)
        assert _next("0 0 * * 7", base) == _next("0 0 * * 0", base)

    def test_day_of_month_and_day_of_week_are_or_ed(self):
        """The Vixie rule. `0 0 1 * 1` is "the 1st OR any Monday" — reading it
        as an AND silently turns a daily-ish schedule into a once-a-year one."""
        base = datetime(2026, 8, 20, 12, 0, tzinfo=UTC)
        assert _next("0 0 1 * 1", base).day == 24   # the Monday, not 1 Sep

        # `describe` reads the clock, so asserting literal dates here made this
        # test rot: it was written in the week of 2026-08-20 and started failing
        # the following week. The Vixie rule is a property, so assert the
        # property — every firing is a Monday **or** the 1st, and both appear.
        got = [datetime.fromisoformat(when) for when in describe("0 0 1 * 1", "UTC", 6)]
        assert all(w.weekday() == 0 or w.day == 1 for w in got)
        assert any(w.weekday() == 0 for w in got)
        assert any(w.day == 1 for w in got)

    @pytest.mark.parametrize("bad", [
        "0 2 * *", "", "not a cron", "0 2 * * 8", "60 * * * *",
        "0 0 * * mon-sun-tue", "*/0 * * * *", "0 0 32 * *",
    ])
    def test_nonsense_is_refused(self, bad):
        with pytest.raises(CronError):
            CronExpression(bad).next_after(datetime.now(UTC))

    def test_an_expression_that_can_never_fire_is_refused(self):
        """`0 0 30 2 *` parses perfectly and there is no 30th of February. Stored
        unchecked it would be a schedule that simply never runs, which is the
        hardest failure to notice."""
        with pytest.raises(CronError, match="no firing time"):
            CronExpression("0 0 30 2 *").next_after(datetime.now(UTC))

    def test_an_unknown_timezone_is_refused(self):
        with pytest.raises(CronError):
            CronExpression("0 2 * * *").next_after(datetime.now(UTC), "Mars/Olympus")


class TestDaylightSaving:
    """A nightly job set for 02:30 must stay at 02:30 local, fire once when the
    hour repeats, and not vanish when the hour does not exist."""

    ZONE = ZoneInfo("Europe/Berlin")

    def _local(self, instant: datetime) -> str:
        return instant.astimezone(self.ZONE).strftime("%Y-%m-%d %H:%M")

    def test_it_holds_the_local_wall_clock_across_a_change(self):
        cursor = datetime(2026, 10, 24, 12, 0, tzinfo=UTC)
        seen = []
        for _ in range(4):
            cursor = _next("30 2 * * *", cursor, "Europe/Berlin")
            seen.append(self._local(cursor))
        assert seen == ["2026-10-25 02:30", "2026-10-26 02:30",
                        "2026-10-27 02:30", "2026-10-28 02:30"]

    def test_the_repeated_hour_fires_once(self):
        """Autumn: 02:00-03:00 happens twice. Comparing UTC instants would fire
        the schedule in both, sending the same nightly mail twice."""
        cursor = datetime(2026, 10, 24, 12, 0, tzinfo=UTC)
        first = _next("30 2 * * *", cursor, "Europe/Berlin")
        second = _next("30 2 * * *", first, "Europe/Berlin")
        assert second - first == timedelta(hours=25)   # one wall-clock day
        assert self._local(first) == "2026-10-25 02:30"
        assert self._local(second) == "2026-10-26 02:30"

    def test_a_time_that_does_not_exist_still_fires_that_day(self):
        """Spring: 02:00-03:00 never happens in Berlin on 2027-03-28. The job
        must not skip the day — it shifts forward to 03:30 local and runs."""
        cursor = datetime(2027, 3, 27, 12, 0, tzinfo=UTC)
        firing = _next("30 2 * * *", cursor, "Europe/Berlin")
        assert self._local(firing) == "2027-03-28 03:30"
        assert self._local(_next("30 2 * * *", firing, "Europe/Berlin")) == \
            "2027-03-29 02:30"
