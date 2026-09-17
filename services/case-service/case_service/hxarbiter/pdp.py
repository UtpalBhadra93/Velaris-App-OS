"""HxArbiter — `decide()`, the façade every enforcement point calls.

The evaluator is pure and knows nothing about failure. This file is where
failure is decided, and there is one rule it exists to keep:

    **A namespace that cannot be evaluated returns its default, and every
    sovereignty-bearing namespace defaults to deny.**

That is fail-closed, and it is not the same as raising. A PEP that receives an
exception has to invent a policy at the call site, and forty call sites will
invent forty different ones — most of them "log it and continue", which is the
failure this engine exists to prevent. So `decide()` does not raise: it answers,
and says why in `Decision.error`.

The second thing this file owns is that **ARB-A cannot change anybody's
answer**. Every namespace ships clamped to `shadow`, so `enforced` is False on
every decision here; a PEP receiving `enforced=False` proceeds exactly as it did
before and the engine only writes to its log. Promotion is a later phase and an
operator's deliberate act.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import asyncio
import logging
import re
import random
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Iterable, Sequence

from sqlalchemy.ext.asyncio import AsyncSession

from . import store
from hxarbiter_core import AttributeCatalog
from .velaris_catalog import VELARIS_CATALOG
from hxarbiter_core import CompiledPolicy
from hxarbiter_core import Evaluator
from hxarbiter_core import AttributeResolver, RequestResolver
from hxarbiter_core import (
    Decision,
    DecisionRequest,
    Effect,
    NamespaceSpec,
    PolicyMode,
    clamp,
)

log = logging.getLogger(__name__)

#: Total budget for resolving every PIP a decision needs. Exceeding it is
#: treated as unresolved rather than waited on: a policy decision that blocks is
#: an outage, and this sits in front of ordinary request handling.
PIP_BUDGET_SECONDS = 0.05

#: An unregistered namespace has no default of its own. Deny is the only safe
#: answer — an action nobody has classified is exactly the one not to wave
#: through — and it is visible immediately, which an accidental allow is not.
UNREGISTERED_DEFAULT = Effect.DENY

#: Context keys never written to the decision log verbatim. The log is evidence,
#: not a second copy of the data it was asked about.
_REDACT_KEYS = frozenset({
    "prompt", "payload", "body", "content", "text", "document",
    "email", "phone", "name", "address", "dob", "national_id",
})
_REDACTED = "[redacted]"

#: Patterns redacted from a context VALUE, whatever the key is called.
#:
#: Key-name redaction alone is a half control, and the live box proved it:
#: `{"note": "call me on 07700900123"}` was stored verbatim because `note` is
#: not in the list above. That matters more here than in an ordinary table —
#: `arbiter_decisions` is append-only and hash-chained, so a row cannot be
#: scrubbed later without breaking the chain that makes the log evidence. PII
#: that reaches it is there for good.
#:
#: Deliberately narrow and shape-based: an email, a long run of digits (phone,
#: national insurance, card), and an IBAN-like token. Narrow because a
#: false positive costs an operator a parity clue, and because a broad
#: heuristic on a security log invites the opposite mistake — trusting it.
_REDACT_VALUE_PATTERNS = (
    re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+"),                     # email
    re.compile(r"(?<!\d)(?:\+?\d[\d\s-]{8,}\d)(?!\d)"),          # phone / long digits
    re.compile(r"\b[A-Z]{2}\d{2}[A-Z0-9]{10,30}\b"),              # IBAN-ish
)

#: Longest context value written to the log unexamined. A value beyond this is
#: not evidence about a decision, it is bulk — and the log is the evidence
#: surface, so bulk in it is a cost with no benefit.
MAX_CONTEXT_VALUE_CHARS = 512


class _CompiledCache:
    """Compiled policies per (tenant, namespace), keyed by a derived generation.

    The generation comes from the rows themselves (`store.policy_generation`),
    so no writer has to remember to bump anything. That is deliberate: this
    platform has already paid once for a bump-based cache where a writer did not
    know it was supposed to bump.
    """

    def __init__(self) -> None:
        self._generation: str | None = None
        self._policies: dict[tuple[str, str], list[CompiledPolicy]] = {}
        #: Retired-but-soaking policies, keyed by (tenant, namespace, hour).
        #:
        #: Cached for the same reason the active set is — this ran an uncached
        #: SELECT with a join on EVERY recorded decision, which is one database
        #: round trip added to the hot path to observe a rule nobody is
        #: enforcing. The `latency_us` on a decision row is stamped before this
        #: runs, so the cost was invisible in the engine's own numbers.
        #:
        #: The hour is in the key because the soak window is defined by TIME,
        #: not by rows: a policy leaves it 14 days after retirement without
        #: anything being written, so a purely generation-keyed cache would go
        #: on observing it forever. An hour bounds that overhang, and an hour of
        #: extra observation on a 14-day window changes nothing an operator
        #: reads.
        self._soaking: dict[tuple[str, str, int], list[CompiledPolicy]] = {}
        self._namespaces: dict[str, NamespaceSpec] = {}
        self._ns_loaded = False

    def invalidate(self) -> None:
        self._generation = None
        self._policies.clear()
        self._soaking.clear()
        self._namespaces.clear()
        self._ns_loaded = False

    async def _sync(self, session: AsyncSession) -> bool:
        """True when the cache is usable; False when freshness is unknown."""
        gen = await store.policy_generation(session)
        if not gen:
            # The freshness check itself failed. Do NOT treat what is held as
            # fresh — a stale allow is the one outcome this module must not
            # produce quietly.
            return False
        if gen != self._generation:
            self._generation = gen
            self._policies.clear()
            self._soaking.clear()
            self._namespaces.clear()
            self._ns_loaded = False
        return True

    async def soaking(
        self, session: AsyncSession, *,
        tenant_id: uuid.UUID | None, namespace: str, catalog: AttributeCatalog,
    ) -> list[CompiledPolicy]:
        """Retired-but-soaking policies, cached like the active set."""
        if not await self._sync(session):
            return await store.load_soaking_policies(
                session, tenant_id=tenant_id, namespace=namespace,
                catalog=catalog)
        hour = int(time.time() // 3600)
        key = (str(tenant_id or "-"), namespace, hour)
        if key not in self._soaking:
            # Drop other hours rather than accumulate one entry per hour per
            # namespace for the life of the process.
            self._soaking = {k: v for k, v in self._soaking.items()
                             if k[2] == hour}
            self._soaking[key] = await store.load_soaking_policies(
                session, tenant_id=tenant_id, namespace=namespace,
                catalog=catalog)
        return self._soaking[key]

    async def namespaces(self, session: AsyncSession) -> dict[str, NamespaceSpec]:
        if not self._ns_loaded:
            self._namespaces = await store.load_namespaces(session)
            self._ns_loaded = True
        return self._namespaces

    async def policies(
        self, session: AsyncSession, *,
        tenant_id: uuid.UUID | None, namespace: str, catalog: AttributeCatalog,
    ) -> list[CompiledPolicy]:
        key = (str(tenant_id or "-"), namespace)
        if key not in self._policies:
            self._policies[key] = await store.load_active_policies(
                session, tenant_id=tenant_id, namespace=namespace, catalog=catalog,
            )
        return self._policies[key]


_cache = _CompiledCache()


def invalidate_cache() -> None:
    """Drop this process's compiled cache. Safe to call at any time."""
    _cache.invalidate()


def _scrub_value(value: Any) -> Any:
    """Redact PII shapes inside a value. Only ever REMOVES information.

    Non-strings are returned unchanged: a bool or a number carries no free text,
    and coercing everything to a string to scan it would change what the log
    says a decision was made on.
    """
    if isinstance(value, str):
        if len(value) > MAX_CONTEXT_VALUE_CHARS:
            return value[:MAX_CONTEXT_VALUE_CHARS] + "…[truncated]"
        for pattern in _REDACT_VALUE_PATTERNS:
            value = pattern.sub(_REDACTED, value)
        return value
    if isinstance(value, list):
        return [_scrub_value(v) for v in value[:50]]
    if isinstance(value, dict):
        return {k: (_REDACTED if str(k).lower() in _REDACT_KEYS
                    else _scrub_value(v))
                for k, v in list(value.items())[:50]}
    return value


def _redact(context: dict[str, Any]) -> dict[str, Any]:
    """What actually gets written to `arbiter_decisions.context`.

    Two passes, and the second is the one that was missing: the key name, then
    the value itself. A caller who puts a phone number in a field called `note`
    is not doing anything wrong, and the log must not turn that into a
    permanent, unscrubbable copy.
    """
    out: dict[str, Any] = {}
    for k, v in context.items():
        out[k] = _REDACTED if k.lower() in _REDACT_KEYS else _scrub_value(v)
    return out


async def _resolve_attributes(
    req: DecisionRequest,
    resolvers: Sequence[AttributeResolver],
    paths: set[str],
) -> tuple[dict[str, Any], str | None]:
    """Resolve exactly the paths the policy set reads, in parallel, under budget.

    Returns (attributes, error). A resolver that raises or times out
    contributes nothing rather than a guess — the evaluator then treats its
    paths as missing, every comparison on them is false, and the namespace
    default applies.
    """
    if not paths:
        return {}, None

    wanted: list[tuple[AttributeResolver, set[str]]] = []
    for r in resolvers:
        owned = (
            set(paths) if r.prefix == "*"
            else {p for p in paths if p.split(".", 1)[0] == r.prefix}
        )
        if owned:
            wanted.append((r, owned))
    if not wanted:
        return {}, None

    async def _one(r: AttributeResolver, owned: set[str]) -> dict[str, Any]:
        try:
            return await r.resolve(req, owned)
        except Exception as exc:                               # noqa: BLE001
            log.warning("hxarbiter: PIP %r failed: %s", r.prefix, exc)
            raise

    error: str | None = None
    attrs: dict[str, Any] = {}
    try:
        results = await asyncio.wait_for(
            asyncio.gather(*(_one(r, o) for r, o in wanted),
                           return_exceptions=True),
            timeout=PIP_BUDGET_SECONDS,
        )
    except asyncio.TimeoutError:
        log.warning("hxarbiter: PIP budget exceeded for %s", req.action)
        return {}, "pip_timeout"

    for res in results:
        if isinstance(res, BaseException):
            error = "pip_error"
            continue
        attrs.update(res)
    return attrs, error


def _keep(decision: Decision, allow_rate: float, default_rate: float,
          legacy_effect: Effect | None) -> bool:
    """Sampling. Everything interesting is kept unconditionally.

    A deny, an obligation-bearing allow, a disagreement with the legacy control
    and any error are never sampled away — those are the rows an auditor and a
    parity report actually read. The rates only thin the uninteresting majority.
    """
    if decision.effect is Effect.DENY:
        return True
    if decision.obligations or decision.error:
        return True
    if legacy_effect is not None and legacy_effect is not decision.effect:
        return True
    rate = default_rate if decision.default_applied else allow_rate
    return rate >= 1.0 or random.random() < rate


async def decide(
    session: AsyncSession,
    req: DecisionRequest,
    *,
    resolvers: Iterable[AttributeResolver] | None = None,
    catalog: AttributeCatalog | None = None,
    legacy_effect: Effect | None = None,
    request_id: str | None = None,
    claimed_by: str | None = None,
    simulated: bool = False,
    record: bool = True,
) -> Decision:
    """Evaluate one request. Never raises; a failure is an answer, with a reason."""
    started = time.perf_counter()
    catalog = catalog or VELARIS_CATALOG
    if resolvers is not None:
        resolvers = list(resolvers)
    else:
        # The full PIP set by default (ARB-C). A caller that passes its own list
        # — `/simulate`, and tests — gets exactly that list and no lookups it
        # did not ask for, which is what makes a what-if reproducible.
        from .pips import default_resolvers
        resolvers = default_resolvers(session)
    namespace = req.namespace

    # Resolve the tenant once, here. Everything downstream — the policy scope
    # and the decision row — uses the UUID, so a log that mixes slugs and UUIDs
    # is impossible rather than merely discouraged.
    tenant_uuid: uuid.UUID | None = None
    try:
        from case_service.tenancy.identity import resolve_tenant
        resolved = await resolve_tenant(session, req.tenant_id)
        if resolved:
            tenant_uuid = resolved[0]
    except Exception as exc:                                   # noqa: BLE001
        log.warning("hxarbiter: tenant resolution failed for %r: %s",
                    req.tenant_id, exc)

    def _finish(d: Decision) -> Decision:
        d.latency_us = int((time.perf_counter() - started) * 1_000_000)
        d.evaluated_at = datetime.now(timezone.utc)
        return d

    # An unresolvable tenant is a denial, never "any tenant".
    if tenant_uuid is None:
        return _finish(Decision(
            effect=UNREGISTERED_DEFAULT, enforced=False,
            default_applied=True, error="tenant_unresolved",
        ))

    fresh = await _cache._sync(session)
    if not fresh:
        # Freshness unknown. Answer with the namespace default and say so,
        # rather than serving a cache that may be stale or raising into a PEP
        # that would have to invent a policy.
        return _finish(Decision(
            effect=UNREGISTERED_DEFAULT, enforced=False,
            default_applied=True, error="policy_store_unavailable",
        ))

    try:
        namespaces = await _cache.namespaces(session)
        spec = namespaces.get(namespace)
        policies = await _cache.policies(
            session, tenant_id=tenant_uuid, namespace=namespace, catalog=catalog,
        )
    except Exception as exc:                                   # noqa: BLE001
        log.error("hxarbiter: could not load the policy set for %s: %s",
                  namespace, exc)
        return _finish(Decision(
            effect=UNREGISTERED_DEFAULT, enforced=False,
            default_applied=True, error="policy_load_failed",
        ))

    default_effect = spec.default_effect if spec else UNREGISTERED_DEFAULT
    ceiling = spec.max_mode if spec else PolicyMode.SHADOW

    needed: set[str] = set()
    for p in policies:
        needed |= set(p.required_attrs)
    attrs, pip_error = await _resolve_attributes(req, resolvers, needed)

    decision = Evaluator().evaluate(
        policies=policies, attrs=attrs,
        namespace_default=default_effect, namespace_max_mode=ceiling,
    )
    decision.error = pip_error
    _finish(decision)

    effective_mode = clamp(
        max((p.mode for p in policies), key=_rank, default=PolicyMode.SHADOW),
        ceiling,
    )

    # ── retirement soak (§19.6) ──────────────────────────────────────────────
    # Strictly after the real decision is finished and strictly without
    # touching it: `decision` is already built above, and nothing below may
    # alter its effect, its matched list or its mode. A retired rule answers
    # one question — what would this still have caught — and an operator reads
    # the answer in the security-event log, not in an audit trail that would
    # then name a rule that was not in force.
    if record and not simulated:
        await _observe_soak(session, req, tenant_uuid, namespace, attrs,
                            catalog, resolvers)

    _observe_metrics(req, decision, effective_mode, legacy_effect)

    if record:
        try:
            allow_rate, default_rate = await store.namespace_sampling(
                session, namespace)
            if _keep(decision, allow_rate, default_rate, legacy_effect):
                decision.decision_id = await store.write_decision(
                    session,
                    req=req,
                    tenant_uuid=tenant_uuid,
                    decision=decision,
                    mode=effective_mode,
                    redacted_context=_redact(req.context),
                    policy_set=store.policy_set_hash(policies),
                    legacy_effect=legacy_effect,
                    request_id=request_id,
                    claimed_by=claimed_by,
                    simulated=simulated,
                )
        except Exception as exc:                               # noqa: BLE001
            # The log is evidence, but losing a row must not deny a request that
            # policy allowed. Loud, and not fatal.
            log.error("hxarbiter: decision could not be recorded: %s", exc)

    return decision


async def _observe_soak(
    session: AsyncSession,
    req: DecisionRequest,
    tenant_uuid,
    namespace: str,
    attrs: dict[str, Any],
    catalog: AttributeCatalog,
    resolvers: Sequence[AttributeResolver],
) -> None:
    """Record what a recently retired policy WOULD have caught.

    Swallows everything. This is observation of a rule nobody is enforcing; a
    failure here must never affect a decision that has already been made, and
    the only cost of losing one of these is a missing line in a report.
    """
    try:
        soaking = await _cache.soaking(
            session, tenant_id=tenant_uuid, namespace=namespace, catalog=catalog)
        if not soaking:
            return

        # A soaking policy's attributes are NOT already resolved. `decide`
        # resolves exactly the paths the LIVE set reads, and a retired policy
        # is by definition not in it — so on an install whose only policy in
        # the namespace has just been retired, `attrs` is empty and the rule
        # would silently never match. That would have made the whole soak look
        # like it worked while observing nothing.
        needed: set[str] = set()
        for cp in soaking:
            needed |= set(cp.required_attrs)
        missing = needed - set(attrs)
        soak_attrs = dict(attrs)
        if missing:
            extra, _err = await _resolve_attributes(req, resolvers, missing)
            soak_attrs.update(extra)

        shadow = Evaluator().evaluate(
            policies=soaking, attrs=soak_attrs,
            # The namespace default is NOT applied: "nothing matched" is not
            # something a retired rule caught, and recording the default here
            # would make every request look like a hit.
            namespace_default=Effect.ALLOW,
            namespace_max_mode=PolicyMode.SHADOW)
        if not shadow.matched:
            return

        from velaris_shared.db.models import SecurityEventModel

        session.add(SecurityEventModel(
            id=uuid.uuid4(),
            event_type="arbiter_retirement_soak",
            severity="info",
            user_id=str(req.subject.id)[:255],
            resource_type="policy_decision",
            resource_id=str(req.resource.id or "")[:255] or None,
            action=str(req.action)[:100],
            outcome="observed",
            details={
                "namespace": namespace,
                "would_have": str(shadow.effect),
                "matched": [m.model_dump(mode="json") for m in shadow.matched],
                "note": ("a policy retired within the soak window would have "
                         "matched this request. It changed nothing."),
            },
        ))
        await session.flush()
    except Exception as exc:                                   # noqa: BLE001
        log.debug("hxarbiter: retirement-soak observation skipped: %s", exc)


def _observe_metrics(
    req: DecisionRequest,
    decision: Decision,
    mode: PolicyMode,
    legacy_effect: Effect | None,
) -> None:
    """Emit the counters. Never raises, and never sampled.

    Sampling thins the decision LOG because it is storage; metrics are counters
    and cost nothing to keep whole, so a namespace's true deny rate is visible
    even where the log only keeps a tenth of the allows. A metric that inherits
    the log's sampling would quietly under-report the thing it exists to alert
    on.
    """
    try:
        from velaris_shared.observability import metrics as _m

        _m.arbiter_decisions_total.labels(
            tenant_id=str(req.tenant_id), namespace=req.namespace,
            effect=str(decision.effect),
            enforced=str(bool(decision.enforced)).lower()).inc()
        _m.arbiter_decision_seconds.labels(
            namespace=req.namespace).observe(
                (decision.latency_us or 0) / 1_000_000)
        if decision.error:
            _m.arbiter_decision_errors_total.labels(
                namespace=req.namespace, error=decision.error).inc()
        if legacy_effect is not None and legacy_effect is not decision.effect:
            direction = ("engine_weaker" if decision.effect is Effect.ALLOW
                         else "engine_stricter")
            _m.arbiter_legacy_disagreements_total.labels(
                namespace=req.namespace, direction=direction).inc()
    except Exception:                                          # noqa: BLE001
        # Telemetry must never be able to affect a decision that has already
        # been made.
        pass


def _rank(mode: PolicyMode) -> int:
    return {PolicyMode.SHADOW: 0, PolicyMode.DRY_RUN: 1, PolicyMode.ENFORCE: 2}[mode]
