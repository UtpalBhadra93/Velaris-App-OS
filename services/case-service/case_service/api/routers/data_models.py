"""Data Models API router.

CRUD for data model definitions.

Copyright (c) 2024-2025 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from case_service.api.schemas.forms import (
    DataModelCreate,
    DataModelListResponse,
    DataModelResponse,
    DataModelUpdate,
)
from velaris_shared.db import repository as repo
from velaris_shared.auth.dependencies import require_role
from velaris_shared.db.session import get_session

from velaris_shared import hxguard

from velaris_shared.auth.dependencies import get_current_user
from velaris_shared.auth.models import AuthenticatedUser
from velaris_shared.db.models import DataModelModel as _DM
from velaris_shared.tenancy import inherit


async def _inherit(awaitable):
    """Run an `inherit` step (8e), answering its refusal as HTTP."""
    try:
        return await awaitable
    except inherit.InheritError as exc:
        raise HTTPException(exc.status, str(exc))


router = APIRouter(prefix="/data-models", tags=["data-models"], dependencies=[Depends(hxguard.guard("design.data_models.access"))])


@router.post("", response_model=DataModelResponse, status_code=201)
async def create_data_model(
    body: DataModelCreate,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(get_current_user),
):
    # 8e: a tenant's data model is its own; only the operator makes a default.
    owner = await _inherit(inherit.owner_for_create(session, user))
    if await inherit.name_taken(session, _DM, owner, name=body.name, version=body.version):
        raise HTTPException(
            status_code=409,
            detail=f"Data model '{body.name}' v{body.version} already exists",
        )

    dm = await repo.create_data_model(
        session,
        data={
            "tenant_id": owner,
            "name": body.name,
            "version": body.version,
            "definition_json": body.definition_json,
        },
    )
    return dm


@router.get("", response_model=DataModelListResponse)
async def list_data_models(
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(get_current_user),
):
    from sqlalchemy import select as _select
    clause = (await inherit.scope_list(_select(_DM), _DM, session, user)).whereclause
    items, total = await repo.list_data_models(
        session,
        offset=(page - 1) * page_size,
        limit=page_size,
        where=clause,   # 8e
    )
    await inherit.flag_updates(session, _DM, items)   # 8k
    return DataModelListResponse(
        items=items, total=total, page=page, page_size=page_size
    )


@router.get("/{model_id}", response_model=DataModelResponse)
async def get_data_model(
    model_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(get_current_user),
):
    dm = await repo.get_data_model(session, model_id)
    if dm is None:
        raise HTTPException(
            status_code=404, detail="Data model not found"
        )
    dm = await inherit.resolve_for_caller(session, user, _DM, dm)   # 8e
    return (await inherit.flag_updates(session, _DM, [dm]))[0]       # 8k


@router.patch("/{model_id}", response_model=DataModelResponse)
async def update_data_model(
    model_id: uuid.UUID,
    body: DataModelUpdate,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(get_current_user),
):
    dm = await repo.get_data_model(session, model_id)
    if dm is None:
        raise HTTPException(
            status_code=404, detail="Data model not found"
        )
    # 8e: a tenant's edit of a default lands in its fork.
    dm = await _inherit(inherit.target_for_write(session, user, _DM, dm))
    model_id = dm.id

    values = {}
    if body.definition_json is not None:
        values["definition_json"] = body.definition_json

    if values:
        await repo.update_data_model(session, model_id, values=values)

    return await repo.get_data_model(session, model_id)


@router.delete("/{model_id}", status_code=204)
async def delete_data_model(
    model_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(get_current_user),
):
    dm = await repo.get_data_model(session, model_id)
    if dm is not None:
        await _inherit(inherit.assert_may_delete(session, user, dm))   # 8e
    deleted = await repo.delete_data_model(session, model_id)
    if not deleted:
        raise HTTPException(
            status_code=404, detail="Data model not found"
        )
