"""Who else is connected to this customer — an HxGraph traversal. Oct item 4.

A rep on a call with A should be able to see that B shares A's phone, that C is
on the same claim, that D works at the same small firm — household, co-party,
employer — without anybody having typed it into `customer_relations` first.

**Why this walks the live data instead of writing customers into `graph_nodes`.**
HxGraph's node table is the platform's configuration graph: it is exported
(`/graph/export`), drawn (`/graph/visualize`), embedded and read by HxNexus. A
customer's phone numbers and household there would put personal data into all
three, and every erasure would have to chase it into the graph as well. So the
graph here is BUILT per question from the rows that already hold the facts —
profiles and the evidence that joins them — and thrown away with the answer.
Nothing about a person is materialised anywhere new.

The graph is bipartite: people on one side, EVIDENCE on the other.

* **number** — a caller number (`customer_interactions.ani`) used on calls
  resolved to two different people: a shared line, usually a household.
* **case** — a case both are linked to (`customer_links`): co-parties on one
  policy or claim.
* **domain** — an email domain two people share, webmail excluded: a likely
  employer. Weak, so it is never walked through to a second hop.

A breadth-first walk from the customer, at most TWO hops (person → evidence →
person → evidence → person), with a fan-out cap per evidence node: a number that
fifty customers have called from is a switchboard, not a household, and a
domain with two hundred people is a large employer, not a relationship. Capped
nodes are skipped and COUNTED, so a thin answer says why it is thin.

Every result is a **suggestion**: labelled so, never `may_act_for`, never
written anywhere. A rep who agrees records it through the ordinary relations
route, as a person's act.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import uuid
from collections import defaultdict
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from velaris_shared.db.models import (
    CaseInstanceModel,
    CustomerIdentifierModel,
    CustomerInteractionModel,
    CustomerLinkModel,
    CustomerProfileModel,
    CustomerRelationModel,
)

MAX_HOPS = 2
MAX_RESULTS = 20
#: The walk runs on every Full-tier 360, so its cost is bounded, not just its
#: depth: at most this many people are expanded into a second hop, and at most
#: this many evidence nodes are visited in all. Hitting either is reported.
MAX_EXPANSIONS = 15
MAX_EVIDENCE = 60
#: Past this many people, an evidence node is not a relationship.
FANOUT = {"number": 8, "case": 25, "domain": 30}
#: How much each kind of evidence says, and how a second hop discounts it.
WEIGHT = {"number": 0.9, "case": 0.8, "domain": 0.4}
HOP_DISCOUNT = 0.5
#: Evidence strong enough to walk THROUGH to a second hop.
TRAVERSABLE = {"number", "case"}
SUGGESTS = {"number": "shares a line", "case": "co-party", "domain": "same organisation"}
SECOND_HOP = {"number": "shares their line", "case": "is on a case with them"}
#: Domains that say nothing about who somebody works for.
WEBMAIL = frozenset({
    "gmail.com", "googlemail.com", "outlook.com", "hotmail.com", "live.com", "msn.com",
    "yahoo.com", "yahoo.co.uk", "icloud.com", "me.com", "mac.com", "aol.com",
    "proton.me", "protonmail.com", "gmx.com", "gmx.de", "web.de", "mail.com",
    "yandex.ru", "zoho.com", "fastmail.com", "hey.com", "qq.com", "163.com",
})


def _mask_name(value: str | None) -> str:
    """The 360's masking of a name (`three_sixty.mask`), kept here because the
    shared layer must not import a service. Enough to confirm, never to use."""
    raw = str(value or "")
    return ("•" * max(0, len(raw) - 2)) + raw[-2:]


def _mask_tail(value: str, keep: int = 3) -> str:
    raw = str(value or "")
    return ("•" * max(0, len(raw) - keep)) + raw[-keep:]


async def _live(session: AsyncSession, tenant_uuid: uuid.UUID,
                ids: set[uuid.UUID]) -> dict[uuid.UUID, CustomerProfileModel]:
    """Only people a rep may be told about: this tenant's, not merged away,
    not erased."""
    if not ids:
        return {}
    out: dict[uuid.UUID, CustomerProfileModel] = {}
    idl = list(ids)
    for i in range(0, len(idl), 500):
        for p in (await session.execute(select(CustomerProfileModel).where(
                CustomerProfileModel.tenant_id == tenant_uuid,
                CustomerProfileModel.id.in_(idl[i:i + 500]),
                CustomerProfileModel.status == "active"))).scalars().all():
            out[p.id] = p
    return out


async def _evidence_of(session: AsyncSession, tenant_uuid: uuid.UUID,
                       person: uuid.UUID) -> dict[tuple[str, str], str]:
    """(kind, key) → a label safe to show a rep, for one person."""
    ev: dict[tuple[str, str], str] = {}
    for ani in (await session.execute(select(CustomerInteractionModel.ani).where(
            CustomerInteractionModel.tenant_id == tenant_uuid,
            CustomerInteractionModel.customer_id == person,
            CustomerInteractionModel.ani.isnot(None)).distinct().limit(50))).scalars().all():
        ev[("number", ani)] = f"calls from a number ending {_mask_tail(ani)[-3:]}"
    for cid in (await session.execute(
            select(CustomerLinkModel.object_id)
            .join(CaseInstanceModel, CaseInstanceModel.id == CustomerLinkModel.object_id)
            .where(CustomerLinkModel.customer_id == person,
                   CustomerLinkModel.object_kind == "case",
                   CaseInstanceModel.tenant_id == tenant_uuid).limit(100))).scalars().all():
        # No case NUMBER: the rep may have no access to that case, and a number
        # is a fact about it. "Linked to the same case" is the relationship.
        ev[("case", str(cid))] = "linked to the same case"
    for email in (await session.execute(select(CustomerIdentifierModel.value_normalised).where(
            CustomerIdentifierModel.tenant_id == tenant_uuid,
            CustomerIdentifierModel.customer_id == person,
            CustomerIdentifierModel.kind == "email"))).scalars().all():
        domain = email.rsplit("@", 1)[-1] if "@" in email else ""
        if domain and domain not in WEBMAIL:
            ev[("domain", domain)] = f"email at the same organisation ({domain})"
    return ev


async def _people_of(session: AsyncSession, tenant_uuid: uuid.UUID,
                     kind: str, key: str, cap: int) -> list[uuid.UUID] | None:
    """Everyone this evidence node joins, or None when it joins too many."""
    if kind == "number":
        q = select(CustomerInteractionModel.customer_id).where(
            CustomerInteractionModel.tenant_id == tenant_uuid,
            CustomerInteractionModel.ani == key,
            CustomerInteractionModel.customer_id.isnot(None)).distinct()
    elif kind == "case":
        q = select(CustomerLinkModel.customer_id).where(
            CustomerLinkModel.tenant_id == tenant_uuid,
            CustomerLinkModel.object_kind == "case",
            CustomerLinkModel.object_id == uuid.UUID(key)).distinct()
    else:
        # Escaped: `_` and `%` in a domain are LIKE wildcards, and an unescaped
        # `my_firm.test` would also match `myxfirm.test`.
        safe = key.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        q = select(CustomerIdentifierModel.customer_id).where(
            CustomerIdentifierModel.tenant_id == tenant_uuid,
            CustomerIdentifierModel.kind == "email",
            CustomerIdentifierModel.value_normalised.like(f"%@{safe}", escape="\\")).distinct()
    rows = (await session.execute(q.limit(cap + 1))).scalars().all()
    return None if len(rows) > cap else list(rows)


async def related(session: AsyncSession, *, tenant_uuid: uuid.UUID,
                  customer_id: uuid.UUID) -> dict[str, Any]:
    """The suggestions for one customer, strongest first, with their reasons."""
    authored = set()
    for a, b in (await session.execute(select(
            CustomerRelationModel.from_customer_id, CustomerRelationModel.to_customer_id).where(
            CustomerRelationModel.tenant_id == tenant_uuid,
            or_(CustomerRelationModel.from_customer_id == customer_id,
                CustomerRelationModel.to_customer_id == customer_id)))).all():
        authored.add(b if a == customer_id else a)

    best: dict[uuid.UUID, dict[str, Any]] = {}
    skipped: dict[str, int] = defaultdict(int)
    seen_people = {customer_id}
    frontier = [(customer_id, 1.0, 0, None)]          # (person, score, hops, via)
    visited_evidence: set[tuple[str, str]] = set()
    truncated = False
    while frontier:
        nxt = []
        # Strongest first, so a cut drops the weakest leads, not arbitrary ones.
        frontier.sort(key=lambda f: -f[1])
        if len(frontier) > MAX_EXPANSIONS and frontier[0][2] > 0:
            frontier, truncated = frontier[:MAX_EXPANSIONS], True
        for person, score, hops, via in frontier:
            if hops >= MAX_HOPS:
                continue
            for (kind, key), label in (await _evidence_of(session, tenant_uuid, person)).items():
                if (kind, key) in visited_evidence:
                    continue
                if len(visited_evidence) >= MAX_EVIDENCE:
                    truncated = True
                    break
                if hops > 0 and kind not in TRAVERSABLE:
                    continue
                visited_evidence.add((kind, key))
                people = await _people_of(session, tenant_uuid, kind, key, FANOUT[kind])
                if people is None:
                    skipped[kind] += 1
                    continue
                for other in people:
                    if other == customer_id or other is None:
                        continue
                    s = score * WEIGHT[kind] * (1.0 if hops == 0 else HOP_DISCOUNT)
                    # A second hop's evidence belongs to OTHER people — their line,
                    # their case — so it is described, never quoted: no digits, no
                    # domain, nothing about the person in between.
                    reason = label if hops == 0 else (
                        f"connected through someone who {SECOND_HOP[kind]}")
                    cur = best.setdefault(other, {"score": 0.0, "because": [],
                                                  "kinds": set(), "hops": hops + 1})
                    cur["score"] = max(cur["score"], s)
                    cur["hops"] = min(cur["hops"], hops + 1)
                    cur["kinds"].add(kind)
                    if reason not in cur["because"]:
                        cur["because"].append(reason)
                    if other not in seen_people and kind in TRAVERSABLE:
                        seen_people.add(other)
                        nxt.append((other, s, hops + 1, "a connected person"))
        frontier = nxt

    profiles = await _live(session, tenant_uuid, set(best))

    out = []
    for pid, info in sorted(best.items(), key=lambda kv: -kv[1]["score"]):
        p = profiles.get(pid)
        if p is None:
            continue                      # merged away, erased, or not this tenant's
        strongest = max(info["kinds"], key=lambda k: WEIGHT[k])
        out.append({
            "customer_id": str(pid),
            "name_initial": (p.display_name or "?")[:1].upper(),
            "display_name_masked": _mask_name(p.display_name),
            "suggested": SUGGESTS[strongest],
            "because": info["because"][:4],
            "score": round(info["score"], 2),
            "hops": info["hops"],
            "already_related": pid in authored,
            # The rules of item 4, in the payload itself.
            "suggestion": True,
            "may_act_for": False,
        })
        if len(out) >= MAX_RESULTS:
            break
    return {"suggestions": out, "skipped_as_too_common": dict(skipped),
            "max_hops": MAX_HOPS, "truncated": truncated}
