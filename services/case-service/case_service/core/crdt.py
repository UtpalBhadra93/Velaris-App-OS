"""CRDT convergence for offline case-variable edits (roadmap #38, §2.7).

The Portal PWA already queues submissions offline and replays them with an
idempotency key, so a *create* can never duplicate. Edits had no such
protection: two clients editing the same variable offline resolved by whichever
write reached the server last, so the loser's edit vanished silently and the
outcome depended on network timing and client clocks.

This is an **LWW-Register per variable** — a real CRDT, not "last write wins by
wall clock". Each variable carries a Lamport counter and an origin id, and
merges are decided on the pair:

    (lamport, origin)  compared lexicographically, higher wins

Which gives the three properties convergence actually requires:

* **commutative** — apply A then B, or B then A: same result;
* **idempotent** — replaying a write changes nothing;
* **deterministic** — ties break on `origin`, a total order, so every replica
  reaches the same value without coordination.

Wall-clock timestamps have none of these when clocks disagree, which offline
clients' clocks reliably do.

The Lamport rule on write is the standard one: a node's counter becomes
``max(local, received) + 1``, so causality is preserved without synchronised
clocks. A client that has been offline for a week still sorts correctly
relative to edits it never saw.

**What this does not do.** An LWW-Register converges on a *whole value*: two
offline edits to the same variable still end with one of them, chosen
deterministically rather than by luck. It does not merge *within* a value —
concurrent edits to different keys of the same JSON object do not combine.
That needs a map/text CRDT per field type and is a larger piece of work;
converging deterministically is the part that stops silent data loss.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

#: Origin ids are opaque client identifiers; bounded so a hostile client
#: cannot win every tie by sending a megabyte of 'z'.
MAX_ORIGIN_LEN = 64


@dataclass(frozen=True)
class Stamp:
    """A version stamp: Lamport counter plus a tie-breaking origin id."""

    lamport: int
    origin: str

    def __post_init__(self) -> None:
        if self.lamport < 0:
            raise ValueError("lamport counter cannot be negative")

    @property
    def key(self) -> tuple[int, str]:
        return (self.lamport, self.origin)

    def dominates(self, other: "Stamp") -> bool:
        """True if this stamp wins against `other`.

        Strictly greater — an equal stamp does not win, which is what makes
        replaying the same write idempotent rather than a no-op that still
        rewrites the row.
        """
        return self.key > other.key


def normalise_origin(origin: Any) -> str:
    origin = str(origin or "").strip()[:MAX_ORIGIN_LEN]
    return origin or "unknown"


def next_lamport(local: int, received: int | None = None) -> int:
    """Lamport's rule: max(local, received) + 1."""
    return max(int(local or 0), int(received or 0)) + 1


def merge(
    current_value: Any,
    current: Stamp,
    incoming_value: Any,
    incoming: Stamp,
) -> tuple[Any, Stamp, str]:
    """Merge one incoming write against the stored value.

    Returns `(value, stamp, outcome)` where outcome is one of:

    * ``applied``   — the incoming write won and should be stored;
    * ``ignored``   — the stored value wins; the incoming write is stale;
    * ``duplicate`` — the exact same stamp, so nothing to do.

    The caller persists on ``applied`` only. `ignored` is not an error: it is
    the normal, correct outcome of a client replaying an edit that a newer one
    has already superseded, and reporting it lets the client stop resending.
    """
    if incoming.key == current.key:
        return current_value, current, "duplicate"
    if incoming.dominates(current):
        return incoming_value, incoming, "applied"
    return current_value, current, "ignored"


def merge_all(
    stored: dict[str, tuple[Any, Stamp]],
    incoming: list[dict[str, Any]],
) -> tuple[dict[str, tuple[Any, Stamp]], list[dict[str, Any]]]:
    """Merge a batch of offline edits against stored state.

    `incoming` items are ``{"key", "value", "lamport", "origin"}``. Returns the
    merged state and a per-item report, so a client learns exactly which of its
    queued edits landed and which were superseded.

    Order-independent by construction: each item merges against whatever is
    current at that moment, and the winner is decided on stamps, so replaying
    the batch in any order converges on the same state.
    """
    state = dict(stored)
    report: list[dict[str, Any]] = []

    for item in incoming:
        key = str(item.get("key") or "")
        if not key:
            report.append({"key": key, "outcome": "rejected",
                           "reason": "missing key"})
            continue
        try:
            stamp = Stamp(
                lamport=int(item.get("lamport") or 0),
                origin=normalise_origin(item.get("origin")),
            )
        except (TypeError, ValueError) as exc:
            report.append({"key": key, "outcome": "rejected", "reason": str(exc)})
            continue

        cur_value, cur_stamp = state.get(key, (None, Stamp(0, "")))
        value, winner, outcome = merge(cur_value, cur_stamp, item.get("value"), stamp)
        state[key] = (value, winner)
        report.append({
            "key": key,
            "outcome": outcome,
            "lamport": winner.lamport,
            "origin": winner.origin,
        })

    return state, report


# ─── Persistence bridge ───────────────────────────────────────────


async def sync_case_variables(
    session,
    case_id,
    edits: list[dict[str, Any]],
    *,
    actor_id: str = "offline-sync",
) -> dict[str, Any]:
    """Merge a batch of offline variable edits into `case_instance_variables`.

    Writes rows directly rather than through the case-vars façade, because the
    façade's job is authorisation and namespacing on a *new* write, while this
    is convergence on rows that already exist and already passed that check
    when they were first created offline. A key with no existing row is
    therefore **rejected**, not created — offline sync converges edits, it does
    not smuggle in new variables.
    """
    from sqlalchemy import select

    from case_service.db.models import CaseInstanceVariableModel

    rows = (await session.execute(
        select(CaseInstanceVariableModel).where(
            CaseInstanceVariableModel.case_id == case_id
        )
    )).scalars().all()
    by_key = {r.full_key: r for r in rows}

    report: list[dict[str, Any]] = []
    applied = 0

    for item in edits:
        key = str(item.get("key") or "")
        row = by_key.get(key)
        if row is None:
            report.append({"key": key, "outcome": "rejected",
                           "reason": "no such variable on this case"})
            continue

        try:
            incoming = Stamp(
                lamport=int(item.get("lamport") or 0),
                origin=normalise_origin(item.get("origin")),
            )
        except (TypeError, ValueError) as exc:
            report.append({"key": key, "outcome": "rejected", "reason": str(exc)})
            continue

        # Writing rows directly means the façade's guards do not apply here,
        # so the value cap has to be re-enforced. Skipping it would make this
        # endpoint the one unbounded write path into case storage.
        from case_service.case_vars.service import _value_size_ok

        if not _value_size_ok(item.get("value")):
            report.append({"key": key, "outcome": "rejected",
                           "reason": "value exceeds the 64KB limit"})
            continue

        current = Stamp(lamport=int(row.lamport or 0), origin=row.origin or "")
        _value, winner, outcome = merge(
            _unpack_row(row), current, item.get("value"), incoming,
        )

        if outcome == "applied":
            _pack_into_row(row, item.get("value"))
            row.lamport = winner.lamport
            row.origin = winner.origin
            row.written_by = actor_id
            applied += 1

        report.append({
            "key": key, "outcome": outcome,
            "lamport": winner.lamport, "origin": winner.origin,
        })

    return {
        "case_id": str(case_id),
        "submitted": len(edits),
        "applied": applied,
        "results": report,
    }


def _unpack_row(row) -> Any:
    for attr in ("value_text", "value_num", "value_bool", "value_json"):
        v = getattr(row, attr, None)
        if v is not None:
            return v
    return None


def _pack_into_row(row, value: Any) -> None:
    """Store a value in the typed column that fits it.

    Every other typed column is cleared, so a variable that changes shape
    (number becoming text, say) cannot leave a stale value behind for
    `_unpack_row` to find first.
    """
    row.value_text = row.value_num = row.value_bool = row.value_json = None
    if isinstance(value, bool):
        row.value_bool = value
    elif isinstance(value, (int, float)):
        row.value_num = float(value)
    elif isinstance(value, str):
        row.value_text = value
    elif value is not None:
        row.value_json = value
