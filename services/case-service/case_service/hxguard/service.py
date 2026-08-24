"""HxGuard Phase A — the policy decision point.

Design rules (report §3.2, all three SPOF mitigations are requirements):
  - FAIL-CLOSED: unknown action, unknown subject kind, or any evaluator
    exception → deny. There is no permissive default anywhere in this module.
  - Short-TTL decision cache (in-process) with explicit invalidation on
    role/group/grant mutations — availability mitigation + per-request cost.
  - Every DENY is a SecurityEvent (HxShield-visible); allows are counted,
    not logged.

The backend is a swappable protocol: Phase A ships RbacBackend (wrapping
require_role semantics + namespace_grants); Phase C may swap in OpenFGA
behind the same check() signature.
"""
from __future__ import annotations

import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Protocol

from fastapi import Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from case_service.auth.dependencies import get_current_user
from case_service.auth.models import AuthenticatedUser
from case_service.db.session import get_session

log = logging.getLogger(__name__)

CACHE_TTL_SECONDS = 30.0


@dataclass(frozen=True)
class Subject:
    """Who is asking. kind mirrors case_vars.CallerContext kinds plus 'user'."""
    kind: str                      # user | connector | devconn | rules | module
    id: str
    roles: tuple[str, ...] = ()
    is_admin: bool = False
    #: Access-group privileges written in the platform-action namespace. Part of
    #: the frozen identity on purpose: `_cache_key` hashes the whole Subject, so
    #: a privilege that is not here would be a decision input the cache cannot
    #: see — the classic way a permission change appears to take effect for one
    #: user and not another.
    privileges: tuple[tuple[tuple[str, Any], ...], ...] = ()
    #: Phase 6 role-store generation, for the same reason as `privileges` above
    #: and one step less obvious. The role → privilege mapping is resolved INSIDE
    #: the evaluator, so when an admin edits a role nothing about the subject
    #: changes — the cache key would be identical and every worker would keep
    #: serving the old answer for the rest of the TTL. Stamping the generation
    #: makes an edit invalidate exactly the entries it should.
    role_store_generation: int = 0


@dataclass(frozen=True)
class Decision:
    allow: bool
    reason: str
    source: str                    # which backend/rule decided
    cached: bool = False


class AuthzBackend(Protocol):
    name: str
    async def evaluate(
        self, session: AsyncSession, subject: Subject,
        action: str, resource: dict[str, Any] | None,
    ) -> Decision: ...


# ── RBAC backend (Phase A) ───────────────────────────────────────────

#: action → roles that may perform it. Mirrors the require_role() semantics
#: it replaces on the pilot surfaces: superadmin and is_admin always pass,
#: empty set = any *authenticated* subject of kind "user".
#: An action ABSENT from this registry (and not grant-backed) is DENIED.
ACTION_ROLES: dict[str, set[str]] = {
    # ONE entry left, and it is not a role decision at all.
    #
    # Phase 5 (2026-08-15) moved the other 14 into the privilege registry
    # (`actions.py`), where `check()` now routes them — they were `action ->
    # role set` tables, the same shape Phase 0 inventoried everywhere else.
    # Their rows are DELETED here rather than left behind: two tables claiming
    # to decide the same action is how a ledger drifts from the code, and the
    # dead one is always the one someone edits.
    #
    # `case.rule.apply` stays because its role set is EMPTY — "any authenticated
    # user". A privilege model cannot express that: a subject with no roles
    # holds no privileges, so registering it would silently close an open
    # surface. The decision here is authentication, not authorization.
    "case.rule.apply":               set(),     # any authenticated user
}

#: grant-backed actions: resolved against namespace_grants, not roles.
GRANT_ACTIONS = {
    "namespace.write": "write",
    "namespace.read":  "read",
}


class RbacBackend:
    name = "rbac"

    async def evaluate(
        self, session: AsyncSession, subject: Subject,
        action: str, resource: dict[str, Any] | None,
    ) -> Decision:
        # grant-backed actions (namespace capabilities) — any subject kind
        if action in GRANT_ACTIONS:
            return await self._evaluate_grant(session, subject, action, resource)

        # role-backed actions — user subjects only
        roles = ACTION_ROLES.get(action)
        if roles is None:
            return Decision(False, f"unknown action '{action}' (fail-closed)", self.name)
        if subject.kind != "user":
            return Decision(False, f"action '{action}' requires a user subject, got {subject.kind}", self.name)
        if not roles:
            # The only remaining entry. An admin bypass above this line would be
            # dead code: everyone authenticated already passes.
            return Decision(True, "any authenticated user", self.name)
        if "superadmin" in subject.roles:
            return Decision(True, "superadmin break-glass", self.name)
        matched = roles & set(subject.roles)
        if matched:
            return Decision(True, f"role {sorted(matched)[0]}", self.name)
        return Decision(False, f"requires one of {sorted(roles)}", self.name)

    async def _evaluate_grant(
        self, session: AsyncSession, subject: Subject,
        action: str, resource: dict[str, Any] | None,
    ) -> Decision:
        namespace_id = (resource or {}).get("namespace_id")
        if namespace_id is None:
            return Decision(False, f"'{action}' requires resource.namespace_id (fail-closed)", self.name)
        from sqlalchemy import select
        from case_service.db.models import NamespaceGrantModel
        row = (await session.execute(
            select(NamespaceGrantModel.id)
            .where(NamespaceGrantModel.namespace_id == namespace_id)
            .where(NamespaceGrantModel.grantee_type == subject.kind)
            .where(NamespaceGrantModel.grantee_ref.in_((subject.id, "*")))
            .where(NamespaceGrantModel.capability == GRANT_ACTIONS[action])
            .limit(1)
        )).scalar_one_or_none()
        if row is not None:
            return Decision(True, f"namespace grant ({GRANT_ACTIONS[action]})", self.name)
        return Decision(
            False,
            f"no {GRANT_ACTIONS[action]} grant for {subject.kind} '{subject.id}' on namespace {namespace_id}",
            self.name,
        )


# ── ReBAC backend (Phase B — case relationships) ─────────────────────

#: action → relations that satisfy it. owner (created_by) and admin/manager
#: roles are derived at decision time, never stored as tuples.
CASE_ACTIONS: dict[str, set[str]] = {
    "case.read":   {"assignee", "editor", "viewer"},
    "case.update": {"assignee", "editor"},
    "case.share":  {"editor"},
    "meet.start":  {"assignee", "editor"},   # HxMeet: start/end a live session = case work
    "meet.join":   {"assignee", "editor", "viewer"},  # HxMeet P2: join an embedded session in-tab
    "meet.recording.view": {"assignee", "editor"},    # HxMeet P3: download/verify a sealed recording
    "messages.read":  {"assignee", "editor", "viewer"},  # Portal v2 P4: read the case thread
    "messages.write": {"assignee", "editor"},            # Portal v2 P4: post to the case thread
    "meet.intelligence.run": {"assignee", "editor"},     # HxMeet P4a: analyze a sealed recording
    "docs.verify":    {"assignee", "editor"},            # HxMeet P4b: record a document verification
    "meet.kyc.run":   {"assignee", "editor"},            # HxMeet P4c: issue liveness challenges / run the signal pass
    "cases.ask":      {"assignee", "editor", "viewer"},  # HxNexus case Q&A: read-derived
    "stt.transcribe": {"assignee", "editor", "viewer"},  # Voice input: dictate into a case surface
    "voicefill.extract": {"assignee", "editor"},         # Voice input P3: propose form values (write-prep)
}


class RebacBackend:
    name = "rebac"

    async def evaluate(
        self, session: AsyncSession, subject: Subject,
        action: str, resource: dict[str, Any] | None,
    ) -> Decision:
        relations = CASE_ACTIONS.get(action)
        if relations is None:
            return Decision(False, f"unknown case action '{action}' (fail-closed)", self.name)
        case_id = (resource or {}).get("case_id")
        if case_id is None:
            return Decision(False, f"'{action}' requires resource.case_id (fail-closed)", self.name)
        if subject.kind != "user":
            return Decision(False, f"case actions require a user subject, got {subject.kind}", self.name)
        # Phase 5 (2026-08-15): was `superadmin or is_admin or "manager" in roles`.
        # That bypass ran before a single relationship was consulted, so "manager
        # sees every case in the deployment" was unrevocable and appeared in no
        # gate, no registry and no screen. It is now a named privilege, seeded to
        # admin+manager so today's answer is unchanged — but revocable, and
        # visible in the action catalogue like everything else.
        if "superadmin" in subject.roles:
            return Decision(True, "superadmin break-glass", self.name)
        if subject_allows(subject, "casework.records.any"):
            return Decision(True, "privilege: casework.records.any", self.name)

        from sqlalchemy import select
        from case_service.db.models import CaseInstanceModel
        row = (await session.execute(
            select(CaseInstanceModel.created_by, CaseInstanceModel.case_type_id)
            .where(CaseInstanceModel.id == case_id)
        )).first()
        if row is None:
            return Decision(False, "case not found (fail-closed)", self.name)
        created_by, case_type_id = row

        if created_by and str(created_by) == subject.id:
            return Decision(True, "owner (created_by)", self.name)

        from .tuples import has_tuple
        rel = await has_tuple(
            session, object_type="case", object_id=case_id,
            relations=relations, subject_type="user", subject_id=subject.id,
        )
        if rel:
            return Decision(True, f"relationship: {rel}", self.name)

        # Access-group parity (cutover safety, user-approved): a user whose
        # active access group allows this case TYPE keeps read/update access.
        # NEVER applies to case.share — type-scoped operators must not be
        # able to spread access to users outside their group (approved
        # design: share = owner / editor / admin only).
        if action != "case.share" and await self._access_group_allows(
            session, subject.id, case_type_id,
        ):
            return Decision(True, "access-group case-type scope", self.name)

        return Decision(
            False,
            f"no relationship ({sorted(relations)}), not owner, "
            "and no access-group scope for this case type",
            self.name,
        )

    async def _access_group_allows(
        self, session: AsyncSession, user_id: str, case_type_id,
    ) -> bool:
        from sqlalchemy import select
        from case_service.db.models import AccessGroupModel, OperatorAccessGroupModel
        rows = (await session.execute(
            select(AccessGroupModel.allowed_case_type_ids)
            .join(OperatorAccessGroupModel,
                  OperatorAccessGroupModel.access_group_id == AccessGroupModel.id)
            .where(OperatorAccessGroupModel.operator_id == user_id)
            .where(AccessGroupModel.is_active == True)  # noqa: E712
        )).scalars().all()
        for allowed in rows:
            if "*" in (allowed or []) or str(case_type_id) in (allowed or []):
                return True
        return False


class PrivilegeAdapter:
    """Routes platform actions to the Phase-1 privilege evaluator.

    A thin adapter rather than a rewrite: `privileges.PrivilegeBackend` is a
    pure function of (subject, action) with no session and no I/O, which is what
    let the Phase-1 parity table prove it exhaustively. Keeping it that way and
    adapting here preserves that proof.
    """

    name = "privilege"

    async def evaluate(
        self, session: AsyncSession, subject: Subject,
        action: str, resource: dict[str, Any] | None,
    ) -> Decision:
        from case_service.hxguard.privileges import PrivilegeBackend

        class _S:
            kind = subject.kind
            roles = list(subject.roles)
            is_admin = subject.is_admin
            privileges = _thaw_privileges(subject.privileges)

        return PrivilegeBackend().evaluate(_S(), action, resource)


_backend: AuthzBackend = RbacBackend()
_rebac_backend: AuthzBackend = RebacBackend()
_privilege_backend: AuthzBackend = PrivilegeAdapter()

#: Actions decided by the privilege model (Phase 1). Imported lazily-ish at
#: module load so an unknown action still fails closed in RbacBackend.
from case_service.hxguard.actions import ACTIONS as _PLATFORM_ACTION_DEFS  # noqa: E402

PLATFORM_ACTIONS: frozenset[str] = frozenset(_PLATFORM_ACTION_DEFS)


# ── Decision cache (availability mitigation) ─────────────────────────

_cache: dict[tuple, tuple[Decision, float]] = {}
_cache_generation = 0   # bumped on invalidation — keys embed it
MAX_CACHE_ENTRIES = 5000   # per-resource keys (e.g. case ids) must not balloon memory

counters = {"allow": 0, "deny": 0, "error": 0, "cache_hit": 0,
            "deny_log_suppressed": 0}

# deny-audit throttle: first deny per (subject, action) is logged, repeats
# within the window are counted but not persisted — a hammered 403 route
# must not become a SecurityEvent / commit flood.
DENY_LOG_WINDOW_SECONDS = 60.0
_deny_logged: dict[tuple, float] = {}


def invalidate_cache() -> None:
    """Call on any role / access-group / namespace-grant mutation."""
    global _cache_generation
    _cache_generation += 1
    _cache.clear()


def _cache_key(subject: Subject, action: str, resource: dict | None) -> tuple:
    res_key = tuple(sorted((k, str(v)) for k, v in (resource or {}).items()))
    return (_cache_generation, subject, action, res_key)


# ── Entry points ─────────────────────────────────────────────────────


async def check(
    session: AsyncSession,
    subject: Subject,
    action: str,
    resource: dict[str, Any] | None = None,
) -> Decision:
    """The single authorization question. NEVER raises — returns a Decision;
    every internal failure is a deny."""
    key = _cache_key(subject, action, resource)
    hit = _cache.get(key)
    now = time.monotonic()
    if hit and hit[1] > now:
        counters["cache_hit"] += 1
        d = hit[0]
        return Decision(d.allow, d.reason, d.source, cached=True)

    backend = (
        _rebac_backend if action in CASE_ACTIONS
        else _privilege_backend if action in PLATFORM_ACTIONS
        else _backend
    )
    try:
        decision = await backend.evaluate(session, subject, action, resource)
    except Exception as exc:                      # fail-closed on ANY error
        counters["error"] += 1
        decision = Decision(False, f"authz evaluation error: {exc}", "error")
        await _maybe_log_deny(session, subject, action, resource, decision, "authz_error")
        return decision

    if len(_cache) >= MAX_CACHE_ENTRIES:
        # sweep expired first; if a hostile resource-id spread still fills
        # the cache, drop it — correctness never depends on the cache
        for k in [k for k, (_, exp) in _cache.items() if exp <= now]:
            _cache.pop(k, None)
        if len(_cache) >= MAX_CACHE_ENTRIES:
            _cache.clear()
    _cache[key] = (decision, now + CACHE_TTL_SECONDS)
    if decision.allow:
        counters["allow"] += 1
    else:
        counters["deny"] += 1
        await _maybe_log_deny(session, subject, action, resource, decision, "authz_denied")
    return decision


async def _maybe_log_deny(
    session: AsyncSession, subject: Subject, action: str,
    resource: dict | None, decision: Decision, event_type: str,
) -> None:
    """Throttled deny audit: first deny per (subject, action) in the window
    is persisted; repeats are counted, not written."""
    now = time.monotonic()
    tkey = (subject.kind, subject.id, action, event_type)
    last = _deny_logged.get(tkey, 0.0)
    if now - last < DENY_LOG_WINDOW_SECONDS:
        counters["deny_log_suppressed"] += 1
        return
    if len(_deny_logged) > 10000:    # same memory discipline as the cache
        _deny_logged.clear()
    _deny_logged[tkey] = now
    await _log_security_event(session, subject, action, resource, decision, event_type)


async def require(
    session: AsyncSession,
    subject: Subject,
    action: str,
    resource: dict[str, Any] | None = None,
) -> None:
    """check() that raises HTTPException 403 on deny — for router code."""
    decision = await check(session, subject, action, resource)
    if not decision.allow:
        raise HTTPException(403, f"Not authorized: {decision.reason}")


def subject_allows(subject: Subject, action: str) -> bool:
    """`user_allows`, for callers that already hold a `Subject`.

    The two backends inside this module work in Subjects, not AuthenticatedUsers
    — `RebacBackend` in particular is handed one by `check()`. Fails closed on
    anything unexpected, like every other decision path here.
    """
    try:
        from case_service.hxguard.privileges import PrivilegeBackend

        class _S:
            kind = subject.kind
            roles = list(subject.roles)
            is_admin = subject.is_admin
            privileges = _thaw_privileges(subject.privileges)

        return bool(PrivilegeBackend().evaluate(_S(), action).allow)
    except Exception as exc:                          # fail-closed, always
        log.warning("subject_allows(%s) failed — denying: %s", action, exc)
        return False


def user_allows(user: AuthenticatedUser, action: str) -> bool:
    """Synchronous privilege test for a PLATFORM action. No session, no I/O.

    Phase 4 step 2 needs this. The imperative role checks it replaces are
    ordinary sync helpers — `_has_sensitive_access(user) -> bool`,
    `_table_allowed(...)`, `_can_view(...)` — called deep inside handler bodies
    and often inside comprehensions. Making all of them async to reach `check()`
    would be a signature change across 19 routers, and the reason they are sync
    is not laziness: **a platform decision is a pure function of the subject.**
    Privileges already travel on `Subject` (they are part of the cache key), so
    there is nothing to await.

    Use this for questions whose answer *shapes* a response — row scoping,
    redaction, whether to show a field. For a gate that refuses the request,
    prefer `require()`: it caches, and it writes the `authz_denied` SecurityEvent
    that HxShield consumes. A redaction test deliberately writes no audit event;
    it is not a denial, and one per row would drown the log.

    Fails closed: an unknown action, a non-platform action, or any error is False.
    """
    if action not in PLATFORM_ACTIONS:
        log.warning("user_allows called with non-platform action %r — denying", action)
        return False
    try:
        from case_service.hxguard.privileges import PrivilegeBackend

        subject = subject_from_user(user)

        class _S:
            kind = subject.kind
            roles = list(subject.roles)
            is_admin = subject.is_admin
            privileges = _thaw_privileges(subject.privileges)

        return bool(PrivilegeBackend().evaluate(_S(), action).allow)
    except Exception as exc:                          # fail-closed, always
        log.warning("user_allows(%s) failed — denying: %s", action, exc)
        return False


async def require_user(
    session: AsyncSession, user: AuthenticatedUser, action: str,
    resource: dict[str, Any] | None = None,
) -> None:
    """`require()` for callers holding an AuthenticatedUser rather than a Subject.

    The imperative sites migrated in Phase 4 step 2 all have the user in hand;
    this saves each of them repeating `subject_from_user`."""
    await require(session, subject_from_user(user), action, resource)


async def require_case(
    session: AsyncSession,
    user: AuthenticatedUser,
    action: str,
    case_id,
) -> None:
    """Case-level ReBAC check honoring VELARIS_CASE_HXGUARD_CASE_ENFORCEMENT.

    off     → skipped entirely
    shadow  → evaluated; would-be denials audited (mode=shadow), request passes
    enforce → denials raise 403
    """
    from case_service.config import get_settings
    mode = (get_settings().hxguard_case_enforcement or "shadow").lower()
    if mode == "off":
        return
    subject = subject_from_user(user)
    decision = await check(session, subject, action, {"case_id": case_id})
    if decision.allow:
        return
    if mode == "enforce":
        # Anti-oracle (enforce cutover): a denied caller learns NOTHING —
        # 404 for every case action, so 403-vs-404 can't confirm that a
        # guessed case id exists. The reason stays in the authz_denied
        # SecurityEvent for operators, never in the response.
        raise HTTPException(404, "Case not found")
    counters["shadow_would_deny"] = counters.get("shadow_would_deny", 0) + 1
    log.info("hxguard shadow would-deny: %s %s case=%s (%s)",
             subject.id, action, case_id, decision.reason)


def subject_from_user(user: AuthenticatedUser) -> Subject:
    ag = getattr(user, "active_access_group", None)
    raw = list(getattr(ag, "privileges", None) or []) if ag else []
    from case_service.hxguard import role_store

    return Subject(
        kind="user", id=user.user_id,
        roles=tuple(user.roles or ()), is_admin=bool(user.is_admin),
        privileges=_freeze_privileges(raw),
        role_store_generation=role_store.generation(),
    )


def _freeze_privileges(privs: list[dict]) -> tuple:
    """Hashable form, because Subject is a frozen dataclass used as a cache key.

    **Lists become tuples, they do NOT become strings.** The first version wrote
    `str(v)`, which turned `{"actions": ["write"]}` into the *string*
    `"['write']"`; thawed back, `_verb_matches` iterated it character by character
    and matched nothing. Every access-group privilege entry in the platform
    namespace therefore evaluated to a silent deny.

    It was invisible for as long as nothing depended on it: platform roles come
    from `user.roles`, and until Phase 4 step 1 an access group reached a
    platform action through the role-name merge instead. Step 1 makes this path
    the only one an access group has, so the round trip has to be faithful.
    """
    def _freeze_value(v):
        if isinstance(v, (list, tuple)):
            return tuple(_freeze_value(x) for x in v)
        if isinstance(v, dict):
            return tuple(sorted((str(k), _freeze_value(x)) for k, x in v.items()))
        return v if isinstance(v, (str, int, float, bool, type(None))) else str(v)

    out = []
    for p in privs:
        if isinstance(p, dict):
            out.append(tuple(sorted((str(k), _freeze_value(v)) for k, v in p.items())))
    return tuple(sorted(out, key=repr))


def _thaw_privileges(frozen: tuple) -> list[dict]:
    """Inverse of `_freeze_privileges` — tuples back to the lists the evaluator
    expects. Nested dicts are not reconstructed: privilege entries are flat
    (`resource`, `actions`, `effect`, `case_type_id`), and inventing a shape the
    evaluator never sees would be the wrong kind of thorough."""
    def _thaw_value(v):
        return [_thaw_value(x) for x in v] if isinstance(v, tuple) else v

    return [{k: _thaw_value(v) for k, v in entry} for entry in frozen]


def guard(action: str):
    """FastAPI dependency: authenticate, authorize, return the user.

    Drop-in replacement for require_role() on routes that map to a single
    HxGuard action."""
    async def _dep(
        user: AuthenticatedUser = Depends(get_current_user),
        session: AsyncSession = Depends(get_session),
    ) -> AuthenticatedUser:
        await require(session, subject_from_user(user), action)
        return user

    # The action is otherwise only visible inside the closure, so a test can
    # assert that a route 403s but not WHICH action it 403s on — and two actions
    # held by the same role are indistinguishable from the outside. Anything
    # walking the built route table can now read the real gate rather than a
    # hand-kept map that drifts.
    _dep.hxguard_action = action  # type: ignore[attr-defined]
    return _dep


async def _log_security_event(
    session: AsyncSession, subject: Subject, action: str,
    resource: dict | None, decision: Decision, event_type: str,
) -> None:
    """Best-effort — a logging failure must never turn into an allow OR
    mask the deny.

    Uses its OWN session + commit: a deny raised as HTTP 403 rolls back the
    request transaction, and the audit record must survive that rollback
    (otherwise HTTP denials would never reach HxShield)."""
    try:
        from case_service.db.session import get_session_factory
        from case_service.enterprise.security_events import log_security_event
        async with get_session_factory()() as audit_session:
            await log_security_event(
                audit_session, event_type=event_type,
                severity="warning" if event_type == "authz_error" else "info",
                user_id=f"{subject.kind}:{subject.id}",
                action=action, outcome="denied",
                resource_type="authz",
                resource_id=str((resource or {}).get("namespace_id") or (resource or {}).get("id") or action),
                details={"reason": decision.reason, "source": decision.source,
                         "roles": list(subject.roles), "resource": {k: str(v) for k, v in (resource or {}).items()}},
            )
            await audit_session.commit()
    except Exception as exc:
        log.warning("hxguard: security event logging failed: %s", exc)
