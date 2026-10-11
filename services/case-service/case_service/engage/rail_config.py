"""Industry action rails — configuration of the rep desk's rail. Oct item 5.

A banking desk handling a card dispute, a telco desk porting a number out, a
clinic booking an appointment: all use the same seven rail actions (item 2),
worded and ordered for their trade, with guidance for the rep and sensible
defaults in the forms. A **rail config** says exactly that, and a marketplace
pack can ship one (`engage_rails.json`) so a tenant installs its industry's
desk instead of commissioning it.

What a config may do — and, deliberately, what it may not:

* It may **order, hide and relabel** the existing actions, add **guidance**
  text, and preset **defaults** from a closed per-action list.
* It may **raise** an action's verification tier — a clinic may want Full
  before any reply — and it may **never lower** one. A pack that tried to let a
  payment link be sent to an unverified caller is refused, with that reason.
* It may **not** add an action, name a privilege, touch consent, or carry code:
  the vocabulary is closed and every field is length-bounded plain text.
  Default messages may use exactly one placeholder, `{portal_link}` — never
  anything about the customer, because a template filled from a profile is a
  way to put personal data into a message nobody reviewed.

Validated twice: by `scripts/build-pack.py` for the author, and at install /
edit here, which is the enforcement point.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import re
import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

ACTION_KEYS = ("reply", "document_request", "payment", "callback", "live_session",
               "consent", "follow_up")
TIERS = ("none", "partial", "full")


def _base_tiers() -> dict[str, str]:
    """The tier item 2's rail requires — read from its own SPECS, so the floor a
    config may not go below can never drift from the rule it protects."""
    from case_service.engage.action_rail import SPECS

    return {s.key: s.min_tier for s in SPECS}


#: A config may only go UP from these.
BASE_TIER = _base_tiers()
CURRENCIES = ("gbp", "eur", "usd", "chf", "sek", "nok", "dkk", "pln", "cad", "aud",
              "nzd", "jpy", "inr", "sgd", "hkd", "zar", "aed")
CAPTURABLE = ("marketing", "recording", "ai_processing", "data_sharing")
CALLBACK_MINUTES = (15, 30, 45, 60)
MAX_RAILS_PER_PACK = 20
_KEY = re.compile(r"^[a-z0-9][a-z0-9-]{1,62}$")
_CTRL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_PLACEHOLDER = re.compile(r"\{([^}]*)\}")

#: Per action, the defaults a config may preset, and each one's bound.
DEFAULTS: dict[str, dict[str, Any]] = {
    "reply": {"message": 1000},
    "document_request": {"message": 1000},
    "payment": {"description": 200, "currency": CURRENCIES},
    "callback": {"minutes": CALLBACK_MINUTES},
    "live_session": {"title": 200},
    "consent": {"purpose": CAPTURABLE},
    "follow_up": {"note": 1000},
}


class RailError(ValueError):
    """The config would do something a rail config may not. Message for a human."""


def _text(value: Any, where: str, limit: int, *, placeholders: bool = False) -> str:
    if not isinstance(value, str):
        raise RailError(f"{where} must be text")
    value = value.strip()
    if len(value) > limit:
        raise RailError(f"{where} is longer than {limit} characters")
    if _CTRL.search(value):
        raise RailError(f"{where} contains control characters")
    for name in _PLACEHOLDER.findall(value):
        if not placeholders or name != "portal_link":
            raise RailError(
                f"{where} uses {{{name}}} — the only placeholder a rail may use is "
                f"{{portal_link}}; nothing about the customer is ever filled in")
    return value


def _only(obj: dict, allowed: set[str], where: str) -> None:
    extra = sorted(set(obj) - allowed)
    if extra:
        raise RailError(f"{where} has unknown field(s): {', '.join(extra)}")


def validate(raw: Any) -> dict[str, Any]:
    """Return the NORMALISED config, or raise RailError saying what is wrong."""
    if not isinstance(raw, dict):
        raise RailError("a rail config is an object")
    _only(raw, {"key", "name", "industry", "summary", "guidance",
                "applies_to_case_types", "actions"}, "the rail")
    key = str(raw.get("key") or "")
    if not _KEY.match(key):
        raise RailError("key is 2–63 lowercase letters, digits and hyphens")
    out: dict[str, Any] = {
        "key": key,
        "name": _text(raw.get("name") or "", "name", 120),
        "industry": _text(raw.get("industry") or "general", "industry", 60).lower(),
        "summary": _text(raw.get("summary") or "", "summary", 500),
        "guidance": _text(raw.get("guidance") or "", "guidance", 1000),
    }
    if not out["name"]:
        raise RailError("a rail needs a name")
    applies = raw.get("applies_to_case_types") or []
    if not isinstance(applies, list) or len(applies) > 50:
        raise RailError("applies_to_case_types is a list of at most 50 case type names")
    out["applies_to_case_types"] = [_text(a, "a case type name", 255) for a in applies]

    actions = raw.get("actions") or []
    if not isinstance(actions, list) or not actions:
        raise RailError("a rail lists at least one action")
    seen: set[str] = set()
    norm_actions = []
    for i, a in enumerate(actions):
        where = f"actions[{i}]"
        if not isinstance(a, dict):
            raise RailError(f"{where} must be an object")
        _only(a, {"key", "label", "guidance", "hidden", "min_tier", "defaults"}, where)
        k = a.get("key")
        if k not in ACTION_KEYS:
            raise RailError(f"{where}.key {k!r} is not a rail action — a config orders and "
                            f"words the existing ones ({', '.join(ACTION_KEYS)}); it cannot add one")
        if k in seen:
            raise RailError(f"{where}: {k} is listed twice")
        seen.add(k)
        item: dict[str, Any] = {"key": k, "hidden": bool(a.get("hidden", False))}
        if a.get("label"):
            item["label"] = _text(a["label"], f"{where}.label", 80)
        if a.get("guidance"):
            item["guidance"] = _text(a["guidance"], f"{where}.guidance", 500)
        if a.get("min_tier") is not None:
            tier = a["min_tier"]
            if tier not in TIERS:
                raise RailError(f"{where}.min_tier is one of {', '.join(TIERS)}")
            if TIERS.index(tier) < TIERS.index(BASE_TIER[k]):
                raise RailError(
                    f"{where}: {k} needs at least '{BASE_TIER[k]}' verification; a rail may "
                    f"raise a tier, never lower one")
            item["min_tier"] = tier
        defaults = a.get("defaults") or {}
        if not isinstance(defaults, dict):
            raise RailError(f"{where}.defaults must be an object")
        _only(defaults, set(DEFAULTS[k]), f"{where}.defaults")
        nd: dict[str, Any] = {}
        for field, rule in DEFAULTS[k].items():
            if field not in defaults:
                continue
            v = defaults[field]
            if isinstance(rule, int):
                nd[field] = _text(v, f"{where}.defaults.{field}", rule,
                                  placeholders=(k == "document_request"))
            elif v not in rule:
                raise RailError(f"{where}.defaults.{field} is one of "
                                f"{', '.join(str(x) for x in rule)}")
            else:
                nd[field] = v
        if nd:
            item["defaults"] = nd
        norm_actions.append(item)
    out["actions"] = norm_actions
    return out


def validate_many(raw: Any) -> list[dict[str, Any]]:
    """A pack's `engage_rails.json`: a list of configs, distinct keys."""
    if not isinstance(raw, list):
        raise RailError("engage_rails.json must be a JSON array")
    if len(raw) > MAX_RAILS_PER_PACK:
        raise RailError(f"a pack ships at most {MAX_RAILS_PER_PACK} rails")
    out, keys = [], set()
    for i, r in enumerate(raw):
        try:
            v = validate(r)
        except RailError as exc:
            raise RailError(f"rail {i}: {exc}") from exc
        if v["key"] in keys:
            raise RailError(f"rail {i}: key {v['key']!r} appears twice")
        keys.add(v["key"])
        out.append(v)
    return out


def apply(actions: list[dict[str, Any]], config: dict[str, Any] | None) -> list[dict[str, Any]]:
    """Reorder, hide, relabel, add guidance/defaults and RAISE tiers.

    `actions` is `action_rail.rail()`'s own output; a raised tier that the call
    has not reached adds a `tier` block exactly as the base rule would.
    """
    if not config:
        return actions
    by_key = {a["key"]: a for a in actions}
    order = [c["key"] for c in config["actions"]] + [a["key"] for a in actions
                                                     if a["key"] not in {c["key"] for c in config["actions"]}]
    conf = {c["key"]: c for c in config["actions"]}
    out = []
    for k in order:
        a = by_key.get(k)
        c = conf.get(k, {})
        if a is None or c.get("hidden"):
            continue
        a = dict(a)
        if c.get("label"):
            a["label"] = c["label"]
        if c.get("guidance"):
            a["guidance"] = c["guidance"]
        if c.get("defaults"):
            a["defaults"] = dict(c["defaults"])
        out.append(a)
    return out


def raised_tier(config: dict[str, Any] | None, key: str) -> str | None:
    if not config:
        return None
    for c in config["actions"]:
        if c["key"] == key and c.get("min_tier"):
            return c["min_tier"]
    return None


# ── storage ──────────────────────────────────────────────────────────────────


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


async def enabled_configs(session: AsyncSession, tenant_uuid: uuid.UUID,
                          tenant_token: str | None = None):
    """The rails that may shape this tenant's desk right now.

    A PACK rail counts only while its package is still installed for the
    tenant: a package that was uninstalled — or revoked because it was
    blacklisted — must stop shaping the rep's screen at once, not linger as
    configuration nobody chose any more. A tenant's own rails are unaffected.
    """
    from velaris_shared.db.models import EngageRailConfigModel as R, MarketplaceInstallModel as I
    from velaris_shared.tenancy.identity import resolve_tenant

    rows = list((await session.execute(select(R).where(
        R.tenant_id == tenant_uuid, R.enabled == True)  # noqa: E712
        .order_by(R.name))).scalars().all())
    pack_ids = {r.package_id for r in rows if r.source == "pack" and r.package_id}
    if not pack_ids:
        return rows
    tokens = {str(tenant_uuid)}
    if tenant_token:
        tokens.add(str(tenant_token))
    resolved = await resolve_tenant(session, str(tenant_token or tenant_uuid))
    if resolved is not None:
        tokens |= {str(resolved[0]), str(resolved[1])}
    live = set((await session.execute(select(I.package_id).where(
        I.tenant_id.in_(sorted(tokens)), I.package_id.in_(sorted(pack_ids)),
        I.revoked_at.is_(None)))).scalars().all())
    return [r for r in rows if r.source != "pack" or r.package_id in live]


def choose(configs: list, case_type_names: list[str], rail_key: str | None):
    """The rail for this call.

    Named explicitly by the rep → that one (if enabled). Otherwise the first
    config that names one of the customer's open cases' types. Otherwise, only
    if the tenant has exactly ONE general rail, that one — two general rails is
    a choice for the rep, not a guess for the server.
    """
    if rail_key:
        return next((c for c in configs if c.rail_key == rail_key), None)
    names = {n.lower() for n in case_type_names}
    for c in configs:
        applies = {n.lower() for n in (c.config.get("applies_to_case_types") or [])}
        if applies & names:
            return c
    general = [c for c in configs if not (c.config.get("applies_to_case_types") or [])]
    return general[0] if len(general) == 1 else None


async def upsert_from_pack(session: AsyncSession, *, tenant_uuid: uuid.UUID,
                           package_id: str, version: str,
                           rails: list[dict[str, Any]]) -> dict[str, Any]:
    """Write a pack's rails. A rail the TENANT has edited is kept, not overwritten —
    the same promise the pack upgrade makes for case types — and reported."""
    from velaris_shared.db.models import EngageRailConfigModel as R

    created, updated, kept = [], [], []
    now = _utcnow()
    for rail in rails:
        row = (await session.execute(select(R).where(
            R.tenant_id == tenant_uuid, R.rail_key == rail["key"]))).scalars().first()
        if row is None:
            session.add(R(id=uuid.uuid4(), tenant_id=tenant_uuid, rail_key=rail["key"],
                          name=rail["name"], industry=rail["industry"], source="pack",
                          package_id=package_id, pack_version=version, config=rail,
                          enabled=True, created_at=now, updated_at=now))
            created.append(rail["key"])
        elif row.source != "pack" or row.package_id != package_id or row.edited_at is not None:
            kept.append(rail["key"])
        else:
            row.name, row.industry, row.config = rail["name"], rail["industry"], rail
            row.pack_version, row.updated_at = version, now
            updated.append(rail["key"])
    await session.flush()
    return {"created": created, "updated": updated, "kept_tenant_edits": kept}


def row_json(r) -> dict[str, Any]:
    from case_service.core.timestamps import iso_utc

    return {"key": r.rail_key, "name": r.name, "industry": r.industry, "source": r.source,
            "package_id": r.package_id, "pack_version": r.pack_version,
            "enabled": bool(r.enabled), "edited": r.edited_at is not None,
            "config": r.config, "updated_at": iso_utc(r.updated_at)}
