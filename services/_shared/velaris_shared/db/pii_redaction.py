"""Export redaction — mechanical half of HxPrivacy (§8.10).

Lives in `core` rather than `compliance` for one reason: `analytics` and
`costing` are service-extraction targets, and importing `compliance` from
either would mean dragging the entire compliance package — audit chain,
ledger, DR, consent — along at split time. This module depends only on
`db.models`, so it travels with anything.

`compliance.pii_scan` re-exports these, so compliance remains their
documented home even though the code sits here.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .models import PIIFindingModel

CONFIRMED = "confirmed"


#: Only CONFIRMED findings redact. `detected` is a heuristic guess awaiting
#: review, and letting a guess silently blank a column in someone's billing
#: export would make the scanner's false positives everyone's problem.
async def confirmed_field_paths(
    session: AsyncSession, *, tenant_id: str | None = None,
) -> set[str]:
    """Field paths a reviewer has confirmed hold personal data.

    Returned as bare path segments too, because an export column is named
    `customer_email`, not `sections[0].fields[2].customer_email`.
    """
    q = select(PIIFindingModel).where(PIIFindingModel.status == CONFIRMED)
    if tenant_id:
        q = q.where(PIIFindingModel.tenant_id == tenant_id)

    paths: set[str] = set()
    for row in (await session.execute(q)).scalars().all():
        path = (row.field_path or "").strip()
        if not path:
            continue
        paths.add(path.lower())
        paths.add(path.rsplit(".", 1)[-1].lower())
    return paths


REDACTED = "[redacted]"


def redact_rows(rows: list[dict[str, Any]], paths: set[str]) -> list[dict[str, Any]]:
    """Replace confirmed-PII values with a marker, keeping the key.

    **Replaced, never removed.** Dropping the key changes the shape of the
    export, so a downstream consumer reading positionally silently shifts a
    column — and a human reading it cannot tell a redacted field from one that
    was never collected. `[redacted]` says which happened.

    Case-insensitive, and matches the final path segment, because an export
    column carries the field's own name rather than its position in a form.
    """
    if not paths or not rows:
        return rows
    out: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            out.append(row)
            continue
        out.append({
            k: (REDACTED if str(k).lower() in paths else v)
            for k, v in row.items()
        })
    return out
