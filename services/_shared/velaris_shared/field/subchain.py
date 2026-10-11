"""The chain a node keeps while it is away, and how the hub checks it.

Design: docs/Future/Done/AI-ERA-ROADMAP.md §F-10: *"Sub-chain anchoring makes offline
periods auditable: tampering with the field log breaks the sub-chain hash handed
over at reconnect."*

**Chain-of-chains, not one chain.** The hub's own records are chained
(F-01, F-03, F-08). A disconnected node cannot participate in that chain — it
cannot see the head — so it keeps its own, starting from a genesis the hub
issued with the dispatch. On reconnection the hub verifies the node's chain in
full and anchors its head. An offline period therefore has exactly the same
tamper-evidence as an online one, without the node ever needing to reach us.

**A stream that does not start at the dispatch's genesis is not this dispatch's
stream.** That check is first, and it is the one that stops a node replaying an
old period's work, or handing over a chain it built somewhere else.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import hashlib
import json
from typing import Any

#: Every field event's payload is committed to by hash. The hub keeps the
#: payload too, but the hash is what the chain covers, so an edit to a stored
#: payload is detectable even by somebody who can write the hub's own tables.
def payload_hash(payload: Any) -> str:
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":"),
                   default=str).encode()).hexdigest()


def event_document(event: dict[str, Any]) -> dict[str, Any]:
    """What the chain covers.

    `node_time` is IN it: the node's own clock is untrustworthy for ordering,
    but it is what the field worker will describe afterwards, and a value nobody
    can alter later is worth more than one nobody can rely on.
    """
    return {
        "seq": int(event.get("seq") or 0),
        "case_id": str(event.get("case_id") or ""),
        "kind": str(event.get("kind") or ""),
        "target": event.get("target"),
        "payload_sha256": str(event.get("payload_sha256") or ""),
        "node_time": event.get("node_time"),
    }


def link(document: dict[str, Any], prev_hash: str) -> str:
    """`sha256(prev || canonical(document))` — the same construction the hub's
    own chains use, so one reader can check both."""
    canonical = json.dumps(document, sort_keys=True, separators=(",", ":"),
                           default=str)
    return hashlib.sha256((prev_hash + canonical).encode()).hexdigest()


def build(events: list[dict[str, Any]], genesis: str) -> list[dict[str, Any]]:
    """Chain a list of raw events. Used by tests and by a node implementation.

    It lives here rather than in a test helper on purpose: the hub and any node
    must agree exactly on what is hashed, and a second implementation of that
    is how two sides come to disagree about whether a log was tampered with.
    """
    out: list[dict[str, Any]] = []
    prev = genesis
    for index, raw in enumerate(events, start=1):
        event = {**raw, "seq": raw.get("seq", index)}
        event["payload_sha256"] = event.get("payload_sha256") or payload_hash(
            event.get("payload"))
        event["prev_hash"] = prev
        event["content_hash"] = link(event_document(event), prev)
        prev = event["content_hash"]
        out.append(event)
    return out


def verify(events: list[dict[str, Any]], genesis: str) -> tuple[bool, str, str]:
    """`(ok, head, reason)`. Reports the FIRST break and stops.

    Continuing past a break would report every later event as broken too, which
    buries the one that actually was.
    """
    prev = genesis
    expected_seq = 1
    for event in events:
        if int(event.get("seq") or 0) != expected_seq:
            return (False, prev,
                    f"sequence {event.get('seq')} arrived where {expected_seq} "
                    f"was expected — a gap is a lost stream, not a late one")
        if event.get("prev_hash") != prev:
            return (False, prev,
                    f"event {expected_seq} does not follow the one before it")
        recomputed = link(event_document(event), prev)
        if recomputed != event.get("content_hash"):
            return (False, prev,
                    f"event {expected_seq} no longer hashes to its own address")
        # Required, not merely checked when present (Oct item 8 audit): an
        # event that declared no hash had a payload the chain did not cover, so
        # the payload could be rewritten — on the device or in the hub's own
        # table — without any link breaking.
        declared = event.get("payload_sha256")
        if not declared or declared != payload_hash(event.get("payload")):
            return (False, prev,
                    f"event {expected_seq}'s payload does not match the hash the "
                    f"chain commits to")
        prev = event["content_hash"]
        expected_seq += 1
    return True, prev, ""
