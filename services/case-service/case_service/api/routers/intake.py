"""Intake router — inbound webhook/HTTP trigger → filter evaluation → case creation.

POST /api/v1/intake/webhook/{case_type_id}
  Receives an external payload, evaluates filter_conditions defined on the
  case type, maps payload fields to case fields, creates a CaseInstance,
  and optionally starts a linked HxFusion process.
"""
from __future__ import annotations

import operator
import re
import uuid
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession

from case_service.db.models import (
    CaseInstanceModel,
    CaseTypeModel,
    IntakeEventModel,
    ProcessDefinitionModel,
    ProcessInstanceModel,
)
from case_service.db.session import get_session
from case_service.intake import service as intake_service

router = APIRouter(prefix="/intake", tags=["intake"])


# ── Filter condition evaluator ────────────────────────────────────────────────

# Filtering and mapping moved to `case_service.intake.filters` — pure functions
# over a payload, which a second transport could not reuse while they lived in
# a router. Re-exported so existing importers keep working.
from case_service.intake.filters import (  # noqa: E402
    apply_field_mapping,
    evaluate_filters,
)


# ── Intake endpoint ───────────────────────────────────────────────────────────

@router.post("/webhook/{case_type_id}", status_code=201)
async def receive_webhook(
    case_type_id: uuid.UUID,
    request: Request,
    session: AsyncSession = Depends(get_session),
):
    """Receive an inbound payload for a case type configured with webhook intake.

    A thin transport wrapper: it supplies the payload and the sender's address,
    translates domain failures to status codes, and owns the commit. Everything
    else — validation, filtering, mapping, case creation, process start — lives
    in `intake.service.ingest`, so it is testable without HTTP and reusable by
    any future transport without being copied.
    """
    try:
        payload: dict = await request.json()
    except Exception:
        payload = {}

    try:
        result = await intake_service.ingest(
            session,
            case_type_id=case_type_id,
            payload=payload,
            source_ref=request.client.host if request.client else None,
            source_kind="webhook",
        )
    except intake_service.IntakeError as exc:
        raise HTTPException(exc.status, str(exc))

    await session.commit()
    return result



# ── Intake event log ──────────────────────────────────────────────────────────

@router.get("/events")
async def list_intake_events(
    case_type_id: uuid.UUID | None = None,
    status: str | None = None,
    limit: int = 50,
    session: AsyncSession = Depends(get_session),
):
    """List recent intake events with filter/status breakdown."""
    from sqlalchemy import select, desc

    q = select(IntakeEventModel).order_by(desc(IntakeEventModel.received_at)).limit(limit)
    if case_type_id:
        q = q.where(IntakeEventModel.case_type_id == case_type_id)
    if status:
        q = q.where(IntakeEventModel.status == status)

    rows = (await session.execute(q)).scalars().all()
    return {
        "events": [
            {
                "id": str(e.id),
                "case_type_id": str(e.case_type_id) if e.case_type_id else None,
                "status": e.status,
                "created_case_id": str(e.created_case_id) if e.created_case_id else None,
                "process_instance_id": str(e.process_instance_id) if e.process_instance_id else None,
                "filter_result": e.filter_result,
                "source_ip": e.source_ip,
                "received_at": e.received_at.isoformat(),
                "processed_at": e.processed_at.isoformat() if e.processed_at else None,
            }
            for e in rows
        ],
        "total": len(rows),
    }


@router.get("/events/{event_id}")
async def get_intake_event(
    event_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
):
    """Get full details of an intake event including raw payload."""
    e = await session.get(IntakeEventModel, event_id)
    if not e:
        raise HTTPException(404, "Event not found")
    return {
        "id": str(e.id),
        "case_type_id": str(e.case_type_id) if e.case_type_id else None,
        "connector_id": str(e.connector_id) if e.connector_id else None,
        "source_ip": e.source_ip,
        "raw_payload": e.raw_payload,
        "status": e.status,
        "filter_result": e.filter_result,
        "created_case_id": str(e.created_case_id) if e.created_case_id else None,
        "process_instance_id": str(e.process_instance_id) if e.process_instance_id else None,
        "error": e.error,
        "received_at": e.received_at.isoformat(),
        "processed_at": e.processed_at.isoformat() if e.processed_at else None,
    }
