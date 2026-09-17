"""The matcher — deterministic, local, and identical on both sides.

Design: docs/Future/Done/AI-ERA-ROADMAP.md §F-12: *"The matcher is deterministic and
runs locally on both sides from the same inputs — both parties can independently
verify the joint composition is what was agreed (no trusted third party)."*

**That sentence only means something if this module is a pure function**, so it
is one: no database, no clock, no randomness, no network. Given two offers and
two requirement sets it returns the same resolution on any machine, which is
what lets each side check the other's answer instead of trusting it.

**Deny-overrides survives negotiation by construction.** The seam's ceiling is
computed as the MINIMUM of what the two sides negotiated and what each side's
own platform permits. A contract cannot widen what a platform would allow on its
own — not because a check runs at the right moment, but because the wider number
is never produced.

Every clause is reported with how it resolved, including the ones that passed.
A negotiation somebody has to sign is not a place for a single green tick.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import hashlib
import json
from typing import Any

#: Ceilings, weakest first. Deliberately F-09's vocabulary — the seam is a
#: handoff, and a second scale would eventually disagree with the first.
#: `special_category` is absent there and absent here: no negotiation produces
#: a term this platform cannot express.
CEILINGS: tuple[str, ...] = ("none", "internal", "personal")


class MatchError(ValueError):
    """The inputs are not a negotiation this matcher can reason about."""


def _rank(ceiling: str) -> int:
    if ceiling not in CEILINGS:
        raise MatchError(
            f"{ceiling!r} is not a ceiling this platform can express. Article 9 "
            f"data is not negotiable — there is no term that would admit it.")
    return CEILINGS.index(ceiling)


def _clause(name: str, ok: bool, detail: str, **extra: Any) -> dict[str, Any]:
    return {"clause": name, "ok": bool(ok), "detail": detail, **extra}


def contract_hash(composition: dict[str, Any], resolution: dict[str, Any]) -> str:
    """What both signatures cover: the composition and the resolved terms.

    Not the row. A hash over the row would still match if somebody edited the
    composition and left the bookkeeping alone.
    """
    return hashlib.sha256(json.dumps(
        {"composition": composition,
         "terms": {c["clause"]: c.get("agreed") for c in resolution["clauses"]}},
        sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()


def match(
    *, our_offer: dict[str, Any], their_offer: dict[str, Any],
    our_requirements: dict[str, Any], their_requirements: dict[str, Any],
    platform_ceiling: str = "personal",
) -> dict[str, Any]:
    """Find a joint composition, or say precisely why there is none.

    `platform_ceiling` is this side's own boundary. It is an INPUT rather than
    something looked up, so the function stays pure and the other side can
    reproduce our answer by passing the same value we published.
    """
    clauses: list[dict[str, Any]] = []

    # ── 1. the seam's ceiling ───────────────────────────────────────────────
    ours = str(our_requirements.get("max_ceiling") or "internal")
    theirs = str(their_requirements.get("max_ceiling") or "internal")
    negotiated = CEILINGS[min(_rank(ours), _rank(theirs))]
    # THE line that makes deny-overrides structural: the wider number is never
    # produced, so it can never be stored, signed or enforced.
    effective = CEILINGS[min(_rank(negotiated), _rank(platform_ceiling))]
    clauses.append(_clause(
        "data_class_ceiling", True,
        f"they will accept at most {theirs!r}, we will send at most {ours!r}, "
        f"and this platform permits at most {platform_ceiling!r} — the seam runs "
        f"at {effective!r}",
        agreed=effective, negotiated=negotiated, platform=platform_ceiling))

    # ── 2. regions ──────────────────────────────────────────────────────────
    our_regions = {str(r) for r in (our_requirements.get("regions") or [])}
    their_regions = {str(r) for r in (their_requirements.get("regions") or [])}
    if our_regions and their_regions:
        shared = sorted(our_regions & their_regions)
        if not shared:
            return _unmatched(clauses, "regions",
                              f"we require {sorted(our_regions)} and they require "
                              f"{sorted(their_regions)}; there is no region both "
                              f"sides will operate in")
        clauses.append(_clause("regions", True, f"both sides accept {shared}",
                               agreed=shared))
    else:
        clauses.append(_clause("regions", True,
                               "neither side constrains the region", agreed=[]))

    # ── 3. the SLA promise ──────────────────────────────────────────────────
    # Each side's REQUIREMENT is what it needs from the other; each side's OFFER
    # is what it promises. A promise must be at least as fast as the need.
    for label, need, promise in (
        ("their_sla", our_requirements.get("max_response_hours"),
         their_offer.get("response_hours")),
        ("our_sla", their_requirements.get("max_response_hours"),
         our_offer.get("response_hours")),
    ):
        if need is None:
            clauses.append(_clause(label, True, "no response time required",
                                   agreed=None))
            continue
        if promise is None:
            return _unmatched(clauses, label,
                              f"a response within {need}h is required and the "
                              f"other side promised none")
        if float(promise) > float(need):
            return _unmatched(clauses, label,
                              f"{promise}h promised against {need}h required")
        clauses.append(_clause(label, True, f"{promise}h against {need}h required",
                               agreed=float(promise)))

    # ── 4. evidence obligations ─────────────────────────────────────────────
    required = {str(e) for e in (our_requirements.get("evidence") or [])} | \
               {str(e) for e in (their_requirements.get("evidence") or [])}
    offered = {str(e) for e in (our_offer.get("evidence") or [])} | \
              {str(e) for e in (their_offer.get("evidence") or [])}
    missing = sorted(required - offered)
    if missing:
        return _unmatched(clauses, "evidence",
                          f"neither side offers {missing}, which one of them "
                          f"requires")
    clauses.append(_clause("evidence", True,
                           f"both sides carry {sorted(required) or 'no'} obligations",
                           agreed=sorted(required)))

    # ── 5. the joint composition ────────────────────────────────────────────
    composition, why = _compose(our_offer, their_offer,
                                our_requirements, their_requirements)
    if composition is None:
        return _unmatched(clauses, "composition", why)
    clauses.append(_clause("composition", True,
                           f"{len(composition['steps'])} steps across both sides",
                           agreed=[s["fragment"] for s in composition["steps"]]))

    resolution = {"clauses": clauses, "matched": True}
    return {
        "matched": True,
        "composition": composition,
        "resolution": resolution,
        "effective_ceiling": effective,
        "contract_sha256": contract_hash(composition, resolution),
    }


def _unmatched(clauses: list[dict[str, Any]], name: str, why: str) -> dict[str, Any]:
    """Not a failure of either side — the two requirement sets simply have no
    joint composition that satisfies both, and saying which clause is the useful
    answer."""
    clauses.append(_clause(name, False, why))
    return {"matched": False, "composition": None,
            "resolution": {"clauses": clauses, "matched": False},
            "effective_ceiling": None, "contract_sha256": None,
            "reason": why}


def _compose(our_offer, their_offer, our_requirements, their_requirements):
    """Interleave the two sides' fragments until both sides' goals are reached.

    Deterministic: each side's fragments are taken in the order it published
    them, and a fragment is chosen only when everything it requires is already
    true. Both sides run this and must produce the same list.
    """
    supply: list[tuple[str, dict[str, Any]]] = []
    for side, offer in (("ours", our_offer), ("theirs", their_offer)):
        for fragment in (offer.get("fragments") or []):
            supply.append((side, fragment))

    goals = sorted({str(g) for g in (our_requirements.get("goals") or [])} |
                   {str(g) for g in (their_requirements.get("goals") or [])})
    if not goals:
        return None, ("neither side named an outcome, so there is nothing to "
                      "compose a joint process towards")

    facts: set[str] = set()
    steps: list[dict[str, Any]] = []
    used: set[str] = set()
    progress = True
    while progress and not all(g in facts for g in goals):
        progress = False
        for side, fragment in supply:
            slug = str(fragment.get("slug") or "")
            if slug in used:
                continue
            requires = {str(r) for r in (fragment.get("requires") or [])}
            if not requires.issubset(facts):
                continue
            used.add(slug)
            facts |= {str(p) for p in (fragment.get("provides") or [])}
            steps.append({
                "side": side, "fragment": slug,
                "provides": sorted(fragment.get("provides") or []),
                # Whether this step's output has to cross the boundary. The seam
                # is where the ceiling and the message contract apply, so it is
                # marked here rather than worked out later.
                "crosses_seam": False,
            })
            progress = True

    unreachable = sorted(g for g in goals if g not in facts)
    if unreachable:
        return None, (f"nothing either side offers provides {unreachable}, so no "
                      f"joint process reaches it")

    # Mark the seams: a step whose successor runs on the other side hands over.
    for index, step in enumerate(steps[:-1]):
        step["crosses_seam"] = steps[index + 1]["side"] != step["side"]
    return {"goals": goals, "steps": steps,
            "seams": sum(1 for s in steps if s["crosses_seam"])}, ""
