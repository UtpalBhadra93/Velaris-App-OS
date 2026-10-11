"""Case Type API router.

Handles deployment, listing, updating, and removal of case type definitions.

Copyright (c) 2024-2025 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import func, update as sa_update, select
from sqlalchemy.ext.asyncio import AsyncSession

from case_service.api.schemas.cases import (
    CaseTypeCreate,
    CaseTypeListResponse,
    CaseTypeResponse,
)
from case_service.case_types import ownership
from velaris_shared.db import repository as repo
from velaris_shared.db.models import case_type_tenant_key
from velaris_shared.db.models import (
    CaseInstanceModel,
    CaseTypeModel,
    CaseTypeMigrationModel,
)
from velaris_shared import hxguard
from velaris_shared.auth.dependencies import get_current_user
from velaris_shared.auth.models import AuthenticatedUser
from velaris_shared.db.session import get_session

router = APIRouter(prefix="/case-types", tags=["case-types"], dependencies=[Depends(get_current_user)])


async def _validate_stage_config(
    session: AsyncSession,
    definition_json: Any,
    *,
    tenant_id: uuid.UUID | None,
    case_type_id: uuid.UUID | None,
) -> None:
    """Refuse a stage naming an action or a status that does not exist.

    Checked here rather than at runtime because both failures are silent by
    nature. An `actions_allowed` holding a typo narrows the stage to fewer
    actions than its author listed and says nothing; an `on_enter_status`
    holding a typo moves the case and changes nothing. Neither shows up until
    somebody notices a button missing or a status not moving, which may be
    weeks.

    A definition with neither key is untouched, so every case type that exists
    today deploys and updates exactly as it does now.
    """
    from case_service.case_actions import stages as _stages

    stage_list = ((definition_json or {}).get("stages")
                  if isinstance(definition_json, dict) else None)
    if not stage_list:
        return
    try:
        _stages.validate_actions_allowed(stage_list)
        await _stages.validate_on_enter_status(
            session, stage_list, tenant_id=tenant_id, case_type_id=case_type_id)
    except _stages.StageConfigError as exc:
        raise HTTPException(status_code=400, detail=f"{exc.code}: {exc}") from exc


@router.post("", response_model=CaseTypeResponse, status_code=201)
async def deploy_case_type(
    body: CaseTypeCreate,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(get_current_user),
):
    """Deploy (register) a new case type definition.

    Access rules (8i): the platform operator may create a default
    (tenant_id = None) or a type for any tenant, exactly as before; anybody
    else creates in their own tenant, and naming another tenant is 403.
    """
    owner = await authorize_create(session, user, body.tenant_id)
    if await name_taken(session, owner, body.name, body.version):
        raise HTTPException(
            status_code=409,
            detail=f"Case type '{body.name}' v{body.version} already exists",
        )

    await _validate_stage_config(
        session, body.definition_json,
        tenant_id=owner, case_type_id=None)

    ct = await repo.create_case_type(
        session,
        data={
            "name": body.name,
            "version": body.version,
            "tenant_id": owner,
            "lifecycle_process_id": body.lifecycle_process_id,
            "data_model_id": body.data_model_id,
            "security_profile_id": body.security_profile_id,
            "default_priority": body.default_priority,
            "definition_json": body.definition_json,
            "icon": body.icon,
            "color": body.color,
            "description": body.description,
            "tags": body.tags,
        },
    )

    return ct


@router.get("", response_model=CaseTypeListResponse)
async def list_case_types(
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    tenant_id: uuid.UUID | None = None,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(get_current_user),
):
    # Resolve which tenant to filter by:
    #   1. Explicit ?tenant_id param (admin override)
    #   2. Auto-derive from the authenticated user's tenant
    #   3. Superadmin with no tenant → no filter (sees everything)
    # 8d (2026-09-29): a tenant token that is a SLUG failed the UUID parse and
    # fell through to "no tenant" — every tenant's case types — and any caller
    # could name another tenant in `tenant_id`. Now the caller's own tenant,
    # resolved in either spelling; only superadmin may name another.
    from case_service.tenancy.identity import is_superadmin, resolve_tenant

    effective_tenant_id = tenant_id if is_superadmin(user) else None
    if effective_tenant_id is None and not is_superadmin(user):
        resolved = await resolve_tenant(session, str(user.tenant_id or "default"))
        # A token that resolves to no tenant fell through to NO filter — every
        # tenant's case types (8i). The nil UUID owns nothing, so it now sees
        # the defaults alone.
        effective_tenant_id = resolved[0] if resolved is not None else uuid.UUID(int=0)

    items, total = await repo.list_case_types(
        session,
        offset=(page - 1) * page_size,
        limit=page_size,
        tenant_id=effective_tenant_id,   # returns tenant's + globals (tenant_id IS NULL)
    )
    stale = await ownership.updates_available(session, items)
    return CaseTypeListResponse(
        items=[_with_update_flag(i, stale) for i in items],
        total=total, page=page, page_size=page_size,
    )


def _with_update_flag(ct: CaseTypeModel, stale: set[uuid.UUID]) -> CaseTypeResponse:
    out = CaseTypeResponse.model_validate(ct)
    out.update_available = ct.id in stale
    return out


# ── 8i: moving legacy defaults to the tenant that uses them ─────────────────
# Declared before `/{case_type_id}`, which would otherwise claim the path.

class BackfillApply(BaseModel):
    case_type_ids: list[uuid.UUID]


async def _operator_only(session: AsyncSession, user: AuthenticatedUser) -> None:
    from case_service.tenancy.identity import is_platform_operator
    if not await is_platform_operator(session, user):
        raise HTTPException(status_code=403,
                            detail="Only the platform operator may move default case types.")


@router.get("/ownership/backfill")
async def preview_ownership_backfill(
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(hxguard.guard("design.case_types.write")),
):
    """Every default case type, who uses it, and what the operator could move.
    Reads only."""
    await _operator_only(session, user)
    return {"items": await ownership.backfill_proposals(session)}


@router.post("/ownership/backfill")
async def apply_ownership_backfill(
    body: BackfillApply,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(hxguard.guard("design.case_types.write")),
):
    """Move the chosen defaults; each is re-checked and skipped with a reason
    when it is no longer a one-tenant default."""
    await _operator_only(session, user)
    return await ownership.apply_backfill(session, body.case_type_ids)


@router.get("/{case_type_id}", response_model=CaseTypeResponse)
async def get_case_type(
    case_type_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(get_current_user),
):
    ct = await repo.get_case_type(session, case_type_id)
    if ct is None:
        raise HTTPException(
            status_code=404, detail="Case type not found"
        )
    # Read-side mirror of the write rule: a tenant-owned case type is invisible
    # outside its tenant (404, anti-oracle). Global case types (tenant_id NULL)
    # and tenant-less callers (platform operators) behave exactly as before.
    # A tenant token may be the slug or the id; a literal string compare refused
    # slug-token users their OWN case types (2026-09-29).
    from case_service.tenancy.identity import same_tenant as _same_tenant
    if ct.tenant_id is not None and user.tenant_id \
            and not await _same_tenant(session, ct.tenant_id, user.tenant_id):
        raise HTTPException(status_code=404, detail="Case type not found")
    return _with_update_flag(ct, await ownership.updates_available(session, [ct]))


async def _assert_owns(
    session: AsyncSession, user: AuthenticatedUser, ct: CaseTypeModel,
) -> None:
    """Delete and restore act on the row itself, never on a fork (8i).

    They were gated on the privilege alone: any tenant's admin could delete
    another tenant's case type, or a default out from under every tenant.
    Another tenant's row is 404; a default is the platform operator's.
    """
    if await ownership.may_write_in_place(session, user, ct):
        return
    if ct.tenant_id is not None:
        raise HTTPException(status_code=404, detail="Case type not found")
    raise HTTPException(
        status_code=403,
        detail="A default case type is shared by every tenant; only the "
               "platform operator may delete or restore it.")


async def authorize_create(
    session: AsyncSession, user: AuthenticatedUser, requested: uuid.UUID | None,
) -> uuid.UUID | None:
    """The tenant a new case type goes into, after the privilege check (8i).

    Every path that creates a case type — this route, HxDraft, HxDBMigrate,
    the NLP builder, the app import — comes through here. The platform operator
    keeps the tenant as asked (NULL = a default, as before); anybody else
    creates in their own tenant.
    """
    try:
        owner = await ownership.owner_for_create(session, user, requested)
    except ownership.OwnershipError as exc:
        raise HTTPException(status_code=403, detail=str(exc))
    _assert_can_write_case_type(user, owner, action="create")
    return owner


async def name_taken(
    session: AsyncSession, owner: uuid.UUID | None, name: str, version: str,
) -> bool:
    """Is name + version already used in this owner's namespace? Unique per
    tenant since migration 212; a default and a tenant's own may share it."""
    return (await session.execute(
        select(CaseTypeModel.id).where(
            CaseTypeModel.tenant_key == case_type_tenant_key(owner),
            CaseTypeModel.name == name,
            CaseTypeModel.version == version,
        ).limit(1)
    )).first() is not None


async def authorize_edit(
    session: AsyncSession, user: AuthenticatedUser, ct: CaseTypeModel,
) -> CaseTypeModel:
    """The row an edit lands on (8i): the row itself, or the caller's fork of a
    default the caller may not change for every tenant.

    Another tenant's row is 404, as a read of it is. The privilege is checked
    BEFORE a fork is made, so a refused edit leaves nothing behind.
    """
    if await ownership.may_write_in_place(session, user, ct):
        _assert_can_write_case_type(user, ct.tenant_id, action="update")
        return ct
    if ct.tenant_id is not None:
        raise HTTPException(status_code=404, detail="Case type not found")
    mine = await ownership.caller_tenant(session, user)
    if mine is None:
        raise HTTPException(status_code=403, detail="Your tenant could not be resolved.")
    _assert_can_write_case_type(user, mine, action="update")
    try:
        return await ownership.fork(session, ct, mine)
    except ownership.OwnershipError as exc:
        raise HTTPException(status_code=409, detail=str(exc))


def _assert_can_write_case_type(
    user: AuthenticatedUser,
    case_type_tenant_id: uuid.UUID | None,
    action: str = "update",
) -> None:
    """Raise 403 if the user cannot create or edit this case type.

    Authorization is driven entirely by the privileges configured in the
    Access Directory (access_roles.privileges).  No role name is hardcoded.

    Rules:
      - Global (tenant_id = None):
            needs case_type write privilege
            + admin.manage privilege (platform administration gate)
      - Tenant-owned:
            needs case_type write privilege
            + user must belong to that tenant
    """
    # Superadmin bypasses everything
    if "superadmin" in (user.roles or []):
        return

    can_write       = user.has_privilege("case_type", action)
    can_edit_global = user.has_privilege("case_type", "global.write")

    if case_type_tenant_id is None:
        # Global case type — requires case_type write + case_type.global.write privilege.
        # Admin bypasses automatically.
        # Developers get this via: Access Directory → Access Roles → add privilege
        #   resource: case_type  action: global.write
        # No admin console access is needed or implied.
        if not (hxguard.user_allows(user, "design.case_types.write")
                or (can_write and can_edit_global)):
            raise HTTPException(
                status_code=403,
                detail=(
                    "Editing global case types requires the 'case_type / global.write' privilege. "
                    "Ask your admin to add it to your access role in "
                    "Admin Console → Access Directory → Access Roles."
                ),
            )
    else:
        # Tenant-owned — privilege + must belong to that tenant.
        # Phase 4 step 3 removed `has_privilege`'s fallback to `is_admin`, so
        # `can_write` alone would now refuse an admin who has no access group
        # assigned. The platform privilege is named explicitly instead of
        # arriving invisibly through that fallback.
        if not (can_write or hxguard.user_allows(user, "design.case_types.write")):
            raise HTTPException(
                status_code=403,
                detail=(
                    "Your access role does not grant case_type write privileges. "
                    "Configure this in Access Directory → Access Roles."
                ),
            )
        # Which tenant's row this is was decided by the caller (8i:
        # `authorize_create` / `authorize_edit`); a string compare here refused
        # slug-token users their own case types.


@router.patch("/{case_type_id}", response_model=CaseTypeResponse)
async def update_case_type(
    case_type_id: uuid.UUID,
    body: dict[str, Any],
    background_tasks: BackgroundTasks,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(get_current_user),
):
    """Update a case type's definition, description, or other fields."""
    ct = await repo.get_case_type(session, case_type_id)
    if ct is None:
        raise HTTPException(
            status_code=404, detail="Case type not found"
        )

    # 8i: a tenant editing a default edits its own fork of it from now on.
    ct = await authorize_edit(session, user, ct)
    case_type_id = ct.id

    allowed = {
        "definition_json", "description", "default_priority",
        "tags", "icon", "color",
        # Intake trigger fields
        "intake_trigger", "trigger_connector_id",
        "filter_conditions", "field_mapping", "process_definition_id",
    }
    values = {k: v for k, v in body.items() if k in allowed}

    if "definition_json" in values:
        # Scoped to the case type being edited, so a status this case type
        # defines for itself is accepted and a neighbouring tenant's is not.
        await _validate_stage_config(
            session, values["definition_json"],
            tenant_id=ct.tenant_id, case_type_id=case_type_id)

    if values:
        stmt = (
            sa_update(CaseTypeModel)
            .where(CaseTypeModel.id == case_type_id)
            .values(**values)
        )
        await session.execute(stmt)

    result = await repo.get_case_type(session, case_type_id)

    # Auto-regenerate any linked HxDocs lifecycle articles in the background
    if "definition_json" in values:
        background_tasks.add_task(_sync_lifecycle_docs, case_type_id)
        # #27 Part B: definition changed → refresh the case type's structural test
        # suite (deterministic); its AI scenarios are flagged stale for manual regen.
        from case_service.testsuite import regen
        background_tasks.add_task(regen.bg_case_type_changed, case_type_id)

    return result


async def _sync_lifecycle_docs(case_type_id: uuid.UUID) -> None:
    """Background task: find and regenerate all lifecycle articles for this case type."""
    import logging
    log = logging.getLogger(__name__)
    try:
        from velaris_shared.db.session import get_engine
        from sqlalchemy.ext.asyncio import AsyncSession as _Session, async_sessionmaker
        from case_service.hxdocs import service as docs_service
        from velaris_shared.hxstream.emitter import emit_trace

        engine = get_engine()
        factory = async_sessionmaker(engine, class_=_Session, expire_on_commit=False)
        async with factory() as session:
            articles = await docs_service.find_lifecycle_articles_for_case_type(session, case_type_id)
            log.info("lifecycle auto-sync: found %d articles for case_type %s", len(articles), case_type_id)
            updated_ids: list[str] = []
            for article in articles:
                await docs_service.regenerate_lifecycle_article(session, article, actor="system")
                updated_ids.append(str(article.id))
            if articles:
                await session.commit()
                # Emit one event per article so the frontend can match by article_id
                for art_id in updated_ids:
                    await emit_trace(
                        "docs.lifecycle_auto_synced",
                        {"case_type_id": str(case_type_id), "article_id": art_id},
                    )
                log.info("lifecycle auto-sync: emitted events for %s", updated_ids)
            else:
                log.info("lifecycle auto-sync: no articles found for case_type %s", case_type_id)
    except Exception as exc:
        import logging as _logging
        _logging.getLogger(__name__).warning("lifecycle auto-sync failed: %s", exc, exc_info=True)


@router.delete("/{case_type_id}", status_code=204)
async def delete_case_type(
    case_type_id: uuid.UUID,
    hard: bool = False,
    session: AsyncSession = Depends(get_session),
    _user: AuthenticatedUser = Depends(hxguard.guard("design.case_types.write")),
):
    """Soft-delete a case type (or hard-delete if ?hard=true).

    Soft-deleted case types are excluded from list by default
    but can be restored via POST /{id}/restore.
    """
    ct = await repo.get_case_type(session, case_type_id)
    if ct is None:
        raise HTTPException(status_code=404, detail="Case type not found")
    await _assert_owns(session, _user, ct)

    if hard:
        # A hard delete of a case type that still has cases violates the
        # case_instances FK. Left unguarded that surfaced as an unhandled
        # IntegrityError (500); the caller needs a conflict it can act on.
        in_use = (await session.execute(
            select(func.count())
            .select_from(CaseInstanceModel)
            .where(CaseInstanceModel.case_type_id == case_type_id)
        )).scalar_one()
        if in_use:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"Case type still has {in_use} case(s); "
                    "delete them first or soft-delete this case type"
                ),
            )
        await repo.delete_case_type(session, case_type_id)
    else:
        now = datetime.now(timezone.utc)
        stmt = (
            sa_update(CaseTypeModel)
            .where(CaseTypeModel.id == case_type_id)
            .values(is_deleted=True, deleted_at=now)
        )
        await session.execute(stmt)


@router.post("/{case_type_id}/restore", response_model=CaseTypeResponse)
async def restore_case_type(
    case_type_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    _user: AuthenticatedUser = Depends(hxguard.guard("design.case_types.write")),
):
    """Restore a soft-deleted case type."""
    ct = await repo.get_case_type(session, case_type_id)
    if ct is None:
        raise HTTPException(status_code=404, detail="Case type not found")
    await _assert_owns(session, _user, ct)

    if not getattr(ct, "is_deleted", False):
        raise HTTPException(status_code=409, detail="Case type is not deleted")

    stmt = (
        sa_update(CaseTypeModel)
        .where(CaseTypeModel.id == case_type_id)
        .values(is_deleted=False, deleted_at=None, deleted_by=None)
    )
    await session.execute(stmt)
    return await repo.get_case_type(session, case_type_id)


# ── Migration History (audit trail) ──────────────────────────────────────────

class MigrationHistoryRecord(BaseModel):
    id:                   uuid.UUID
    case_type_id:         uuid.UUID
    run_id:               uuid.UUID | None
    source_platform:      str
    source_filename:      str
    imported_by_user_id:  str
    imported_by_email:    str
    imported_at:          str
    stages_count:         int
    steps_count:          int
    forms_count:          int
    rules_count:          int
    slas_count:           int
    notes:                str


@router.get("/{case_type_id}/migration-history", response_model=list[MigrationHistoryRecord])
async def get_migration_history(
    case_type_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
):
    """Return the full import history for a case type.

    Visible to all authenticated users — no special permission required.
    Every HxMigrate import appends an immutable record here.
    """
    rows = (await session.execute(
        select(CaseTypeMigrationModel)
        .where(CaseTypeMigrationModel.case_type_id == case_type_id)
        .order_by(CaseTypeMigrationModel.imported_at.desc())
    )).scalars().all()

    return [
        MigrationHistoryRecord(
            id=r.id,
            case_type_id=r.case_type_id,
            run_id=r.run_id,
            source_platform=r.source_platform,
            source_filename=r.source_filename,
            imported_by_user_id=r.imported_by_user_id,
            imported_by_email=r.imported_by_email,
            imported_at=r.imported_at.isoformat(),
            stages_count=r.stages_count,
            steps_count=r.steps_count,
            forms_count=r.forms_count,
            rules_count=r.rules_count,
            slas_count=r.slas_count,
            notes=r.notes,
        )
        for r in rows
    ]
