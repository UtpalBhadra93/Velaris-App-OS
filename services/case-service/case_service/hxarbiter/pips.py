"""HxArbiter ARB-A/C — the attribute resolvers.

Each one answers for a single prefix, and each is allowed to answer *nothing*.
That is the important property: an omitted path makes every comparison on it
false, so a resolver that cannot reach its source can never accidentally satisfy
an allow rule. It fails towards the namespace default, which for every
sovereignty-bearing namespace is deny.

None of them guesses. `tenant.region` in particular resolves through HxGlobal
(§6.1) because **there is no `region` column on `tenants`** — a resolver that
invented one would be confidently wrong on the single attribute the product's
main claim rests on.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from hxarbiter_core import DecisionRequest

log = logging.getLogger(__name__)


class TenantResolver:
    """`tenant.region`, `tenant.plan`, `tenant.flags.local_only`."""

    prefix = "tenant"

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def resolve(self, req: DecisionRequest, paths: set[str]) -> dict[str, Any]:
        out: dict[str, Any] = {}

        if "tenant.region" in paths:
            region = await self._region(req.tenant_id)
            if region is not None:
                out["tenant.region"] = region

        if {"tenant.plan", "tenant.flags.local_only"} & paths:
            settings = await self._tenant_settings(req.tenant_id)
            if settings is not None:
                if "tenant.plan" in paths and settings.get("plan"):
                    out["tenant.plan"] = str(settings["plan"])
                if "tenant.flags.local_only" in paths:
                    ai = settings.get("ai") or {}
                    egress = str(ai.get("egress") or "local_only").lower()
                    out["tenant.flags.local_only"] = egress != "external_allowed"
        return out

    async def _region(self, tenant_token: str) -> str | None:
        """Through HxGlobal, never from a column that does not exist (§6.1)."""
        try:
            from case_service.hxglobal.sovereignty import resolve_region
            region = await resolve_region(self._session, tenant_token)
            return str(region) if region else None
        except Exception as exc:                               # noqa: BLE001
            log.debug("hxarbiter: region unresolved for %r: %s", tenant_token, exc)
            return None

    async def _tenant_settings(self, tenant_token: str) -> dict | None:
        try:
            from velaris_shared.db.models import TenantModel
            from case_service.tenancy.identity import resolve_tenant

            resolved = await resolve_tenant(self._session, tenant_token)
            if resolved is None:
                return None
            row = (await self._session.execute(
                select(TenantModel).where(TenantModel.id == resolved[0])
            )).scalars().first()
            return dict(row.settings or {}) if row else None
        except Exception as exc:                               # noqa: BLE001
            log.debug("hxarbiter: tenant settings unresolved: %s", exc)
            return None


class ConsentResolver:
    """`consent.basis` — the lawful basis for the request's declared purpose.

    Two values matter more than the rest and are easy to conflate:

    * **`none`** is a resolved fact: there is no lawful basis. A policy can and
      should be able to deny on it.
    * **an omitted path** means HxConsent could not be asked at all, which is not
      the same claim and must not be able to satisfy a rule.

    `anonymized` is preserved rather than mapped to a basis, because the
    retention module already treats it as a distinct refusal — a NULL data
    subject reads as "no data subject", and flattening it here would re-open the
    hole that fix closed.
    """

    prefix = "consent"

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def resolve(self, req: DecisionRequest, paths: set[str]) -> dict[str, Any]:
        if "consent.basis" not in paths:
            return {}
        purpose = req.context.get("purpose")
        subject_ref = req.resource.attrs.get("data_subject_id") or req.resource.id
        if not purpose or not subject_ref:
            # Not enough to ask the question. Omit rather than answer "none",
            # which would be a claim we have not established.
            return {}
        try:
            from case_service.compliance import consent as _consent

            state = await _consent.current_state(
                self._session, str(subject_ref), tenant_id=req.tenant_id)
            entry = state.get(str(purpose))
            if entry is None:
                # The subject exists and nothing was recorded for this purpose.
                # That IS a fact — there is no lawful basis — and distinct from
                # not being able to ask, which returns {} above.
                return {"consent.basis": "none"}
            if not entry.get("granted"):
                return {"consent.basis": "none"}
            basis = entry.get("basis")
            return {"consent.basis": str(basis) if basis else "consent"}
        except Exception as exc:                               # noqa: BLE001
            log.debug("hxarbiter: consent basis unresolved: %s", exc)
            return {}


class DataClassResolver:
    """`resource.data_classes` — data classifications for the resource.

    Supplements, never replaces, what the PEP supplied: a call site that already
    knows the classes it is handling is the better source, and this only fills
    the gap when it does not.

    Backed by HxPrivacy's confirmed PII findings, and **confirmed only**. A
    finding still awaiting review is a suspicion; treating it as a class would
    let an unreviewed scan silently start denying real work.
    """

    prefix = "resource"

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def resolve(self, req: DecisionRequest, paths: set[str]) -> dict[str, Any]:
        if "resource.data_classes" not in paths:
            return {}
        if "data_classes" in req.resource.attrs:
            return {}                       # the PEP already answered
        if req.resource.kind != "case" or not req.resource.id:
            return {}
        try:
            from velaris_shared.db.models import PIIFindingModel
            from case_service.compliance.pii_scan import CONFIRMED

            # Scoped to THIS case and THIS tenant. An unscoped query would hand
            # every case the union of every finding on the box, which is both a
            # cross-tenant leak and a policy that denies everything.
            rows = (await self._session.execute(
                select(PIIFindingModel.category, PIIFindingModel.special_category)
                .where(
                    PIIFindingModel.status == CONFIRMED,
                    PIIFindingModel.resource_id == str(req.resource.id),
                    PIIFindingModel.tenant_id == req.tenant_id,
                )
            )).all()
            classes = {str(cat) for cat, _ in rows if cat}
            if any(special for _, special in rows):
                # GDPR Article 9 data is the class most policies are written
                # about, and it is a flag on the finding rather than a category
                # of its own — so it would otherwise be invisible to a rule.
                classes.add("special_category")
            return {"resource.data_classes": sorted(classes)} if classes else {}
        except Exception as exc:                               # noqa: BLE001
            log.debug("hxarbiter: data classes unresolved: %s", exc)
            return {}


def default_resolvers(session: AsyncSession) -> list[Any]:
    """The set every in-process PEP gets unless it says otherwise.

    `RequestResolver` is first and owns `*`, so PEP-supplied facts win over
    anything a PIP would look up — the call site knows what it is actually
    doing, and a lookup that disagreed with it would be answering a different
    question.
    """
    from hxarbiter_core import RequestResolver

    return [
        RequestResolver(),
        TenantResolver(session),
        ConsentResolver(session),
        DataClassResolver(session),
    ]
