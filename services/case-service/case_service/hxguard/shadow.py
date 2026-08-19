"""Privilege authorization Phase 2 — shadow comparison.

`require_role` stays the decider. Alongside it, the privilege backend answers
the same question, and any disagreement is recorded. Nothing about the response
changes: this phase exists to find the cases the Phase-1 parity table cannot,
because that table runs against *constructed* subjects while this runs against
real ones — real access groups, real role combinations, real routes.

**Why this lives inside `require_role` rather than at 109 call sites.** Editing
every gate to pass an action name would be Phase-3-sized churn during a phase
whose entire promise is that nothing changes. The route already knows which
handler it is about to call, and Phase 1 recorded which action each handler's
gate became, so the mapping is a lookup rather than an edit.

**It must never affect a request.** Every path here is wrapped: a failure to
resolve, evaluate or record is counted and swallowed. A shadow that can break a
request is worse than no shadow, because it converts an observability feature
into an outage.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import logging
import time
from typing import Any, Iterable

from case_service.hxguard.actions import (
    ACTION_BY_GATE,
    ACTIONS,
    MODULE_ACTION_BY_FILE,
)
from case_service.hxguard.privileges import PrivilegeBackend, legacy_role_gate_allows

log = logging.getLogger(__name__)

#: Same shape as `hxguard.service.counters` — cheap, in-process, scraped by tests
#: and by whoever is watching the rollout.
counters: dict[str, int] = {
    "compared": 0,
    "agreed": 0,
    "mismatch_would_allow": 0,   # privilege says yes, require_role says no
    "mismatch_would_deny": 0,    # privilege says no, require_role says yes
    "unresolved_route": 0,       # no action for this gate — a mapping gap
    "error": 0,
}

#: One mismatch per (action, roles) per window; repeats are counted only. A
#: mismatch on a hot route would otherwise write a SecurityEvent per request.
_LOG_WINDOW_SECONDS = 300.0
_logged: dict[tuple, float] = {}

_backend = PrivilegeBackend()


class _ShadowSubject:
    """The subject as the privilege backend sees it.

    Deliberately built from the same `AuthenticatedUser` the gate just decided
    on, so a mismatch can only come from the decision model — never from the two
    sides looking at different users.
    """

    kind = "user"

    def __init__(self, user: Any):
        self.roles = list(getattr(user, "roles", None) or [])
        self.is_admin = bool(getattr(user, "is_admin", False))
        ag = getattr(user, "active_access_group", None)
        self.privileges = list(getattr(ag, "privileges", None) or []) if ag else None
        self.id = str(getattr(user, "user_id", "") or "")


def resolve_action(request: Any, required_roles: Iterable[str]) -> str | None:
    """Which action this gate became, from the route about to be called.

    A route can carry *two* gates — a router-level one and an endpoint-level one
    (`admin.py` and `enterprise.py` both do). They are distinguished by their
    role tuple, because that is the only thing the dependency knows about itself.
    """
    route = (getattr(request, "scope", None) or {}).get("route")
    endpoint = getattr(route, "endpoint", None)
    if endpoint is None:
        return None

    module = getattr(endpoint, "__module__", "").rsplit(".", 1)[-1]
    file_key = f"{module}.py"
    wanted = tuple(required_roles)

    endpoint_action = ACTION_BY_GATE.get(f"{file_key}::{endpoint.__name__}")
    module_action = MODULE_ACTION_BY_FILE.get(file_key)

    for candidate in (endpoint_action, module_action):
        if candidate and ACTIONS[candidate].legacy_roles == wanted:
            return candidate
    # Roles did not match either record — the gate changed since Phase 0. Prefer
    # the endpoint action so the comparison still runs, and let the mismatch
    # counter surface the drift rather than silently skipping.
    return endpoint_action or module_action


async def compare(request: Any, user: Any, required_roles: Iterable[str], allowed: bool) -> None:
    """Record whether the privilege model agrees with the gate that just ran.

    `allowed` is what `require_role` decided. This never raises and never
    returns a decision — the caller has already decided.
    """
    try:
        action = resolve_action(request, required_roles)
        if action is None:
            counters["unresolved_route"] += 1
            # Name it: a bare counter says a gate is unmapped but not which, and
            # "some route somewhere" is a debugging session per occurrence.
            log.warning(
                "authz shadow: no action for gate roles=%s path=%s — the Phase-0 "
                "inventory is missing this route",
                list(required_roles),
                getattr(getattr(request, "url", None), "path", "?"),
            )
            return

        subject = _ShadowSubject(user)
        decision = _backend.evaluate(subject, action)
        counters["compared"] += 1

        if decision.allow == allowed:
            counters["agreed"] += 1
            return

        key = "mismatch_would_allow" if decision.allow else "mismatch_would_deny"
        counters[key] += 1
        await _record(request, subject, action, required_roles, allowed, decision, key)
    except Exception:                       # never break a request
        counters["error"] += 1
        log.debug("authz shadow comparison failed", exc_info=True)


async def _record(request, subject, action, required_roles, allowed, decision, kind) -> None:
    """Throttled SecurityEvent, in its own session.

    Its own session because the request's transaction may still roll back, and a
    parity mismatch that vanishes with the request it was observed on is the one
    piece of evidence this phase exists to collect.
    """
    now = time.monotonic()
    tkey = (action, tuple(sorted(subject.roles)), kind)
    if now - _logged.get(tkey, 0.0) < _LOG_WINDOW_SECONDS:
        counters["log_suppressed"] = counters.get("log_suppressed", 0) + 1
        return
    if len(_logged) > 10000:
        _logged.clear()
    _logged[tkey] = now

    log.warning(
        "authz parity mismatch: action=%s roles=%s require_role=%s privilege=%s (%s)",
        action, subject.roles, allowed, decision.allow, decision.reason,
    )

    from case_service.db.session import get_session_factory
    from case_service.enterprise.security_events import log_security_event

    factory = get_session_factory()
    async with factory() as session:
        await log_security_event(
            session,
            event_type="authz_parity_mismatch",
            severity="medium",
            user_id=subject.id or None,
            action=action,
            outcome="denied" if not allowed else "success",
            details={
                "action": action,
                "roles": subject.roles,
                "required_roles": list(required_roles),
                "require_role_allowed": allowed,
                "privilege_allowed": decision.allow,
                "privilege_reason": decision.reason,
                "kind": kind,
                "path": str(getattr(getattr(request, "url", None), "path", "")),
            },
        )
        await session.commit()


def snapshot() -> dict[str, int]:
    """Counters for the rollout dashboard and for tests."""
    return dict(counters)


def reset() -> None:
    """Test hook — the counters are process-global by design."""
    for k in counters:
        counters[k] = 0
    _logged.clear()


__all__ = ["compare", "resolve_action", "counters", "snapshot", "reset",
           "legacy_role_gate_allows"]
