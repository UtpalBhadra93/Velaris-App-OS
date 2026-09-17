"""HxArbiter — persistence for policies, versions, namespaces and decisions.

This is the first file in the module that touches a database, and therefore the
first that is **not** part of the Apache-2.0 core. `schema`, `catalog`,
`compiler`, `evaluator` and `protocols` stay pure; everything from here down is
Velaris' half.

Three things here are less obvious than they look:

* **The generation is derived, never bumped.** A compiled-policy cache has to
  know when policy changed elsewhere. HxGuard solved that with a counter that
  every mutation bumps, and the failure mode of that design has already been
  paid for once in this platform — access-group writes bumped nothing, and
  revocations went stale for 30 seconds. A generation computed from the rows
  themselves cannot be forgotten by a writer that does not know it exists.
* **Activation is an interval, not a pointer.** Activating a version stamps
  `activated_at`, and deactivating the previous one stamps `deactivated_at`, in
  the same transaction. "Which version was active on 3 March" is the question
  the product is sold on, and a current-version pointer cannot answer it.
* **`tenant_key` is maintained here.** It is what makes policy names unique in
  both scopes; the database CHECK refuses a value that disagrees with
  `tenant_id`, so a bug here fails loudly rather than silently permitting a
  duplicate platform policy.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import hashlib
import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Sequence

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from velaris_shared.db.models import (
    ArbiterDecisionModel,
    ArbiterNamespaceModel,
    ArbiterPolicyModel,
    ArbiterPolicyVersionModel,
)

from hxarbiter_core import AttributeCatalog
from hxarbiter_core import CompiledPolicy, CompileError, PolicyCompiler
from hxarbiter_core import (
    Decision,
    DecisionRequest,
    Effect,
    NamespaceSpec,
    PolicyMode,
    PolicyStatus,
    PolicyVersion,
    Rule,
)

log = logging.getLogger(__name__)

#: How long a retired policy keeps being evaluated, clamped to shadow (§19.6).
#:
#: Decommissioning is not the mirror of creating. Adding a rule fails safe — it
#: lands in shadow and changes nothing until promoted. Removing one takes
#: enforcement away, silently, and the only way to see whether that was right
#: is to keep asking the question for a while afterwards and look at the log.
RETIREMENT_SOAK_DAYS = 14

#: What a platform-scoped policy's `tenant_key` holds. A NULL tenant_id cannot
#: carry uniqueness (NULL <> NULL), so it collapses to this literal.
PLATFORM_KEY = "platform"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def tenant_key_for(tenant_id: uuid.UUID | None) -> str:
    return PLATFORM_KEY if tenant_id is None else str(tenant_id)


# ── generation ───────────────────────────────────────────────────────────────


async def policy_generation(session: AsyncSession) -> str:
    """A fingerprint of the policy set, used to key the compiled cache.

    Derived from row counts and the newest timestamps rather than from a counter
    somebody has to remember to bump. Two aggregate reads over small tables —
    policies number in the tens, not the millions — which is cheaper than the
    compilation it protects.

    Returns a sentinel on failure so the caller can tell "unchanged" from
    "unknown". A cache must not be treated as fresh because the freshness check
    itself failed.
    """
    try:
        pol = (await session.execute(
            select(func.count(ArbiterPolicyModel.id),
                   func.max(ArbiterPolicyModel.updated_at))
        )).one()
        ns = (await session.execute(
            select(func.count(ArbiterNamespaceModel.namespace),
                   func.max(ArbiterNamespaceModel.updated_at))
        )).one()
        ver = (await session.execute(
            select(func.count(), func.max(ArbiterPolicyVersionModel.activated_at))
            .select_from(ArbiterPolicyVersionModel)
        )).one()
        return "|".join(str(x) for x in (*pol, *ns, *ver))
    except Exception as exc:                                   # noqa: BLE001
        log.warning("hxarbiter: policy generation read failed: %s", exc)
        return ""


# ── namespaces ───────────────────────────────────────────────────────────────


async def load_namespaces(session: AsyncSession) -> dict[str, NamespaceSpec]:
    rows = (await session.execute(select(ArbiterNamespaceModel))).scalars().all()
    return {
        r.namespace: NamespaceSpec(
            namespace=r.namespace,
            default_effect=Effect(r.default_effect),
            max_mode=PolicyMode(r.max_mode),
            description=r.description or "",
        )
        for r in rows
    }


async def namespace_sampling(session: AsyncSession, namespace: str) -> tuple[float, float]:
    """(allow rate, default rate) for the namespace. Both 1.0 if unregistered."""
    row = (await session.execute(
        select(ArbiterNamespaceModel.sample_allow_rate,
               ArbiterNamespaceModel.sample_default_rate)
        .where(ArbiterNamespaceModel.namespace == namespace)
    )).one_or_none()
    if row is None:
        return 1.0, 1.0
    return float(row[0]), float(row[1])


async def promote_namespace(
    session: AsyncSession, namespace: str, max_mode: PolicyMode,
) -> None:
    """Raise (or lower) a namespace ceiling.

    `promoted_at` is stamped only when the namespace actually leaves shadow, so
    it records the moment enforcement became possible rather than the last time
    somebody edited the row.
    """
    values: dict[str, Any] = {"max_mode": str(max_mode), "updated_at": _utcnow()}
    if max_mode is not PolicyMode.SHADOW:
        values["promoted_at"] = _utcnow()
    await session.execute(
        update(ArbiterNamespaceModel)
        .where(ArbiterNamespaceModel.namespace == namespace)
        .values(**values)
    )


# ── policies and versions ────────────────────────────────────────────────────


async def create_policy(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID | None,
    name: str,
    namespace: str,
    description: str = "",
    origin: str = "user",
    created_by: uuid.UUID | None = None,
) -> ArbiterPolicyModel:
    row = ArbiterPolicyModel(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        tenant_key=tenant_key_for(tenant_id),
        name=name,
        description=description,
        namespace=namespace,
        status=str(PolicyStatus.DRAFT),
        origin=origin,
        created_by=created_by,
        created_at=_utcnow(),
        updated_at=_utcnow(),
    )
    session.add(row)
    await session.flush()
    return row


async def add_version(
    session: AsyncSession,
    *,
    policy_id: uuid.UUID,
    rules: Sequence[Rule],
    compiler: PolicyCompiler,
    mode: PolicyMode = PolicyMode.SHADOW,
    created_by: uuid.UUID | None = None,
    nl_source: str | None = None,
    nl_readback: str | None = None,
) -> ArbiterPolicyVersionModel:
    """Append the next version. Compilation failure means it is never stored.

    Refusing at write time rather than at activation is deliberate: a stored
    version that cannot compile is a trap for whoever tries to activate it later
    under time pressure.
    """
    nxt = (await session.execute(
        select(func.coalesce(func.max(ArbiterPolicyVersionModel.version), 0))
        .where(ArbiterPolicyVersionModel.policy_id == policy_id)
    )).scalar_one() + 1

    version = PolicyVersion(
        policy_id=policy_id, version=nxt, rules=list(rules), mode=mode,
        created_by=created_by, nl_source=nl_source, nl_readback=nl_readback,
    )
    report = compiler.check_version(version)
    if not report.ok:
        raise CompileError("; ".join(report.errors))

    row = ArbiterPolicyVersionModel(
        policy_id=policy_id,
        version=nxt,
        rules=[r.model_dump(mode="json", by_alias=True) for r in version.rules],
        mode=str(mode),
        nl_source=nl_source,
        nl_readback=nl_readback,
        created_by=created_by,
        signature=version.signature(),
        created_at=_utcnow(),
    )
    session.add(row)
    await session.flush()
    return row


async def activate_version(
    session: AsyncSession, *, policy_id: uuid.UUID, version: int,
) -> None:
    """Point the policy at one version, and close the previous interval.

    Both halves happen here, in one transaction. Stamping `activated_at`
    without closing the old version's `deactivated_at` would make two versions
    look simultaneously active — and "which rule was live on 3 March" would then
    have two answers, which is worse than none.
    """
    now = _utcnow()
    policy = (await session.execute(
        select(ArbiterPolicyModel).where(ArbiterPolicyModel.id == policy_id)
    )).scalar_one()

    if policy.active_version is not None and policy.active_version != version:
        await session.execute(
            update(ArbiterPolicyVersionModel)
            .where(ArbiterPolicyVersionModel.policy_id == policy_id,
                   ArbiterPolicyVersionModel.version == policy.active_version,
                   ArbiterPolicyVersionModel.deactivated_at.is_(None))
            .values(deactivated_at=now)
        )

    await session.execute(
        update(ArbiterPolicyVersionModel)
        .where(ArbiterPolicyVersionModel.policy_id == policy_id,
               ArbiterPolicyVersionModel.version == version)
        .values(activated_at=now, deactivated_at=None)
    )
    policy.active_version = version
    policy.status = str(PolicyStatus.ACTIVE)
    policy.updated_at = now
    await session.flush()


async def retire_policy(session: AsyncSession, *, policy_id: uuid.UUID) -> None:
    """Retire a policy, closing its active version's interval.

    `retired_at` starts the soak: for `RETIREMENT_SOAK_DAYS` the policy keeps
    being evaluated, clamped to shadow by `load_active_policies`, so an
    operator can see in the decision log what it would still have caught.
    Removing enforcement is not the mirror of adding it, and a retrieval hop is
    a thin basis for doing it silently.

    The version's interval is closed here regardless. "Which rule was in force
    on 3 March" must answer *not this one* from the moment of retirement — the
    soak is observation, not enforcement, and conflating the two would put a
    fictional enforcement window into an audit answer.
    """
    now = _utcnow()
    policy = (await session.execute(
        select(ArbiterPolicyModel).where(ArbiterPolicyModel.id == policy_id)
    )).scalar_one()
    if policy.active_version is not None:
        await session.execute(
            update(ArbiterPolicyVersionModel)
            .where(ArbiterPolicyVersionModel.policy_id == policy_id,
                   ArbiterPolicyVersionModel.version == policy.active_version,
                   ArbiterPolicyVersionModel.deactivated_at.is_(None))
            .values(deactivated_at=now)
        )
    policy.status = str(PolicyStatus.RETIRED)
    policy.retired_at = now
    policy.updated_at = now
    await session.flush()


async def version_active_at(
    session: AsyncSession, *, policy_id: uuid.UUID, moment: datetime,
) -> ArbiterPolicyVersionModel | None:
    """Which version was active at `moment` — the audit question, answered.

    An open interval (`deactivated_at IS NULL`) means "still active", so a
    version activated before the moment and never deactivated matches.
    """
    return (await session.execute(
        select(ArbiterPolicyVersionModel).where(
            ArbiterPolicyVersionModel.policy_id == policy_id,
            ArbiterPolicyVersionModel.activated_at.isnot(None),
            ArbiterPolicyVersionModel.activated_at <= moment,
            (ArbiterPolicyVersionModel.deactivated_at.is_(None)
             | (ArbiterPolicyVersionModel.deactivated_at > moment)),
        ).order_by(ArbiterPolicyVersionModel.activated_at.desc()).limit(1)
    )).scalars().first()


# ── loading the active set ───────────────────────────────────────────────────


async def load_active_policies(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID | None,
    namespace: str,
    catalog: AttributeCatalog,
) -> list[CompiledPolicy]:
    """Platform AND tenant policies for the namespace, compiled.

    Both scopes in one list, because deny-overrides then applies across them
    without anybody having to remember precedence — a tenant tightens the
    platform baseline and cannot loosen it (§5.3). Ordering is irrelevant by
    construction, which is the point.

    A version that no longer compiles is **skipped and logged, not fatal**. The
    catalog can lose an attribute between the write and this read; dropping one
    policy is bad, and refusing to answer any decision in the namespace is worse
    — the namespace default then applies, which for every sovereignty-bearing
    namespace is deny.

    Retired policies are **not** here, deliberately — see
    `load_soaking_policies`, which evaluates them in a separate pass whose
    result cannot touch this one.
    """
    compiler = PolicyCompiler(catalog)
    # Platform rows always; the caller's own tenant as well when there is one.
    scope = (
        ArbiterPolicyModel.tenant_id.is_(None)
        if tenant_id is None
        else ArbiterPolicyModel.tenant_id.is_(None)
        | (ArbiterPolicyModel.tenant_id == tenant_id)
    )

    rows = (await session.execute(
        select(ArbiterPolicyModel, ArbiterPolicyVersionModel)
        .join(ArbiterPolicyVersionModel,
              (ArbiterPolicyVersionModel.policy_id == ArbiterPolicyModel.id)
              & (ArbiterPolicyVersionModel.version == ArbiterPolicyModel.active_version))
        .where(
            ArbiterPolicyModel.namespace == namespace,
            ArbiterPolicyModel.status == str(PolicyStatus.ACTIVE),
            ArbiterPolicyModel.active_version.isnot(None),
            scope,
        )
    )).all()

    out: list[CompiledPolicy] = []
    for policy, version in rows:
        try:
            pv = PolicyVersion(
                policy_id=policy.id,
                version=version.version,
                rules=[Rule.model_validate(r) for r in (version.rules or [])],
                mode=PolicyMode(version.mode),
            )
            out.append(compiler.compile_version(
                policy_id=policy.id,
                tenant_id=str(policy.tenant_id) if policy.tenant_id else None,
                namespace=policy.namespace,
                version=pv,
                mode=PolicyMode(version.mode),
            ))
        except Exception as exc:                               # noqa: BLE001
            log.error(
                "hxarbiter: active policy %s v%s in namespace %s does not "
                "compile and was SKIPPED: %s",
                policy.id, version.version, namespace, exc,
            )
    return out


async def load_soaking_policies(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID | None,
    namespace: str,
    catalog: AttributeCatalog,
) -> list[CompiledPolicy]:
    """Policies retired within the soak window, compiled and pinned to shadow.

    §19.6: **decommissioning is not the mirror of creating.** Adding a rule
    fails safe — it lands in shadow and changes nothing until promoted.
    Removing one takes enforcement away silently, and the only way to know
    whether that was right is to keep asking the question afterwards.

    These are returned SEPARATELY from `load_active_policies`, and that
    separation is the whole safety property. The obvious implementation — let a
    retired policy back into the live set at `shadow` — looks harmless because
    the clamp stops it binding, and it is not:

    * a retired **deny** rejoining the set changes the decision's `effect`
      under deny-overrides, so the answer in the log stops being the answer the
      platform gave;
    * that shows up in the parity report as `engine_stricter`, which can
      **block a promotion** that was eligible the day before the upgrade;
    * and it would appear in `matched`, so the audit trail would name a rule
      that was not in force.

    Evaluated in its own pass, a soaking policy can do none of those. It
    answers one question — *what would this have caught?* — and nothing else.
    """
    compiler = PolicyCompiler(catalog)
    soak_from = _utcnow() - timedelta(days=RETIREMENT_SOAK_DAYS)
    scope = (
        ArbiterPolicyModel.tenant_id.is_(None)
        if tenant_id is None
        else ArbiterPolicyModel.tenant_id.is_(None)
        | (ArbiterPolicyModel.tenant_id == tenant_id)
    )
    rows = (await session.execute(
        select(ArbiterPolicyModel, ArbiterPolicyVersionModel)
        .join(ArbiterPolicyVersionModel,
              (ArbiterPolicyVersionModel.policy_id == ArbiterPolicyModel.id)
              & (ArbiterPolicyVersionModel.version == ArbiterPolicyModel.active_version))
        .where(
            ArbiterPolicyModel.namespace == namespace,
            ArbiterPolicyModel.status == str(PolicyStatus.RETIRED),
            ArbiterPolicyModel.active_version.isnot(None),
            ArbiterPolicyModel.retired_at.isnot(None),
            ArbiterPolicyModel.retired_at >= soak_from,
            scope,
        )
    )).all()

    out: list[CompiledPolicy] = []
    for policy, version in rows:
        try:
            pv = PolicyVersion(
                policy_id=policy.id, version=version.version,
                rules=[Rule.model_validate(r) for r in (version.rules or [])],
                mode=PolicyMode.SHADOW)
            out.append(compiler.compile_version(
                policy_id=policy.id,
                tenant_id=str(policy.tenant_id) if policy.tenant_id else None,
                namespace=policy.namespace, version=pv,
                mode=PolicyMode.SHADOW))
        except Exception as exc:                               # noqa: BLE001
            log.debug("hxarbiter: soaking policy %s does not compile: %s",
                      policy.id, exc)
    return out


def policy_set_hash(policies: Sequence[CompiledPolicy]) -> str:
    """Digest over the policy set that produced a decision.

    Recorded on every decision row, including one where nothing matched — a
    `default_applied` row otherwise names no policy at all, and "nothing
    matched" is the answer most worth being able to question later.
    """
    parts = sorted(f"{p.policy_id}:{p.version}" for p in policies)
    return hashlib.sha256("|".join(parts).encode()).hexdigest()


# ── decisions ────────────────────────────────────────────────────────────────


async def write_decision(
    session: AsyncSession,
    *,
    req: DecisionRequest,
    tenant_uuid: uuid.UUID,
    decision: Decision,
    mode: PolicyMode,
    redacted_context: dict[str, Any],
    policy_set: str | None,
    legacy_effect: Effect | None = None,
    request_id: str | None = None,
    claimed_by: str | None = None,
    simulated: bool = False,
) -> uuid.UUID:
    row = ArbiterDecisionModel(
        id=uuid.uuid4(),
        tenant_id=tenant_uuid,
        namespace=req.namespace,
        action=req.action,
        subject_kind=str(req.subject.kind),
        subject_id=req.subject.id,
        subject=req.subject.model_dump(mode="json"),
        resource=req.resource.model_dump(mode="json"),
        context=redacted_context,
        effect=str(decision.effect),
        mode=str(mode),
        enforced=decision.enforced,
        default_applied=decision.default_applied,
        matched=[m.model_dump(mode="json") for m in decision.matched],
        obligations=[o.model_dump(mode="json") for o in decision.obligations],
        legacy_effect=str(legacy_effect) if legacy_effect else None,
        policy_set_hash=policy_set,
        error=decision.error,
        request_id=request_id,
        claimed_by=claimed_by,
        simulated=simulated,
        latency_us=decision.latency_us,
        evaluated_at=decision.evaluated_at or _utcnow(),
    )
    session.add(row)
    await session.flush()
    return row.id
