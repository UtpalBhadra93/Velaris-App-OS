"""HxSync — sync pipeline.

run_sync(destination_id, session) does the following:
  1. Load destination + its field mappings + redaction rules
  2. Extract CaseInstance rows since last watermark (CDC-style incremental)
  3. Consent gate (opt-in per destination) — drop rows with no lawful basis
  4. Apply field mappings (rename + transform)
  5. Apply redaction rules (hash/drop/mask PII)
  6. Ensure schema on destination
  7. Push rows
  8. Update SyncDestination.last_synced_at + SyncRun record

Step 3 is the HxConsent control for `data_sharing`. Redaction (step 5) already
decided *which columns* may leave; it cannot decide whether a given person's
row may leave at all, because that is a fact about the subject rather than the
schema. The two are complementary and neither replaces the other.
"""
from __future__ import annotations

import uuid
import logging
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from velaris_shared.db.models import (
    CaseInstanceModel,
    SyncDestinationModel,
    SyncFieldMappingModel,
    SyncRedactionRuleModel,
    SyncRunModel,
)
from case_service.compliance import consent as consent_svc
from case_service.hxsync import destinations as _dest_mod  # noqa: F401 — registers adapters
from case_service.hxsync.protocol import get_destination
from case_service.hxsync.redaction import apply_redaction, apply_transforms, TransformError

logger = logging.getLogger(__name__)

_TABLE = "helix_cases"

_DEFAULT_COLUMNS = [
    {"name": "id",              "type": "VARCHAR"},
    {"name": "case_number",     "type": "VARCHAR"},
    {"name": "case_type_id",    "type": "VARCHAR"},
    {"name": "status",          "type": "VARCHAR"},
    {"name": "priority",        "type": "VARCHAR"},
    {"name": "created_by",      "type": "VARCHAR"},
    {"name": "created_at",      "type": "TIMESTAMPTZ"},
    {"name": "updated_at",      "type": "TIMESTAMPTZ"},
    {"name": "resolved_at",     "type": "TIMESTAMPTZ"},
    {"name": "current_stage_id","type": "VARCHAR"},
]


def _case_to_row(case: CaseInstanceModel) -> dict:
    return {
        "id":               str(case.id),
        "case_number":      case.case_number,
        "case_type_id":     str(case.case_type_id) if case.case_type_id else None,
        "status":           case.status,
        "priority":         case.priority,
        "created_by":       case.created_by,
        "created_at":       case.created_at.isoformat() if case.created_at else None,
        "updated_at":       case.updated_at.isoformat() if case.updated_at else None,
        "resolved_at":      case.resolved_at.isoformat() if case.resolved_at else None,
        "current_stage_id": str(case.current_stage_id) if case.current_stage_id else None,
    }


async def run_sync(destination_id: uuid.UUID, session: AsyncSession) -> dict:
    """Execute a full incremental sync. Returns a summary dict."""
    dest = await session.get(SyncDestinationModel, destination_id)
    if dest is None:
        raise ValueError(f"Destination {destination_id} not found")

    run = SyncRunModel(destination_id=destination_id, watermark_from=dest.last_synced_at)
    session.add(run)
    await session.flush()

    try:
        # Load mappings + redaction rules
        mappings_rows = (await session.execute(
            select(SyncFieldMappingModel).where(SyncFieldMappingModel.destination_id == destination_id)
        )).scalars().all()
        redaction_rows = (await session.execute(
            select(SyncRedactionRuleModel).where(SyncRedactionRuleModel.destination_id == destination_id)
        )).scalars().all()

        mappings = [{
            "source_field": m.source_field,
            "dest_column": m.dest_column,
            "transform": m.transform,
            "transform_module_id": m.transform_module_id,
        } for m in mappings_rows]

        # HxSandbox (#17): load every WASM module this destination needs ONCE.
        # `run_transform` caches the compiled module by content hash, so the
        # per-row cost after this is execution only.
        modules: dict[str, bytes] = {}
        wanted = {m["transform_module_id"] for m in mappings
                  if m["transform"] == "wasm" and m["transform_module_id"]}
        if wanted:
            from velaris_shared.db.models import HxSandboxModuleModel
            mod_rows = (await session.execute(
                select(HxSandboxModuleModel).where(
                    HxSandboxModuleModel.tenant_id == dest.tenant_id,
                    HxSandboxModuleModel.package_id.in_(wanted),
                )
            )).scalars().all()
            modules = {r.package_id: r.wasm_bytes for r in mod_rows}
        redactions = [{"field_path": r.field_path, "action": r.action} for r in redaction_rows]

        # Extract cases (incremental by updated_at watermark)
        q = select(CaseInstanceModel).order_by(CaseInstanceModel.updated_at)
        if dest.last_synced_at:
            q = q.where(CaseInstanceModel.updated_at > dest.last_synced_at)
        cases = (await session.execute(q)).scalars().all()

        now = datetime.now(timezone.utc)
        rows = []
        failed_rows: list[dict] = []
        consent_skipped = 0
        for case in cases:
            # HxConsent gate (opt-in per destination). Redaction answers "which
            # columns may leave"; this answers "may this person's row leave at
            # all". Checked BEFORE the row is built, so a refused case produces
            # no artefact anywhere in the run.
            if dest.consent_gated:
                try:
                    # allows_case, not allows_subject: an anonymized case has a
                    # NULL submitter, which on its own reads as "no data subject"
                    # and would let an erased row sync out to the warehouse.
                    allowed, reason = await consent_svc.allows_case(
                        session, case, consent_svc.DATA_SHARING,
                        tenant_id=dest.tenant_id,
                    )
                except Exception as exc:  # noqa: BLE001
                    # A gate that opens when the ledger is unreachable is not a
                    # gate. Refuse the row and let the run report why.
                    logger.warning("consent check failed for case %s: %s", case.id, exc)
                    allowed, reason = False, "check_failed"
                if not allowed:
                    consent_skipped += 1
                    failed_rows.append({
                        "case_id": str(case.id),
                        "error": f"consent: {reason} for data_sharing",
                    })
                    continue

            row = _case_to_row(case)
            if redactions:
                row = apply_redaction(row, redactions)
            if mappings:
                try:
                    row = apply_transforms(row, mappings, modules)
                except TransformError as exc:
                    # A customer transform failed. Drop THIS row and keep
                    # syncing: one broken case must not stall the pipeline,
                    # and shipping it untransformed would be worse than
                    # shipping nothing, since the warehouse cannot tell.
                    logger.warning("sync row skipped for case %s: %s", case.id, exc)
                    failed_rows.append({"case_id": str(case.id), "error": str(exc)})
                    continue
            rows.append(row)

        # Determine columns for schema ensure
        if mappings:
            columns = [{"name": m["dest_column"], "type": "VARCHAR"} for m in mappings]
        else:
            columns = _DEFAULT_COLUMNS

        # Push to destination
        adapter = get_destination(dest.dest_type, dest.connection_config)
        adapter.ensure_schema(_TABLE, columns)
        pushed = adapter.push_rows(_TABLE, rows)

        # Update destination watermark
        dest.last_synced_at = now
        dest.last_sync_status = "success"
        dest.updated_at = now

        run.status = "success"
        run.rows_synced = pushed
        run.watermark_to = now
        run.finished_at = now

        if consent_skipped:
            # "We stopped sharing their data" is an assertion until there is a
            # record of it. Same reason the email gate audits its drops.
            try:
                from velaris_shared.db.security_events import log_security_event

                await log_security_event(
                    session,
                    event_type="consent.sync_filtered",
                    severity="info",
                    resource_type="sync_destination",
                    resource_id=str(dest.id),
                    action=f"sync:{consent_svc.DATA_SHARING}",
                    outcome="blocked" if not rows else "partial",
                    details={
                        "purpose": consent_svc.DATA_SHARING,
                        "rows_dropped": consent_skipped,
                        "rows_delivered": len(rows),
                        "destination": dest.name,
                    },
                )
            except Exception:  # noqa: BLE001
                # The gate already held; losing its audit row must not fail the run.
                logger.exception("consent.sync_filtered audit failed for run %s", run.id)

        if failed_rows:
            # Surfaced, not swallowed: the run succeeded but some cases did
            # not make it, and an operator needs to see which and why.
            run.error_msg = (
                f"{len(failed_rows)} row(s) skipped: "
                + "; ".join(f"{f['case_id']}: {f['error']}" for f in failed_rows[:5])
            )[:1000]
            logger.warning(
                "sync %s completed with %d skipped row(s)", run.id, len(failed_rows)
            )

        await session.commit()
        return {
            "status": "success",
            "rows_synced": pushed,
            "rows_skipped": len(failed_rows),
            # Broken out from rows_skipped: a transform bug is something to fix,
            # a consent refusal is the system working. Collapsing them would
            # send an operator hunting for a fault that is not there.
            "consent_skipped": consent_skipped,
            "skipped": failed_rows[:20],
            "run_id": str(run.id),
        }

    except Exception as exc:
        run.status = "error"
        run.error_msg = str(exc)
        run.finished_at = datetime.now(timezone.utc)
        dest.last_sync_status = "error"
        await session.commit()
        return {"status": "error", "error": str(exc), "run_id": str(run.id)}
