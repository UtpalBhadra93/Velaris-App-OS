"""HxSentinel P4a — marketplace signal packs (vertical rule bundles as content).

A pack is a manifest of declarative rules whose ``when`` conditions are
safe-expression strings, evaluated by the in-core engine against a FIXED, FLAT
feature context. Expressions are validated CONFORMING (the strict grammar in
``core.safe_expression``) at install time, so a pack expresses new rule logic
WITHOUT shipping executable code — Layer-3 stays forbidden.

The feature context is the lasting contract packs write against; keep it flat
(the grammar allows no attribute/subscript access) and documented.

Copyright (c) 2024-2026 HELIX Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import statistics
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from case_service.core import safe_expression
from case_service.hxbridge.security import validate_outbound_url
from case_service.db.models import (
    CaseInstanceModel,
    CaseInstanceVariableModel,
    CaseOutcomeLabelModel,
    CaseRiskSignalPackModel,
)
from case_service.sentinel import engine as eng
from case_service.sentinel import rules as rulecat

#: The flat feature names a pack expression may reference. Documented + fixed:
#: adding a feature is an API change. (The grammar forbids attribute/subscript,
#: so everything a pack sees is a bare scalar/bool name.)
FEATURE_KEYS = (
    "amount", "amount_zscore",
    "phone_velocity", "email_velocity", "address_velocity",
    "shares_doc_with_bad", "shared_signals_with_bad",
)

_AMOUNT_HINTS = ("amount", "value", "total", "claim")
_MIN_SAMPLES_Z = 8


class ManifestError(ValueError):
    """Raised when a pack manifest is malformed or carries an unsafe rule."""


# ── Manifest validation ──────────────────────────────────────────────────────


def validate_manifest(manifest: dict) -> None:
    """Structural + safety validation. Raises ManifestError; returns None if ok."""
    if not isinstance(manifest, dict):
        raise ManifestError("manifest must be an object")
    pack_id = manifest.get("id")
    if not isinstance(pack_id, str) or not (1 <= len(pack_id) <= 128):
        raise ManifestError("manifest.id is required (1..128 chars)")
    if not isinstance(manifest.get("name"), str) or not manifest["name"]:
        raise ManifestError("manifest.name is required")
    if not isinstance(manifest.get("version"), str) or not manifest["version"]:
        raise ManifestError("manifest.version is required")

    rules = manifest.get("rules")
    if not isinstance(rules, list) or not rules:
        raise ManifestError("manifest.rules must be a non-empty list")
    if len(rules) > 100:
        raise ManifestError("manifest.rules too large (max 100)")
    seen: set[str] = set()
    for r in rules:
        if not isinstance(r, dict):
            raise ManifestError("each rule must be an object")
        key = r.get("key")
        if not isinstance(key, str) or not key:
            raise ManifestError("rule.key is required")
        if key in seen:
            raise ManifestError(f"duplicate rule key '{key}'")
        seen.add(key)
        if r.get("severity") not in rulecat.SEVERITIES:
            raise ManifestError(f"rule '{key}': severity must be one of {rulecat.SEVERITIES}")
        for f in ("title", "message"):
            if not isinstance(r.get(f), str) or not r[f]:
                raise ManifestError(f"rule '{key}': {f} is required")
        when = r.get("when")
        if not isinstance(when, str) or not when.strip():
            raise ManifestError(f"rule '{key}': when-expression is required")
        cls, reason = safe_expression.classify_expression(when)
        # Only the STRICT grammar is allowed for pack rules — no hardened
        # fallback, no rejected expressions. This is the sandbox boundary.
        if cls is not safe_expression.Classification.CONFORMING:
            raise ManifestError(f"rule '{key}': expression not allowed ({reason})")

    taxonomy = manifest.get("taxonomy", [])
    if not isinstance(taxonomy, list):
        raise ManifestError("manifest.taxonomy must be a list")
    for t in taxonomy:
        if not isinstance(t, dict) or not isinstance(t.get("key"), str) or not t["key"]:
            raise ManifestError("each taxonomy entry needs a key")


# ── Install / list / uninstall ───────────────────────────────────────────────


async def install_pack(
    session: AsyncSession, *, tenant: str, manifest: dict, installed_by: str,
) -> CaseRiskSignalPackModel:
    validate_manifest(manifest)
    pack_id = manifest["id"]
    row = (await session.execute(
        select(CaseRiskSignalPackModel).where(
            CaseRiskSignalPackModel.tenant_id == tenant,
            CaseRiskSignalPackModel.pack_id == pack_id)
    )).scalars().first()
    if row is None:
        row = CaseRiskSignalPackModel(tenant_id=tenant, pack_id=pack_id, installed_by=installed_by)
        session.add(row)
    row.name = manifest["name"]
    row.vertical = manifest.get("vertical")
    row.version = manifest["version"]
    row.manifest = manifest
    row.enabled = True
    row.installed_by = installed_by

    # A pack's taxonomy extensions become tenant labels (core keys are never
    # shadowed; existing keys are left as-is).
    for t in manifest.get("taxonomy", []):
        key = str(t["key"]).strip().lower()
        if not key or rulecat_is_core(key):
            continue
        exists = (await session.execute(
            select(CaseOutcomeLabelModel.id).where(
                CaseOutcomeLabelModel.tenant_id == tenant, CaseOutcomeLabelModel.key == key)
        )).first()
        if not exists:
            session.add(CaseOutcomeLabelModel(
                tenant_id=tenant, key=key, display=str(t.get("display") or key),
                description=t.get("description"), is_negative=bool(t.get("is_negative", True))))
    await session.flush()
    return row


def rulecat_is_core(key: str) -> bool:
    from case_service.sentinel import taxonomy
    return taxonomy.is_core(key)


#: Cap on a fetched manifest — a pack manifest is a few KB of rules.
_MANIFEST_MAX_BYTES = 256 * 1024


async def _fetch(url: str) -> tuple[int, bytes]:
    import httpx
    async with httpx.AsyncClient(timeout=10.0, follow_redirects=False) as c:
        r = await c.get(url)
        return r.status_code, r.content


async def install_from_url(
    session: AsyncSession, *, tenant: str, url: str, installed_by: str,
) -> CaseRiskSignalPackModel:
    """Install a community pack from a remote manifest URL (E2 distribution).

    Safety: the URL is SSRF-guarded (DNS-resolved, private/loopback/link-local
    hosts refused) BEFORE any request; the response is size-capped; and the
    manifest goes through the exact same CONFORMING sandbox validation as a
    pasted one — a remote pack gets no more trust than a local one.
    """
    import json as _json
    await validate_outbound_url(url)      # raises ValueError on SSRF/blocked host
    status, content = await _fetch(url)
    if status != 200:
        raise ManifestError(f"could not fetch manifest (HTTP {status})")
    if len(content) > _MANIFEST_MAX_BYTES:
        raise ManifestError("manifest is too large")
    try:
        manifest = _json.loads(content)
    except Exception:
        raise ManifestError("manifest URL did not return valid JSON")
    return await install_pack(session, tenant=tenant, manifest=manifest, installed_by=installed_by)


async def list_packs(session: AsyncSession, tenant: str) -> list[CaseRiskSignalPackModel]:
    return list((await session.execute(
        select(CaseRiskSignalPackModel).where(CaseRiskSignalPackModel.tenant_id == tenant)
        .order_by(CaseRiskSignalPackModel.installed_at.desc()))).scalars().all())


async def uninstall_pack(session: AsyncSession, tenant: str, pack_id: str) -> bool:
    row = (await session.execute(
        select(CaseRiskSignalPackModel).where(
            CaseRiskSignalPackModel.tenant_id == tenant,
            CaseRiskSignalPackModel.pack_id == pack_id)
    )).scalars().first()
    if row is None:
        return False

    # Proactively retire the findings this pack produced — otherwise they linger
    # active until each case happens to be re-evaluated. Prefix-match is escaped
    # by startswith(). Any verify-gate hold one of them opened is released as
    # system-cleared: the gate's reason (the pack) no longer exists, and a human
    # should not have to clear a hold for a rule that's gone.
    from case_service.db.models import CaseRiskFindingModel, CaseRiskHoldModel
    findings = (await session.execute(
        select(CaseRiskFindingModel).where(
            CaseRiskFindingModel.tenant_id == tenant,
            CaseRiskFindingModel.rule_key.startswith(f"pack:{pack_id}:"),
            CaseRiskFindingModel.status == "active"))).scalars().all()
    fids = [f.id for f in findings]
    for f in findings:
        f.status = "stale"
    if fids:
        holds = (await session.execute(
            select(CaseRiskHoldModel).where(
                CaseRiskHoldModel.finding_id.in_(fids),
                CaseRiskHoldModel.status == "open"))).scalars().all()
        from datetime import datetime, timezone
        for h in holds:
            h.status = "cleared"
            h.resolved_by = "system"
            h.resolved_at = datetime.now(timezone.utc)
            h.resolution_note = f"signal pack '{pack_id}' uninstalled"

    await session.delete(row)
    await session.flush()
    return True


# ── Feature context + evaluation ─────────────────────────────────────────────


async def _amount_features(session: AsyncSession, case: CaseInstanceModel) -> tuple[float, float]:
    rows = (await session.execute(
        select(CaseInstanceVariableModel.full_key, CaseInstanceVariableModel.value_num)
        .where(CaseInstanceVariableModel.case_id == case.id,
               CaseInstanceVariableModel.value_num.is_not(None)))).all()
    cand = [(fk, v) for fk, v in rows if any(h in (fk or "").lower() for h in _AMOUNT_HINTS)]
    if not cand or case.case_type_id is None:
        return 0.0, 0.0
    full_key, value = cand[0]
    others = (await session.execute(
        select(CaseInstanceVariableModel.value_num)
        .join(CaseInstanceModel, CaseInstanceModel.id == CaseInstanceVariableModel.case_id)
        .where(CaseInstanceModel.case_type_id == case.case_type_id,
               CaseInstanceVariableModel.full_key == full_key,
               CaseInstanceVariableModel.value_num.is_not(None),
               CaseInstanceVariableModel.case_id != case.id))).scalars().all()
    if len(others) < _MIN_SAMPLES_Z:
        return float(value), 0.0
    mean, std = statistics.mean(others), statistics.pstdev(others)
    z = round((value - mean) / std, 2) if std else 0.0
    return float(value), z


async def build_feature_context(session: AsyncSession, case: CaseInstanceModel, tenant: str) -> dict:
    """Compute the flat feature dict a pack expression is evaluated against."""
    signals = await eng._signals_of(session, case.id)
    vel = {"identity_phone": 0, "identity_email": 0, "identity_address": 0}
    overlap: dict[uuid.UUID, set[str]] = {}
    for sig in signals:
        others = set(await eng._matching_cases(session, tenant, sig.signal_type, sig.signal_key, case.id))
        if sig.signal_type in vel:
            vel[sig.signal_type] = max(vel[sig.signal_type], len(others) + 1)
        for o in others:
            overlap.setdefault(o, set()).add(sig.signal_type)

    shares_doc_with_bad = False
    shared_signals_with_bad = 0
    if overlap:
        negatives = await eng._negative_keys(session, tenant)
        outcomes = await eng._current_outcomes(session, set(overlap))
        for cid, types in overlap.items():
            oc = outcomes.get(cid)
            if oc in negatives:
                shared_signals_with_bad = max(shared_signals_with_bad, len(types))
                if "doc_hash" in types and oc in eng._SERIOUS_NEGATIVES:
                    shares_doc_with_bad = True

    amount, amount_z = await _amount_features(session, case)
    return {
        "amount": amount, "amount_zscore": amount_z,
        "phone_velocity": vel["identity_phone"],
        "email_velocity": vel["identity_email"],
        "address_velocity": vel["identity_address"],
        "shares_doc_with_bad": shares_doc_with_bad,
        "shared_signals_with_bad": shared_signals_with_bad,
    }


async def enabled_packs(session: AsyncSession, tenant: str) -> list[CaseRiskSignalPackModel]:
    return list((await session.execute(
        select(CaseRiskSignalPackModel).where(
            CaseRiskSignalPackModel.tenant_id == tenant,
            CaseRiskSignalPackModel.enabled.is_(True)))).scalars().all())


def evaluate_pack_rules(packs: list[CaseRiskSignalPackModel], ctx: dict) -> list[dict]:
    """Pure evaluation of pack rules against an already-computed feature context.

    A rule fires only when its expression evaluates to exactly ``True`` — a stray
    numeric/None result never fires. rule_key namespaces the pack:
    ``pack:{pack_id}:{rule_key}``.
    """
    out: list[dict] = []
    for pack in packs:
        for r in (pack.manifest or {}).get("rules", []):
            when = r.get("when")
            if not isinstance(when, str):
                continue
            result = safe_expression.evaluate(when, ctx)
            if result is True:
                # Data minimisation: cite only the feature values the rule
                # actually referenced — not the whole context (which carries the
                # raw amount even when the rule never used it).
                used = {k: ctx[k] for k in FEATURE_KEYS if k in when}
                out.append({
                    "rule_key": f"pack:{pack.pack_id}:{r['key']}",
                    "severity": r["severity"],
                    "title": r["title"],
                    "message": r["message"],
                    "evidence": {"source": "signal_pack", "pack_id": pack.pack_id,
                                 "when": when, "features": used},
                })
    return out


async def evaluate_packs(session: AsyncSession, case: CaseInstanceModel, tenant: str) -> list[dict]:
    """Standalone convenience: fetch enabled packs, build the context, evaluate."""
    packs = await enabled_packs(session, tenant)
    if not packs:
        return []
    ctx = await build_feature_context(session, case, tenant)
    return evaluate_pack_rules(packs, ctx)
