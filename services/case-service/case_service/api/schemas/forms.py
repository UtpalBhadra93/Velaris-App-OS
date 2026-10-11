"""Pydantic schemas for the forms and data models API.

Copyright (c) 2024-2025 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field


# ─── Data Models ──────────────────────────────────────────────────


class DataModelCreate(BaseModel):
    name: str
    version: str
    definition_json: dict[str, Any]


class DataModelUpdate(BaseModel):
    definition_json: dict[str, Any] | None = None


class DataModelResponse(BaseModel):
    id: UUID
    name: str
    version: str
    definition_json: dict[str, Any]
    created_at: datetime
    updated_at: datetime

    # 8e: whose row this is (None = a default), and which default it forks.
    tenant_id: UUID | None = None
    forked_from_id: UUID | None = None
    # 8k: a fork whose default changed since is offered the update, never given it.
    update_available: bool = False

    model_config = {"from_attributes": True}


class DataModelListResponse(BaseModel):
    items: list[DataModelResponse]
    total: int
    page: int
    page_size: int


# ─── Forms ────────────────────────────────────────────────────────


class FormCreate(BaseModel):
    name: str
    version: str
    data_model_id: UUID | None = None
    definition_json: dict[str, Any]


class FormUpdate(BaseModel):
    data_model_id: UUID | None = None
    definition_json: dict[str, Any] | None = None
    version: str | None = None


class FormResponse(BaseModel):
    id: UUID
    name: str
    version: str
    data_model_id: UUID | None
    definition_json: dict[str, Any]
    created_at: datetime
    updated_at: datetime
    # 8e: whose form this is (None = a default), and which default it forks.
    tenant_id: UUID | None = None
    forked_from_id: UUID | None = None
    # 8k: a fork whose default changed since is offered the update, never given it.
    update_available: bool = False

    model_config = {"from_attributes": True}


class FormListResponse(BaseModel):
    items: list[FormResponse]
    total: int
    page: int
    page_size: int


# ─── Form Submissions (Phase 7) ──────────────────────────────────


class FormSubmission(BaseModel):
    form_id: UUID
    values: dict[str, Any]
    completed_by: str | None = None


class FormSubmissionResponse(BaseModel):
    assignment_id: UUID
    case_id: UUID
    form_id: UUID
    values: dict[str, Any]
    status: str
