"""What an external party is shown — computed, never filtered after the fact.

Design: docs/Future/Done/AI-ERA-ROADMAP.md §F-09: *"Scoped projection: the external
agent receives a redacted case view computed from the data-class ceiling
(HxPrivacy classes drive field stripping), never raw case JSON."*

**The distinction in that sentence is the whole module.** "Send it and redact"
and "compute what may be sent" look the same from the outside and are not the
same at all: the first has the data in a serialised payload that a logging
middleware, a retry buffer or a bug can leak, and the second never assembles it.
Nothing here ever builds the full case and then removes fields — `project()`
starts from an empty dict and adds only what the ceiling admits.

**Three ceilings, and a fourth that deliberately does not exist.**

    none      — no case data at all. Identifiers, status and stage only.
    internal  — non-personal fields.
    personal  — ordinary personal data as well.

There is no `special_category`. GDPR Article 9 data is **never** projected to an
external party by this protocol, and making that inexpressible is stronger than
making it refusable: a ceiling that cannot be named cannot be granted by a
mistaken dispatcher, a widened counterparty record, or a future caller who
thought they were being helpful.

**Two classifiers, and the stricter always wins.** A field is classified by
HxPrivacy's own detector on its name, AND by any CONFIRMED PII finding recorded
against the case type's forms. Either saying "personal" is enough. The findings
alone would be too weak (a scan may never have run); the detector alone would
miss a field whose name says nothing but which a human reviewer has flagged.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from case_service.compliance import pii_detect
from velaris_shared.db.models import CaseInstanceModel, PIIFindingModel

log = logging.getLogger(__name__)

CEILING_NONE = "none"
CEILING_INTERNAL = "internal"
CEILING_PERSONAL = "personal"

#: Ordered weakest to strongest. `special_category` is absent on purpose; see
#: the module docstring.
CEILINGS: tuple[str, ...] = (CEILING_NONE, CEILING_INTERNAL, CEILING_PERSONAL)

#: Keys that are never case CONTENT — they are the platform's own scaffolding,
#: and an external party needs them to talk about the case at all.
_STRUCTURAL = frozenset({"__meta__"})


class CeilingError(ValueError):
    """The ceiling is not one this protocol can express."""


def rank(ceiling: str) -> int:
    if ceiling not in CEILINGS:
        raise CeilingError(
            f"{ceiling!r} is not a ceiling this protocol can express; it is one "
            f"of {', '.join(CEILINGS)}. Article 9 data is never projected to an "
            f"external party, so there is no ceiling that would admit it.")
    return CEILINGS.index(ceiling)


def at_most(requested: str, allowed: str) -> str:
    """The lower of the two. A dispatcher can never exceed the enrolment."""
    return requested if rank(requested) <= rank(allowed) else allowed


async def _confirmed_classes(session: AsyncSession, tenant_token: str
                             ) -> dict[str, tuple[str, bool]]:
    """`{field_name: (category, is_special)}` from CONFIRMED HxPrivacy findings.

    Keyed on the last segment of `field_path`, because that is the name a case's
    `data` uses. A finding whose path does not end in a usable name is skipped
    rather than matched loosely — a wrong match here would strip a harmless
    field or, far worse, fail to strip a sensitive one.
    """
    try:
        from case_service.compliance.pii_scan import CONFIRMED

        rows = (await session.execute(
            select(PIIFindingModel.field_path, PIIFindingModel.category,
                   PIIFindingModel.special_category)
            .where(PIIFindingModel.status == CONFIRMED,
                   PIIFindingModel.tenant_id == tenant_token))).all()
    except Exception as exc:                                     # noqa: BLE001
        # A scan that never ran is not a clean bill of health, and it is not a
        # reason to refuse either: the name-based detector still runs, and it is
        # the stricter of the two far more often than not.
        log.info("handoff: PII findings unavailable: %s", exc)
        return {}
    out: dict[str, tuple[str, bool]] = {}
    for path, category, special in rows:
        name = str(path or "").rsplit(".", 1)[-1].strip()
        if not name:
            continue
        prior = out.get(name)
        # The stricter classification wins when a name appears twice.
        if prior is None or (special and not prior[1]):
            out[name] = (str(category), bool(special))
    return out


def classify(name: str, confirmed: dict[str, tuple[str, bool]]
             ) -> tuple[str | None, bool]:
    """`(category_or_None, is_special)` for one field name.

    Both classifiers are consulted and the STRICTER answer is returned. Either
    one saying "personal" is enough; neither one is trusted to be complete.
    """
    category, special = None, False
    found = pii_detect.classify_field(name, name, "")
    if found:
        category = str(found.get("category"))
        special = bool(found.get("special_category"))
    hit = confirmed.get(name)
    if hit is not None:
        category = category or hit[0]
        special = special or hit[1]
    return category, special


async def project(
    session: AsyncSession, *, case: CaseInstanceModel, ceiling: str,
    tenant_token: str,
) -> dict[str, Any]:
    """Build the view an external party may hold. Additive, never subtractive.

    Returns `{"case": {...}, "disclosed": [...], "withheld": [{name, why}]}`.

    `withheld` names the fields that were NOT sent and why. It is returned to
    the external party on purpose: an agent that can see a field exists but not
    its value can ask a human for it, whereas one that cannot tell the field
    exists will confidently report the case as incomplete.
    """
    limit = rank(ceiling)
    confirmed = await _confirmed_classes(session, tenant_token)

    # Never case content. An external party needs these to refer to the case at
    # all, and none of them is a value somebody typed into a form.
    view: dict[str, Any] = {
        "case_id": str(case.id),
        "case_type_id": str(case.case_type_id),
        "status": case.status,
        "priority": case.priority,
        "current_stage_id": case.current_stage_id,
        "created_at": case.created_at.isoformat() if case.created_at else None,
    }

    disclosed: list[str] = []
    withheld: list[dict[str, str]] = []
    data = case.data if isinstance(case.data, dict) else {}
    fields: dict[str, Any] = {}

    for name, value in data.items():
        if name in _STRUCTURAL:
            continue
        category, special = classify(str(name), confirmed)
        if special:
            # No ceiling admits this. Not a policy check — there is no value of
            # `ceiling` that would let this line be skipped.
            withheld.append({"field": str(name),
                             "why": f"special category ({category})"})
            continue
        needed = CEILING_PERSONAL if category else CEILING_INTERNAL
        if rank(needed) > limit:
            withheld.append({"field": str(name),
                             "why": f"{category or 'personal'} data is above the "
                                    f"{ceiling!r} ceiling"})
            continue
        if limit == 0:
            withheld.append({"field": str(name),
                             "why": "this work order carries no data ceiling"})
            continue
        fields[str(name)] = value
        disclosed.append(str(name))

    view["fields"] = fields
    return {"case": view, "ceiling": ceiling,
            "disclosed": sorted(disclosed),
            "withheld": sorted(withheld, key=lambda w: w["field"])}
