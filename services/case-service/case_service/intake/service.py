"""Inbound intake — transport-independent ingest logic.

All of this previously lived inside the `POST /intake/webhook/{case_type_id}`
handler. Extracting it is not preparation for a feature: it is worth doing on
its own terms, because logic reachable only through HTTP can only be tested
through HTTP, and the sole way to add a second transport was copy-paste — which
is how two divergent definitions of "what an inbound event does" appear.

Roadmap #42 (MQTT/sensor ingest, §2.9) is explicitly demand-driven and is NOT
built. If a vertical ever asks for it, the adapter is a thin caller of
:func:`ingest` rather than a second copy of this.

Two deliberate boundaries:

**No HTTP types.** Failures raise :class:`IntakeError`; the route translates to
status codes. A queue consumer has no 404 to raise.

**No commit.** The caller owns the transaction, because a batching consumer
will want many ingests in one, and a service that commits mid-batch removes
that choice.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from velaris_shared.db.models import (
    CaseInstanceModel,
    CaseTypeModel,
    IntakeEventModel,
    ProcessDefinitionModel,
)
from case_service.intake.filters import apply_field_mapping, evaluate_filters

logger = logging.getLogger(__name__)

#: Triggers that accept a pushed inbound payload.
#:
#: `channel` is here rather than telling customers to set `webhook` on an SMS
#: case type: that lie would leak into filters, docs and the knowledge graph,
#: and a designer reading "webhook" on a case type fed by text messages has
#: been misinformed by us.
ACCEPTED_TRIGGERS = ("webhook", "http", "channel")


class IntakeError(Exception):
    """Ingest was refused. `status` is what an HTTP caller should return."""

    def __init__(self, message: str, *, status: int = 400) -> None:
        super().__init__(message)
        self.status = status


async def ingest(
    session: AsyncSession,
    *,
    case_type_id: uuid.UUID,
    payload: dict[str, Any],
    source_ref: str | None = None,
    source_kind: str = "webhook",
    now: datetime | None = None,
) -> dict[str, Any]:
    """Run one inbound payload through filter, mapping and case creation.

    `source_ref` is whatever identifies the sender for the transport in use —
    a client IP for HTTP, a client id for a broker. It is recorded, not trusted.

    Does not commit. Behaviour is identical to the original handler, including
    the filtered-but-accepted response shape and the non-fatal process start.
    """
    now = now or datetime.now(timezone.utc)
    payload = payload if isinstance(payload, dict) else {}

    ct = await session.get(CaseTypeModel, case_type_id)
    if not ct:
        raise IntakeError(f"Case type {case_type_id} not found", status=404)
    if ct.intake_trigger not in ACCEPTED_TRIGGERS:
        raise IntakeError(
            f"Case type '{ct.name}' does not accept webhook intake "
            f"(trigger={ct.intake_trigger})",
            status=400,
        )

    event = IntakeEventModel(
        case_type_id=case_type_id,
        connector_id=ct.trigger_connector_id,
        source_ip=source_ref,
        raw_payload=payload,
        status="received",
    )
    session.add(event)
    await session.flush()

    passed, filter_results = evaluate_filters(payload, ct.filter_conditions or {})
    event.filter_result = {"passed": passed, "rules": filter_results}

    if not passed:
        # Filtered is a normal outcome, not an error: the sender did nothing
        # wrong and the event is kept so "why was nothing created" is
        # answerable later.
        event.status = "filtered"
        event.processed_at = now
        return {
            "accepted": False,
            "reason": "filter_conditions not met",
            "filter_result": filter_results,
            "event_id": str(event.id),
        }

    mapped_fields = apply_field_mapping(payload, ct.field_mapping or {})

    case_data = {
        "_intake_source": source_kind,
        "_intake_event_id": str(event.id),
        **mapped_fields,
        "_raw_payload": payload,
    }

    case = CaseInstanceModel(
        case_type_id=case_type_id,
        case_type_version=ct.version,
        tenant_id=ct.tenant_id,
        status="new",
        priority=ct.default_priority,
        data=case_data,
        created_by=f"intake_{source_kind}",
    )
    session.add(case)
    await session.flush()

    event.created_case_id = case.id
    event.status = "created"

    process_instance_id = await _maybe_start_process(
        session, ct=ct, case=case, case_data=case_data,
        case_type_id=case_type_id, event=event,
    )

    event.processed_at = now

    return {
        "accepted": True,
        "event_id": str(event.id),
        "case_id": str(case.id),
        "case_type": ct.name,
        "process_instance_id": str(process_instance_id) if process_instance_id else None,
        "mapped_fields": list(mapped_fields.keys()),
    }


async def _maybe_start_process(
    session: AsyncSession, *, ct, case, case_data, case_type_id, event,
):
    """Start the linked HxFusion process, if one is configured.

    Non-fatal by design: the case has already been created from a real inbound
    event, and discarding it because an orchestration failed to start would
    lose the customer's data to fix a workflow problem.
    """
    if not ct.process_definition_id:
        return None
    try:
        from case_service.hxfusion.engine import start_instance as _start_fusion

        defn = await session.get(ProcessDefinitionModel, ct.process_definition_id)
        if defn and defn.status == "active":
            proc_inst = await _start_fusion(
                definition_id=ct.process_definition_id,
                case_id=case.id,
                context={**case_data, "case_type_id": str(case_type_id)},
                tenant_id=str(ct.tenant_id) if ct.tenant_id else None,
                stage_id=None,
                step_id=None,
                session=session,
            )
            event.process_instance_id = proc_inst.id
            return proc_inst.id
    except Exception as exc:
        event.filter_result["process_error"] = str(exc)
        logger.warning("intake: process start failed for case %s: %s", case.id, exc)
    return None
