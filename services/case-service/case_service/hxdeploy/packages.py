"""HxDeploy packages — the frozen artifact a promotion actually ships.

A package is built once from an explicit selection, **sealed** (frozen and
checksummed), and it is those bytes that promote: to one environment, and then
to the next one after it. What QA tested and what production receives are the
same bundle, and the checksum is what proves that on arrival.

Before this module the push rebuilt the whole environment from live dev at
execute time, so the Promote screen's artifact picker changed nothing and the
approver approved a risk summary rather than a payload.
"""
from __future__ import annotations

import hashlib
import json
import logging
import uuid
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from velaris_shared.db.models import DeploymentPackageModel
from case_service.hxdeploy.packager import build_bundle, normalise_selection

logger = logging.getLogger(__name__)

#: Fields the push adds to the body in transit. They are not part of what was
#: sealed, so they are stripped before the checksum is computed at either end —
#: otherwise every push would fail its own integrity check.
#: `rollout_request` (Oct item 18a) is the source's REQUEST for how the target
#: rolls the content out. Outside the seal on purpose: changing how something
#: goes out is not changing what goes out, and the target's operator decides.
TRANSPORT_KEYS = ("deployment_run_id", "source_env", "package_checksum", "package_id",
                  "rollout_request")


class PackageError(Exception):
    """A package could not be built, sealed, or used."""


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def canonical_bytes(bundle: dict) -> bytes:
    """The exact bytes a checksum is taken over.

    Sorted keys and no incidental whitespace, so the same bundle hashes the same
    on both sides of the wire regardless of how either end serialises JSON.
    Transport fields are excluded — see `TRANSPORT_KEYS`.
    """
    body = {k: v for k, v in (bundle or {}).items() if k not in TRANSPORT_KEYS}
    return json.dumps(body, sort_keys=True, separators=(",", ":"), default=str).encode()


def checksum_of(bundle: dict) -> str:
    """SHA-256 over `canonical_bytes`, full 64 hex characters."""
    return hashlib.sha256(canonical_bytes(bundle)).hexdigest()


async def create_package(
    session: AsyncSession,
    tenant_id: str,
    name: str,
    version: str,
    created_by: str,
    description: str = "",
    selection: dict | None = None,
    source_env_id: uuid.UUID | None = None,
    since: datetime | None = None,
) -> DeploymentPackageModel:
    """Resolve a selection into a bundle and store it as a draft package."""
    if not name.strip():
        raise PackageError("A package needs a name.")
    if not version.strip():
        raise PackageError("A package needs a version.")

    try:
        selection = normalise_selection(selection)
    except ValueError as exc:
        raise PackageError(str(exc)) from exc

    existing = (await session.execute(
        select(DeploymentPackageModel).where(
            DeploymentPackageModel.tenant_id == tenant_id,
            DeploymentPackageModel.name == name,
            DeploymentPackageModel.version == version,
        )
    )).scalar_one_or_none()
    if existing is not None:
        raise PackageError(
            f"Package '{name}' version '{version}' already exists. "
            f"A sealed package is immutable — bump the version."
        )

    bundle = await build_bundle(session, tenant_id, since=since, selection=selection)

    pkg = DeploymentPackageModel(
        tenant_id=tenant_id, name=name.strip(), version=version.strip(),
        description=description or "", selection=selection, bundle=bundle,
        summary=bundle.get("summary", {}),
        bundle_schema_version=str(bundle.get("bundle_schema_version", "2")),
        status="draft", source_env_id=source_env_id, created_by=created_by,
    )
    session.add(pkg)
    await session.flush()
    logger.info("deployment package built: %s v%s (%s) for tenant %s",
                pkg.name, pkg.version, pkg.id, tenant_id)
    return pkg


async def rebuild_package(
    session: AsyncSession, pkg: DeploymentPackageModel, since: datetime | None = None,
) -> DeploymentPackageModel:
    """Re-resolve a **draft** package against the current environment.

    Refused once sealed: that is the whole point of sealing.
    """
    if pkg.status == "sealed":
        raise PackageError(
            f"Package '{pkg.name}' v{pkg.version} is sealed and cannot be rebuilt. "
            f"Create a new version instead."
        )
    bundle = await build_bundle(session, pkg.tenant_id, since=since, selection=pkg.selection)
    pkg.bundle = bundle
    pkg.summary = bundle.get("summary", {})
    pkg.updated_at = _utcnow()
    return pkg


async def seal_package(
    session: AsyncSession, pkg: DeploymentPackageModel, sealed_by: str,
) -> DeploymentPackageModel:
    """Freeze the package and stamp its checksum.

    Sealing twice is refused rather than ignored: a second seal would restamp
    the checksum, and a checksum that can move is not evidence of anything.
    """
    if pkg.status == "sealed":
        raise PackageError(f"Package '{pkg.name}' v{pkg.version} is already sealed.")
    if not pkg.bundle:
        raise PackageError("Nothing to seal — the package holds no bundle.")

    pkg.checksum = checksum_of(pkg.bundle)
    pkg.status = "sealed"
    pkg.sealed_by = sealed_by
    pkg.sealed_at = _utcnow()
    pkg.updated_at = _utcnow()
    logger.info("deployment package sealed: %s v%s checksum=%s",
                pkg.name, pkg.version, pkg.checksum)
    return pkg


def verify_package(pkg: DeploymentPackageModel) -> None:
    """Refuse a package whose stored bundle no longer matches its seal."""
    if pkg.status != "sealed" or not pkg.checksum:
        raise PackageError(
            f"Package '{pkg.name}' v{pkg.version} is not sealed — seal it before promoting."
        )
    actual = checksum_of(pkg.bundle)
    if actual != pkg.checksum:
        raise PackageError(
            f"Package '{pkg.name}' v{pkg.version} does not match its seal "
            f"(sealed {pkg.checksum}, now {actual}) — it was modified after sealing."
        )


def manifest_to_selection(manifest: dict) -> dict:
    """Translate the Promote screen's manifest into a selection.

    The screen has always sent `{case_types, forms, connectors, include_sla,
    include_rules, version}` and the server has always thrown it away. Mapping it
    here is what makes the existing UI's picker real without waiting for the
    Studio work.

    The screen's own words are the rule: *"leave blank to include everything"*.
    So an empty picker is the whole environment, and picking anything narrows the
    package to what was picked — plus the FK parents those rows need, which
    `build_bundle` pulls in for itself.
    """
    manifest = manifest or {}

    def _ids(key: str) -> list[str]:
        """Ids, from either shape the manifest has carried.

        The screen sends ids; older callers and tests send objects. An entry
        that names no id contributes nothing rather than becoming a string that
        matches no row."""
        out = []
        for entry in (manifest.get(key) or []):
            value = entry.get("id") if isinstance(entry, dict) else entry
            if value:
                out.append(str(value))
        return out

    case_types  = _ids("case_types")
    forms       = _ids("forms")
    connectors  = _ids("connectors")
    bots        = _ids("bots")
    schedules   = _ids("schedules")
    picked = bool(case_types or forms or connectors or bots or schedules)

    if not picked:
        # Nothing chosen: the whole environment, exactly as before.
        selection: dict = {"sections": {}}
    else:
        selection = {"sections": {
            "case_types":        case_types,
            "form_definitions":  forms,
            "connectors":        connectors,
            "bot_definitions":   bots,
            "schedules":         schedules,
            # A narrowed package carries only what was named. Rules are the one
            # section the screen offers as a separate opt-in.
            "rule_definitions":  None if manifest.get("include_rules") else [],
            "data_models":            [],
            "business_calendars":     [],
            "access_roles":           [],
            "portals":                [],
            "email_templates":        [],
            "email_accounts":         [],
            "process_definitions":    [],
            "escalation_trees":       [],
            "webhook_subscriptions":  [],
            "outbound_connector_rules": [],
            "webhook_receiver_rules": [],
            "access_groups":          [],
        }}

    if "include_sla" in manifest:
        # Recorded, never acted on: SLA policies live inside a case type's
        # `definition_json.sla_policies`, so they travel with the case type and
        # there is no section to include or exclude.
        selection["include_sla"] = bool(manifest.get("include_sla"))
    selection["source_manifest"] = {
        k: manifest.get(k) for k in ("version", "include_sla", "include_rules")
        if k in manifest
    }
    return normalise_selection(selection)
