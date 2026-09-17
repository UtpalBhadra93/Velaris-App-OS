"""The validation ladder — every rung mandatory, and it stops at the first no.

Design: docs/Future/Done/AI-ERA-ROADMAP.md §F-08: *"Validation ladder (all
mandatory): conformance gate → F-03 counterfactual over the affected cohort →
F-04 twin simulation → thresholds."*

**It stops at the first refusal**, and that is not laziness. A change that
cannot even be expressed by the operator catalog should not then be replayed
across the whole recorded caseload to discover whether it would also have been
slow — the expensive rungs exist to answer questions that only matter once the
cheap ones have said yes.

**One rung of the roadmap does not apply here, and it is recorded as such rather
than quietly dropped.** F-03's counterfactual proof is about HxArbiter *policy*:
it replays recorded decisions against a candidate policy version. A healing
change cannot touch policy — the operator catalog cannot name it — so the rung
is run, finds nothing to prove, and records `not_applicable` with that reason.
An auditor reading the ladder sees five rungs either way.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import logging
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from case_service.healing import catalog

log = logging.getLogger(__name__)

RUNGS = ("catalog", "gate", "replay", "proof", "twin", "thresholds")


def _rung(name: str, ok: bool, detail: str, **extra: Any) -> dict[str, Any]:
    return {"rung": name, "ok": bool(ok), "detail": detail, **extra}


def patched_definition(operator: str, definition: dict[str, Any],
                       proposal: dict[str, Any]) -> dict[str, Any]:
    """The case-type definition as the change would leave it.

    Delegates to `hxevolve.patch` — the SAME function the `/hxevolve/insights/
    {id}/stage` route uses, so a healing episode rehearses and applies exactly
    the shape a human-staged insight would. Two implementations of "what does
    this proposal change" would eventually disagree about the same proposal, and
    the one that rehearsed would not be the one that shipped.

    Returns the definition unchanged for the two rule operators: they write a
    rule row and leave the definition alone, and returning a copy would make a
    twin run report a change nobody made.
    """
    from case_service.hxevolve import patch as evolve_patch

    patched, errors = evolve_patch.validate_and_patch(definition, operator, proposal)
    if errors or patched is None:
        return definition
    return patched


async def run(
    session: AsyncSession,
    *,
    operator: str,
    proposal: dict[str, Any],
    insight: Any,
    case_type: Any,
    tenant_uuid: Any,
    require_twin: bool,
    twin_cases: int = 5,
) -> dict[str, Any]:
    """Climb the ladder. Returns `{"rungs": [...], "ok": bool, "stopped_at": ...}`.

    Every rung that ran is recorded, including the ones that passed — a reviewer
    approving a change to their own live process is entitled to see what was
    actually checked, not a single green tick.
    """
    rungs: list[dict[str, Any]] = []
    refs: dict[str, Any] = {}

    def _stop(name: str, detail: str) -> dict[str, Any]:
        rungs.append(_rung(name, False, detail))
        return {"rungs": rungs, "ok": False, "stopped_at": name,
                "stopped_reason": detail, "refs": refs}

    # ── 1. catalog ──────────────────────────────────────────────────────────
    try:
        catalog.check(operator, proposal)
    except catalog.CatalogError as exc:
        return _stop("catalog", str(exc))
    rungs.append(_rung(
        "catalog", True,
        f"{operator} is in the catalog and reaches only "
        f"{sorted(catalog.writable_fields(operator))}",
        reaches=sorted(catalog.writable_fields(operator))))

    # ── 2. gate ─────────────────────────────────────────────────────────────
    # HxEvolve's own gates, re-run against the config as it stands NOW. The
    # insight was gate-validated when it was mined, and the world has moved
    # since — a proposal that was valid a week ago is not evidence today.
    try:
        errors = await _revalidate(session, operator, proposal, case_type)
    except Exception as exc:                                     # noqa: BLE001
        return _stop("gate", f"the proposal could not be re-validated: {exc}")
    if errors:
        return _stop("gate", "no longer valid against current config: "
                             + "; ".join(errors[:3]))
    rungs.append(_rung("gate", True,
                       "re-validated against the config as it stands now"))

    # ── 3. replay ───────────────────────────────────────────────────────────
    # HxEvolve already replayed this proposal across the recorded caseload; an
    # insight it did not surface never becomes an episode a person is asked
    # about.
    status = getattr(insight, "status", None)
    if status != "surfaced":
        return _stop("replay", f"HxEvolve did not surface this proposal "
                               f"(status {status!r}); it has no replay evidence")
    refs["replay_run_id"] = getattr(insight, "replay_run_id", None)
    evidence = getattr(insight, "evidence", None) or {}
    rungs.append(_rung(
        "replay", True,
        f"replay-proven by HxEvolve ({getattr(insight, 'evidence_kind', None)})",
        evidence_kind=getattr(insight, "evidence_kind", None),
        replay_run_id=str(refs["replay_run_id"]) if refs["replay_run_id"] else None,
        summary={k: evidence.get(k) for k in ("improvement", "cases", "verdict")
                 if k in evidence}))

    # ── 4. proof (F-03) ─────────────────────────────────────────────────────
    # Recorded, not skipped. See the module docstring.
    rungs.append(_rung(
        "proof", True,
        "not applicable: an F-03 counterfactual proves a change to HxArbiter "
        "policy, and the operator catalog cannot name policy — so there is no "
        "policy delta to prove",
        applicable=False))

    # ── 5. twin (F-04) ──────────────────────────────────────────────────────
    from case_service.hxdeploy import twin as twin_mod

    if not twin_mod.configured():
        if require_twin:
            return _stop("twin",
                         "no twin is configured, and this tenant requires a "
                         "rehearsal before a healing change may be applied. "
                         "Nothing is wrong with the proposal.")
        rungs.append(_rung("twin", True,
                           "no twin configured, and this tenant does not require "
                           "one — the change is evidenced by replay alone",
                           ran=False))
    else:
        try:
            bundle = await _candidate_bundle(session, case_type, operator, proposal)
            twin_row = await twin_mod.run(
                session, tenant_uuid=tenant_uuid, bundle=bundle, run_row=None,
                cases_per_type=twin_cases, created_by="healing")
        except Exception as exc:                                 # noqa: BLE001
            log.exception("healing: twin rehearsal failed")
            return _stop("twin", f"the rehearsal could not be run: {exc}")
        refs["twin_run_id"] = twin_row.id
        if twin_row.status != "complete":
            detail = twin_row.error or twin_row.refusal_reason or twin_row.status
            if require_twin:
                return _stop("twin", f"the rehearsal did not complete: {detail}")
            rungs.append(_rung("twin", True,
                               f"rehearsal unavailable ({detail}); this tenant "
                               f"does not require one", ran=False,
                               twin_run_id=str(twin_row.id)))
        else:
            rungs.append(_rung(
                "twin", True,
                f"{twin_row.terminal_cases}/{twin_row.started_cases} synthetic "
                f"cases finished (p95 {twin_row.p95_ms}ms)",
                ran=True, twin_run_id=str(twin_row.id),
                started=twin_row.started_cases, terminal=twin_row.terminal_cases,
                stuck=twin_row.stuck_cases, failed=twin_row.failed_cases,
                p95_ms=twin_row.p95_ms))

            # ── 6. thresholds ───────────────────────────────────────────────
            # One threshold, and it is absolute rather than a percentage: a
            # change the platform makes to itself may not strand a single case.
            # A human author may knowingly ship something with a rough edge;
            # this is not a human author.
            bad = int(twin_row.stuck_cases or 0) + int(twin_row.failed_cases or 0)
            if bad:
                reasons = "; ".join(
                    f["reason"] for f in (twin_row.failures or [])[:2] if f.get("reason"))
                return _stop(
                    "thresholds",
                    f"{bad} of {twin_row.started_cases} rehearsed cases stalled or "
                    f"failed under this change. A change the platform makes to "
                    f"itself may not strand any. {reasons}")

    if not any(r["rung"] == "thresholds" for r in rungs):
        rungs.append(_rung("thresholds", True,
                           "no rehearsed case stalled or failed"))
    return {"rungs": rungs, "ok": True, "stopped_at": None,
            "stopped_reason": None, "refs": refs}


async def _revalidate(session: AsyncSession, operator: str,
                      proposal: dict[str, Any], case_type: Any) -> list[str]:
    """HxEvolve's own gates, against current state. Never a second opinion.

    The insight was gate-validated when it was mined, and the world has moved
    since — a proposal that was valid a week ago is not evidence today.
    """
    from case_service.hxevolve import patch as evolve_patch

    _, errors = evolve_patch.validate_and_patch(
        case_type.definition_json or {}, operator, proposal)
    return errors


async def _candidate_bundle(session: AsyncSession, case_type: Any,
                            operator: str, proposal: dict[str, Any]) -> dict[str, Any]:
    """The case type AS THE CHANGE WOULD LEAVE IT, packaged for the twin.

    Built through `packager.build_bundle` and then patched, rather than
    hand-assembled: the twin must rehearse the same shape a promotion would
    carry, not a convenient approximation of it.
    """
    from case_service.hxdeploy.packager import build_bundle

    tenant = str(getattr(case_type, "tenant_id", "") or "default")
    bundle = await build_bundle(session, tenant, [case_type.id])
    for ct in bundle.get("case_types") or []:
        if str(ct.get("id")) != str(case_type.id):
            continue
        ct["definition_json"] = patched_definition(
            operator, ct.get("definition_json") or {}, proposal)
        # A distinct name, so the twin's own provisioning cannot collide with a
        # case type of the same name already sitting in the twin from an earlier
        # rehearsal.
        ct["name"] = f"{ct.get('name')} [healing candidate]"
    return bundle
