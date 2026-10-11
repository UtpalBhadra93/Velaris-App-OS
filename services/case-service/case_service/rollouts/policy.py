"""A rollout's optional policy — shadow, targeting, ramp, guard (Oct 18a, phases 2–3).

No policy row is phase 1 exactly. Every field is closed and bounded, because a
policy is data an operator types and the sweep then acts on unattended.

* **shadow** — replay each baseline case through the candidate's rules, with no
  effect (see `shadow.py`).
* **targeting** — `include` (a pilot: always the new version once approved),
  `exclude` (never), and `field`/`values` (only cases whose `data[field]` is one
  of `values` are eligible for the percentage). Subjects are compared trimmed
  and case-folded.
* **ramp** — `steps` (ascending percentages) and `every_hours`: the sweep raises
  the percentage one step at a time. It never promotes; that stays a person's.
* **guard** — `metric` (`sla_breach_rate`, `reopen_rate`, `avg_resolution_hours`),
  `max_delta` (how much worse the candidate may be), `min_cases` (per arm, before
  anything is judged), `action` (`hold` the ramp, or `rollback`), and
  `require_significance` (default on: a difference that could be noise is not
  acted on).

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

from typing import Any

GUARD_METRICS = ("sla_breach_rate", "reopen_rate", "avg_resolution_hours")
GUARD_ACTIONS = ("hold", "rollback")
MAX_SUBJECTS = 500
MAX_VALUES = 50


class PolicyError(ValueError):
    pass


def norm(subject: Any) -> str:
    return str(subject or "").strip().casefold()


def _subjects(raw: Any, what: str) -> list[str]:
    if raw in (None, []):
        return []
    if not isinstance(raw, list) or not all(isinstance(s, (str, int)) for s in raw):
        raise PolicyError(f"targeting.{what} is a list of customer ids or emails")
    if len(raw) > MAX_SUBJECTS:
        raise PolicyError(f"targeting.{what} holds at most {MAX_SUBJECTS} entries")
    return sorted({norm(s) for s in raw if norm(s)})


def validate_targeting(raw: Any) -> dict[str, Any] | None:
    if raw in (None, {}):
        return None
    if not isinstance(raw, dict) or set(raw) - {"include", "exclude", "field", "values"}:
        raise PolicyError("targeting takes include, exclude, field and values only")
    out: dict[str, Any] = {"include": _subjects(raw.get("include"), "include"),
                           "exclude": _subjects(raw.get("exclude"), "exclude")}
    if set(out["include"]) & set(out["exclude"]):
        raise PolicyError("a subject cannot be both included and excluded")
    field, values = raw.get("field"), raw.get("values")
    if field or values:
        if not isinstance(field, str) or not field.strip() or len(field) > 128:
            raise PolicyError("targeting.field is a case data field name")
        if not isinstance(values, list) or not values or len(values) > MAX_VALUES:
            raise PolicyError(f"targeting.values is a list of 1 to {MAX_VALUES} values")
        out["field"] = field.strip()
        out["values"] = sorted({str(v) for v in values})
    return out


def validate_ramp(raw: Any, strategy: str) -> dict[str, Any] | None:
    if raw in (None, {}):
        return None
    if strategy != "percentage":
        raise PolicyError("a ramp only applies to a percentage rollout")
    if not isinstance(raw, dict) or set(raw) - {"steps", "every_hours"}:
        raise PolicyError("ramp takes steps and every_hours only")
    steps = raw.get("steps")
    if not isinstance(steps, list) or not 1 <= len(steps) <= 20 or \
            not all(isinstance(s, int) and 0 < s <= 100 for s in steps) or \
            steps != sorted(set(steps)):
        raise PolicyError("ramp.steps is 1 to 20 ascending percentages between 1 and 100")
    hours = raw.get("every_hours")
    if not isinstance(hours, (int, float)) or not 0.25 <= float(hours) <= 24 * 30:
        raise PolicyError("ramp.every_hours is between 0.25 and 720")
    return {"steps": steps, "every_hours": float(hours)}


def validate_guard(raw: Any) -> dict[str, Any] | None:
    if raw in (None, {}):
        return None
    allowed = {"metric", "max_delta", "min_cases", "action", "require_significance"}
    if not isinstance(raw, dict) or set(raw) - allowed:
        raise PolicyError(f"guard takes {', '.join(sorted(allowed))} only")
    metric = raw.get("metric")
    if metric not in GUARD_METRICS:
        raise PolicyError(f"guard.metric is one of {', '.join(GUARD_METRICS)}")
    delta = raw.get("max_delta")
    if not isinstance(delta, (int, float)) or delta < 0:
        raise PolicyError("guard.max_delta is a non-negative number")
    if metric != "avg_resolution_hours" and delta > 1:
        raise PolicyError("for a rate, guard.max_delta is a fraction (0.05 = five points)")
    min_cases = raw.get("min_cases", 30)
    if not isinstance(min_cases, int) or not 5 <= min_cases <= 100_000:
        raise PolicyError("guard.min_cases is between 5 and 100000")
    action = raw.get("action", "hold")
    if action not in GUARD_ACTIONS:
        raise PolicyError(f"guard.action is one of {', '.join(GUARD_ACTIONS)}")
    sig = raw.get("require_significance", True)
    if not isinstance(sig, bool):
        raise PolicyError("guard.require_significance is true or false")
    return {"metric": metric, "max_delta": float(delta), "min_cases": min_cases,
            "action": action, "require_significance": sig}


def target(targeting: dict[str, Any] | None, *, subject: str | None,
           data: dict[str, Any] | None) -> str | None:
    """`"candidate"`, `"baseline"`, or None (the percentage decides)."""
    if not targeting:
        return None
    key = norm(subject)
    if key and key in set(targeting.get("exclude") or []):
        return "baseline"
    if key and key in set(targeting.get("include") or []):
        return "candidate"
    field = targeting.get("field")
    if field:
        value = (data or {}).get(field) if isinstance(data, dict) else None
        if value is None or str(value) not in set(targeting.get("values") or []):
            return "baseline"
    return None
