"""HxArbiter ARB-I — policy upkeep. What a live policy set is quietly doing wrong.

ARB-A…G answer "is this rule correct when it is written" and "does the engine
agree with the control it replaces". Neither answers the question that arrives
three months later: **a policy set nobody has touched since activation drifts,
and nothing in the platform was looking.**

Everything in this module is deterministic and read-only. It performs no
writes, changes no decision, and needs no AI — the model in `nlp/advisor.py`
only ever *narrates* what is computed here. That order is deliberate: a finding
an LLM produced is a suggestion, and a finding this file produces is a fact
about the database. Mixing them would make the whole report a suggestion.

Nine checks, and the three worth naming:

* **`stale_attribute`** is the one that silently disables a control.
  `store.load_active_policies` skips a version that no longer compiles and logs
  it — correct at decision time (refusing every decision in the namespace would
  be worse) but it means an upgrade that drops an attribute path turns a policy
  off with nothing on any screen saying so. This check recompiles every active
  version against the *current* catalog and says which controls are not being
  applied.
* **`cross_policy_*`** runs `analyze()` over the whole live set in a namespace.
  `analyze()` has only ever been run over one draft plus the active set; two
  policies activated separately, months apart, have never been compared with
  each other. Rule ids are prefixed with their policy before analysis — ids are
  unique only within a policy, and `analyze()` skips a pair sharing an id, so
  without the prefix the most likely collision (two copies of the same rule,
  the very thing worth reporting) would be the one silently dropped.
* **`dormant_rule`** is the one most likely to be believed too readily, so it is
  the one hedged hardest. A rule that never appears in the decision log is NOT
  proof it never fired:
    - allow-effect matches are recorded on `allow` decisions, which the
      namespace's `sample_allow_rate` thins;
    - the evaluator returns `matched=denies` on a deny, so an allow rule that
      matched alongside a deny leaves **no trace at all**;
    - a platform policy binds every tenant, and a tenant-scoped reader can only
      see its own decisions.
  Each of those sets `assessable=False` with the reason in words rather than
  quietly weakening the claim. **Nothing here ever proposes deleting a rule on
  dormancy alone**, and no path in this module retires anything.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Sequence

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from hxarbiter_core import (
    AttributeCatalog,
    PolicyCompiler,
    PolicyMode,
    PolicyVersion,
    Rule,
    analyze,
)
from velaris_shared.db.models import (
    ArbiterDecisionModel,
    ArbiterNamespaceModel,
    ArbiterPolicyModel,
    ArbiterPolicyVersionModel,
)

from case_service.core.timestamps import iso_utc

from . import chain as _chain
from . import parity as _parity
from .velaris_catalog import VELARIS_CATALOG

log = logging.getLogger(__name__)

#: Default look-back. Longer than the parity soak on purpose: parity asks
#: "does the engine agree today", upkeep asks "has this rule been earning its
#: place", and a fortnight is too short to conclude anything about the second.
DEFAULT_WINDOW_DAYS = 30

#: Most decision rows read into memory for the dormancy scan. Beyond this the
#: scan is narrowed to the most recent rows and the EFFECTIVE window is
#: reported, rather than the requested one — a report that silently examined
#: two days while its header said thirty is worse than one that says so.
MAX_DECISION_SCAN = 20_000

#: Below this, "no recorded match" means "nothing happened here", not "this rule
#: is unused". Dormancy is not reported at all under it.
MIN_DECISIONS_FOR_DORMANCY = 50

#: How long a mirror may go unregenerated before the parity report it feeds is
#: worth doubting.
BRIDGE_STALE_DAYS = 30

#: Unsealed rows older than this mean the sealer is not running. `chain.seal_once`
#: batches by the minute, so an hour is many missed batches, not a slow one.
UNSEALED_STALE_HOURS = 6

#: After this, a retired policy is simply old. Matches the parity soak, because
#: it is the same question — has enough time passed to draw a conclusion.
RETIREMENT_SOAK_DAYS = _parity.DEFAULT_SOAK_DAYS

Severity = str  # "error" | "warning" | "info"

_SEVERITY_RANK = {"error": 0, "warning": 1, "info": 2}


def _aware(value: datetime | None) -> datetime | None:
    """Both `timestamp with/without time zone` land here. Both hold UTC."""
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


@dataclass
class UpkeepFinding:
    """One thing about the live policy set that somebody should look at.

    `assessable` is not decoration. A finding whose evidence cannot support it
    is still worth showing — "this rule has no recorded match, and here is why
    that might mean nothing" is useful — but it must never be counted with the
    ones that are sound, and it must never be acted on automatically.
    """

    severity: Severity
    kind: str
    message: str
    #: What a person might do. Never done by this module, or by any other.
    suggestion: str = ""
    namespace: str | None = None
    policy_id: str | None = None
    policy_name: str | None = None
    scope: str | None = None                       # "platform" | "tenant"
    rule_ids: list[str] = field(default_factory=list)
    assessable: bool = True
    caveat: str | None = None
    evidence: dict[str, Any] = field(default_factory=dict)
    #: Assigned once, when the report is assembled. It is NOT derived purely
    #: from the content: two cross-policy conflicts in one namespace whose rules
    #: happen to share ids produced the same key, and the advisor stores its
    #: entries in a dict — so one finding's advice was silently rendered against
    #: another. Seen live: `cross_policy_conflict:ai:r1,r1`.
    seq: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.finding_id,
            "severity": self.severity,
            "kind": self.kind,
            "message": self.message,
            "suggestion": self.suggestion,
            "namespace": self.namespace,
            "policy_id": self.policy_id,
            "policy_name": self.policy_name,
            "scope": self.scope,
            "rule_ids": self.rule_ids,
            "assessable": self.assessable,
            "caveat": self.caveat,
            "evidence": self.evidence,
        }

    @property
    def finding_id(self) -> str:
        """Stable within one report, and the key the advisor answers against.

        The advisor's reply is matched back to these, so a model cannot invent
        a finding: an id that was not sent is dropped.
        """
        base = f"{self.kind}:{self.policy_id or self.namespace or '-'}" + (
            ":" + ",".join(self.rule_ids[:3]) if self.rule_ids else "")
        return f"{base}#{self.seq}"


@dataclass
class UpkeepReport:
    window_days: int
    since: datetime
    generated_at: datetime
    scope: str                                     # "tenant" | "platform"
    policies_examined: int
    namespaces_examined: int
    decisions_examined: int
    findings: list[UpkeepFinding] = field(default_factory=list)
    #: What this run could NOT check, in words. A clean report from a run that
    #: skipped half its checks reads exactly like a clean platform.
    limitations: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        counts = {"error": 0, "warning": 0, "info": 0}
        for f in self.findings:
            counts[f.severity] = counts.get(f.severity, 0) + 1
        return {
            "window_days": self.window_days,
            "since": iso_utc(self.since),
            "generated_at": iso_utc(self.generated_at),
            "scope": self.scope,
            "policies_examined": self.policies_examined,
            "namespaces_examined": self.namespaces_examined,
            "decisions_examined": self.decisions_examined,
            "counts": counts,
            "findings": [f.to_dict() for f in self.findings],
            "limitations": self.limitations,
        }


# ── the scan ─────────────────────────────────────────────────────────────────


async def upkeep_report(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    platform_scope: bool = False,
    window_days: int = DEFAULT_WINDOW_DAYS,
    catalog: AttributeCatalog | None = None,
) -> UpkeepReport:
    """Everything decidable about the policy set this caller can see.

    `platform_scope` widens the DECISION scan to every tenant, and nothing else.
    It is the platform operator's view, and it exists because a platform policy
    binds every tenant: judged from inside one, a platform rule that fires all
    day somewhere else looks dead.
    """
    catalog = catalog or VELARIS_CATALOG
    now = datetime.now(timezone.utc)
    since = now - timedelta(days=max(1, window_days))
    findings: list[UpkeepFinding] = []
    limitations: list[str] = []

    namespaces = {
        n.namespace: n
        for n in (await session.execute(
            select(ArbiterNamespaceModel).order_by(ArbiterNamespaceModel.namespace)
        )).scalars().all()
    }

    scope_clause = (
        ArbiterPolicyModel.tenant_id.is_(None)
        | (ArbiterPolicyModel.tenant_id == tenant_id)
    )
    policies = (await session.execute(
        select(ArbiterPolicyModel).where(scope_clause)
        .order_by(ArbiterPolicyModel.namespace, ArbiterPolicyModel.name)
    )).scalars().all()

    active = [p for p in policies if p.status == "active" and p.active_version]
    versions = await _active_versions(session, active)

    findings += _check_stale_attributes(active, versions, catalog)
    findings += _check_cross_policy(active, versions, namespaces)
    findings += _check_bridge_freshness(active, now)
    findings += _check_retirement_soak(policies, now)

    matched, scanned, effective_since = await _matched_rule_index(
        session,
        tenant_id=None if platform_scope else tenant_id,
        since=since,
    )
    if effective_since > since:
        limitations.append(
            f"the decision scan hit its {MAX_DECISION_SCAN:,}-row ceiling, so "
            f"dormancy was judged over the {(now - effective_since).days} day(s) "
            f"since {iso_utc(effective_since)}, not the {window_days} requested.")

    findings += _check_dormancy(
        active, versions, matched, namespaces,
        scanned=scanned, since=effective_since,
        platform_scope=platform_scope,
    )

    findings += await _check_namespace_traffic(
        session, tenant_id=tenant_id, namespaces=namespaces,
        active=active, since=since, platform_scope=platform_scope)

    findings += await _check_traffic_without_policy(
        session, tenant_id=tenant_id, namespaces=namespaces,
        active=active, since=since, platform_scope=platform_scope)

    findings += await _check_promotion_readiness(
        session, tenant_id=tenant_id, namespaces=namespaces,
        active=active, window_days=window_days)

    if platform_scope:
        findings += await _check_seal_health(session, now=now)
    else:
        # The chain spans every tenant — it has to, or one tenant's rewrite
        # would go unnoticed by the others' verification. That makes its health
        # a PLATFORM fact, and an unsealed-row count handed to a tenant admin
        # is a count of other tenants' decisions. Named, not silently omitted.
        limitations.append(
            "decision-chain health (seal backlog, chain verification) is a "
            "platform-wide fact and is not reported inside a single tenant. A "
            "platform operator's report includes it.")

    if not platform_scope and any(p.tenant_id is None for p in active):
        limitations.append(
            "platform-scoped policies bind every tenant on this deployment. "
            "Read from inside one tenant, their use cannot be judged — a "
            "platform operator sees the whole picture.")

    limitations.append(
        "a retired policy stops being evaluated immediately (`status='retired'` "
        "is filtered out of the active load), so the design's shadow soak after "
        "retirement is not running. Retirement is reported here; it is not "
        "observed.")

    findings.sort(key=lambda f: (_SEVERITY_RANK.get(f.severity, 3),
                                 not f.assessable, f.kind,
                                 f.policy_name or "", f.namespace or ""))
    # After the sort, so ids are stable for the order the caller is shown.
    for i, f in enumerate(findings):
        f.seq = i

    return UpkeepReport(
        window_days=window_days,
        since=since,
        generated_at=now,
        scope="platform" if platform_scope else "tenant",
        policies_examined=len(policies),
        namespaces_examined=len(namespaces),
        decisions_examined=scanned,
        findings=findings,
        limitations=limitations,
    )


async def _active_versions(
    session: AsyncSession, active: Sequence[ArbiterPolicyModel],
) -> dict[uuid.UUID, ArbiterPolicyVersionModel]:
    if not active:
        return {}
    pairs = {(p.id, p.active_version) for p in active}
    rows = (await session.execute(
        select(ArbiterPolicyVersionModel).where(
            ArbiterPolicyVersionModel.policy_id.in_([p.id for p in active]))
    )).scalars().all()
    return {r.policy_id: r for r in rows if (r.policy_id, r.version) in pairs}


def _rules_of(version: ArbiterPolicyVersionModel | None) -> list[Rule] | None:
    """Parsed rules, or None when the stored JSON no longer validates."""
    if version is None:
        return None
    try:
        return [Rule.model_validate(r) for r in (version.rules or [])]
    except Exception:                                          # noqa: BLE001
        return None


def _scope_of(policy: ArbiterPolicyModel) -> str:
    return "platform" if policy.tenant_id is None else "tenant"


# ── check 1: a control that is no longer being applied ───────────────────────


def _check_stale_attributes(
    active: Sequence[ArbiterPolicyModel],
    versions: dict[uuid.UUID, ArbiterPolicyVersionModel],
    catalog: AttributeCatalog,
) -> list[UpkeepFinding]:
    """Recompile every live version against today's catalog.

    The highest-severity check here, because its failure mode is silence. A
    version that stops compiling is skipped by `load_active_policies` and logged
    at ERROR — and a log line on a box nobody tails is not a control.
    """
    out: list[UpkeepFinding] = []
    compiler = PolicyCompiler(catalog)
    for policy in active:
        version = versions.get(policy.id)
        rules = _rules_of(version)
        if version is None:
            out.append(UpkeepFinding(
                severity="error", kind="active_version_missing",
                message=(f"policy {policy.name!r} points at version "
                         f"{policy.active_version}, which does not exist. It "
                         f"contributes nothing to any decision."),
                suggestion="Activate a version that exists, or retire the policy.",
                namespace=policy.namespace, policy_id=str(policy.id),
                policy_name=policy.name, scope=_scope_of(policy)))
            continue
        if rules is None:
            out.append(UpkeepFinding(
                severity="error", kind="stale_attribute",
                message=(f"policy {policy.name!r} v{version.version} holds rules "
                         f"that no longer parse. It is SKIPPED on every "
                         f"decision, so this control is not being applied."),
                suggestion=("Author a replacement version and activate it. The "
                            "stored rules are unchanged and still readable."),
                namespace=policy.namespace, policy_id=str(policy.id),
                policy_name=policy.name, scope=_scope_of(policy)))
            continue
        try:
            report = compiler.check_version(PolicyVersion(
                policy_id=policy.id, version=version.version, rules=rules,
                mode=PolicyMode(version.mode)))
        except Exception as exc:                               # noqa: BLE001
            report = None
            errors = [str(exc)]
        else:
            errors = list(report.errors)
        if report is None or not report.ok:
            out.append(UpkeepFinding(
                severity="error", kind="stale_attribute",
                message=(f"policy {policy.name!r} v{version.version} no longer "
                         f"compiles against the current attribute catalogue, so "
                         f"it is SKIPPED on every decision and this control is "
                         f"not being applied."),
                suggestion=("An attribute this policy names has been removed or "
                            "changed type. Rewrite the rule against a path that "
                            "still exists, then activate the new version."),
                namespace=policy.namespace, policy_id=str(policy.id),
                policy_name=policy.name, scope=_scope_of(policy),
                rule_ids=[r.id for r in rules],
                evidence={"errors": errors[:10]}))
    return out


# ── check 2: the whole live set, analysed against itself ─────────────────────


def _check_cross_policy(
    active: Sequence[ArbiterPolicyModel],
    versions: dict[uuid.UUID, ArbiterPolicyVersionModel],
    namespaces: dict[str, ArbiterNamespaceModel],
) -> list[UpkeepFinding]:
    """`analyze()` over every active rule in a namespace, across policies.

    Rule ids are prefixed with a short policy id first. Two reasons, and the
    second is the one that bites: ids are unique only within a policy, so a
    message naming `deny-external` would be ambiguous — and `analyze()` skips
    any pair whose ids are equal, which would silently drop the single most
    likely finding, two policies carrying the same rule.
    """
    out: list[UpkeepFinding] = []
    by_ns: dict[str, list[tuple[ArbiterPolicyModel, list[Rule]]]] = {}
    for policy in active:
        rules = _rules_of(versions.get(policy.id))
        if not rules:
            continue
        by_ns.setdefault(policy.namespace, []).append((policy, rules))

    for ns, entries in sorted(by_ns.items()):
        if len(entries) < 2 and len(entries[0][1]) < 2:
            continue
        owner: dict[str, tuple[ArbiterPolicyModel, str]] = {}
        prefixed: list[Rule] = []
        for policy, rules in entries:
            short = str(policy.id)[:8]
            for rule in rules:
                tag = f"{short}:{rule.id}"
                owner[tag] = (policy, rule.id)
                # `model_copy` rather than a re-validated construction: `id` is
                # capped at 64 characters and a prefixed id can exceed it. The
                # copy never leaves this function and is never stored.
                prefixed.append(rule.model_copy(update={"id": tag}))

        for finding in analyze(prefixed):
            policies_named = {owner[t][0].id for t in finding.rule_ids if t in owner}
            names = sorted({owner[t][0].name for t in finding.rule_ids if t in owner})
            cross = len(policies_named) > 1
            if not cross and finding.kind in ("conflict", "shadowed"):
                # Within one policy this is already reported at authoring time
                # by the draft analysis. Repeating it here is noise.
                continue
            # `analyze()` names the PREFIXED ids, which are an internal device
            # (see above) and meaningless to an operator: the live report read
            # "rules '423dfa76:r1' and '0d3ff8ce:r1'", asking a human to resolve
            # two UUID prefixes by hand. Rewritten to name policy and rule.
            message = finding.message
            for tag in finding.rule_ids:
                if tag in owner:
                    pol, rid = owner[tag]
                    message = message.replace(repr(tag), f"{rid!r} (in {pol.name!r})")
            out.append(UpkeepFinding(
                severity="warning" if finding.severity != "error" else "error",
                kind=f"cross_policy_{finding.kind}" if cross else finding.kind,
                message=(message
                         + (f" Policies involved: {', '.join(names)}."
                            if cross else "")),
                suggestion=(
                    "Two separately-authored policies now overlap. Decide which "
                    "one owns the case and narrow the other — deny-overrides "
                    "means the redundant rule is not harmless, it is invisible."
                    if cross else
                    "Rewrite the rule so it can fire."),
                namespace=ns,
                policy_id=(str(next(iter(policies_named)))
                           if len(policies_named) == 1 else None),
                policy_name=names[0] if len(names) == 1 else None,
                scope=None,
                rule_ids=[owner[t][1] for t in finding.rule_ids if t in owner],
                evidence={"policies": names,
                          "namespace_default": (
                              namespaces[ns].default_effect
                              if ns in namespaces else None)}))
    return out


# ── check 3: dormancy, hedged ────────────────────────────────────────────────


async def _matched_rule_index(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID | None,
    since: datetime,
) -> tuple[set[tuple[str, str]], int, datetime]:
    """(policy_id, rule_id) pairs seen in the log, how many rows, from when.

    Read in Python rather than with a JSON predicate: `matched` is a portable
    JSON column and the three supported engines spell containment three
    different ways, with an index on none of them. The row cap is what keeps
    that honest.
    """
    where = [
        ArbiterDecisionModel.evaluated_at >= since,
        ArbiterDecisionModel.simulated.is_(False),
    ]
    if tenant_id is not None:
        where.append(ArbiterDecisionModel.tenant_id == tenant_id)

    rows = (await session.execute(
        select(ArbiterDecisionModel.matched, ArbiterDecisionModel.evaluated_at)
        .where(*where)
        .order_by(ArbiterDecisionModel.evaluated_at.desc())
        .limit(MAX_DECISION_SCAN)
    )).all()

    seen: set[tuple[str, str]] = set()
    oldest = since
    for matched, evaluated_at in rows:
        for m in (matched or []):
            if isinstance(m, dict) and m.get("policy_id") and m.get("rule_id"):
                seen.add((str(m["policy_id"]), str(m["rule_id"])))
    if len(rows) >= MAX_DECISION_SCAN and rows:
        oldest = _aware(rows[-1][1]) or since
    return seen, len(rows), oldest


def _check_dormancy(
    active: Sequence[ArbiterPolicyModel],
    versions: dict[uuid.UUID, ArbiterPolicyVersionModel],
    matched: set[tuple[str, str]],
    namespaces: dict[str, ArbiterNamespaceModel],
    *,
    scanned: int,
    since: datetime,
    platform_scope: bool,
) -> list[UpkeepFinding]:
    if scanned < MIN_DECISIONS_FOR_DORMANCY:
        # Not "everything is dormant" — nothing happened. Reporting every rule
        # on a quiet install would train an operator to ignore the whole tab.
        return []

    out: list[UpkeepFinding] = []
    for policy in active:
        rules = _rules_of(versions.get(policy.id))
        if not rules:
            continue
        ns = namespaces.get(policy.namespace)
        allow_rate = float(ns.sample_allow_rate) if ns else 1.0
        platform_policy = policy.tenant_id is None

        for rule in rules:
            if (str(policy.id), rule.id) in matched:
                continue

            caveats: list[str] = []
            assessable = True
            if str(rule.effect) == "allow":
                caveats.append(
                    "an allow rule that matched alongside a deny leaves no "
                    "trace: the evaluator records only the denies that caused "
                    "the outcome")
                assessable = False
                if allow_rate < 1.0:
                    caveats.append(
                        f"this namespace samples allow decisions at "
                        f"{allow_rate:g}, so unremarkable matches are thinned "
                        f"out of the log by design")
            if platform_policy and not platform_scope:
                caveats.append(
                    "this is a platform policy and binds every tenant; only "
                    "this tenant's decisions were scanned")
                assessable = False

            out.append(UpkeepFinding(
                severity="info" if not assessable else "warning",
                kind="dormant_rule",
                message=(f"rule {rule.id!r} in {policy.name!r} has no recorded "
                         f"match since {iso_utc(since)}."),
                suggestion=(
                    "If the situation it guards genuinely no longer arises, "
                    "propose a version without it — and keep the namespace in "
                    "its current mode when you activate, because a version "
                    "landing in `shadow` stops the rest of the policy "
                    "enforcing too. Never remove a rule on this signal alone."),
                namespace=policy.namespace, policy_id=str(policy.id),
                policy_name=policy.name, scope=_scope_of(policy),
                rule_ids=[rule.id],
                assessable=assessable,
                caveat="; ".join(caveats) or None,
                evidence={"effect": str(rule.effect),
                          "decisions_scanned": scanned,
                          "sample_allow_rate": allow_rate}))
    return out


# ── check 4: is anything reaching this namespace at all ──────────────────────


async def _check_namespace_traffic(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    namespaces: dict[str, ArbiterNamespaceModel],
    active: Sequence[ArbiterPolicyModel],
    since: datetime,
    platform_scope: bool,
) -> list[UpkeepFinding]:
    """A namespace with policies and no traffic, or traffic and no matches.

    Both mean the same thing from an auditor's chair — the control exists on a
    screen and has never applied to anything — and they have different causes:
    the first is usually a PEP that was never wired, the second a rule set that
    never fits the requests actually arriving.
    """
    out: list[UpkeepFinding] = []
    with_policies = {p.namespace for p in active}
    for ns in sorted(with_policies):
        where = [
            ArbiterDecisionModel.namespace == ns,
            ArbiterDecisionModel.evaluated_at >= since,
            ArbiterDecisionModel.simulated.is_(False),
        ]
        if not platform_scope:
            where.append(ArbiterDecisionModel.tenant_id == tenant_id)

        total = (await session.execute(
            select(func.count()).select_from(ArbiterDecisionModel).where(*where)
        )).scalar_one()
        if total == 0:
            out.append(UpkeepFinding(
                severity="warning", kind="namespace_unexercised",
                message=(f"namespace {ns!r} has active policies and not one "
                         f"recorded decision since {iso_utc(since)}."),
                suggestion=("Either nothing in the platform asks about this "
                            "namespace — check the PEP is wired at the call "
                            "site — or it genuinely never happens. A promotion "
                            "on this evidence would be a green light earned by "
                            "silence."),
                namespace=ns,
                evidence={"decisions": 0}))
            continue

        defaulted = (await session.execute(
            select(func.count()).select_from(ArbiterDecisionModel).where(
                *where, ArbiterDecisionModel.default_applied.is_(True))
        )).scalar_one()
        if defaulted == total and total >= MIN_DECISIONS_FOR_DORMANCY:
            out.append(UpkeepFinding(
                severity="warning", kind="namespace_always_default",
                message=(f"every one of {total} decisions in namespace {ns!r} "
                         f"fell through to the namespace default. No policy in "
                         f"it has ever matched a real request."),
                suggestion=("The rules do not fit the requests arriving. Open a "
                            "decision and compare its attributes with the "
                            "conditions — the Simulate tab replays the same "
                            "evaluator."),
                namespace=ns,
                evidence={"decisions": total, "default_applied": defaulted,
                          "default_effect": (namespaces[ns].default_effect
                                             if ns in namespaces else None)}))
    return out


# ── check 4b: traffic arriving where no policy exists ────────────────────────


async def _check_traffic_without_policy(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    namespaces: dict[str, ArbiterNamespaceModel],
    active: Sequence[ArbiterPolicyModel],
    since: datetime,
    platform_scope: bool,
) -> list[UpkeepFinding]:
    """Decisions in a namespace this caller has NO active policy in.

    The check that had to be added after running the report against the live
    box: five policies, all retired, 129 decisions still arriving and every one
    of them answered by the namespace default — and the report said "nothing to
    report". Every other check here iterates namespaces that HAVE active
    policies, so an install with none was, by construction, invisible to all of
    them. A clean report on a policy engine that is enforcing nothing is the
    worst output this module could produce.

    Also catches a namespace nobody registered. `pdp` answers those with
    `UNREGISTERED_DEFAULT` (deny) — safe, and a sign that a PEP is naming an
    action whose namespace was never added to `arbiter_namespaces`, which is
    usually a typo at a call site.
    """
    out: list[UpkeepFinding] = []
    have_policies = {p.namespace for p in active}

    where = [
        ArbiterDecisionModel.evaluated_at >= since,
        ArbiterDecisionModel.simulated.is_(False),
    ]
    if not platform_scope:
        where.append(ArbiterDecisionModel.tenant_id == tenant_id)

    rows = (await session.execute(
        select(ArbiterDecisionModel.namespace, func.count())
        .where(*where).group_by(ArbiterDecisionModel.namespace)
    )).all()

    for ns, count in rows:
        if ns not in namespaces:
            out.append(UpkeepFinding(
                severity="warning", kind="unregistered_namespace",
                message=(f"{count} decision(s) were asked about namespace "
                         f"{ns!r}, which is not registered. Every one of them "
                         f"was answered with the unregistered default (deny) "
                         f"and no policy could ever apply."),
                suggestion=("Usually a typo in a PEP's action string — the "
                            "namespace is the part before the first dot. Either "
                            "correct the call site or register the namespace."),
                namespace=ns,
                evidence={"decisions": count}))
            continue
        if ns in have_policies:
            continue
        out.append(UpkeepFinding(
            severity="warning", kind="namespace_no_active_policy",
            message=(f"{count} decision(s) arrived in namespace {ns!r} and this "
                     f"scope has no active policy in it. Every answer came from "
                     f"the namespace default ({namespaces[ns].default_effect})."),
            suggestion=("If policies here were retired or never activated, the "
                        "control an operator believes is in place is not. If the "
                        "default is the intended posture, nothing is wrong — but "
                        "it should be a decision, not an accident."),
            namespace=ns,
            evidence={"decisions": count,
                      "default_effect": namespaces[ns].default_effect,
                      "max_mode": namespaces[ns].max_mode}))
    return out


# ── check 5: a promotion nobody made ─────────────────────────────────────────


async def _check_promotion_readiness(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    namespaces: dict[str, ArbiterNamespaceModel],
    active: Sequence[ArbiterPolicyModel],
    window_days: int,
) -> list[UpkeepFinding]:
    """Shadow is the safe state, not the finished one.

    Reported as `info`, never higher. Staying in shadow is a legitimate
    standing choice, and an upkeep tab that nags about it in red teaches
    operators to promote to silence the screen — which is the opposite of what
    the promotion gate is for.
    """
    out: list[UpkeepFinding] = []
    for ns in sorted({p.namespace for p in active}):
        spec = namespaces.get(ns)
        if spec is None or spec.max_mode != "shadow":
            continue
        report = await _parity.parity_report(
            session, tenant_id=tenant_id, namespace=ns,
            window_days=min(window_days, 365))
        if not report.eligible:
            continue
        out.append(UpkeepFinding(
            severity="info", kind="promotion_available",
            message=(f"namespace {ns!r} is still in shadow and its parity is "
                     f"clean: {report.compared} compared decisions, no "
                     f"disagreements, no errors."),
            suggestion=("Nothing here is wrong. If enforcement is the intent, "
                        "the evidence now supports it — promote on the "
                        "Namespaces tab. Backing out is one call and is never "
                        "gated."),
            namespace=ns,
            evidence={"compared": report.compared,
                      "agreement_rate": round(report.agreement_rate, 6)}))
    return out


# ── check 6: the oracle behind the parity report ─────────────────────────────


def _check_bridge_freshness(
    active: Sequence[ArbiterPolicyModel], now: datetime,
) -> list[UpkeepFinding]:
    """A legacy mirror that has not been regenerated is a stale oracle.

    Parity measures the engine against these policies. If the legacy setting
    they mirror has changed since the last `bridge/sync`, parity is comparing
    the engine with what the platform used to do — and a promotion would rest
    on it.
    """
    out: list[UpkeepFinding] = []
    for policy in active:
        if policy.origin != "legacy_bridge":
            continue
        updated = _aware(policy.updated_at)
        if updated is None:
            continue
        age = (now - updated).days
        if age < BRIDGE_STALE_DAYS:
            continue
        out.append(UpkeepFinding(
            severity="warning", kind="bridge_stale",
            message=(f"the legacy mirror {policy.name!r} has not been "
                     f"regenerated for {age} days."),
            suggestion=("Run `POST /arbiter/bridge/sync` (Namespaces tab). It "
                        "is content-addressed, so it appends nothing if the "
                        "legacy setting is unchanged — and if it does append, "
                        "the parity report was being measured against a stale "
                        "oracle."),
            namespace=policy.namespace, policy_id=str(policy.id),
            policy_name=policy.name, scope=_scope_of(policy),
            evidence={"days_since_sync": age}))
    return out


# ── check 7: retirement ──────────────────────────────────────────────────────


def _check_retirement_soak(
    policies: Sequence[ArbiterPolicyModel], now: datetime,
) -> list[UpkeepFinding]:
    out: list[UpkeepFinding] = []
    for policy in policies:
        retired = _aware(policy.retired_at)
        if policy.status != "retired" or retired is None:
            continue
        age = (now - retired).days
        if age < RETIREMENT_SOAK_DAYS:
            continue
        out.append(UpkeepFinding(
            severity="info", kind="retirement_soak_elapsed",
            message=(f"policy {policy.name!r} was retired {age} days ago and "
                     f"has stopped being evaluated. Its versions and their "
                     f"activation intervals are kept."),
            suggestion=("Nothing to do. Retirement is not deletion here — the "
                        "history stays, so \"which rule was live on 3 March\" "
                        "still has an answer."),
            namespace=policy.namespace, policy_id=str(policy.id),
            policy_name=policy.name, scope=_scope_of(policy),
            evidence={"days_retired": age,
                      "retired_at": iso_utc(retired)}))
    return out


# ── check 8: the log is only evidence while it is sealed ─────────────────────


async def _check_seal_health(
    session: AsyncSession, *, now: datetime,
) -> list[UpkeepFinding]:
    """Platform-wide, and therefore only run for a platform operator.

    The chain is one across every tenant, so its health cannot be narrowed to
    a tenant — and an unsealed-row count is a count of decisions the caller may
    not read. Called only under `platform_scope`; a tenant report says in its
    limitations that this was not checked rather than leaving the omission to
    be discovered.

    A verification failure is an `error` and nothing else in this file
    outranks it: the decision log's whole claim is that it cannot be edited
    without showing.
    """
    out: list[UpkeepFinding] = []
    try:
        ok, problem = await _chain.verify_chain(session)
    except Exception as exc:                                   # noqa: BLE001
        log.warning("hxarbiter upkeep: chain verification failed to run: %s", exc)
        ok, problem = True, None
    if not ok:
        out.append(UpkeepFinding(
            severity="error", kind="chain_broken",
            message=f"the decision chain does not verify: {problem}",
            suggestion=("Stop and investigate. A broken chain means sealed "
                        "decisions no longer hash to their recorded content, "
                        "which is either tampering or data loss."),
            evidence={"problem": problem}))

    cutoff = now - timedelta(hours=UNSEALED_STALE_HOURS)
    stale = (await session.execute(
        select(func.count()).select_from(ArbiterDecisionModel).where(
            ArbiterDecisionModel.chain_sequence.is_(None),
            ArbiterDecisionModel.evaluated_at < cutoff)
    )).scalar_one()
    if stale:
        out.append(UpkeepFinding(
            severity="warning", kind="unsealed_backlog",
            message=(f"{stale} decision(s) older than {UNSEALED_STALE_HOURS}h "
                     f"have never been sealed into the chain."),
            suggestion=("The sealer batches by the minute, so this is many "
                        "missed batches. Check the sealer task is running, or "
                        "call `POST /arbiter/decisions/seal` — it is "
                        "idempotent. Until they are sealed those rows are not "
                        "tamper-evident."),
            evidence={"unsealed": stale,
                      "older_than_hours": UNSEALED_STALE_HOURS}))
    return out


def findings_for_advice(findings: Iterable[UpkeepFinding]) -> list[dict[str, Any]]:
    """What the model is allowed to see (§19.5).

    Kinds, counts, policy names and rule ids — all operator-authored or
    structural. **No subject, no resource, no decision row, no tenant data of
    any kind**, and no free-text context. If a field here ever needs to come
    from `arbiter_decisions`, it does not belong in this list.
    """
    return [
        {
            "id": f.finding_id,
            "kind": f.kind,
            "severity": f.severity,
            "namespace": f.namespace,
            "policy_name": f.policy_name,
            "rule_ids": f.rule_ids[:10],
            "assessable": f.assessable,
            "message": f.message,
            "caveat": f.caveat,
            "evidence": {k: v for k, v in f.evidence.items() if k != "errors"},
        }
        for f in findings
    ]
