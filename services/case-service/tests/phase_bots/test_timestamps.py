"""Every timestamp a bot or schedule surface returns carries a UTC offset.

Half this schema's datetime columns are `timestamp with time zone` and half are
not — `bot_credential_leases.leased_at` is naive while `revoked_at` beside it is
aware, and `bot_jobs.queued_at` is naive while `started_at` is aware. All of
them hold UTC. Serialising them straight through `.isoformat()` emitted an
offset for one and not the other, so a browser read the bare ones as local time
and the Audit view showed a three-second credential lease as a two-hour one.

Found in a browser on 2026-08-21, on the exact screen somebody would open to ask
how long a bot held a supplier's password.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import inspect
from datetime import datetime, timedelta, timezone

from case_service.api.routers import bots as bots_router
from case_service.api.routers import scheduler as scheduler_router
from case_service.core.timestamps import iso_utc


class TestIsoUtc:
    def test_a_naive_value_is_stamped_utc_not_read_as_local(self):
        assert iso_utc(datetime(2026, 8, 21, 8, 27, 32)) == "2026-08-21T08:27:32+00:00"

    def test_an_aware_value_is_converted_not_relabelled(self):
        aware = datetime(2026, 8, 21, 10, 27, 32,
                         tzinfo=timezone(timedelta(hours=2)))
        assert iso_utc(aware) == "2026-08-21T08:27:32+00:00"

    def test_none_stays_none(self):
        assert iso_utc(None) is None

    def test_the_two_halves_of_a_lease_agree(self):
        """The defect itself: one instant, two columns, two hours apart."""
        naive_leased = datetime(2026, 8, 21, 8, 27, 32)
        aware_revoked = datetime(2026, 8, 21, 8, 27, 35, tzinfo=timezone.utc)
        leased = datetime.fromisoformat(iso_utc(naive_leased))
        revoked = datetime.fromisoformat(iso_utc(aware_revoked))
        assert (revoked - leased).total_seconds() == 3


class TestNoRouterSerialisesRawIsoformat:
    """A regression guard, not a style rule.

    The bug is invisible in any single line — `x.isoformat()` looks correct, and
    is, for the aware half of the schema. What makes it a defect is doing it
    beside a naive column. Naming `iso_utc` as the only way out of these two
    routers is what stops the next timestamp from reintroducing it.
    """

    def test_bots_router_has_no_bare_isoformat(self):
        assert ".isoformat()" not in inspect.getsource(bots_router)

    def test_scheduler_router_has_no_bare_isoformat(self):
        assert ".isoformat()" not in inspect.getsource(scheduler_router)
