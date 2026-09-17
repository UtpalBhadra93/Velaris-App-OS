"""P55 HxDeploy — Intelligent Deployment Governance service."""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import time
import uuid
from datetime import datetime, timezone

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from velaris_shared.db.models import (
    DeploymentHealthCheckModel,
    DeploymentPackageModel,
    DeploymentRunModel,
    DeploymentWindowModel,
    EnvironmentRegistryModel,
)

logger = logging.getLogger(__name__)

RISK_ORDER = ["low", "medium", "high", "critical"]


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _same_actor(a: str | None, b: str | None) -> bool:
    """Case- and whitespace-insensitive identity compare.

    `initiated_by` and `approved_by` are both written by `_actor()`, which
    prefers username, then email, then user id — so the two sides are the same
    shape for the same person. Comparing loosely is deliberate: the failure to
    avoid is letting one person pass as two.
    """
    if not a or not b:
        return False
    return a.strip().casefold() == b.strip().casefold()


# ── Risk classification ───────────────────────────────────────────────────────

async def classify_risk(package_manifest: dict, to_env: str) -> dict:
    """Use HxNexus to classify deployment risk; fall back to heuristics."""
    changes = package_manifest.get("case_types", [])
    forms   = package_manifest.get("forms", [])
    notes   = package_manifest.get("notes", "")

    prompt = (
        f"You are a deployment risk classifier for Velaris BPM.\n"
        f"A package is being promoted to environment: {to_env}\n"
        f"Package contents:\n"
        f"- Case types changed/added: {len(changes)}\n"
        f"- Forms changed/added: {len(forms)}\n"
        f"- Notes: {notes[:500]}\n\n"
        f"Classify the deployment risk. Return JSON:\n"
        f'{{"risk_level": "low|medium|high|critical", "reason": "...", '
        f'"affected_items": [], "recommendation": "..."}}\n\n'
        f"Rules:\n"
        f"- low: UI labels, colour, optional field additions only\n"
        f"- medium: new stages/steps, required field additions, new connectors\n"
        f"- high: SLA changes, permission/role changes, stage deletions\n"
        f"- critical: breaking schema changes, table drops, prod with >50 case types changed"
    )

    try:
        from velaris_shared.llm.factory import generate_json
        result = await generate_json(prompt)
        if result and "risk_level" in result:
            return result
    except Exception as exc:
        logger.warning("HxNexus risk classification failed: %s", exc)

    return _heuristic_risk(package_manifest, to_env)


def _heuristic_risk(manifest: dict, to_env: str) -> dict:
    ct_count = len(manifest.get("case_types", []))
    has_sla  = bool(manifest.get("sla_sql"))

    if to_env == "prod" and ct_count > 5:
        level = "high"
    elif to_env == "prod" and has_sla:
        level = "high"
    elif ct_count == 0 and not has_sla:
        level = "low"
    elif ct_count <= 2:
        level = "medium"
    else:
        level = "medium"

    return {
        "risk_level":    level,
        "reason":        f"{ct_count} case type(s) changed; target: {to_env}",
        "affected_items": manifest.get("case_types", [])[:10],
        "recommendation": "Review changes before deploying to production." if to_env == "prod" else "Standard review.",
    }


def _within_window(window: DeploymentWindowModel) -> bool:
    now = _utcnow()
    return (
        now.weekday() in (window.days_of_week or list(range(7)))
        and window.start_hour_utc <= now.hour < window.end_hour_utc
    )


# ── Environments ──────────────────────────────────────────────────────────────

async def list_environments(session: AsyncSession, tenant_id: str) -> list[EnvironmentRegistryModel]:
    rows = (await session.execute(
        select(EnvironmentRegistryModel)
        .where(EnvironmentRegistryModel.tenant_id == tenant_id)
        .order_by(EnvironmentRegistryModel.order_index)
    )).scalars().all()
    return list(rows)


async def register_environment(
    session: AsyncSession,
    tenant_id: str,
    name: str,
    label: str,
    url: str | None,
    order_index: int,
    delivery_method: str = "manual",
    webhook_url: str | None = None,
    webhook_secret: str | None = None,
    import_api_key: str | None = None,
) -> EnvironmentRegistryModel:
    existing = (await session.execute(
        select(EnvironmentRegistryModel).where(
            EnvironmentRegistryModel.tenant_id == tenant_id,
            EnvironmentRegistryModel.name == name,
        )
    )).scalar_one_or_none()

    if existing:
        existing.label           = label
        existing.url             = url or existing.url
        existing.order_index     = order_index
        existing.delivery_method = delivery_method
        if webhook_url is not None:
            existing.webhook_url = webhook_url
        if webhook_secret is not None:
            existing.webhook_secret = webhook_secret
        if import_api_key is not None:
            existing.import_api_key = import_api_key
        return existing

    env = EnvironmentRegistryModel(
        tenant_id=tenant_id, name=name, label=label,
        url=url, order_index=order_index,
        delivery_method=delivery_method,
        webhook_url=webhook_url,
        webhook_secret=webhook_secret,
        import_api_key=import_api_key,
    )
    session.add(env)
    await session.flush()
    return env


async def get_environment_status(session: AsyncSession, env_id: uuid.UUID) -> dict:
    env = await session.get(EnvironmentRegistryModel, env_id)
    if not env:
        return {"error": "Environment not found"}

    health = {"status": env.status, "current_version": env.current_version}

    if env.url:
        try:
            start = time.monotonic()
            async with httpx.AsyncClient(timeout=5) as client:
                resp = await client.get(env.url + "/api/v1/health")
            ms = int((time.monotonic() - start) * 1000)
            health["http_status"] = resp.status_code
            health["response_ms"] = ms
            health["reachable"]   = resp.status_code < 500
        except Exception as exc:
            health["reachable"] = False
            health["error"]     = str(exc)[:200]

    return health


# ── Deployments ───────────────────────────────────────────────────────────────

async def promote(
    session: AsyncSession,
    tenant_id: str,
    to_env_id: uuid.UUID,
    package_id: uuid.UUID | None,
    package_manifest: dict,
    initiated_by: str,
    deploy_notes: str = "",
    from_env_id: uuid.UUID | None = None,
    assign_to_user_id: str | None = None,
    assign_to_name: str | None = None,
) -> DeploymentRunModel:
    """Queue a run against a **sealed package**.

    Naming an existing package is the intended path. Without one, the manifest
    the Promote screen already sends is resolved into a package here and sealed
    on the spot — which is what makes that screen's artifact picker real, rather
    than the decoration it was while the push rebuilt the whole environment at
    execute time.
    """
    from case_service.tenancy.identity import same_tenant

    to_env = await session.get(EnvironmentRegistryModel, to_env_id)
    # Ownership, not just existence. Promoting into another tenant's environment
    # would push this tenant's artifacts through THEIR stored import key, into
    # THEIR target — a cross-tenant write dressed as a deployment.
    if not to_env or not await same_tenant(session, to_env.tenant_id, tenant_id):
        raise ValueError("Target environment not found")

    pkg = await _resolve_package(
        session, tenant_id, package_id, package_manifest, to_env, initiated_by,
        from_env_id=from_env_id,
    )

    windows = (await session.execute(
        select(DeploymentWindowModel).where(
            DeploymentWindowModel.env_id == to_env_id,
            DeploymentWindowModel.enabled == True,  # noqa: E712
        )
    )).scalars().all()

    window_blocked = bool(windows) and not any(_within_window(w) for w in windows)

    risk = await classify_risk(package_manifest, to_env.name)
    risk_level = risk.get("risk_level", "medium")

    # All deployments require explicit human approval regardless of risk level.
    initial_status = "awaiting_approval"
    if window_blocked:
        risk["window_blocked"] = True
    if assign_to_user_id:
        risk["assigned_to_user_id"] = assign_to_user_id
        risk["assigned_to_name"]    = assign_to_name or assign_to_user_id

    run = DeploymentRunModel(
        tenant_id=tenant_id,
        package_id=pkg.id if pkg else None,
        package_checksum=pkg.checksum if pkg else None,
        from_env_id=from_env_id,
        to_env_id=to_env_id,
        risk_level=risk_level,
        risk_summary=risk,
        status=initial_status,
        initiated_by=initiated_by,
        deploy_notes=deploy_notes,
    )
    session.add(run)
    await session.flush()

    await _emit(run.id, "deploy.promoted", {
        "risk": risk_level, "status": run.status,
        "package": f"{pkg.name} v{pkg.version}" if pkg else None,
        "checksum": pkg.checksum if pkg else None,
    })
    return run


async def _resolve_package(
    session: AsyncSession,
    tenant_id: str,
    package_id: uuid.UUID | None,
    package_manifest: dict,
    to_env: EnvironmentRegistryModel,
    initiated_by: str,
    from_env_id: uuid.UUID | None = None,
) -> DeploymentPackageModel | None:
    """The bytes this run will ship, decided now rather than at execute time."""
    from case_service.hxdeploy import packages
    from case_service.tenancy.identity import same_tenant

    if package_id:
        pkg = await session.get(DeploymentPackageModel, package_id)
        if pkg is None:
            raise ValueError("Package not found")
        if not await same_tenant(session, pkg.tenant_id, tenant_id):
            # 404-shaped on purpose: another tenant's package must not be
            # distinguishable from one that does not exist.
            raise ValueError("Package not found")
        try:
            packages.verify_package(pkg)
        except packages.PackageError as exc:
            raise ValueError(str(exc)) from exc
        return pkg

    # No package named — resolve the screen's manifest into one and seal it.
    selection = packages.manifest_to_selection(package_manifest or {})
    stamp = _utcnow().strftime("%Y%m%d%H%M%S")
    declared = str((package_manifest or {}).get("version") or "0.0.0")
    try:
        pkg = await packages.create_package(
            session, tenant_id,
            name=f"auto-{to_env.name}",
            version=f"{declared}+{stamp}",
            created_by=initiated_by,
            description=f"Built automatically when promoting to {to_env.name}.",
            selection=selection,
            source_env_id=from_env_id,
        )
        await packages.seal_package(session, pkg, initiated_by)
    except packages.PackageError as exc:
        raise ValueError(str(exc)) from exc
    return pkg


async def _trigger_webhook(
    run: DeploymentRunModel,
    env: EnvironmentRegistryModel,
    manifest: dict,
) -> None:
    """POST HMAC-signed deployment payload to env.webhook_url (Option A)."""
    if not env.webhook_url:
        raise ValueError("webhook_url is not set on this environment")

    payload = {
        "run_id":       str(run.id),
        "tenant_id":    run.tenant_id,
        "to_env":       env.name,
        "to_env_label": env.label,
        "risk_level":   run.risk_level,
        "initiated_by": run.initiated_by,
        "deploy_notes": run.deploy_notes or "",
        "manifest_summary": {
            "case_types": len(manifest.get("case_types", [])),
            "version":    manifest.get("version", ""),
        },
        "timestamp":    _utcnow().isoformat(),
    }
    body_bytes = json.dumps(payload).encode()

    headers = {"Content-Type": "application/json"}
    if env.webhook_secret:
        sig = hmac.new(
            env.webhook_secret.encode(), body_bytes, hashlib.sha256
        ).hexdigest()
        headers["X-Deploy-Signature"] = f"sha256={sig}"

    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(env.webhook_url, content=body_bytes, headers=headers)
        resp.raise_for_status()

    logger.info("webhook triggered for run %s → %s (status %s)", run.id, env.webhook_url, resp.status_code)


def _assert_transport_safe(url: str) -> None:
    """The push carries the target's import key in an Authorization header.

    Over plain HTTP to another host that key is readable by anything on the
    path, and it is the credential that authorises writing design-time
    artifacts — from bundle v3, container images — into that environment. So
    `http://` is allowed only where it cannot leave the machine or the private
    network: loopback and RFC1918. Everything else must be TLS.
    """
    import ipaddress
    from urllib.parse import urlsplit

    parts = urlsplit(url)
    if parts.scheme == "https":
        return
    host = parts.hostname or ""
    if host in ("localhost", "127.0.0.1", "::1"):
        return
    try:
        if ipaddress.ip_address(host).is_private:
            return
    except ValueError:
        pass
    raise ValueError(
        f"Refusing to push to {url}: the import key would travel in clear text. "
        f"Use https for anything outside loopback or a private network."
    )


async def _push_bundle(
    session: AsyncSession,
    run: DeploymentRunModel,
    env: EnvironmentRegistryModel,
) -> None:
    """Serialise bundle and POST to target Velaris instance (Option B)."""
    if not env.url:
        raise ValueError("env.url is not set — cannot push bundle")
    if not env.import_api_key:
        raise ValueError("import_api_key is not set on this environment")
    _assert_transport_safe(env.url)

    from case_service.hxdeploy import packages

    # The sealed bundle, verbatim. This used to rebuild from live dev at execute
    # time, which meant the payload was not the payload anybody approved — and it
    # ignored the selection entirely, so every push was the whole environment.
    if not run.package_id:
        raise ValueError("This run has no package — nothing to push.")
    pkg = await session.get(DeploymentPackageModel, run.package_id)
    if pkg is None:
        raise ValueError("The package this run was approved against no longer exists.")
    packages.verify_package(pkg)
    if run.package_checksum and run.package_checksum != pkg.checksum:
        raise ValueError(
            f"Package checksum changed since approval "
            f"(approved {run.package_checksum}, now {pkg.checksum}) — refusing to push."
        )

    bundle = dict(pkg.bundle)
    bundle["deployment_run_id"] = str(run.id)
    bundle["source_env"] = getattr(env, "name", "unknown")
    # Travels beside the bundle, never inside what was hashed: the receiver
    # recomputes over the same canonical bytes and refuses a mismatch.
    bundle["package_id"] = str(pkg.id)
    bundle["package_checksum"] = pkg.checksum

    target_url = env.url.rstrip("/") + "/api/v1/deploy/import"
    async with httpx.AsyncClient(timeout=60) as client:
        resp = await client.post(
            target_url,
            json=bundle,
            headers={
                "Authorization": f"Bearer {env.import_api_key}",
                "Content-Type": "application/json",
            },
        )
        resp.raise_for_status()

    result = resp.json()
    logger.info(
        "package %s v%s (%s) pushed to %s: imported=%s skipped=%s errors=%s needs_config=%s",
        pkg.name, pkg.version, pkg.checksum[:12], target_url,
        result.get("imported"), result.get("skipped"),
        len(result.get("errors", [])), len(result.get("needs_configuration", [])),
    )
    env.current_version = pkg.version

    # Persist push result in risk_summary so the Studio can surface it
    existing = dict(run.risk_summary or {})
    existing["push_result"] = {
        "package":           f"{pkg.name} v{pkg.version}",
        "checksum":          pkg.checksum,
        "imported":          result.get("imported", 0),
        "skipped":           result.get("skipped", 0),
        "errors":            result.get("errors", [])[:5],
        "needs_configuration": result.get("needs_configuration", []),
    }
    run.risk_summary = existing


async def _execute_deployment(
    session: AsyncSession,
    run: DeploymentRunModel,
    env: EnvironmentRegistryModel,
    manifest: dict,
) -> None:
    run.status      = "deploying"
    run.deployed_at = _utcnow()
    await session.flush()

    delivery = getattr(env, "delivery_method", "manual")

    try:
        if delivery == "webhook":
            await _trigger_webhook(run, env, manifest)
            # webhook run stays "triggered" until callback
            run.status = "triggered"
            run.completed_at = None
        elif delivery == "push":
            await _push_bundle(session, run, env)
            env.current_package_id = run.package_id
            env.last_deployed_at   = _utcnow()
            run.status       = "deployed"
            run.completed_at = _utcnow()
        else:
            # manual: governance-only, mark deployed immediately. The version
            # recorded is the package's, not a number typed into the manifest —
            # the two used to be able to disagree.
            env.current_package_id = run.package_id
            pkg = await session.get(DeploymentPackageModel, run.package_id) if run.package_id else None
            env.current_version    = pkg.version if pkg else manifest.get("version", "unknown")
            env.last_deployed_at   = _utcnow()
            run.status       = "deployed"
            run.completed_at = _utcnow()
    except Exception as exc:
        logger.error("deployment execution failed (method=%s): %s", delivery, exc)
        run.status       = "failed"
        run.completed_at = _utcnow()
        run.deploy_notes = (run.deploy_notes or "") + f"\n[ERROR] {exc}"

    await _emit(run.id, "deploy.executed", {"env": env.name, "delivery": delivery})


async def approve_run(
    session: AsyncSession,
    run_id: uuid.UUID,
    approved_by: str,
    package_manifest: dict,
) -> DeploymentRunModel:
    run = await session.get(DeploymentRunModel, run_id)
    if not run:
        raise ValueError("Deployment run not found")
    if run.status != "awaiting_approval":
        raise ValueError(f"Run is not awaiting approval (status: {run.status})")

    # Two people, the same rule bots and schedules already carry. An approval
    # the initiator can grant themselves is a form, not a gate — and this run
    # writes design-time artifacts (from phase 3, container images) into another
    # environment. `deploy.runs.approve` being separately grantable is the other
    # half: holding promote must not imply holding approve.
    if _same_actor(run.initiated_by, approved_by):
        raise PermissionError(
            "A deployment must be approved by somebody other than the person who "
            "started it."
        )

    # ── F-04: the simulation gate ────────────────────────────────────────────
    #
    # Placed after the four-eyes check and before anything executes. It is
    # SILENT unless an operator has configured and enabled a gate: no
    # `simulation_gates` row means not enforced, which is what every install
    # taking this update has. A deployment that approved yesterday approves
    # today.
    #
    # It raises PermissionError, the same type the four-eyes rule above raises,
    # so the router's existing handling maps it to the same status without a
    # second code path.
    from case_service.hxdeploy import simulation as _sim
    from case_service.tenancy.identity import resolve_tenant

    resolved = await resolve_tenant(session, run.tenant_id)
    if resolved is not None:
        await _sim.assert_gate_clear(session, run, tenant_uuid=resolved[0])

    run.approved_by = approved_by
    to_env = await session.get(EnvironmentRegistryModel, run.to_env_id) if run.to_env_id else None
    if to_env:
        await _execute_deployment(session, run, to_env, package_manifest)
    else:
        run.status = "deployed"; run.completed_at = _utcnow()

    await _emit(run.id, "deploy.approved", {"by": approved_by})
    return run


async def reject_run(
    session: AsyncSession,
    run_id: uuid.UUID,
    rejected_by: str,
    reason: str,
) -> DeploymentRunModel:
    run = await session.get(DeploymentRunModel, run_id)
    if not run:
        raise ValueError("Deployment run not found")
    if run.status != "awaiting_approval":
        raise ValueError(f"Run is not awaiting approval (status: {run.status})")

    run.rejected_by      = rejected_by
    run.rejection_reason = reason
    run.status           = "rejected"
    run.completed_at     = _utcnow()

    await _emit(run.id, "deploy.rejected", {"by": rejected_by, "reason": reason})
    return run


def _same_origin(a: str, b: str) -> bool:
    """Scheme, host and port all equal — never a prefix compare."""
    from urllib.parse import urlsplit

    pa, pb = urlsplit(a), urlsplit(b)
    return (pa.scheme, pa.hostname, pa.port) == (pb.scheme, pb.hostname, pb.port)


async def run_health_check(
    session: AsyncSession,
    run_id: uuid.UUID,
    check_url: str | None = None,
) -> DeploymentHealthCheckModel:
    """Probe the target environment.

    The URL is **derived from the registered environment**, not chosen by the
    caller. It used to be taken from a query parameter and fetched as given,
    which made this a server-side request forgery: anybody holding
    `deploy.runs.read` — a developer, a manager — could point it at
    `169.254.169.254`, an internal admin port, or any host on the deployment's
    network, and read back the status code, the timing and the connection error.
    A supplied URL is now accepted only when it is the same origin as the
    environment being deployed to, which is the only case the feature ever
    needed.
    """
    run = await session.get(DeploymentRunModel, run_id)
    if not run:
        raise ValueError("Run not found")

    env_url = None
    if run.to_env_id:
        target = await session.get(EnvironmentRegistryModel, run.to_env_id)
        env_url = getattr(target, "url", None)

    if check_url:
        if not env_url or not _same_origin(check_url, env_url):
            raise ValueError(
                "A health check may only probe the environment this run deploys "
                "to. Register the URL on the environment rather than passing one "
                "here."
            )
    elif env_url:
        # `/health`, not `/api/v1/health`: the service has never served the
        # latter, so every derived probe answered 404 — and a 404 counts as
        # healthy under the `< 500` rule, which made this check report "up" for
        # a URL that does not exist.
        check_url = env_url.rstrip("/") + "/health"

    check = DeploymentHealthCheckModel(run_id=run_id, check_url=check_url)
    session.add(check)

    if check_url:
        try:
            start = time.monotonic()
            # follow_redirects stays off: a 302 from the target to an internal
            # host would walk straight around the same-origin check above.
            async with httpx.AsyncClient(timeout=10, follow_redirects=False) as client:
                resp = await client.get(check_url)
            ms = int((time.monotonic() - start) * 1000)
            check.status_code = resp.status_code
            check.response_ms = ms
            check.healthy     = resp.status_code < 500
        except Exception as exc:
            check.healthy = False
            check.error   = str(exc)[:300]
    else:
        check.healthy = None
        check.error   = "No URL to probe"

    if run.to_env_id:
        env = await session.get(EnvironmentRegistryModel, run.to_env_id)
        if env:
            env.status = "healthy" if check.healthy else "degraded"

    await session.flush()
    return check


async def list_runs(
    session: AsyncSession,
    tenant_id: str,
    status: str | None = None,
    limit: int = 50,
) -> list[DeploymentRunModel]:
    q = (select(DeploymentRunModel)
         .where(DeploymentRunModel.tenant_id == tenant_id)
         .order_by(DeploymentRunModel.created_at.desc())
         .limit(limit))
    if status:
        q = q.where(DeploymentRunModel.status == status)
    return list((await session.execute(q)).scalars().all())


async def get_run(session: AsyncSession, run_id: uuid.UUID) -> DeploymentRunModel | None:
    return await session.get(DeploymentRunModel, run_id)


async def _emit(run_id: uuid.UUID, event: str, data: dict) -> None:
    try:
        from velaris_shared.hxstream.emitter import emit_event
        await emit_event(str(run_id), event, data)
    except Exception:
        pass
