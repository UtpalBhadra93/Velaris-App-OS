"""HxShield — the wire between recorded security events and the detector.

**The gap this closes.** `engine.evaluate()` had one caller: the explicit
`POST /hxshield/score`. Nothing on a request path called it, so on 2026-08-24
the live platform held **3,905 security events, 2 enabled rules and 0 incidents
ever raised**. Eight detection patterns that had never fired — not because they
were wrong, but because nobody asked them anything.

`log_security_event` is the chokepoint 51 call sites already funnel through, so
this registers there rather than editing 51 sites that would drift apart.

Three properties, each load-bearing:

* **Its own session.** The caller is mid-request, often mid-transaction —
  frequently a failing one, since half these events are denials. Writing an
  incident into that transaction would tie the record of an attack to the
  rollback of the request that triggered it.
* **Never raises.** Enforced here as well as at the hook, because "we noticed
  something" must never become "your login failed".
* **Off by default.** `VELARIS_CASE_HXSHIELD_DETECT_ENABLED`. Eight rules that
  have never seen production traffic do not get switched on across a fleet by an
  upgrade; an operator turns them on and watches.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

#: Platform event type → the name the engine's patterns match on.
#:
#: **This table is the second half of the defect.** `_score_event` matches
#: *exact* event names — `case_created`, `portal_submit`, `login`, `case_read`,
#: `export` — and the platform records none of those. Its real vocabulary,
#: read from the live database on 2026-08-24, is `auth.login`,
#: `auth.login_failed`, `authz_denied`, `api.post`, `gdpr.data_exported` and a
#: dozen admin events. So wiring the hook alone would have produced exactly
#: what existed before: zero incidents, now with more machinery behind them.
#:
#: Translation lives here rather than in the engine so the rules keep their
#: published contract (a rule says `pattern_type: account_takeover`, and that
#: still means what the docs say) and so this mapping is one readable list
#: rather than eight edited conditionals.
EVENT_TRANSLATION = {
    "auth.login":         "login",           # → account_takeover
    "gdpr.data_exported": "export",          # → off_hours_bulk_access
}

#: Event types worth scoring at all. Everything else is recorded and not scored.
#:
#: **`api.*` is deliberately excluded**, and the live distribution is why: of
#: 3,905 recorded events, **2,414 were `api.post`** — the generic per-request
#: audit, actor `__system__`. Scoring those would put a `shield_events` write on
#: every API request and drown every rule's baseline in system traffic.
SCORED_PREFIXES = (
    "auth", "login", "logout", "mfa", "password", "token", "session",
    "authz", "permission", "access", "privilege", "role",
    "case", "document", "export", "portal",
)

#: Patterns that cannot fire because **nothing writes the event they match on**.
#: Fixing these is a change to the *call sites*, not to HxShield.
#:
#: | Pattern | Needs |
#: |---|---|
#: | `velocity_anomaly` | `case_closed` / `bulk_action` |
#: | `insider_threat` | `case_read` |
#:
#: `case_read` is deliberately still absent: a security event per case read
#: repeats the `api.post` problem (2,414 of 3,905 rows), so `insider_threat`
#: wants a narrower trigger — unassigned access only — rather than a blanket
#: write.
PATTERNS_WITHOUT_A_PRODUCER = (
    "velocity_anomaly", "insider_threat",
)

#: Patterns whose event DOES arrive, but whose firing condition reads context
#: fields no call site populates. The event flows, the rule is evaluated, and
#: the condition is simply never true — a subtler dead end than a missing
#: producer, and worth naming separately because the fix is different.
#:
#: * `account_takeover` needs `new_country` AND `sensitive_case_access`.
#:   Deriving the first means IP geolocation, which is personal-data processing
#:   and therefore a decision, not a wiring fix.
#: * `field_value_anomaly` needs `field_anomaly_score`. Nothing scores field
#:   values; that is a feature, not a producer.
PATTERNS_WITHOUT_CONTEXT = (
    "account_takeover", "field_value_anomaly",
)

#: `replay_attack` is a third case: it cannot work **as written**.
#:
#: The rule computes `_payload_hash(context)` and compares it against
#: `context["recent_hashes"]` — a list the caller is expected to supply. But the
#: hash covers the *whole* context, so injecting the list changes the very hash
#: being compared, and it changes on every call. The comparison input lives
#: inside the dict it is compared against.
#:
#: The data it needs already exists: `_persist_shield_event` stores a
#: `payload_hash` per event. Making this live means the engine looking up recent
#: hashes itself and hashing a stable subset of the context — a change to the
#: rule, which is why it is not done here.
PATTERNS_BROKEN_AS_WRITTEN = (
    "replay_attack",
)

#: Live after the 2026-08-24 producers: `off_hours_bulk_access` (via
#: `gdpr.data_exported → export`), plus `duplicate_case_flood` and
#: `dos_submission` (via `case_created` and `portal_submit`).


def _is_scored(event_type: str) -> bool:
    et = (event_type or "").lower()
    return any(et.startswith(p) for p in SCORED_PREFIXES)


async def detect(
    *,
    event_type: str,
    actor_id: str | None,
    resource_type: str | None = None,
    resource_id: str | None = None,
    action: str | None = None,
    outcome: str = "success",
    severity: str = "info",
    ip_address: str | None = None,
    details: dict[str, Any] | None = None,
) -> None:
    """Score one recorded security event. Never raises, never blocks the caller."""
    if not _is_scored(event_type):
        return

    # Translate into the engine's vocabulary. An event with no translation is
    # still passed through unchanged: a rule may match it directly, and dropping
    # it here would be a silent second filter.
    engine_event = EVENT_TRANSLATION.get(event_type, event_type)

    details = details or {}
    try:
        from velaris_shared.config import get_settings
        from velaris_shared.db.session import get_session_factory

        from case_service.hxshield.engine import evaluate

        if not get_settings().hxshield_detect_enabled:
            return

        # `tenant_id` and `case_type_id` are what the engine groups by. They are
        # not columns on a security event, so they come from the details the
        # call site already passes — absent is fine, and means "platform scope".
        tenant_id = details.get("tenant_id") or details.get("tenant")
        case_type_id = (
            resource_id if (resource_type or "").lower() == "case_type"
            else details.get("case_type_id")
        )

        context = {
            **details,
            "resource_type": resource_type,
            "resource_id": resource_id,
            "action": action,
            "outcome": outcome,
            "severity": severity,
            "ip_address": ip_address,
        }

        async with get_session_factory()() as session:
            verdict = await evaluate(
                event_type=engine_event,
                actor_id=actor_id,
                tenant_id=tenant_id,
                case_type_id=case_type_id,
                context=context,
                session=session,
            )

            # The response layer (roadmap #25). Until 2026-09-09 `evaluate`
            # computed an `action` — flag, alert, suspend, block — and NOTHING
            # performed it; the string was returned to a caller that wrote it
            # down. This is the call that acts on it, and almost all of what it
            # does is decline to: with responses switched off (the default) it
            # records what would have happened and performs nothing, and a
            # destructive response waits for a person whatever the switch says.
            if verdict.get("incident_id"):
                from case_service.hxshield.response import propose

                await propose(
                    session,
                    action=verdict.get("action") or "",
                    incident_id=verdict["incident_id"],
                    actor_id=actor_id,
                    tenant_id=tenant_id,
                    case_id=(resource_id if (resource_type or "").lower() == "case" else None),
                    reason=verdict.get("explanation"),
                    patterns=verdict.get("patterns_matched"),
                    score=verdict.get("score"),
                )
            await session.commit()
    except Exception:  # noqa: BLE001
        logger.exception("HxShield detection failed for %s — event still recorded",
                         event_type)


def install() -> bool:
    """Register the detector. Returns whether it was installed.

    Called from the service lifespan. Registering unconditionally and checking
    the flag inside `detect` is deliberate: the flag can then be flipped without
    a restart, and an install that turns detection on does not have to remember
    that the hook is bound at boot.
    """
    from velaris_shared.db.security_events import register_detector

    register_detector(detect)
    return True
