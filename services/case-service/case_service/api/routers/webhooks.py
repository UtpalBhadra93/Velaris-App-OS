"""Webhook management API.

CRUD for webhook subscriptions + delivery history.

Copyright (c) 2024-2025 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from velaris_shared.auth.models import AuthenticatedUser
from pydantic import BaseModel, Field
from sqlalchemy import select, func, update, delete
from sqlalchemy.ext.asyncio import AsyncSession

from velaris_shared import hxguard
from velaris_shared.db.models import WebhookSubscriptionModel, WebhookDeliveryModel
from velaris_shared.auth.dependencies import get_current_user
from velaris_shared.db.session import get_session
from case_service.integrations.webhook_dispatcher import CASE_EVENTS

router = APIRouter(prefix="/webhooks", tags=["webhooks"], dependencies=[Depends(get_current_user)])

# 8g (2026-09-29). Until today every route here needed a login and nothing
# else: a VIEWER could create a subscription with no case type — which the
# relay delivers EVERY tenant's case events to — and `/test` would POST to any
# URL, internal addresses included. Now: a privilege per route; a platform-wide
# subscription (no case type) is the install operator's alone; and every URL
# passes the same SSRF check the relay already applies before delivering.
_read = hxguard.guard("webhooks.subscriptions.read")
_write = hxguard.guard("webhooks.subscriptions.write")


async def _operator_or(session: AsyncSession, user: AuthenticatedUser, status: int = 404,
                       msg: str = "Webhook not found") -> None:
    from velaris_shared.tenancy.identity import is_platform_operator
    if not await is_platform_operator(session, user):
        raise HTTPException(status, msg)


async def _reach(session: AsyncSession, user: AuthenticatedUser, model) -> None:
    """A platform-wide subscription is the operator's; a tenant's is the path
    guard's (its case type decides) — so only the NULL case needs a check here."""
    if model.case_type_id is None:
        await _operator_or(session, user)


async def _safe_url(url: str) -> None:
    from case_service.hxbridge.security import validate_outbound_url
    try:
        await validate_outbound_url(url)
    except ValueError as exc:
        raise HTTPException(422, f"VEL-WEBHOOK-URL-422: {exc}") from exc


# ─── Schemas ──────────────────────────────────────────────────────

class WebhookCreate(BaseModel):
    name: str
    url: str
    secret: str | None = None
    events: list[str] = Field(default_factory=lambda: ["*"])
    case_type_id: str | None = None
    headers: dict[str, str] = Field(default_factory=dict)
    retry_count: int = 3
    timeout_seconds: int = 10


class WebhookUpdate(BaseModel):
    name: str | None = None
    url: str | None = None
    secret: str | None = None
    events: list[str] | None = None
    case_type_id: str | None = None
    is_active: bool | None = None
    headers: dict[str, str] | None = None
    retry_count: int | None = None
    timeout_seconds: int | None = None


class WebhookResponse(BaseModel):
    id: str
    name: str
    url: str
    events: list[str]
    case_type_id: str | None
    is_active: bool
    headers: dict[str, str]
    retry_count: int
    timeout_seconds: int
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class WebhookDeliveryResponse(BaseModel):
    id: str
    subscription_id: str
    event_type: str
    payload: dict[str, Any]
    response_status: int | None
    attempt: int
    status: str
    error_message: str | None
    delivered_at: datetime | None
    created_at: datetime

    model_config = {"from_attributes": True}


class WebhookTestResponse(BaseModel):
    success: bool
    status_code: int | None = None
    error: str | None = None


# ─── Endpoints ────────────────────────────────────────────────────

@router.get("/events")
async def list_event_types(_u: AuthenticatedUser = Depends(_read)):
    """List all supported webhook event types."""
    return {"events": CASE_EVENTS}


@router.post("", response_model=WebhookResponse, status_code=201)
async def create_webhook(
    body: WebhookCreate,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(_write),
):
    # Validate events
    for evt in body.events:
        if evt != "*" and evt not in CASE_EVENTS:
            raise HTTPException(400, f"Unknown event type: {evt}")
    if not body.case_type_id:
        await _operator_or(session, user, 403,
                           "A subscription with no case type receives every tenant's events; "
                           "only the platform operator may create one. Name a case type.")
    await _safe_url(body.url)

    model = WebhookSubscriptionModel(
        name=body.name,
        url=body.url,
        secret=body.secret,
        events=body.events,
        case_type_id=uuid.UUID(body.case_type_id) if body.case_type_id else None,
        headers=body.headers,
        retry_count=body.retry_count,
        timeout_seconds=body.timeout_seconds,
    )
    session.add(model)
    await session.flush()

    return _to_response(model)


@router.get("", response_model=list[WebhookResponse])
async def list_webhooks(
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(_read),
):
    stmt = select(WebhookSubscriptionModel).order_by(WebhookSubscriptionModel.name)
    from velaris_shared.tenancy.identity import is_platform_operator
    if not await is_platform_operator(session, user):
        # 8g: a platform-wide subscription (no case type) names where EVERY
        # tenant's events go; it is not a tenant's to see.
        stmt = stmt.where(WebhookSubscriptionModel.case_type_id.isnot(None))
    # Every tenant's rows until 2026-09-29 (8d).
    from case_service.tenancy.identity import scope_to_caller
    from velaris_shared.db.models import CaseTypeModel
    stmt = await scope_to_caller(stmt, WebhookSubscriptionModel, session, user,
                                 via=(WebhookSubscriptionModel.case_type_id, CaseTypeModel))
    result = await session.execute(stmt)
    return [_to_response(m) for m in result.scalars().all()]


@router.get("/{webhook_id}", response_model=WebhookResponse)
async def get_webhook(
    webhook_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(_read),
):
    model = await _get_or_404(session, webhook_id)
    await _reach(session, user, model)
    return _to_response(model)


@router.patch("/{webhook_id}", response_model=WebhookResponse)
async def update_webhook(
    webhook_id: uuid.UUID,
    body: WebhookUpdate,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(_write),
):
    model = await _get_or_404(session, webhook_id)
    await _reach(session, user, model)
    values = {k: v for k, v in body.model_dump(exclude_unset=True).items()}
    if "case_type_id" in values and not values["case_type_id"]:
        await _operator_or(session, user, 403,
                           "Removing the case type would make this subscription receive every "
                           "tenant's events; only the platform operator may do that.")
        values["case_type_id"] = None
    for evt in values.get("events") or []:
        if evt != "*" and evt not in CASE_EVENTS:
            raise HTTPException(400, f"Unknown event type: {evt}")
    if values.get("url"):
        await _safe_url(values["url"])
    if "case_type_id" in values and values["case_type_id"]:
        values["case_type_id"] = uuid.UUID(values["case_type_id"])
    if values:
        stmt = update(WebhookSubscriptionModel).where(
            WebhookSubscriptionModel.id == webhook_id
        ).values(**values)
        await session.execute(stmt)
    stmt2 = select(WebhookSubscriptionModel).where(WebhookSubscriptionModel.id == webhook_id)
    result = await session.execute(stmt2)
    return _to_response(result.scalar_one())


@router.delete("/{webhook_id}", status_code=204)
async def delete_webhook(
    webhook_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(_write),
):
    await _reach(session, user, await _get_or_404(session, webhook_id))
    stmt = delete(WebhookSubscriptionModel).where(WebhookSubscriptionModel.id == webhook_id)
    await session.execute(stmt)


@router.get("/{webhook_id}/deliveries", response_model=list[WebhookDeliveryResponse])
async def list_deliveries(
    webhook_id: uuid.UUID,
    limit: int = Query(50, ge=1, le=200),
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(_read),
):
    await _reach(session, user, await _get_or_404(session, webhook_id))
    stmt = (
        select(WebhookDeliveryModel)
        .where(WebhookDeliveryModel.subscription_id == webhook_id)
        .order_by(WebhookDeliveryModel.created_at.desc())
        .limit(limit)
    )
    result = await session.execute(stmt)
    return [_delivery_response(m) for m in result.scalars().all()]


@router.post("/{webhook_id}/test", response_model=WebhookTestResponse)
async def test_webhook(
    webhook_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(_write),
):
    """Send a test ping to the webhook URL."""
    model = await _get_or_404(session, webhook_id)
    await _reach(session, user, model)
    # Checked at send time too: a subscription stored before 8g, or a hostname
    # whose DNS now answers differently, must not become an internal request.
    await _safe_url(model.url)
    import json
    try:
        import httpx
        payload = json.dumps({
            "event": "webhook.test",
            "timestamp": datetime.now().isoformat(),
            "webhook_id": str(model.id),
        })
        headers = {"Content-Type": "application/json", **(model.headers or {})}
        if model.secret:
            from case_service.integrations.webhook_dispatcher import compute_signature
            headers["X-Helix-Signature"] = compute_signature(payload, model.secret)

        async with httpx.AsyncClient(timeout=model.timeout_seconds) as client:
            resp = await client.post(model.url, content=payload, headers=headers)
            return WebhookTestResponse(
                success=resp.status_code < 400,
                status_code=resp.status_code,
            )
    except Exception as e:
        return WebhookTestResponse(success=False, error=str(e)[:500])


# ─── Helpers ──────────────────────────────────────────────────────

async def _get_or_404(session, webhook_id):
    stmt = select(WebhookSubscriptionModel).where(WebhookSubscriptionModel.id == webhook_id)
    result = await session.execute(stmt)
    model = result.scalar_one_or_none()
    if model is None:
        raise HTTPException(404, "Webhook not found")
    return model


def _to_response(model) -> dict:
    return {
        "id": str(model.id),
        "name": model.name,
        "url": model.url,
        "events": model.events or [],
        "case_type_id": str(model.case_type_id) if model.case_type_id else None,
        "is_active": model.is_active,
        "headers": model.headers or {},
        "retry_count": model.retry_count,
        "timeout_seconds": model.timeout_seconds,
        "created_at": model.created_at,
        "updated_at": model.updated_at,
    }


def _delivery_response(model) -> dict:
    return {
        "id": str(model.id),
        "subscription_id": str(model.subscription_id),
        "event_type": model.event_type,
        "payload": model.payload,
        "response_status": model.response_status,
        "attempt": model.attempt,
        "status": model.status,
        "error_message": model.error_message,
        "delivered_at": model.delivered_at,
        "created_at": model.created_at,
    }
