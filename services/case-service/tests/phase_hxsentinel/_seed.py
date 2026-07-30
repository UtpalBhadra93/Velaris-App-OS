"""Shared FK-safe seeding for the direct-insert HxSentinel tests.

Several sentinel tests exercise ``evaluate_case`` / the model / oversight / tuning
by inserting their case rows *directly on the test session* (never via the HTTP
client) so they stay immune to SQLite's StaticPool shared-connection churn. SQLite
does not enforce foreign keys, so a fabricated ``case_type_id`` (and a case_id with
no backing row) sails through there. On a real engine (PostgreSQL / MySQL / MariaDB)
those keys ARE enforced — ``case_instances.case_type_id`` -> ``case_types``,
``case_instances.tenant_id`` -> ``tenants``, and ``case_audit_log.case_id`` ->
``case_instances`` (written by ``set_outcome(audit=True)`` / ``dispute_score``).

These helpers add the parent rows in the same in-session, client-free style. Each
parent is FLUSHED before the child that references it is inserted — the unit of
work does not reliably order a merged parent ahead of its child, and on a real
engine the child insert would hit a foreign-key violation. Explicit flush makes the
order deterministic on every engine while preserving the flake-free property.

Copyright (c) 2024-2026 HELIX Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import uuid

from case_service.db.models import CaseInstanceModel, CaseTypeModel, TenantModel


async def seed_case_type(session, case_type_id) -> None:
    """Ensure a ``case_types`` row for ``case_type_id`` exists and is flushed.

    Idempotent (``get`` first) so callers that share one case type across many
    cases — e.g. the P5 model's z-score corpus — can call it per case. The name is
    derived from the id because ``case_types`` has a UNIQUE(name, version): distinct
    ids must not collide on a shared name.
    """
    if await session.get(CaseTypeModel, case_type_id) is None:
        session.add(CaseTypeModel(
            id=case_type_id, name=f"risk-test-{case_type_id}", version="1.0.0",
            definition_json={}))
        await session.commit()


async def seed_tenant(session, tenant_id) -> None:
    """Ensure a ``tenants`` row for a UUID written to a case's tenant_id, flushed."""
    if await session.get(TenantModel, tenant_id) is None:
        session.add(TenantModel(
            id=tenant_id, slug=f"rt-{tenant_id}", name="risk-test-tenant"))
        await session.commit()


async def seed_case(session, case_id=None, *, tenant=None) -> uuid.UUID:
    """Insert ``case_types`` (fresh) + optional ``tenants`` + a ``case_instances`` row.

    Parents are flushed first, so the case insert never precedes them. Returns the
    case id. ``tenant`` is written to ``case_instances.tenant_id`` (a ``tenants`` FK)
    — pass a UUID (seeded here) or ``None`` for the common NULL case.
    """
    cid = case_id or uuid.uuid4()
    ctid = uuid.uuid4()
    await seed_case_type(session, ctid)
    if tenant is not None:
        await seed_tenant(session, tenant)
    session.add(CaseInstanceModel(
        id=cid, case_type_id=ctid, case_type_version="1.0.0", tenant_id=tenant))
    await session.commit()
    return cid
