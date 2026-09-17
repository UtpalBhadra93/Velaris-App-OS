"""F-03 — generating and verifying a compliance proof.

The engine deliberately reuses HxArbiter's what-if primitives rather than
reimplementing them: `whatif.unreplayable_paths` decides what can honestly be
replayed at all, and `whatif._attrs_from` rebuilds the request-derived
attributes exactly as `RequestResolver` would. A proof produced by a second,
subtly different replay would not be a proof of anything the live engine does.

Four properties, and each is a decision that could have gone the other way:

* **The whole cohort, or a refusal.** "0 of 12,844" is the claim; a sample
  cannot make it. So when a cohort exceeds the ceiling the proof is REFUSED
  and says by how much, rather than quietly sampling and reading as complete.
* **Refusals are stored.** "We could not prove this, and here is why" is an
  answer an auditor keeps. Returning nothing invites the question being asked
  again until it accidentally succeeds.
* **Content-addressed and chained.** The document's hash is its address, and
  each proof links to the one before it, so removing or back-dating one breaks
  every proof after it.
* **The count is the finding; the list is case data.** The divergent count is
  the same for everybody; who may see WHICH cases diverged is filtered at read
  time, per viewer.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import hashlib
import json
import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Sequence

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from hxarbiter_core import (
    AttributeCatalog,
    Effect,
    Evaluator,
    PolicyCompiler,
    PolicyMode,
    PolicyVersion,
    Rule,
)
from velaris_shared.db.models import (
    ArbiterDecisionModel,
    ArbiterNamespaceModel,
    ArbiterPolicyModel,
    ArbiterPolicyVersionModel,
    ComplianceProofModel,
    _utcnow,
)

from case_service.hxarbiter.nlp import whatif

log = logging.getLogger(__name__)

GENESIS = "0" * 64

#: The largest cohort one proof will replay. Not a sampling threshold — a
#: REFUSAL threshold. Above it the proof says how many decisions it found and
#: declines, because a proof that silently sampled would be read as complete.
#: The design's exit criterion is 1,000 cases; this is fifty times that, and an
#: install that legitimately needs more needs the Temporal job F-03 names, not
#: a bigger number here.
MAX_COHORT = 50_000

#: Divergent decisions kept in the document. The COUNT is always exact; this
#: bounds the list, because a proof over a badly-drafted rule could otherwise
#: put a million rows in one JSON column.
MAX_DIVERGENT_LISTED = 1_000

#: k-anonymity floor for a platform-scope proof. A cross-tenant cohort smaller
#: than this can identify a tenant by arithmetic alone.
K_ANONYMITY_FLOOR = 25


class ProofError(ValueError):
    """The proof cannot be generated as asked."""


def _canonical(payload: Any) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)


def _sha(payload: Any) -> str:
    return hashlib.sha256(_canonical(payload).encode("utf-8")).hexdigest()


def rules_digest(rules: Sequence[Rule]) -> str:
    """Commit to the exact rule set, order-independently.

    Sorted by id: a policy that lists the same rules in a different order is the
    same policy, and a digest that disagreed would make two identical proofs
    look like different ones.
    """
    return _sha(sorted((r.model_dump() for r in rules),
                       key=lambda d: str(d.get("id"))))


def inputs_digest(rows: Sequence[ArbiterDecisionModel]) -> str:
    """Commit to what was replayed, and to what it was replayed AGAINST.

    Both halves matter. Hashing only the ids would let somebody edit a
    decision's context afterwards and have the proof still verify against it —
    the proof would be about inputs that no longer exist.
    """
    return _sha([
        {"id": str(r.id), "attrs": whatif._attrs_from(r), "effect": r.effect}
        for r in sorted(rows, key=lambda x: str(x.id))
    ])


def content_digest(document: dict[str, Any], prev_hash: str) -> str:
    """The document's address: `sha256(prev || canonical(document))`.

    `prev` is included so the address is also the chain link — two separate
    hashes over the same bytes would be two things to keep in step.
    """
    return hashlib.sha256(
        (prev_hash + _canonical(document)).encode("utf-8")).hexdigest()


async def _tip(session: AsyncSession) -> tuple[int, str]:
    row = (await session.execute(
        select(ComplianceProofModel)
        .order_by(ComplianceProofModel.sequence.desc()).limit(1)
        .execution_options(populate_existing=True)
    )).scalars().first()
    return (0, GENESIS) if row is None else (row.sequence, row.content_sha256)


async def _load_rules(
    session: AsyncSession, policy_id: uuid.UUID, version: int,
) -> tuple[list[Rule], ArbiterPolicyModel]:
    policy = await session.get(ArbiterPolicyModel, policy_id)
    if policy is None:
        raise ProofError("no such policy")
    row = (await session.execute(
        select(ArbiterPolicyVersionModel).where(
            ArbiterPolicyVersionModel.policy_id == policy_id,
            ArbiterPolicyVersionModel.version == version,
        )
    )).scalars().first()
    if row is None:
        raise ProofError(f"policy has no version {version}")
    return [Rule.model_validate(r) for r in (row.rules or [])], policy


async def _cohort(
    session: AsyncSession,
    *,
    namespace: str,
    tenant_id: uuid.UUID | None,
    since: datetime | None,
    until: datetime | None,
) -> list[ArbiterDecisionModel]:
    """Every real decision in scope. Not a sample, and never `simulated`.

    Ordered by id rather than by time: the digest sorts by id anyway, and a
    time ordering would make two runs over the same rows differ whenever two
    decisions share a timestamp.
    """
    where = [
        ArbiterDecisionModel.namespace == namespace,
        ArbiterDecisionModel.simulated.is_(False),
    ]
    if tenant_id is not None:
        where.append(ArbiterDecisionModel.tenant_id == tenant_id)
    if since is not None:
        where.append(ArbiterDecisionModel.evaluated_at >= since)
    if until is not None:
        where.append(ArbiterDecisionModel.evaluated_at < until)
    return list((await session.execute(
        select(ArbiterDecisionModel).where(*where)
        .order_by(ArbiterDecisionModel.id)
        .execution_options(populate_existing=True)
    )).scalars())


async def _cohort_size(
    session: AsyncSession, *, namespace: str, tenant_id: uuid.UUID | None,
    since: datetime | None, until: datetime | None,
) -> int:
    where = [
        ArbiterDecisionModel.namespace == namespace,
        ArbiterDecisionModel.simulated.is_(False),
    ]
    if tenant_id is not None:
        where.append(ArbiterDecisionModel.tenant_id == tenant_id)
    if since is not None:
        where.append(ArbiterDecisionModel.evaluated_at >= since)
    if until is not None:
        where.append(ArbiterDecisionModel.evaluated_at < until)
    return int((await session.execute(
        select(func.count()).select_from(ArbiterDecisionModel).where(*where)
    )).scalar_one())


def _replay_all(
    rows: Sequence[ArbiterDecisionModel],
    compiled: Any,
    default_effect: Effect,
) -> tuple[dict[str, int], list[dict[str, Any]]]:
    """Evaluate every row. Returns (distribution, divergent list).

    The candidate version is evaluated ALONE — it answers "what would this
    policy decide", not "what would the whole set decide with it added". The
    second needs the other policies' attributes, which the decision log has the
    same recording problem with, and answering it approximately would be worse
    than declining.
    """
    evaluator = Evaluator()
    dist = {"unchanged": 0, "to_deny": 0, "to_allow": 0, "no_opinion": 0}
    divergent: list[dict[str, Any]] = []
    for row in rows:
        outcome = evaluator.evaluate(
            policies=[compiled], attrs=whatif._attrs_from(row),
            namespace_default=default_effect,
            namespace_max_mode=PolicyMode.ENFORCE)

        # A policy that matched NO rule has expressed no opinion about this
        # decision, and counting it as divergent is the difference between a
        # useful proof and a worthless one.
        #
        # The candidate is evaluated alone, so an unmatched decision falls
        # through to the NAMESPACE DEFAULT — `deny` in every sovereignty-bearing
        # namespace. Without this branch, a policy about one narrow thing
        # reports every unrelated decision in the namespace as "would have been
        # denied", and the headline number becomes the cohort size. That is a
        # true statement about a hypothetical where this is the only policy on
        # the box, and a useless answer to the question actually asked, which is
        # what THIS policy would have done differently.
        #
        # Counted and reported separately rather than hidden: an auditor needs
        # to know how much of the cohort the policy is silent about.
        if not outcome.matched:
            dist["no_opinion"] += 1
            continue

        if str(outcome.effect) == row.effect:
            dist["unchanged"] += 1
            continue
        would_be = str(outcome.effect)
        dist["to_deny" if outcome.effect is Effect.DENY else "to_allow"] += 1
        if len(divergent) < MAX_DIVERGENT_LISTED:
            resource = row.resource or {}
            divergent.append({
                "decision_id": str(row.id),
                "action": row.action,
                "was": row.effect,
                "would_be": would_be,
                "resource_kind": resource.get("kind"),
                "resource_id": resource.get("id"),
                "matched": [m.rule_id for m in outcome.matched],
            })
    return dist, divergent


async def generate(
    session: AsyncSession,
    *,
    policy_id: Any,
    policy_version: int,
    catalog: AttributeCatalog,
    tenant_id: Any | None,
    since: datetime | None = None,
    until: datetime | None = None,
    generated_by: Any | None = None,
    aggregate_only: bool = False,
) -> ComplianceProofModel:
    """Replay a policy version across a whole cohort and store the proof.

    Read-only over the decision log: it evaluates, and the single row it writes
    is the proof itself. The caller commits.
    """
    pid = uuid.UUID(str(policy_id))
    tid = uuid.UUID(str(tenant_id)) if tenant_id is not None else None
    rules, policy = await _load_rules(session, pid, policy_version)
    namespace = policy.namespace

    compiled = PolicyCompiler(catalog).compile_version(
        policy_id=pid, tenant_id=None, namespace=namespace,
        version=PolicyVersion(policy_id=pid, version=policy_version,
                              rules=list(rules)),
        mode=PolicyMode.ENFORCE)

    cohort_def = {
        "namespace": namespace,
        "tenant_id": str(tid) if tid else None,
        "scope": "tenant" if tid else "platform",
        "since": since.isoformat() if since else None,
        "until": until.isoformat() if until else None,
    }

    async def _refuse(reason: str, detail: str, extra: dict | None = None):
        return await _store(
            session, tenant_id=tid, policy=policy, policy_version=policy_version,
            namespace=namespace, status="refused", refusal_reason=detail,
            cohort={**cohort_def, **(extra or {}), "refused": reason},
            inputs_sha256=_sha(None), rules_sha256=rules_digest(rules),
            result={"refused": reason}, divergent=[],
            aggregate_only=aggregate_only, generated_by=generated_by)

    # 1. Can this be replayed honestly at all? The what-if's refusal, and for
    #    exactly its reason: a rule reading a PIP-resolved attribute the log
    #    never stored would have every comparison on it read false, and the
    #    proof would say "nothing would change" for a rule that changes a great
    #    deal. That is the most dangerous output this function has.
    blocked = whatif.unreplayable_paths(compiled.required_attrs)
    if blocked:
        return await _refuse(
            "attributes_not_recorded",
            "These rules read attributes the decision log does not store — "
            "they are resolved by a PIP at decision time, not carried on the "
            "request. Replaying anyway would treat them as missing, make every "
            "comparison on them false, and report 'nothing would change' for a "
            "rule that may change a great deal.",
            {"unreplayable_attributes": blocked})

    # 2. Is the cohort provable in one pass? Counted BEFORE loading, so an
    #    oversized cohort is refused rather than pulled into memory first.
    size = await _cohort_size(session, namespace=namespace, tenant_id=tid,
                              since=since, until=until)
    if size > MAX_COHORT:
        return await _refuse(
            "cohort_too_large",
            f"The cohort holds {size} decisions and this proof replays at most "
            f"{MAX_COHORT} in one pass. Narrow the window rather than accepting "
            f"a sample: a sampled result cannot support a statement of the form "
            f"'0 of {size}', which is what a proof is for.",
            {"cohort_size": size})
    if size == 0:
        return await _refuse(
            "empty_cohort",
            "No decisions were recorded in this namespace and window, so there "
            "is nothing to replay against. That is not evidence the policy is "
            "harmless — it is evidence nothing has been asked of it.",
            {"cohort_size": 0})
    if tid is None and size < K_ANONYMITY_FLOOR:
        # A cross-tenant cohort this small can identify a tenant by arithmetic.
        return await _refuse(
            "below_k_anonymity",
            f"A platform-scope proof needs at least {K_ANONYMITY_FLOOR} "
            f"decisions; this cohort holds {size}, which is small enough to "
            f"identify a tenant by arithmetic alone.",
            {"cohort_size": size})

    rows = await _cohort(session, namespace=namespace, tenant_id=tid,
                         since=since, until=until)

    spec = (await session.execute(
        select(ArbiterNamespaceModel)
        .where(ArbiterNamespaceModel.namespace == namespace)
    )).scalars().first()
    default_effect = Effect(spec.default_effect) if spec else Effect.DENY

    dist, divergent = _replay_all(rows, compiled, default_effect)
    total = len(rows)
    result = {
        "cohort_size": total,
        "unchanged": dist["unchanged"],
        "would_change": dist["to_deny"] + dist["to_allow"],
        "would_change_to_deny": dist["to_deny"],
        "would_change_to_allow": dist["to_allow"],
        # How much of the cohort this policy says nothing about. Reported
        # because it is the proof's own coverage: "3 of 12,844 would change"
        # means something very different when the policy is silent about 12,000
        # of them, and an auditor is entitled to see that.
        "no_opinion": dist["no_opinion"],
        "matched": total - dist["no_opinion"],
        # The sentence an auditor actually quotes.
        "statement": (
            f"{dist['to_deny'] + dist['to_allow']} of {total} recorded "
            f"decisions in {namespace!r} would have been decided differently "
            f"under this policy version "
            f"({dist['no_opinion']} of them are outside its rules)."),
        "divergent_listed": len(divergent),
        "divergent_truncated": (dist["to_deny"] + dist["to_allow"]) > len(divergent),
        "namespace_default": str(default_effect),
    }

    return await _store(
        session, tenant_id=tid, policy=policy, policy_version=policy_version,
        namespace=namespace, status="complete", refusal_reason=None,
        cohort={**cohort_def, "cohort_size": total},
        inputs_sha256=inputs_digest(rows), rules_sha256=rules_digest(rules),
        result=result,
        # A platform-scope proof carries NO divergent list at all: the
        # distribution is the whole permitted output.
        divergent=([] if (aggregate_only or tid is None) else divergent),
        aggregate_only=(aggregate_only or tid is None),
        generated_by=generated_by)


async def _store(
    session: AsyncSession, *, tenant_id, policy, policy_version, namespace,
    status, refusal_reason, cohort, inputs_sha256, rules_sha256, result,
    divergent, aggregate_only, generated_by,
) -> ComplianceProofModel:
    sequence, prev_hash = await _tip(session)
    document = {
        "tenant_id": str(tenant_id) if tenant_id else None,
        "subject_kind": "policy_version",
        "policy_id": str(policy.id),
        "policy_name": policy.name,
        "policy_version": policy_version,
        "namespace": namespace,
        "status": status,
        "refusal_reason": refusal_reason,
        "cohort": cohort,
        "inputs_sha256": inputs_sha256,
        "rules_sha256": rules_sha256,
        "result": result,
        "divergent": divergent,
        "aggregate_only": aggregate_only,
        "sequence": sequence + 1,
    }
    row = ComplianceProofModel(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        subject_kind="policy_version",
        policy_id=policy.id,
        policy_version=policy_version,
        namespace=namespace,
        status=status,
        refusal_reason=refusal_reason,
        cohort=cohort,
        inputs_sha256=inputs_sha256,
        rules_sha256=rules_sha256,
        result=result,
        divergent=divergent,
        aggregate_only=aggregate_only,
        prev_hash=prev_hash,
        content_sha256=content_digest(document, prev_hash),
        sequence=sequence + 1,
        generated_by=(uuid.UUID(str(generated_by))
                      if generated_by is not None else None),
        created_at=_utcnow(),
    )
    session.add(row)
    await session.flush()
    return row


def rebuild_document(proof: ComplianceProofModel) -> dict[str, Any]:
    """The exact bytes the address was taken over.

    Kept beside `_store` on purpose: if the two ever disagree about a field,
    every proof stops verifying at once, which is loud. A mismatch that
    affected only new proofs would be quiet and far worse.
    """
    return {
        "tenant_id": str(proof.tenant_id) if proof.tenant_id else None,
        "subject_kind": proof.subject_kind,
        "policy_id": str(proof.policy_id) if proof.policy_id else None,
        "policy_name": None,          # filled by the caller, which holds it
        "policy_version": proof.policy_version,
        "namespace": proof.namespace,
        "status": proof.status,
        "refusal_reason": proof.refusal_reason,
        "cohort": proof.cohort,
        "inputs_sha256": proof.inputs_sha256,
        "rules_sha256": proof.rules_sha256,
        "result": proof.result,
        "divergent": proof.divergent,
        "aggregate_only": bool(proof.aggregate_only),
        "sequence": proof.sequence,
    }


async def verify(
    session: AsyncSession, proof: ComplianceProofModel, *,
    catalog: AttributeCatalog, sample: int = 50,
) -> dict[str, Any]:
    """Re-derive the proof, and say precisely which parts held.

    Four independent checks, reported separately rather than as one boolean —
    "it verifies" and "the document is intact but the inputs it was taken over
    have since been edited" are different answers, and collapsing them would
    hide the second.
    """
    policy = await session.get(ArbiterPolicyModel, proof.policy_id)
    document = rebuild_document(proof)
    document["policy_name"] = policy.name if policy else None

    out: dict[str, Any] = {
        "proof_id": str(proof.id),
        "content_address_holds": (
            content_digest(document, proof.prev_hash) == proof.content_sha256),
        "chain_links": None,
        "rules_unchanged": None,
        "inputs_unchanged": None,
        "sampled": 0,
        "sample_agreed": None,
    }

    # The chain link: this proof must follow the one before it.
    if proof.sequence == 1:
        out["chain_links"] = proof.prev_hash == GENESIS
    else:
        prior = (await session.execute(
            select(ComplianceProofModel)
            .where(ComplianceProofModel.sequence == proof.sequence - 1)
            .execution_options(populate_existing=True)
        )).scalars().first()
        out["chain_links"] = (prior is not None
                              and prior.content_sha256 == proof.prev_hash)

    if proof.status != "complete" or policy is None:
        return out

    try:
        rules, _ = await _load_rules(session, proof.policy_id, proof.policy_version)
    except ProofError:
        out["rules_unchanged"] = False
        return out
    out["rules_unchanged"] = rules_digest(rules) == proof.rules_sha256

    cohort = proof.cohort or {}
    since = _parse(cohort.get("since"))
    until = _parse(cohort.get("until"))
    rows = await _cohort(session, namespace=proof.namespace,
                         tenant_id=proof.tenant_id, since=since, until=until)
    out["inputs_unchanged"] = inputs_digest(rows) == proof.inputs_sha256

    # Re-run a sample through the same evaluator. `sample` bounds the work, not
    # the claim: the claim was already made over the whole cohort, and this is
    # a spot check that the recorded distribution is reproducible.
    if not rows:
        return out
    compiled = PolicyCompiler(catalog).compile_version(
        policy_id=proof.policy_id, tenant_id=None, namespace=proof.namespace,
        version=PolicyVersion(policy_id=proof.policy_id,
                              version=proof.policy_version, rules=list(rules)),
        mode=PolicyMode.ENFORCE)
    spec = (await session.execute(
        select(ArbiterNamespaceModel)
        .where(ArbiterNamespaceModel.namespace == proof.namespace)
    )).scalars().first()
    default_effect = Effect(spec.default_effect) if spec else Effect.DENY

    # Deterministic sample — the FIRST n by id, not a random draw. A verifier
    # that sampled randomly would give a different answer each run, and "verify
    # it yourself and get the same thing" is the property being sold.
    subset = rows[:max(1, min(int(sample), len(rows)))]
    dist, _ = _replay_all(subset, compiled, default_effect)
    out["sampled"] = len(subset)

    if len(subset) == len(rows):
        # The sample IS the cohort, so the distribution must match exactly.
        out["sample_agreed"] = (
            dist["unchanged"] == proof.result.get("unchanged")
            and dist["to_deny"] == proof.result.get("would_change_to_deny")
            and dist["to_allow"] == proof.result.get("would_change_to_allow")
            and dist["no_opinion"] == proof.result.get("no_opinion"))
    else:
        # A partial sample can only contradict, never confirm: it agrees unless
        # it finds more divergence than the whole cohort was said to have.
        out["sample_agreed"] = (
            (dist["to_deny"] + dist["to_allow"])
            <= int(proof.result.get("would_change") or 0))
    return out


def _parse(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
