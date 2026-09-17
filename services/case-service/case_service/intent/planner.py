"""The planner — deterministic, closed-world, and it never sees the intent.

Design: docs/Future/Done/AI-ERA-ROADMAP.md §F-11: *"the LLM never emits BPMN; it
emits a goal spec, and the planner assembles only signed, capability-declared
fragments"*.

**This module takes a goal spec and returns a composition. It has no access to
the model, the intent text, or anything a person typed.** That is the security
property stated as a module boundary: whatever a prompt injection persuades a
model to say, the worst it can produce is a goal spec, and a goal spec can only
name outcomes and facts from vocabularies the fragments themselves defined.

**The search is deterministic.** Given the same fragments and the same goal, it
returns the same plan — no ranking heuristic, no model in the loop, no
tie-breaking by anything but a stable sort. HxGraph semantic ranking is
explicitly a *heuristic only* in the roadmap; it is not built here, and a
deterministic planner is worth more than a cleverer one that cannot be reasoned
about.

**Runaway plans are structurally impossible.** Each fragment may appear at most
once, so the composition is acyclic by construction rather than by a cycle check
that could be wrong; and the step budget is enforced during the search, not
after it.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import hashlib
import json
from typing import Any

#: A plan may not exceed this many steps. Bounded during the search, so a plan
#: that would be too long is never built rather than built and then rejected.
MAX_STEPS = 60
#: And this many fragments, which also bounds the search itself.
MAX_FRAGMENTS = 12


class PlanError(ValueError):
    """No plan could be composed. The message says which fact was missing."""


def plan_hash(definition: dict[str, Any]) -> str:
    """The hash a confirmation binds to. Over the DEFINITION, not the plan row.

    What a person is agreeing to is the process that will run, so that is what
    is hashed — a hash over the row would still match if somebody edited the
    definition and left the row's other columns alone.
    """
    return hashlib.sha256(json.dumps(
        definition, sort_keys=True, separators=(",", ":"),
        default=str).encode()).hexdigest()


def compose(
    goal_spec: dict[str, Any], fragments: list[Any],
    *, max_steps: int = MAX_STEPS,
) -> dict[str, Any]:
    """Find the shortest composition that satisfies the goal.

    A breadth-first search over `provides` → `requires`, which returns the
    fewest fragments that reach the goal. Shortest is the right objective here
    and not merely a convenient one: every extra fragment widens the capability
    union a person is being asked to approve.
    """
    goals = sorted({str(g) for g in (goal_spec.get("goals") or []) if g})
    if not goals:
        raise PlanError(
            "the goal spec names no outcome. A planner with nothing to reach "
            "cannot compose anything, and inventing a goal would be exactly the "
            "free-form generation this design forbids.")
    given = {str(f) for f in (goal_spec.get("given") or []) if f}

    usable = [f for f in fragments if getattr(f, "status", "") == "verified"]
    if not usable:
        raise PlanError(
            "no verified fragments exist. A draft fragment is one somebody is "
            "still writing, and composing from it would put unreviewed steps "
            "into a live process.")

    by_slug = {f.slug: f for f in usable}
    # Deterministic order: the search visits fragments in slug order, so the
    # same library and the same goal always give the same plan.
    ordered = sorted(usable, key=lambda f: f.slug)

    # Breadth-first over (facts, chosen). `chosen` is a tuple, so a fragment
    # cannot repeat and the composition is acyclic by construction.
    start = (frozenset(given), ())
    seen = {start[0]}
    queue = [start]
    while queue:
        facts, chosen = queue.pop(0)
        if all(g in facts for g in goals):
            return _assemble(list(chosen), by_slug, goal_spec, goals)
        if len(chosen) >= MAX_FRAGMENTS:
            continue
        for fragment in ordered:
            if fragment.slug in chosen:
                continue
            requires = {str(r) for r in (fragment.requires or [])}
            if not requires.issubset(facts):
                continue
            steps = sum(int(by_slug[s].step_count or 0) for s in chosen)
            if steps + int(fragment.step_count or 0) > max_steps:
                # Bounded DURING the search: a plan that would be too long is
                # never built, rather than built and then thrown away.
                continue
            nxt = facts | {str(p) for p in (fragment.provides or [])}
            if nxt in seen:
                continue
            seen.add(nxt)
            queue.append((nxt, (*chosen, fragment.slug)))

    missing = sorted(g for g in goals if g not in given)
    raise PlanError(
        f"no composition of the verified fragments reaches {missing}. "
        f"Nothing in the library provides it, and the planner will not invent a "
        f"step to bridge the gap.")


def _assemble(chosen: list[str], by_slug: dict[str, Any],
              goal_spec: dict[str, Any], goals: list[str]) -> dict[str, Any]:
    """Turn a fragment sequence into a definition and its unions."""
    stages: list[dict[str, Any]] = []
    capabilities: set[str] = set()
    data_classes: set[str] = set()
    steps = 0
    order = 0
    for slug in chosen:
        fragment = by_slug[slug]
        for stage in (fragment.stages or []):
            order += 1
            stages.append({**stage,
                           # Namespaced so two fragments contributing a stage
                           # with the same id cannot collide silently.
                           "id": f"{slug}.{stage.get('id')}",
                           "order": order,
                           "fragment": slug})
        capabilities |= {str(c) for c in (fragment.capabilities or [])}
        data_classes |= {str(d) for d in (fragment.data_classes or [])}
        steps += int(fragment.step_count or 0)

    definition = {
        "stages": stages,
        # Composed processes run on the existing interpreter unchanged; this
        # marks where the definition came from, for anybody reading it later.
        "composed_from": list(chosen),
        "goal": goals,
    }
    return {
        "fragment_slugs": list(chosen),
        "definition": definition,
        "capabilities": sorted(capabilities),
        "data_classes": sorted(data_classes),
        "step_count": steps,
        "plan_sha256": plan_hash(definition),
    }


def readable(plan: dict[str, Any], by_slug: dict[str, Any]) -> list[dict[str, Any]]:
    """The step list a person reads before confirming.

    Fragment names and what each one is for, in order — not stage ids. The
    confirmation is consent, and consent to something unreadable is not consent.
    """
    out = []
    for index, slug in enumerate(plan["fragment_slugs"], start=1):
        fragment = by_slug.get(slug)
        out.append({
            "step": index,
            "fragment": slug,
            "name": getattr(fragment, "display_name", slug),
            "does": getattr(fragment, "description", "") or "",
            "capabilities": sorted(getattr(fragment, "capabilities", []) or []),
        })
    return out
