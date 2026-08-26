"""App codegen API.

Copyright (c) 2024-2025 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel, Field

from velaris_shared.auth.dependencies import require_role
from velaris_shared import hxguard
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from case_service.codegen.generator import AppConfig, generate_app, generate_zip
from velaris_shared.db.session import get_session

router = APIRouter(prefix="/codegen", tags=["codegen"], dependencies=[Depends(hxguard.guard("design.codegen.access"))])


class AppGenerateRequest(BaseModel):
    app_name: str = Field(default="Velaris Mobile", min_length=1, max_length=100)
    app_slug: str = Field(default="velaris-mobile")
    primary_color: str = "#4ecdc4"
    default_api_url: str = "http://localhost:8200"
    default_tenant: str = "default"
    case_type_ids: list[str] = Field(default_factory=list)
    app_version: str = "1.0.0"
    ios_bundle_id: str = "com.example.helixmobile"
    android_package: str = "com.example.helixmobile"
    app_description: str = ""
    #: Which portal a customer build signs in against. Blank falls back to the
    #: tenant slug, which is what a one-portal-per-tenant deployment wants.
    portal_slug: str = ""
    #: both | staff | customer. A single-audience build omits the other side's
    #: screens rather than hiding them.
    audience: str = "both"


class AppGeneratePreview(BaseModel):
    files: dict[str, str]
    file_count: int
    total_size_bytes: int


@router.get("/platforms")
async def list_supported_platforms():
    """List platforms the codegen supports."""
    return {
        "platforms": [
            {
                "id": "react-native-expo",
                "name": "React Native (Expo)",
                "description": "Cross-platform mobile app for iOS, Android, and Web",
                "features": ["Case list", "Case detail", "Create case", "My work", "Settings"],
            },
        ]
    }


@router.post("/preview", response_model=AppGeneratePreview)
async def preview_app(body: AppGenerateRequest):
    """Preview generated files without downloading."""
    config = AppConfig(**body.model_dump())
    files = generate_app(config)
    return AppGeneratePreview(
        files=files,
        file_count=len(files),
        total_size_bytes=sum(len(c.encode("utf-8")) for c in files.values()),
    )


async def _check_audience_is_available(audience: str, session: AsyncSession) -> None:
    """Refuse a customer-only build against an instance with no customer accounts.

    Every `/portal/{slug}/auth/*` route answers 404 while the
    `customer_accounts` feature is off, so such a build would install, open, and
    fail at sign-in with "not found" — which reads as a broken app rather than a
    feature nobody switched on. Better to say so at build time.

    **Reads the row, not `is_feature_enabled()`.** That helper answers from an
    in-memory cache the release cron fills, and returns False for the first ~10
    seconds after any restart — its own docstring says so. Wired to the cache
    this refused a perfectly legitimate build moments after a reload, which is
    the cold-cache mistake: absence of knowledge is not evidence of "off". A
    build happens once and can afford the query.
    """
    # Only a customer-ONLY build is refused. With `both`, the staff half still
    # works and the customer half simply has nothing to sign in to — degraded,
    # not broken, and a deployment may well enable the feature later.
    if audience != "customer":
        return

    from velaris_shared.db.models import ScheduledReleaseModel
    enabled = (await session.execute(
        select(ScheduledReleaseModel.enabled)
        .where(ScheduledReleaseModel.feature_key == "customer_accounts")
    )).scalar_one_or_none()
    if enabled is None:
        raise HTTPException(
            400,
            "This Velaris instance does not have Customer Accounts enabled, so "
            "customers could not sign in to the generated app. Enable the "
            "feature, or set the audience to 'staff'.",
        )


@router.post("/generate")
async def generate_app_endpoint(
    body: AppGenerateRequest,
    session: AsyncSession = Depends(get_session),
):
    """Generate a downloadable ZIP containing the complete app."""
    await _check_audience_is_available(body.audience, session)
    config = AppConfig(**body.model_dump())
    data = generate_zip(config)
    return Response(
        content=data,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{config.app_slug}.zip"'},
    )
