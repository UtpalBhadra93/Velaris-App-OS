"""Pydantic schemas for the rules API.

Copyright (c) 2024-2025 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field


class RuleCreate(BaseModel):
    name: str
    version: str
    rule_type: str
    scope: str = "global"
    scope_target_id: str | None = None
    definition_json: dict[str, Any]
    enabled: bool = True
    priority: int = 0


class RuleUpdate(BaseModel):
    definition_json: dict[str, Any] | None = None
    enabled: bool | None = None
    priority: int | None = None


class RuleResponse(BaseModel):
    id: UUID
    name: str
    version: str
    rule_type: str
    scope: str
    scope_target_id: str | None
    definition_json: dict[str, Any]
    enabled: bool
    priority: int
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class RuleListResponse(BaseModel):
    items: list[RuleResponse]
    total: int
    page: int
    page_size: int


class DmnImportRequest(BaseModel):
    """A DMN file, and what to do with the tables in it.

    ``dry_run`` defaults to **True**: an import previews first and creates
    nothing, so the second click confirms something that has been read. The
    calendar's `.ics` import settled this shape and a decision table deserves it
    more — these rows decide money and eligibility.
    """

    xml: str
    dry_run: bool = True
    scope: str = "global"
    scope_target_id: str | None = None
    version: str = "1.0.0"
    #: New rules land DISABLED unless this says otherwise. An imported table
    #: that starts deciding the moment the file lands is an operator's decision
    #: nobody made.
    enabled: bool = False


class DmnImportedTable(BaseModel):
    name: str
    rule_type: str
    hit_policy: str
    input_count: int
    output_count: int
    row_count: int
    notes: list[str] = []
    #: Present only once the rules exist — a preview creates nothing.
    rule_id: UUID | None = None


class DmnImportResponse(BaseModel):
    dry_run: bool
    tables: list[DmnImportedTable]
    created: int


class RuleEvaluateRequest(BaseModel):
    """Request body for evaluating a rule against context."""
    context: dict[str, Any]


class RuleEvaluateResponse(BaseModel):
    rule_id: str
    result: dict[str, Any]


class RuleBatchEvaluateRequest(BaseModel):
    """Evaluate multiple rules against the same context."""
    rule_ids: list[UUID]
    context: dict[str, Any]


class DataValidateRequest(BaseModel):
    """Validate data against a data model's rules."""
    data_model_id: UUID
    data: dict[str, Any]


class DataValidateResponse(BaseModel):
    valid: bool
    errors: list[dict[str, Any]]
    field_count: int
    validated_count: int
