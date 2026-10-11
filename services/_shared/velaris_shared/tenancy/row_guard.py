"""A router-level "whose row is this?" check for services without 8d's guard.

case-service has `core/tenant_path_guard.py`: one app-wide dependency that, for
every id in a path, loads the row and answers 404 when the caller's tenant does
not own it. The other services had nothing of the kind, and until migration 213
(8e, 2026-10-01) the hxmigrate tables had no owner to check anyway.

This is the same rule, declared per router:

    router = APIRouter(dependencies=[Depends(owned_rows({
        "job_id": ImportJobModel,
        "task_id": (MigrationTaskModel, "project_id", MigrationProjectModel),
    }))])

* a missing row passes — the handler's own 404 is more specific;
* a NULL tenant is platform-shared and passes, as `same_tenant` has it;
* superadmin passes (break-glass);
* anything else in another tenant is 404, indistinguishable from missing.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import uuid
from typing import Any

from fastapi import Depends, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession

from velaris_shared.auth.dependencies import get_current_user
from velaris_shared.auth.models import AuthenticatedUser
from velaris_shared.db.session import get_session
from velaris_shared.tenancy.identity import is_superadmin, same_tenant

#: param → Model, or (Model, fk_column_on_model, ParentModel) for a row whose
#: owner is its parent's.
Rules = dict[str, Any]


def owned_rows(rules: Rules):
    async def _check(
        request: Request,
        session: AsyncSession = Depends(get_session),
        user: AuthenticatedUser = Depends(get_current_user),
    ) -> None:
        if is_superadmin(user):
            return
        for param, spec in rules.items():
            raw = request.path_params.get(param)
            if raw is None:
                continue
            try:
                key = uuid.UUID(str(raw))
            except ValueError:
                continue
            model, fk, parent = (spec if isinstance(spec, tuple) else (spec, None, None))
            row = await session.get(model, key)
            if row is None:
                continue
            if fk is not None:
                pid = getattr(row, fk, None)
                row = await session.get(parent, pid) if pid is not None else None
                if row is None:
                    continue
            if not await same_tenant(session, row.tenant_id, user.tenant_id):
                raise HTTPException(status_code=404, detail="Not found")
    return _check
