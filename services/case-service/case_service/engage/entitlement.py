"""Is Customer Engagement switched on here — and for THIS tenant?

Design: docs/Future/Done/CustomerEngagement.md §11b. The packaging was settled with
the user on 2026-08-27: **the code ships with the platform and is off by
default; the marketplace listing is the entitlement, and installing it is what
turns the feature on for that tenant.**

The listing carries no payload. It is not a content pack (this needs tables,
endpoints and hxguard actions) and it is not a sandboxed package (those have zero
production data access by design, which is the one thing a 360 must have). So the
install writes an entitlement, and the platform code — already on the box,
dormant — wakes up for that tenant.

**Two gates, and the inner one is per TENANT.** Today's release flags are global;
an install is a tenant's decision. A platform-wide flip would switch a customer
360 on for every tenant on a shared box, which is precisely the thing this
must not do:

    released (platform)  AND  entitled (this tenant)

The entitlement is an existing `marketplace_installs` row rather than a new
table. That is not a shortcut — it is what "installing the listing is the
entitlement" means, and a second table would be a second place for the answer to
live.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import logging
import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

log = logging.getLogger(__name__)

#: The marketplace listing whose install is the entitlement.
PACKAGE_ID = "velaris/customer-engagement"
#: The platform release flag — the OUTER gate.
FEATURE_KEY = "customer_engagement"


async def released(session: AsyncSession) -> bool:
    """Has the platform released the feature at all?

    A missing row is **not released**. An install that has never heard of this
    feature must not have it switched on by a tenant installing a listing.
    """
    from velaris_shared.db.models import ScheduledReleaseModel

    try:
        row = (await session.execute(
            select(ScheduledReleaseModel).where(
                ScheduledReleaseModel.feature_key == FEATURE_KEY))).scalars().first()
    except Exception as exc:                                     # noqa: BLE001
        log.info("engage: release flag unreadable: %s", exc)
        return False
    if row is None:
        return False
    return str(row.enabled or "").strip().lower() in ("1", "true", "yes", "on") \
        or str(row.status or "").strip().lower() == "released"


async def entitled(session: AsyncSession, tenant_token: str) -> bool:
    """Has THIS tenant installed the listing, and not revoked it?"""
    from velaris_shared.db.models import MarketplaceInstallModel

    try:
        row = (await session.execute(
            select(MarketplaceInstallModel).where(
                MarketplaceInstallModel.tenant_id == str(tenant_token),
                MarketplaceInstallModel.package_id == PACKAGE_ID,
                MarketplaceInstallModel.revoked_at.is_(None)))).scalars().first()
    except Exception as exc:                                     # noqa: BLE001
        log.info("engage: entitlement unreadable: %s", exc)
        return False
    return row is not None


async def enabled_for(session: AsyncSession, tenant_token: str) -> tuple[bool, str]:
    """`(allowed, why_not)`. Both gates, reported separately.

    An operator who has not released the feature and a tenant who has not
    installed it are different situations that send somebody to different
    screens, so "unavailable" on its own is not an acceptable answer.
    """
    if not await released(session):
        return False, ("Customer Engagement has not been released on this "
                       "platform (feature flag 'customer_engagement')")
    if not await entitled(session, tenant_token):
        return False, ("this tenant has not installed Customer Engagement from "
                       "the marketplace")
    return True, ""


async def status(session: AsyncSession, tenant_token: str) -> dict[str, Any]:
    is_released = await released(session)
    is_entitled = await entitled(session, tenant_token)
    return {
        "enabled": is_released and is_entitled,
        "released": is_released,
        "entitled": is_entitled,
        "package_id": PACKAGE_ID,
        "feature_key": FEATURE_KEY,
        "note": ("The platform releases the feature; a tenant installs the "
                 "listing. Both, or it stays dark for that tenant."),
    }
