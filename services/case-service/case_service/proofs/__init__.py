"""F-03 — counterfactual compliance proofs.

HxArbiter's what-if (`hxarbiter/nlp/whatif.py`) already answers *"if this draft
had been live, what would have decided differently"*. It is built for a person
about to press a button: capped at 2,000 rows, windowed to 30 days, kept on the
draft, and never re-derivable.

An auditor asks something else — *"prove to me what WOULD have happened, across
the entire caseload, and let me re-derive it myself"*. That needs the whole
cohort rather than a sample, a fixed content-addressed document so two people
arguing about a number are arguing about the same document, and an append-only
chain so a proof cannot be quietly removed once its result is inconvenient.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

from . import engine  # noqa: F401

__all__ = ["engine"]
