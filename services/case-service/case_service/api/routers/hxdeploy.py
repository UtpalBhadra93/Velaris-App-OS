"""P55 HxDeploy — Intelligent Deployment Governance router."""
from __future__ import annotations

import hashlib
import hmac
import uuid
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from velaris_shared.auth.dependencies import get_current_user
from velaris_shared.auth.models import AuthenticatedUser
from case_service.core.timestamps import iso_utc
from velaris_shared.db.session import get_session
from case_service.hxdeploy import service
from velaris_shared.middleware.endpoint_rate_limit import rate_limit
from velaris_shared import hxguard

router = APIRouter(prefix="/deploy", tags=["deploy"])

#: The bundle receiver and the CI/CD callback authenticate on a shared key and
#: an HMAC rather than a session, so they carry their own bound: without one,
#: guessing an environment's import key is limited only by how fast a caller can
#: send requests.
_import_rl = rate_limit(max_calls=30, window_seconds=60, name="deploy-import")


def _tenant(user: AuthenticatedUser) -> str:
    return getattr(user, "tenant_id", None) or "default"


def _actor(user: AuthenticatedUser) -> str:
    """Return the most human-readable identifier for the user."""
    return (getattr(user, "username", None)
            or getattr(user, "email", None)
            or getattr(user, "user_id", None)
            or "system")


# ── Schemas ───────────────────────────────────────────────────────────────────

class EnvIn(BaseModel):
    name:            str
    label:           str
    url:             str | None = None
    order_index:     int = 0
    delivery_method: str = "manual"   # manual | webhook | push
    webhook_url:     str | None = None
    webhook_secret:  str | None = None
    import_api_key:  str | None = None


class EnvOut(BaseModel):
    id:               uuid.UUID
    name:             str
    label:            str
    url:              str | None
    order_index:      int
    current_version:  str | None
    status:           str
    last_deployed_at: str | None
    delivery_method:  str
    webhook_url:      str | None
    # secrets are never returned in GET — only their presence is indicated
    has_webhook_secret: bool
    has_import_api_key: bool

    model_config = {"from_attributes": True}

    @classmethod
    def from_model(cls, e: Any) -> "EnvOut":
        return cls(
            id=e.id, name=e.name, label=e.label, url=e.url,
            order_index=e.order_index, current_version=e.current_version,
            status=e.status,
            last_deployed_at=e.last_deployed_at.isoformat() if e.last_deployed_at else None,
            delivery_method=getattr(e, "delivery_method", "manual"),
            webhook_url=getattr(e, "webhook_url", None),
            has_webhook_secret=bool(getattr(e, "webhook_secret", None)),
            has_import_api_key=bool(getattr(e, "import_api_key", None)),
        )


class PromoteIn(BaseModel):
    to_env_id:          uuid.UUID
    package_id:         uuid.UUID | None = None
    package_manifest:   dict = {}
    from_env_id:        uuid.UUID | None = None
    deploy_notes:       str = ""
    assign_to_user_id:  str | None = None
    assign_to_name:     str | None = None


class ApproveIn(BaseModel):
    package_manifest: dict = {}


class RejectIn(BaseModel):
    reason: str


class RunOut(BaseModel):
    id:              uuid.UUID
    risk_level:      str
    risk_summary:    dict
    status:          str
    initiated_by:    str
    approved_by:     str | None
    rejected_by:     str | None
    rejection_reason: str | None
    deploy_notes:    str | None
    to_env_id:       uuid.UUID | None
    from_env_id:     uuid.UUID | None
    package_id:      uuid.UUID | None
    package_checksum: str | None = None
    created_at:      str
    deployed_at:     str | None
    completed_at:    str | None

    @classmethod
    def from_model(cls, r: Any) -> "RunOut":
        return cls(
            id=r.id, risk_level=r.risk_level, risk_summary=r.risk_summary,
            status=r.status, initiated_by=r.initiated_by,
            approved_by=r.approved_by, rejected_by=r.rejected_by,
            rejection_reason=r.rejection_reason, deploy_notes=r.deploy_notes,
            to_env_id=r.to_env_id, from_env_id=r.from_env_id, package_id=r.package_id,
            package_checksum=getattr(r, "package_checksum", None),
            created_at=r.created_at.isoformat(),
            deployed_at=r.deployed_at.isoformat() if r.deployed_at else None,
            completed_at=r.completed_at.isoformat() if r.completed_at else None,
        )


class RunPatchIn(BaseModel):
    deploy_notes:     str | None = None
    to_env_id:        uuid.UUID | None = None
    package_manifest: dict | None = None


class WindowIn(BaseModel):
    env_id:         uuid.UUID
    name:           str
    days_of_week:   list[int] = list(range(7))
    start_hour_utc: int = 0
    end_hour_utc:   int = 23
    enabled:        bool = True


class HealthCheckOut(BaseModel):
    id:          uuid.UUID
    run_id:      uuid.UUID
    check_url:   str | None
    status_code: int | None
    response_ms: int | None
    healthy:     bool | None
    error:       str | None
    checked_at:  str

    @classmethod
    def from_model(cls, h: Any) -> "HealthCheckOut":
        return cls(
            id=h.id, run_id=h.run_id, check_url=h.check_url,
            status_code=h.status_code, response_ms=h.response_ms,
            healthy=h.healthy, error=h.error,
            checked_at=h.checked_at.isoformat(),
        )


# ── Environments ──────────────────────────────────────────────────────────────

@router.get("/environments", response_model=list[EnvOut])
async def list_environments(
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(hxguard.guard("deploy.environments.read")),
):
    envs = await service.list_environments(session, _tenant(user))
    return [EnvOut.from_model(e) for e in envs]


@router.post("/environments", response_model=EnvOut, status_code=201)
async def register_environment(
    body: EnvIn,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(hxguard.guard("deploy.environments.write")),
):
    env = await service.register_environment(
        session, _tenant(user), body.name, body.label, body.url, body.order_index,
        delivery_method=body.delivery_method,
        webhook_url=body.webhook_url,
        webhook_secret=body.webhook_secret,
        import_api_key=body.import_api_key,
    )
    await session.commit()
    return EnvOut.from_model(env)


@router.patch("/environments/{env_id}", response_model=EnvOut)
async def update_environment(
    env_id: uuid.UUID,
    body: EnvIn,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(hxguard.guard("deploy.environments.write")),
):
    env = await _env_or_404(session, env_id, user)
    env.label            = body.label
    env.url              = body.url if body.url is not None else env.url
    env.order_index      = body.order_index
    env.delivery_method  = body.delivery_method
    if body.webhook_url is not None:
        env.webhook_url = body.webhook_url
    if body.webhook_secret is not None:
        env.webhook_secret = body.webhook_secret
    if body.import_api_key is not None:
        env.import_api_key = body.import_api_key
    await session.commit()
    return EnvOut.from_model(env)


@router.delete("/environments/{env_id}", status_code=204)
async def delete_environment(
    env_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(hxguard.guard("deploy.environments.write")),
):
    env = await _env_or_404(session, env_id, user)
    await session.delete(env)
    await session.commit()


@router.get("/environments/{env_id}/status")
async def get_environment_status(
    env_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(hxguard.guard("deploy.environments.read")),
):
    await _env_or_404(session, env_id, user)
    return await service.get_environment_status(session, env_id)


# ── Deployment Runs ───────────────────────────────────────────────────────────

@router.post("/promote", response_model=RunOut, status_code=201)
async def promote(
    body: PromoteIn,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(hxguard.guard("deploy.runs.promote")),
):
    try:
        run = await service.promote(
            session, _tenant(user),
            to_env_id=body.to_env_id,
            package_id=body.package_id,
            package_manifest=body.package_manifest,
            initiated_by=_actor(user),
            deploy_notes=body.deploy_notes,
            from_env_id=body.from_env_id,
            assign_to_user_id=body.assign_to_user_id,
            assign_to_name=body.assign_to_name,
        )
        await session.commit()
        return RunOut.from_model(run)
    except ValueError as exc:
        raise HTTPException(400, str(exc))


@router.get("/runs", response_model=list[RunOut])
async def list_runs(
    status: str | None = None,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(hxguard.guard("deploy.runs.read")),
):
    rows = await service.list_runs(session, _tenant(user), status=status)
    return [RunOut.from_model(r) for r in rows]


@router.get("/runs/{run_id}", response_model=RunOut)
async def get_run(
    run_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(hxguard.guard("deploy.runs.read")),
):
    await _run_or_404(session, run_id, user)
    run = await service.get_run(session, run_id)
    if not run:
        raise HTTPException(404, "Run not found")
    return RunOut.from_model(run)


@router.post("/runs/{run_id}/approve", response_model=RunOut)
async def approve_run(
    run_id: uuid.UUID,
    body: ApproveIn,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(hxguard.guard("deploy.runs.approve")),
):
    await _run_or_404(session, run_id, user)
    try:
        run = await service.approve_run(session, run_id, _actor(user), body.package_manifest)
        await session.commit()
        return RunOut.from_model(run)
    except PermissionError as exc:
        # 403, not 400: the request is well formed and the caller holds the
        # action — it is *this* caller who may not approve *this* run.
        raise HTTPException(403, str(exc))
    except ValueError as exc:
        raise HTTPException(400, str(exc))


@router.post("/runs/{run_id}/reject", response_model=RunOut)
async def reject_run(
    run_id: uuid.UUID,
    body: RejectIn,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(hxguard.guard("deploy.runs.approve")),
):
    await _run_or_404(session, run_id, user)
    try:
        run = await service.reject_run(session, run_id, _actor(user), body.reason)
        await session.commit()
        return RunOut.from_model(run)
    except ValueError as exc:
        raise HTTPException(400, str(exc))


@router.post("/runs/{run_id}/health-check", response_model=HealthCheckOut)
async def run_health_check(
    run_id: uuid.UUID,
    check_url: str | None = None,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(hxguard.guard("deploy.runs.read")),
):
    await _run_or_404(session, run_id, user)
    try:
        hc = await service.run_health_check(session, run_id, check_url)
        await session.commit()
        return HealthCheckOut.from_model(hc)
    except ValueError as exc:
        raise HTTPException(400, str(exc))


@router.patch("/runs/{run_id}", response_model=RunOut)
async def update_run(
    run_id: uuid.UUID,
    body: RunPatchIn,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(hxguard.guard("deploy.runs.promote")),
):
    run = await _run_or_404(session, run_id, user)
    if run.status not in ("pending", "awaiting_approval"):
        raise HTTPException(400, f"Cannot edit run in status '{run.status}'")

    if body.deploy_notes is not None:
        run.deploy_notes = body.deploy_notes

    if body.to_env_id is not None and body.to_env_id != run.to_env_id:
        env = await _env_or_404(session, body.to_env_id, user)
        run.to_env_id = body.to_env_id
        manifest = body.package_manifest or {"case_types": (run.risk_summary or {}).get("affected_items", [])}
        risk = await service.classify_risk(manifest, env.name)
        run.risk_level  = risk.get("risk_level", run.risk_level)
        run.risk_summary = risk
    elif body.package_manifest is not None:
        from velaris_shared.db.models import EnvironmentRegistryModel
        to_env = await session.get(EnvironmentRegistryModel, run.to_env_id) if run.to_env_id else None
        risk = await service.classify_risk(body.package_manifest, to_env.name if to_env else "unknown")
        run.risk_level  = risk.get("risk_level", run.risk_level)
        run.risk_summary = risk

    await session.commit()
    return RunOut.from_model(run)


@router.delete("/runs/{run_id}", status_code=204)
async def delete_run(
    run_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(hxguard.guard("deploy.runs.promote")),
):
    run = await _run_or_404(session, run_id, user)
    await session.delete(run)
    await session.commit()


# ── Change Windows ────────────────────────────────────────────────────────────

@router.post("/windows", status_code=201)
async def create_window(
    body: WindowIn,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(hxguard.guard("deploy.windows.write")),
):
    from velaris_shared.db.models import DeploymentWindowModel
    w = DeploymentWindowModel(
        tenant_id=_tenant(user), env_id=body.env_id,
        name=body.name, days_of_week=body.days_of_week,
        start_hour_utc=body.start_hour_utc, end_hour_utc=body.end_hour_utc,
        enabled=body.enabled,
    )
    session.add(w)
    await session.commit()
    return {"id": str(w.id), "name": w.name, "env_id": str(w.env_id),
            "days": w.days_of_week, "start": w.start_hour_utc, "end": w.end_hour_utc,
            "enabled": w.enabled}


@router.get("/windows")
async def list_windows(
    env_id: uuid.UUID | None = None,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(hxguard.guard("deploy.windows.read")),
):
    from sqlalchemy import select
    from velaris_shared.db.models import DeploymentWindowModel
    q = select(DeploymentWindowModel).where(DeploymentWindowModel.tenant_id == _tenant(user))
    if env_id:
        q = q.where(DeploymentWindowModel.env_id == env_id)
    rows = (await session.execute(q)).scalars().all()
    return [{"id": str(w.id), "name": w.name, "env_id": str(w.env_id),
             "days": w.days_of_week, "start": w.start_hour_utc, "end": w.end_hour_utc,
             "enabled": w.enabled} for w in rows]


@router.patch("/windows/{window_id}", status_code=200)
async def update_window(
    window_id: uuid.UUID,
    body: WindowIn,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(hxguard.guard("deploy.windows.write")),
):
    from velaris_shared.db.models import DeploymentWindowModel
    w = await _window_or_404(session, window_id, user)
    w.name           = body.name
    w.days_of_week   = body.days_of_week
    w.start_hour_utc = body.start_hour_utc
    w.end_hour_utc   = body.end_hour_utc
    w.enabled        = body.enabled
    await session.commit()
    return {"id": str(w.id), "name": w.name, "env_id": str(w.env_id),
            "days": w.days_of_week, "start": w.start_hour_utc, "end": w.end_hour_utc,
            "enabled": w.enabled}


@router.delete("/windows/{window_id}", status_code=204)
async def delete_window(
    window_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(hxguard.guard("deploy.windows.write")),
):
    from velaris_shared.db.models import DeploymentWindowModel
    w = await _window_or_404(session, window_id, user)
    await session.delete(w)
    await session.commit()


# ── My approvals ─────────────────────────────────────────────────────────────

@router.get("/my-approvals", response_model=list[RunOut])
async def my_approvals(
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(hxguard.guard("deploy.runs.read")),
):
    """Return deployment runs awaiting approval assigned to the current user.

    Matches against both username and user_id UUID since the user-directory
    uses the username string as its user_id field.
    """
    # user.user_id is the UUID from velaris_users.id (JWT sub claim)
    # user.username is the login name — user-directory stores this as user_id
    identifiers = {
        str(getattr(user, "user_id", "") or ""),
        str(getattr(user, "username", "") or ""),
        str(getattr(user, "email", "") or ""),
    } - {""}
    rows = await service.list_runs(session, _tenant(user), status="awaiting_approval", limit=200)
    assigned = [
        r for r in rows
        if (r.risk_summary or {}).get("assigned_to_user_id") in identifiers
    ]
    return [RunOut.from_model(r) for r in assigned]


# ── Risk analysis endpoint (standalone) ──────────────────────────────────────

@router.post("/analyse-risk")
async def analyse_risk(
    body: PromoteIn,
    user: AuthenticatedUser = Depends(hxguard.guard("deploy.runs.read")),
):
    """Classify risk without starting a deployment — useful for pre-flight checks."""
    result = await service.classify_risk(body.package_manifest, str(body.to_env_id))
    return result


# ── Bundle / Packager (Option B) ─────────────────────────────────────────────

class PackageIn(BaseModel):
    case_type_ids: list[uuid.UUID] | None = None   # None = all case types
    #: {"sections": {"case_types": [...] | null, ...}} — see
    #: `packager.normalise_selection`. Absent section = everything.
    selection:     dict | None = None
    #: The Promote screen's own manifest. Accepted so a preview resolves through
    #: exactly the same mapping the promotion will use — a preview that shows
    #: more than the package ships is the defect this phase exists to remove.
    manifest:      dict | None = None


def _selection_of(body: "PackageIn") -> dict | None:
    if body.selection is not None:
        return body.selection
    if body.manifest is not None:
        from case_service.hxdeploy.packages import manifest_to_selection
        return manifest_to_selection(body.manifest)
    return None


class PackageCreateIn(BaseModel):
    name:          str
    version:       str
    description:   str = ""
    selection:     dict | None = None
    source_env_id: uuid.UUID | None = None
    delta_env_id:  uuid.UUID | None = None   # resolve delta at build time


class PackageOut(BaseModel):
    id:          uuid.UUID
    name:        str
    version:     str
    description: str
    status:      str
    checksum:    str | None
    summary:     dict
    selection:   dict
    created_by:  str
    created_at:  str
    sealed_by:   str | None
    sealed_at:   str | None

    @classmethod
    def from_model(cls, p: Any) -> "PackageOut":
        return cls(
            id=p.id, name=p.name, version=p.version, description=p.description or "",
            status=p.status, checksum=p.checksum, summary=p.summary or {},
            selection=p.selection or {}, created_by=p.created_by,
            created_at=iso_utc(p.created_at) or "",
            sealed_by=p.sealed_by, sealed_at=iso_utc(p.sealed_at),
        )


@router.post("/package")
async def build_package(
    body: PackageIn = PackageIn(),
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(hxguard.guard("deploy.packages.read")),
):
    """Serialise all (or selected) case types into a deployment bundle.

    Returns the full bundle JSON — ready to preview or POST to /deploy/import.
    """
    from case_service.hxdeploy.packager import build_bundle
    try:
        bundle = await build_bundle(session, _tenant(user), body.case_type_ids,
                                    selection=_selection_of(body))
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    return bundle


@router.post("/package/delta")
async def build_delta_package(
    env_id: uuid.UUID,
    body: PackageIn = PackageIn(),
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(hxguard.guard("deploy.packages.read")),
):
    """Build a delta bundle: only artifacts changed since env's last deployment.

    Returns full bundle if env has never been deployed to (same as /package).
    """
    from case_service.hxdeploy.packager import build_delta_bundle
    # The delta is measured against THIS tenant's environment. Reading another
    # tenant's `last_deployed_at` would leak when they last shipped.
    await _env_or_404(session, env_id, user)
    try:
        bundle = await build_delta_bundle(session, _tenant(user), env_id,
                                          body.case_type_ids, selection=_selection_of(body))
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    return bundle


# ── Ownership ────────────────────────────────────────────────────────────────
#
# Every by-id path goes through one of these. Loading a row by primary key and
# acting on it is how "who does this belong to?" gets forgotten — the bots and
# scheduler routers had exactly this shape, and a 204 came back for another
# tenant's schedule. A row belonging to somebody else answers **404**: it must
# not be distinguishable from one that does not exist.

async def _owned_or_404(session: AsyncSession, model, row_id, user: AuthenticatedUser,
                        what: str):
    from case_service.tenancy.identity import same_tenant

    row = await session.get(model, row_id)
    if row is None or not await same_tenant(session, row.tenant_id, _tenant(user)):
        raise HTTPException(404, f"{what} not found")
    return row


async def _env_or_404(session, env_id, user):
    from velaris_shared.db.models import EnvironmentRegistryModel
    return await _owned_or_404(session, EnvironmentRegistryModel, env_id, user, "Environment")


async def _run_or_404(session, run_id, user):
    from velaris_shared.db.models import DeploymentRunModel
    return await _owned_or_404(session, DeploymentRunModel, run_id, user, "Run")


async def _window_or_404(session, window_id, user):
    from velaris_shared.db.models import DeploymentWindowModel
    return await _owned_or_404(session, DeploymentWindowModel, window_id, user, "Window")


# ── Deployment packages (the frozen artifact a promotion ships) ──────────────

async def _package_or_404(session: AsyncSession, package_id: uuid.UUID,
                          user: AuthenticatedUser) -> Any:
    """Load a package, refusing another tenant's as absent rather than forbidden."""
    from velaris_shared.db.models import DeploymentPackageModel
    from case_service.tenancy.identity import same_tenant

    pkg = await session.get(DeploymentPackageModel, package_id)
    if pkg is None or not await same_tenant(session, pkg.tenant_id, _tenant(user)):
        raise HTTPException(404, "Package not found")
    return pkg


@router.post("/packages", response_model=PackageOut, status_code=201)
async def create_package(
    body: PackageCreateIn,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(hxguard.guard("deploy.packages.write")),
):
    """Resolve a selection into a bundle and store it as a **draft** package.

    Draft, not sealed: the point of a separate seal is that somebody can look at
    what the selection actually resolved to before it becomes the thing that
    ships.
    """
    from velaris_shared.db.models import EnvironmentRegistryModel
    from case_service.hxdeploy import packages

    since = None
    if body.delta_env_id:
        env = await _env_or_404(session, body.delta_env_id, user)
        since = getattr(env, "last_deployed_at", None)

    try:
        pkg = await packages.create_package(
            session, _tenant(user), name=body.name, version=body.version,
            created_by=_actor(user), description=body.description,
            selection=body.selection, source_env_id=body.source_env_id, since=since,
        )
    except packages.PackageError as exc:
        raise HTTPException(400, str(exc))
    await session.commit()
    await session.refresh(pkg)
    return PackageOut.from_model(pkg)


@router.get("/packages", response_model=list[PackageOut])
async def list_packages(
    status: str | None = None,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(hxguard.guard("deploy.packages.read")),
):
    from sqlalchemy import select as _select, desc
    from velaris_shared.db.models import DeploymentPackageModel

    stmt = (_select(DeploymentPackageModel)
            .where(DeploymentPackageModel.tenant_id == _tenant(user))
            .order_by(desc(DeploymentPackageModel.created_at)))
    if status:
        stmt = stmt.where(DeploymentPackageModel.status == status)
    rows = (await session.execute(stmt)).scalars().all()
    return [PackageOut.from_model(p) for p in rows]


@router.get("/packages/{package_id}", response_model=PackageOut)
async def get_package(
    package_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(hxguard.guard("deploy.packages.read")),
):
    return PackageOut.from_model(await _package_or_404(session, package_id, user))


@router.get("/packages/{package_id}/bundle")
async def get_package_bundle(
    package_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(hxguard.guard("deploy.packages.read")),
):
    """The frozen bundle itself — what the target will receive, byte for byte."""
    pkg = await _package_or_404(session, package_id, user)
    return pkg.bundle


@router.post("/packages/{package_id}/rebuild", response_model=PackageOut)
async def rebuild_package(
    package_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(hxguard.guard("deploy.packages.write")),
):
    """Re-resolve a draft against the current environment. Refused once sealed."""
    from case_service.hxdeploy import packages
    pkg = await _package_or_404(session, package_id, user)
    try:
        await packages.rebuild_package(session, pkg)
    except packages.PackageError as exc:
        raise HTTPException(409, str(exc))
    await session.commit()
    await session.refresh(pkg)
    return PackageOut.from_model(pkg)


@router.post("/packages/{package_id}/seal", response_model=PackageOut)
async def seal_package(
    package_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(hxguard.guard("deploy.packages.seal")),
):
    """Freeze the bundle and stamp its checksum. A sealed package never changes."""
    from case_service.hxdeploy import packages
    pkg = await _package_or_404(session, package_id, user)
    try:
        await packages.seal_package(session, pkg, _actor(user))
    except packages.PackageError as exc:
        raise HTTPException(409, str(exc))
    await session.commit()
    await session.refresh(pkg)
    return PackageOut.from_model(pkg)


@router.delete("/packages/{package_id}", status_code=204)
async def delete_package(
    package_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(hxguard.guard("deploy.packages.write")),
):
    """Discard a draft. A sealed package is evidence of what was shipped and
    stays; runs reference it."""
    pkg = await _package_or_404(session, package_id, user)
    if pkg.status == "sealed":
        raise HTTPException(409, "A sealed package cannot be deleted — it is what a run shipped.")
    await session.delete(pkg)
    await session.commit()
    return None


@router.post("/import", dependencies=[Depends(_import_rl)])
async def import_bundle(
    request: Request,
    session: AsyncSession = Depends(get_session),
    authorization: str | None = Header(default=None),
    x_deploy_key: str | None = Header(default=None, alias="X-Deploy-Key"),
):
    """Receive a deployment bundle and upsert design-time artifacts.

    This is a machine-to-machine endpoint (CI/CD or push delivery).
    Auth: Bearer <import_api_key> in Authorization header, OR X-Deploy-Key header.
    """
    from sqlalchemy import select as _select
    from velaris_shared.db.models import EnvironmentRegistryModel
    from case_service.hxdeploy.packager import apply_bundle

    # Resolve the provided key
    provided_key: str | None = None
    if authorization and authorization.lower().startswith("bearer "):
        provided_key = authorization[7:].strip()
    elif x_deploy_key:
        provided_key = x_deploy_key.strip()

    if not provided_key:
        raise HTTPException(401, "Missing import key — provide Authorization: Bearer <key>")

    # Find the environment whose import_api_key matches
    all_envs = (await session.execute(
        _select(EnvironmentRegistryModel)
        .where(EnvironmentRegistryModel.import_api_key == provided_key)
    )).scalars().all()

    if not all_envs:
        raise HTTPException(401, "Invalid import key")

    target_env  = all_envs[0]
    target_tenant = target_env.tenant_id

    bundle = await request.json()
    if not isinstance(bundle, dict):
        raise HTTPException(400, "Body must be a JSON object (bundle)")

    # Integrity, not just authenticity. The key says who sent it; the checksum
    # says the bundle is the one that was sealed and approved. A package built
    # by this platform always carries one — an older sender may not, and that
    # case is allowed through rather than breaking existing pushes.
    declared = bundle.get("package_checksum")
    if declared:
        from case_service.hxdeploy.packages import canonical_bytes
        from case_service.marketplace.checksum import ChecksumError, verify_checksum
        try:
            verify_checksum(canonical_bytes(bundle), str(declared))
        except ChecksumError as exc:
            raise HTTPException(400, str(exc))

    try:
        # The import identity, not a person: everything this bundle creates is
        # attributed to the machine credential that delivered it, which is also
        # what makes an imported bot's `created_by` somebody nobody can be.
        result = await apply_bundle(session, bundle, target_tenant,
                                    imported_by=f"import:{target_env.name}")
    except ValueError as ve:
        raise HTTPException(400, str(ve))
    await session.commit()
    return {**result, "target_env": target_env.name, "tenant_id": target_tenant}


# ── CI/CD Callback (Option A) ─────────────────────────────────────────────────

class DeployCallbackIn(BaseModel):
    status:           str            # "deployed" | "failed"
    deployed_version: str | None = None
    error_message:    str | None = None


@router.post("/runs/{run_id}/callback", dependencies=[Depends(_import_rl)])
async def deploy_callback(
    run_id: uuid.UUID,
    body: DeployCallbackIn,
    request: Request,
    session: AsyncSession = Depends(get_session),
    x_deploy_signature: str | None = Header(default=None, alias="X-Deploy-Signature"),
):
    """CI/CD pipeline calls this after completing a webhook-triggered deployment.

    Verifies HMAC signature against the environment's webhook_secret,
    then updates the run status accordingly.
    """
    from velaris_shared.db.models import DeploymentRunModel, EnvironmentRegistryModel

    run = await session.get(DeploymentRunModel, run_id)
    if not run:
        raise HTTPException(404, "Run not found")
    if run.status not in ("triggered", "deploying"):
        raise HTTPException(400, f"Run is in unexpected status: {run.status}")

    # Verify HMAC when the environment has a webhook_secret.
    #
    # FAIL CLOSED (2026-08-15). The condition used to be
    # `if env and env.webhook_secret and x_deploy_signature:` — so the signature
    # was verified only when the caller chose to send one. Omitting the header
    # skipped verification entirely and fell through to the status update below,
    # letting an anonymous caller who knows a run id mark a deployment
    # `deployed` or `failed`. Same shape as the Stripe/Onfido/DocuSign webhooks
    # fixed the same day: a missing signature is not an absent requirement.
    #
    # An environment with NO webhook_secret configured stays unverified — that
    # is the documented design for an unconfigured environment, not an oversight,
    # and it is recorded in `tests/phase_authz/test_ungated_routes.py`.
    if run.to_env_id:
        env = await session.get(EnvironmentRegistryModel, run.to_env_id)
        if env and env.webhook_secret:
            if not x_deploy_signature:
                raise HTTPException(401, "Missing webhook signature")
            raw_body = await request.body()
            expected = "sha256=" + hmac.new(
                env.webhook_secret.encode(), raw_body, hashlib.sha256
            ).hexdigest()
            if not hmac.compare_digest(expected, x_deploy_signature):
                raise HTTPException(401, "Invalid webhook signature")
            if body.status == "deployed":
                env.current_version   = body.deployed_version or run.deployed_at and str(run.deployed_at)[:10] or "unknown"
                env.last_deployed_at  = service._utcnow()

    if body.status == "deployed":
        run.status       = "deployed"
        run.completed_at = service._utcnow()
    else:
        run.status       = "failed"
        run.completed_at = service._utcnow()
        run.deploy_notes = (run.deploy_notes or "") + f"\n[CI/CD ERROR] {body.error_message or 'unknown'}"

    await session.commit()
    return {"run_id": str(run.id), "status": run.status}
