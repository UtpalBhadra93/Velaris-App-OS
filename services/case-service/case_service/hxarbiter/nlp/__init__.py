"""HxArbiter ARB-F — natural-language authoring.

**The model proposes. The analyzer decides what the proposal does. A human
approves. The deterministic evaluator enforces.** No step is skippable and the
model is present in only one of them.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
# The analyser is deterministic and platform-agnostic, so it ships in
# `hxarbiter-core` with the evaluator. Re-exported for the callers here.
from hxarbiter_core import Finding, analyze, overlaps, seed_requests, subsumes

__all__ = ["Finding", "analyze", "overlaps", "seed_requests", "subsumes"]
