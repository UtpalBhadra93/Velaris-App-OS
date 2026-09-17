"""Verified fragments — and the gate that makes a declaration true.

Design: docs/Future/Done/AI-ERA-ROADMAP.md §F-11.

A fragment declares four things: what it `requires`, what it `provides`, the
data classes it touches, and the capabilities it needs. **Those declarations are
the planner's entire vocabulary** — it matches `provides` to `requires` and has
no opinion whatever about what a fragment means.

That is only safe if the declarations are true, which is what the conformance
gate is for. The roadmap's exit criterion is precise about the failure it must
catch: *"a fragment whose declared capabilities understate its behavior is
caught by the conformance gate"*. Understating is the dangerous direction —
a fragment that declares less than it does gets composed into plans that were
approved on the strength of a smaller capability union than the one that will
actually run.

**The capability vocabulary is closed**, like every other vocabulary in this
platform that a decision depends on. A fragment naming a capability outside it
cannot be verified, so it cannot be composed.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import hashlib
import json
from typing import Any

#: Every side effect a fragment may declare. Closed, and each maps to something
#: a composed process can actually do through the existing interpreter.
CAPABILITIES: frozenset[str] = frozenset({
    "case.advance",        # move the case along
    "case.write_variable", # set a case variable
    "case.note",           # add a note
    "notify.customer",     # send the customer something
    "notify.internal",     # send staff something
    "document.generate",   # produce a document
    "payment.request",     # ask for money
    "payment.refund",      # send money back
    "approval.request",    # require a human decision
})

#: Capabilities whose declaration a person must be shown at confirm time,
#: because they leave the platform or move money. Not a different check — the
#: plan lists everything — but these are the ones a reader must not skim.
CONSEQUENTIAL: frozenset[str] = frozenset({
    "notify.customer", "payment.request", "payment.refund", "document.generate",
})

#: What a step type in a fragment's stages IMPLIES it needs. The gate compares
#: this against the declaration, and it is deliberately generous: a step type
#: that implies nothing is not evidence that the fragment does nothing.
STEP_IMPLIES: dict[str, tuple[str, ...]] = {
    "notification": ("notify.internal",),
    "customer_notification": ("notify.customer",),
    "email": ("notify.customer",),
    "document": ("document.generate",),
    "payment": ("payment.request",),
    "refund": ("payment.refund",),
    "approval": ("approval.request",),
    "form": ("case.write_variable",),
    "automated": ("case.advance",),
}


class FragmentError(ValueError):
    """The fragment is not something the library can hold."""


def canonical(payload: Any) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)


def content_hash(fragment: dict[str, Any]) -> str:
    """Over the declaration AND the stages together.

    Both, because a fragment whose stages changed under an unchanged declaration
    is exactly the supply-chain problem content addressing exists to catch.
    """
    return hashlib.sha256(canonical({
        "slug": fragment.get("slug"),
        "requires": sorted(fragment.get("requires") or []),
        "provides": sorted(fragment.get("provides") or []),
        "data_classes": sorted(fragment.get("data_classes") or []),
        "capabilities": sorted(fragment.get("capabilities") or []),
        "stages": fragment.get("stages") or [],
    }).encode()).hexdigest()


def validate(fragment: dict[str, Any]) -> list[str]:
    """Structural problems. Empty list means the shape is acceptable."""
    errors: list[str] = []
    if not str(fragment.get("slug") or "").strip():
        errors.append("a fragment needs a slug")
    for key in ("requires", "provides", "data_classes", "capabilities"):
        value = fragment.get(key)
        if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
            errors.append(f"{key} must be a list of strings")
    unknown = sorted(set(fragment.get("capabilities") or []) - CAPABILITIES)
    if unknown:
        errors.append(
            f"capabilities outside the closed vocabulary: {unknown}. A fragment "
            f"that names a capability this platform does not define cannot be "
            f"reasoned about, so it cannot be composed.")
    stages = fragment.get("stages")
    if not isinstance(stages, list) or not stages:
        errors.append("a fragment contributes at least one stage")
    else:
        for stage in stages:
            if not isinstance(stage, dict) or not stage.get("id"):
                errors.append("every stage needs an id")
    if not fragment.get("provides"):
        # A fragment that provides nothing can never be chosen by the planner,
        # because nothing it could satisfy would ever match.
        errors.append("a fragment that provides nothing can never be planned in")
    return errors


def implied_capabilities(stages: list[dict[str, Any]]) -> set[str]:
    """What the stages say they need, read from their step types."""
    out: set[str] = set()
    for stage in stages or []:
        for step in (stage.get("steps") or []):
            for cap in STEP_IMPLIES.get(str(step.get("step_type") or ""), ()):
                out.add(cap)
    return out


def conformance(fragment: dict[str, Any]) -> dict[str, Any]:
    """Does the declaration cover the behaviour?

    **Understating is fatal; overstating is not.** A fragment that declares more
    than it does is merely pessimistic — plans containing it are judged against a
    larger capability union than they need, which is the safe direction. One
    that declares LESS gets composed into plans approved on the strength of a
    union smaller than what will run, and that is the failure the roadmap's exit
    criterion names.
    """
    declared = set(fragment.get("capabilities") or [])
    implied = implied_capabilities(fragment.get("stages") or [])
    understated = sorted(implied - declared)
    overstated = sorted(declared - implied)
    return {
        "declared": sorted(declared),
        "implied": sorted(implied),
        "understated": understated,
        "overstated": overstated,
        "passed": not understated,
        "reason": ("" if not understated else
                   f"the stages need {understated}, which the fragment does not "
                   f"declare. A plan approved on this fragment's declaration "
                   f"would be approved on less than it actually does."),
    }


def step_count(stages: list[dict[str, Any]]) -> int:
    return sum(len(stage.get("steps") or []) for stage in (stages or []))
