"""Dialect-portable SQL fragments.

Expressions that PostgreSQL and MySQL/MariaDB spell differently. Each helper
takes the session's live bind so the choice follows the database actually
connected, not the configured backend name — the two can disagree during a
migration between engines.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

from typing import Any

from sqlalchemy import extract, func, text
from sqlalchemy.ext.asyncio import AsyncSession


def dialect_of(session: AsyncSession) -> str:
    """The live bind's dialect name ('postgresql' | 'mysql' | 'sqlite' ...).

    SQLAlchemy reports "mysql" for MariaDB too; the distinction does not matter
    for anything in this module, since MariaDB accepts the same functions.
    """
    return session.get_bind().dialect.name


def duration_seconds(session: AsyncSession, start: Any, end: Any) -> Any:
    """Seconds between two DATETIME columns, as a SQL expression.

    ``EXTRACT(epoch FROM ts)`` is PostgreSQL-only: MySQL and MariaDB reject it
    with a syntax error at the word ``epoch``, which surfaced as a 500 on
    /analytics/overview for every MySQL install. TIMESTAMPDIFF is the portable
    spelling there.
    """
    if dialect_of(session) == "postgresql":
        return extract("epoch", end) - extract("epoch", start)
    return func.timestampdiff(text("SECOND"), start, end)
