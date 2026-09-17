"""F-03 — the counterfactual compliance proof API.

Three routes, and the interesting one is `verify`: it re-derives the proof and
reports **four separate findings** rather than one boolean, because "it
verifies" and "the document is intact but the decisions it was taken over have
since been edited" are different answers and collapsing them hides the second.

Two access rules worth stating:

* **A platform-scope proof is superadmin-only and aggregate-only.** A
  cross-tenant cohort definition is itself a statement about what exists, and
  below a k-anonymity floor the arithmetic identifies a tenant.
* **The count is the finding; the list is case data.** The divergent count is
  the same for every reader; which cases are named is filtered per viewer at
  read time. Filtering at write time would make the proof's own number depend
  on who generated it.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from velaris_shared import hxguard
from velaris_shared.auth.models import AuthenticatedUser
from velaris_shared.db.models import ComplianceProofModel
from velaris_shared.db.session import get_session
from velaris_shared.middleware.endpoint_rate_limit import rate_limit

from case_service.core.timestamps import iso_utc
from case_service.hxarbiter import VELARIS_CATALOG
from case_service.proofs import engine
from case_service.tenancy.identity import actor_uuid, is_superadmin, resolve_tenant

log = logging.getLogger(__name__)

router = APIRouter(prefix="/proofs", tags=["proofs"])

#: Generating replays an entire cohort through the evaluator. It is the most
#: expensive read this platform offers, and it is not a thing anybody does
#: dozens of times a minute legitimately.
_generate_rl = rate_limit(max_calls=5, window_seconds=60, name="proofs-generate")
_verify_rl = rate_limit(max_calls=20, window_seconds=60, name="proofs-verify")


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ProofIn(_Strict):
    policy_id: uuid.UUID
    policy_version: int = Field(ge=1)
    #: The cohort window. Omitting `days` means everything ever recorded, which
    #: is the honest default for a proof — "across the entire historical
    #: caseload" is the claim it exists to support.
    days: int | None = Field(default=None, ge=1, le=3650)
    #: Cross-tenant. Superadmin only, and aggregate-only whatever else is asked.
    platform: bool = False


async def _tenant_uuid(session: AsyncSession, user: AuthenticatedUser) -> uuid.UUID:
    token = user.tenant_id or (user.attributes or {}).get("tenant_id")
    resolved = await resolve_tenant(session, token)
    if resolved is None:
        raise HTTPException(403, "VEL-PROOF-TENANT-UNRESOLVED-403")
    return resolved[0]


async def _load(
    session: AsyncSession, proof_id: uuid.UUID, user: AuthenticatedUser,
) -> ComplianceProofModel:
    row = await session.get(ComplianceProofModel, proof_id)
    if row is None:
        raise HTTPException(404, "VEL-PROOF-404: no such proof")
    if row.tenant_id is None:
        # Platform-scope: readable only by a platform operator, for the same
        # reason it was generatable only by one.
        if not is_superadmin(user):
            raise HTTPException(404, "VEL-PROOF-404: no such proof")
        return row
    if row.tenant_id != await _tenant_uuid(session, user):
        # 404, not 403 — a 403 confirms the proof exists.
        raise HTTPException(404, "VEL-PROOF-404: no such proof")
    return row


async def _visible_divergent(
    session: AsyncSession, proof: ComplianceProofModel, user: AuthenticatedUser,
) -> tuple[list[dict[str, Any]], int]:
    """The divergent entries this viewer may see, and how many were withheld.

    The proof shows "17 divergent decisions" to everybody and names only those
    the reader is allowed to know about — the design's own rule. A caller who
    may read the ledger but not a given case learns the count, not the case.
    """
    entries = list(proof.divergent or [])
    if not entries:
        return [], 0
    # A reader holding case-wide access sees everything; otherwise entries that
    # name a case are withheld. Deliberately coarse: a per-case ACL probe over
    # a thousand entries would be a thousand queries, and the safe direction
    # when in doubt is to withhold.
    broad = hxguard.user_allows(user, "casework.sensitive.read")
    if broad:
        return entries, 0
    visible = [e for e in entries if e.get("resource_kind") != "case"]
    return visible, len(entries) - len(visible)


def _proof_json(
    proof: ComplianceProofModel, divergent: list[dict[str, Any]], withheld: int,
) -> dict[str, Any]:
    return {
        "id": str(proof.id),
        "sequence": proof.sequence,
        "status": proof.status,
        "refusal_reason": proof.refusal_reason,
        "scope": "platform" if proof.tenant_id is None else "tenant",
        "namespace": proof.namespace,
        "policy_id": str(proof.policy_id) if proof.policy_id else None,
        "policy_version": proof.policy_version,
        "cohort": proof.cohort,
        "result": proof.result,
        "aggregate_only": bool(proof.aggregate_only),
        "divergent": divergent,
        # Reported rather than silently omitted: a reader shown 3 of 17 needs to
        # know the other 14 exist and are not theirs to see.
        "divergent_withheld": withheld,
        "inputs_sha256": proof.inputs_sha256,
        "rules_sha256": proof.rules_sha256,
        "content_sha256": proof.content_sha256,
        "prev_hash": proof.prev_hash,
        "created_at": iso_utc(proof.created_at),
    }


@router.post("", status_code=201, dependencies=[Depends(_generate_rl)])
async def create_proof(
    payload: ProofIn,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(hxguard.guard("compliance.proofs.generate")),
) -> dict[str, Any]:
    """Replay a policy version across the whole cohort and store the proof.

    A refusal is a 201 with `status: "refused"`, not a 4xx. The caller asked a
    well-formed question and the platform answered it — "this cannot be proven,
    and here is why" is the answer, and it is stored so it can be produced
    again rather than re-attempted until it accidentally succeeds.
    """
    if payload.platform and not is_superadmin(user):
        raise HTTPException(
            403,
            "VEL-PROOF-PLATFORM-SCOPE-403: a cross-tenant proof spans every "
            "tenant on this deployment and is reserved to a platform operator.")

    tenant = None if payload.platform else await _tenant_uuid(session, user)
    since = (datetime.now(timezone.utc) - timedelta(days=payload.days)
             if payload.days else None)
    try:
        proof = await engine.generate(
            session,
            policy_id=payload.policy_id,
            policy_version=payload.policy_version,
            catalog=VELARIS_CATALOG,
            tenant_id=tenant,
            since=since,
            generated_by=await actor_uuid(session, user),
        )
    except engine.ProofError as exc:
        raise HTTPException(422, f"VEL-PROOF-INVALID-422: {exc}") from exc
    await session.commit()
    divergent, withheld = await _visible_divergent(session, proof, user)
    log.info("proof %s generated by %s: %s", proof.id, user.user_id, proof.status)
    return _proof_json(proof, divergent, withheld)


@router.get("")
async def list_proofs(
    limit: int = Query(50, ge=1, le=200),
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(hxguard.guard("compliance.proofs.read")),
) -> dict[str, Any]:
    """This tenant's proofs, newest first. Summaries only — no divergent lists."""
    tenant = await _tenant_uuid(session, user)
    rows = (await session.execute(
        select(ComplianceProofModel)
        .where(ComplianceProofModel.tenant_id == tenant)
        .order_by(ComplianceProofModel.created_at.desc()).limit(limit)
    )).scalars().all()
    return {
        "proofs": [
            {
                "id": str(p.id),
                "sequence": p.sequence,
                "status": p.status,
                "namespace": p.namespace,
                "policy_id": str(p.policy_id) if p.policy_id else None,
                "policy_version": p.policy_version,
                "statement": (p.result or {}).get("statement"),
                "content_sha256": p.content_sha256,
                "created_at": iso_utc(p.created_at),
            }
            for p in rows
        ]
    }


@router.get("/{proof_id}")
async def get_proof(
    proof_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(hxguard.guard("compliance.proofs.read")),
) -> dict[str, Any]:
    proof = await _load(session, proof_id, user)
    divergent, withheld = await _visible_divergent(session, proof, user)
    return _proof_json(proof, divergent, withheld)


@router.post("/{proof_id}/verify", dependencies=[Depends(_verify_rl)])
async def verify_proof(
    proof_id: uuid.UUID,
    sample: int = Query(50, ge=1, le=5000),
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(hxguard.guard("compliance.proofs.read")),
) -> dict[str, Any]:
    """Re-derive the proof and report each finding separately.

    `content_address_holds` — the document is what it says it is.
    `chain_links`          — it still follows the proof before it.
    `rules_unchanged`      — the policy version has not been rewritten since.
    `inputs_unchanged`     — the decisions it was taken over have not been edited.
    `sample_agreed`        — re-running a deterministic subset reproduces it.

    All five can differ, and each names a distinct kind of problem. One boolean
    would hide four of them.
    """
    proof = await _load(session, proof_id, user)
    findings = await engine.verify(
        session, proof, catalog=VELARIS_CATALOG, sample=sample)
    findings["verified"] = all(
        v is not False for v in (
            findings["content_address_holds"], findings["chain_links"],
            findings["rules_unchanged"], findings["inputs_unchanged"],
            findings["sample_agreed"]))
    return findings
