"""Per-job credential leases — the part that decides whether RPA is safe.

A bot that signs into a supplier portal needs that portal's password. Putting it
in `declared_env` would make it static, stored on the definition row, identical
for every job, and readable by anyone who can read the table. That is the
insecure version of this feature, and shipping it would be calling RPA done
while it is not.

Instead a bot names `credential_refs`, and the runner takes a **lease** per job:
resolved at start, injected into that container only, revoked at teardown, with
a row recording who used which credential on whose behalf and when.

**One deviation from the design, recorded here because it matters.** The design
says the refs are HxVault paths. HxVault is envelope encryption for data at rest
(`encrypt`/`decrypt` under a per-tenant DEK), not a path-addressed secret store —
so the store is `bot_credentials`, and the HxVault property that actually
matters is kept: the value is encrypted under the tenant's DEK, so a database
dump yields nothing and crypto-shredding a tenant destroys its bot credentials
along with everything else of theirs.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from case_service.db.models import (
    BotCredentialLeaseModel,
    BotCredentialModel,
    BotJobModel,
)

log = logging.getLogger(__name__)

#: Env var names must be plain identifiers — a ref of `PATH` or one containing a
#: shell metacharacter would be an injection into the container's environment.
_ENV_SAFE = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")

#: Never overridable from a credential ref, whatever an admin names it.
RESERVED_ENV = {
    "PATH", "HOME", "LD_PRELOAD", "LD_LIBRARY_PATH", "PYTHONPATH",
    "VELARIS_JOB_URL", "VELARIS_JOB_TOKEN", "VELARIS_APP", "VELARIS_TENANT",
    "VELARIS_PACKAGE", "VELARIS_BROKER_URL", "VELARIS_BROKER_TOKEN",
}


class CredentialError(Exception):
    """A lease could not be issued. The job fails rather than running without it."""


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def env_name_for(ref: str) -> str:
    """`vault://tenant/acme/supplier-portal` → `SUPPLIER_PORTAL`."""
    tail = (ref or "").rstrip("/").rsplit("/", 1)[-1]
    return re.sub(r"[^A-Za-z0-9]+", "_", tail).strip("_").upper()


async def resolve_leases(
    session: AsyncSession,
    *,
    job: BotJobModel,
    refs: list[str],
    ttl_seconds: int,
) -> dict[str, str]:
    """Lease every ref for this job. Returns the env to inject.

    A ref that does not resolve is an error, not a silent omission: a bot that
    runs without its password will sign in as nobody and report a confusing
    failure from inside a container, where nobody can see why.
    """
    env: dict[str, str] = {}
    if not refs:
        return env

    from case_service.hxbridge.encryption import decrypt_credentials

    expires_at = _utcnow() + timedelta(seconds=max(60, ttl_seconds))

    for ref in refs:
        row = (await session.execute(
            select(BotCredentialModel).where(
                BotCredentialModel.tenant_id == job.tenant_id,
                BotCredentialModel.ref == ref,
            )
        )).scalar_one_or_none()
        if row is None:
            raise CredentialError(
                f"credential {ref!r} is not stored for this tenant — the job "
                f"would run without it and fail inside the container")

        name = env_name_for(ref)
        if not _ENV_SAFE.match(name) or name in RESERVED_ENV:
            raise CredentialError(
                f"credential {ref!r} maps to environment variable {name!r}, "
                f"which is reserved or not a safe identifier")

        try:
            value = decrypt_credentials(row.value_enc) or {}
        except Exception as exc:            # a shredded DEK lands here
            raise CredentialError(f"credential {ref!r} could not be decrypted") from exc

        secret = str(value.get("secret") or "")
        if not secret:
            raise CredentialError(f"credential {ref!r} holds no value")

        session.add(BotCredentialLeaseModel(
            job_id=job.id, vault_ref=ref, expires_at=expires_at,
        ))
        env[name] = secret

    await session.flush()
    return env


async def revoke_leases(session: AsyncSession, *, job_id) -> int:
    """Revoke at teardown. Returns how many were live.

    Revocation is a row edit rather than a delete: the record of *which*
    credential a bot held on a case is the audit trail, and deleting it to tidy
    up would destroy the only evidence that a supplier password was used.
    """
    rows = (await session.execute(
        select(BotCredentialLeaseModel).where(
            BotCredentialLeaseModel.job_id == job_id,
            BotCredentialLeaseModel.revoked_at.is_(None),
        )
    )).scalars().all()
    now = _utcnow()
    for row in rows:
        row.revoked_at = now
    await session.flush()
    return len(rows)


def redact(text: str, secrets_used: list[str]) -> str:
    """Remove known secret values from text bound for an artifact or a log.

    A screenshot of a login screen contains the password, and so does an HTML
    dump and, often, an error message quoting the request. This is the cheap
    half of that defence; the artifact policy is the other half.
    """
    out = text or ""
    for secret in secrets_used:
        if secret and len(secret) >= 4:
            out = out.replace(secret, "«redacted»")
    return out
