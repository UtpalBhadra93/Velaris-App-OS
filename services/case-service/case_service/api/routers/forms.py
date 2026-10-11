"""Forms API router.

CRUD for form definitions.

Copyright (c) 2024-2025 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from case_service.api.schemas.forms import (
    FormCreate,
    FormListResponse,
    FormResponse,
    FormUpdate,
)
from velaris_shared.db import repository as repo
from velaris_shared import hxguard
from velaris_shared.auth.dependencies import get_current_user
from velaris_shared.auth.models import AuthenticatedUser
from velaris_shared.db.session import get_session
from velaris_shared.db.models import FormDefinitionModel
from velaris_shared.tenancy import inherit

router = APIRouter(prefix="/forms", tags=["forms"], dependencies=[Depends(get_current_user)])


@router.post("", response_model=FormResponse, status_code=201)
async def create_form(
    body: FormCreate,
    session: AsyncSession = Depends(get_session),
    _user: AuthenticatedUser = Depends(hxguard.guard("design.forms.write")),
):
    # Validate data model reference if provided
    if body.data_model_id:
        dm = await repo.get_data_model(session, body.data_model_id)
        if dm is None:
            raise HTTPException(
                status_code=404, detail="Data model not found"
            )

    # 8e: a tenant's form is its own; only the platform operator makes a
    # default (as before). Unique per tenant since migration 214.
    owner = await _inherit(inherit.owner_for_create(session, _user))
    if await inherit.name_taken(session, FormDefinitionModel, owner,
                                name=body.name, version=body.version):
        raise HTTPException(409, f"Form '{body.name}' v{body.version} already exists")

    form = await repo.create_form(
        session,
        data={
            "tenant_id": owner,
            "name": body.name,
            "version": body.version,
            "data_model_id": body.data_model_id,
            "definition_json": body.definition_json,
        },
    )
    return form


@router.get("", response_model=FormListResponse)
async def list_forms(
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(get_current_user),
):
    # 8e: the caller's own forms and the defaults it has not forked.
    from sqlalchemy import select as _select
    clause = (await inherit.scope_list(_select(FormDefinitionModel), FormDefinitionModel,
                                       session, user)).whereclause
    items, total = await repo.list_forms(
        session,
        offset=(page - 1) * page_size,
        limit=page_size,
        where=clause,
    )
    await inherit.flag_updates(session, FormDefinitionModel, items)   # 8k
    return FormListResponse(
        items=items, total=total, page=page, page_size=page_size
    )


@router.get("/{form_id}", response_model=FormResponse)
async def get_form(
    form_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(get_current_user),
):
    form = await repo.get_form(session, form_id)
    if form is None:
        raise HTTPException(status_code=404, detail="Form not found")
    # 8e: case types name forms by id; a tenant that forked this default gets
    # its fork. (Another tenant's form is 404 at the path guard.)
    form = await inherit.resolve_for_caller(session, user, FormDefinitionModel, form)
    return (await inherit.flag_updates(session, FormDefinitionModel, [form]))[0]   # 8k


@router.patch("/{form_id}", response_model=FormResponse)
async def update_form(
    form_id: uuid.UUID,
    body: FormUpdate,
    session: AsyncSession = Depends(get_session),
    _user: AuthenticatedUser = Depends(hxguard.guard("design.forms.write")),
):
    form = await repo.get_form(session, form_id)
    if form is None:
        raise HTTPException(status_code=404, detail="Form not found")
    # 8e: a tenant's edit of a default lands in its fork.
    form = await _inherit(inherit.target_for_write(session, _user, FormDefinitionModel, form))
    form_id = form.id

    values = {}
    if body.data_model_id is not None:
        values["data_model_id"] = body.data_model_id
    if body.definition_json is not None:
        values["definition_json"] = body.definition_json
    if body.version is not None:
        values["version"] = body.version

    if values:
        await repo.update_form(session, form_id, values=values)

    return await repo.get_form(session, form_id)


@router.delete("/{form_id}", status_code=204)
async def delete_form(
    form_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    _user: AuthenticatedUser = Depends(hxguard.guard("design.forms.write")),
):
    form = await repo.get_form(session, form_id)
    if form is not None:
        await _inherit(inherit.assert_may_delete(session, _user, form))
    deleted = await repo.delete_form(session, form_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Form not found")


async def _inherit(awaitable):
    """Run an `inherit` step, answering its refusal as HTTP."""
    try:
        return await awaitable
    except inherit.InheritError as exc:
        raise HTTPException(exc.status, str(exc))
