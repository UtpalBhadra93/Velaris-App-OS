"""Inbound intake — payload filtering and field mapping.

Moved here from `api/routers/intake.py` alongside the ingest service. These are
pure functions over a payload, with no HTTP in them; leaving them in a router
meant a second transport could not reuse them without importing a router.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import operator
import re
from typing import Any


_OPS: dict[str, Any] = {
    "eq":       operator.eq,
    "neq":      operator.ne,
    "gt":       operator.gt,
    "gte":      operator.ge,
    "lt":       operator.lt,
    "lte":      operator.le,
    "contains": lambda a, b: b in str(a),
    "regex":    lambda a, b: bool(re.search(str(b), str(a))),
    "exists":   lambda a, _: a is not None,
}


def _get_path(data: dict, path: str) -> Any:
    """Resolve a dot-notation path from a nested dict. Returns None if missing."""
    parts = path.split(".")
    cur: Any = data
    for p in parts:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(p)
    return cur


def evaluate_filters(payload: dict, conditions: dict) -> tuple[bool, list[dict]]:
    """Evaluate filter_conditions against the incoming payload.

    conditions format:
      {
        "logic": "and" | "or",
        "rules": [
          {"field": "event.type", "operator": "eq",  "value": "claim_submitted"},
          {"field": "data.amount", "operator": "gte", "value": 0}
        ]
      }

    Returns (passed: bool, results: list of per-rule outcomes).
    """
    if not conditions or not conditions.get("rules"):
        return True, []   # no filter = accept everything

    logic = conditions.get("logic", "and").lower()
    rules = conditions.get("rules", [])
    results = []

    for rule in rules:
        field   = rule.get("field", "")
        op_name = rule.get("operator", "eq")
        value   = rule.get("value")
        actual  = _get_path(payload, field)
        op_fn   = _OPS.get(op_name)
        try:
            passed = op_fn(actual, value) if op_fn else False
        except Exception:
            passed = False
        results.append({"field": field, "operator": op_name, "expected": value, "actual": actual, "passed": passed})

    passed_list = [r["passed"] for r in results]
    overall = all(passed_list) if logic == "and" else any(passed_list)
    return overall, results


def apply_field_mapping(payload: dict, mapping: dict) -> dict:
    """Map payload dot-paths to case field keys.

    mapping: {"payload.field.path": "case_field_key"}
    Returns a flat dict of case field values.
    """
    result: dict = {}
    for src_path, dest_key in mapping.items():
        val = _get_path(payload, src_path)
        if val is not None:
            result[dest_key] = val
    return result
