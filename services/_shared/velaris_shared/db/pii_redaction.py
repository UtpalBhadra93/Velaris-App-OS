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


def _redact_value(value: Any, paths: set[str]) -> Any:
    if isinstance(value, dict):
        return {k: (REDACTED if str(k).lower() in paths else _redact_value(v, paths))
                for k, v in value.items()}
    if isinstance(value, list):
        return [_redact_value(v, paths) for v in value]
    if isinstance(value, str) and value[:1] in "{[":
        # A JSON column arrives as text from a raw SELECT on some drivers.
        import json
        try:
            parsed = json.loads(value)
        except (TypeError, ValueError):
            return value
        cleaned = _redact_value(parsed, paths)
        return value if cleaned == parsed else json.dumps(cleaned, default=str)
    return value


def redact_rows_deep(rows: list[dict[str, Any]], paths: set[str]) -> list[dict[str, Any]]:
    """`redact_rows`, and inside nested values too.

    A raw table export is not a flat report: a case's fields sit inside the
    `data` JSON column, so matching column names alone redacts nothing that
    matters. Same contract as `redact_rows` — the key stays, the value becomes
    the marker, and anything that is not a dict passes through untouched.
    """
    if not paths or not rows:
        return rows
    return [_redact_value(r, paths) if isinstance(r, dict) else r for r in rows]


def values_at(data: Any, paths: set[str], *, min_len: int = 3, limit: int = 200) -> list[str]:
    """The values held under confirmed-PII keys, as strings.

    For masking those values where they turn up in free text. Short values are
    skipped: masking every "no" or "1" in a prompt destroys it and hides nothing.
    """
    found: list[str] = []

    def walk(node: Any, hit: bool) -> None:
        if len(found) >= limit:
            return
        if isinstance(node, dict):
            for k, v in node.items():
                walk(v, hit or str(k).lower() in paths)
        elif isinstance(node, list):
            for v in node:
                walk(v, hit)
        elif hit and isinstance(node, (str, int, float)) and not isinstance(node, bool):
            text = str(node).strip()
            if len(text) >= min_len and text != REDACTED and text not in found:
                found.append(text)

    if paths:
        walk(data, False)
    return found
