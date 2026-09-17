"""F-01 — the AI decision ledger.

Velaris already recorded that an AI call happened and, for one step type, what
a human did about it. Neither is evidence:

* `llm_calls` (mig 142) is a **metric** — model, tokens, latency. It answers
  "what is AI costing us", which is a different question from "on what basis
  was this decision made".
* `ai_action_proposals` covers the `ai_action` step type only. HxNexus
  answering a question about a case, and HxSentinel scoring one, produced
  nothing at all.

This is the third thing: a per-decision, hash-chained, human-override-inclusive
record that an EU AI Act / GDPR Article 22 dossier can be built from.

Three parts:

* `records`  — writing and reading the ledger, and the verdicts on it.
* `chain`    — sealing it, so it is evidence rather than rows.
* `evidence` — the `ai_governance` pack, on top of the existing generator.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

from . import chain, evidence, records  # noqa: F401

__all__ = ["chain", "evidence", "records"]
