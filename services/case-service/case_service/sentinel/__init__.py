"""HxSentinel — historical-proof risk & recommendation engine.

P1 (this phase) ships **no recommendations**. It establishes the two things
everything later depends on:

* ``case_outcomes`` — confirmed outcomes on closed cases, the ground truth;
* ``case_risk_signals`` — extracted, matchable features per case.

A risk engine with no labelled history has nothing to be right or wrong about,
so the labelling comes first and starts accumulating immediately.

Design stance, unchanged from the design doc: the engine **recommends and
cites, a human decides**. Nothing here ever blocks a case.
"""
from case_service.sentinel.taxonomy import CORE_KEYS, CORE_LABELS, core_as_dicts, is_core

__all__ = ["CORE_KEYS", "CORE_LABELS", "core_as_dicts", "is_core"]
