"""`duration_seconds` must answer on the harness's own dialect.

The helper branched on PostgreSQL and fell through to TIMESTAMPDIFF for
everything else — a function SQLite does not have — so six dashboard tests
failed with `no such column: SECOND` on the harness and passed nowhere but
Postgres. The point of this test is the arithmetic, not the spelling: whatever
branch runs, an hour must come back as 3600 seconds.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from sqlalchemy import literal, select

from velaris_shared.db.portable_sql import duration_seconds


@pytest.mark.asyncio
async def test_duration_seconds_counts_an_hour_as_3600(session) -> None:
    start = datetime(2026, 9, 10, 9, 0, 0)
    end = start + timedelta(hours=1)

    expr = duration_seconds(session, literal(start), literal(end))
    value = (await session.execute(select(expr))).scalar_one()

    assert round(float(value)) == 3600


@pytest.mark.asyncio
async def test_duration_seconds_is_signed(session) -> None:
    """End before start is negative, not an absolute value.

    Callers subtract to get an elapsed time; silently flipping the sign would
    turn a clock-skewed row into a plausible-looking duration.
    """
    start = datetime(2026, 9, 10, 9, 0, 0)
    expr = duration_seconds(session, literal(start), literal(start - timedelta(minutes=5)))

    value = (await session.execute(select(expr))).scalar_one()

    assert round(float(value)) == -300
