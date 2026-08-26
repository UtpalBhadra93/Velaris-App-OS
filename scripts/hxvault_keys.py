#!/usr/bin/env python3
"""HxVault key operations — inspect DEKs, retire unreadable ones, adopt a real KEK.

Three jobs that all need the same thing: the running platform's KEK, plus the
willingness to write to `tenant_deks`. Kept together so the dangerous half is
one file an operator can read end to end.

    python scripts/hxvault_keys.py status
    python scripts/hxvault_keys.py retire-unreadable [--apply]
    python scripts/hxvault_keys.py set-kek [--apply]

Every subcommand is **dry-run by default**. Run from the repository root:
`.env` is read relative to the working directory, and a run from
services/case-service silently misses it.

## Why `set-kek` is a script and not a config change

With `VELARIS_CASE_KEK` unset the KEK is derived from `auth_secret`. Setting the
variable therefore changes the KEK, and every DEK already wrapped under the
derived one stops opening — the platform comes back with its tenants'
encrypted data unreadable and nothing saying why. This unwraps each DEK with
the CURRENT KEK, re-wraps it under the new one, and writes them in a single
transaction. Set the variable only after this has run.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import asyncio
import json
import os
import secrets
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "services" / "case-service"))
sys.path.insert(0, str(ROOT / "services" / "_shared"))

def _reexec_in_venv() -> None:
    """Re-run under the repository venv when the system python cannot import
    the platform's dependencies.

    The rest of the repo invokes scripts as `uv run python scripts/…`, but this
    one is meant to be typed by an operator during an incident, and
    `./scripts/…` picks whatever `python3` is on PATH — which on this box is a
    system 3.14 with no sqlalchemy. Failing with a ModuleNotFoundError in the
    middle of a key operation is the wrong first impression; find the right
    interpreter instead.
    """
    try:
        import sqlalchemy  # noqa: F401
        return
    except ImportError:
        pass
    venv = ROOT / ".venv" / "bin" / "python"
    if venv.is_file() and Path(sys.executable).resolve() != venv.resolve():
        os.execv(str(venv), [str(venv), str(Path(__file__).resolve()), *sys.argv[1:]])
    print("✗ sqlalchemy is not importable and no repository venv was found at "
          f"{venv}.\n  Run it as:  uv run python {Path(__file__).name}", file=sys.stderr)
    raise SystemExit(2)


_reexec_in_venv()



def _parts():
    from case_service.hxvault import crypto, keyring
    from velaris_shared.db.models import TenantDekModel
    from velaris_shared.db.session import get_session_factory
    return crypto, keyring, TenantDekModel, get_session_factory


async def _load(session, TenantDekModel):
    from sqlalchemy import select
    return (await session.execute(select(TenantDekModel))).scalars().all()


def _readable(crypto, keyring, kek, row) -> bool:
    try:
        crypto.open_(kek, keyring._b64d(row.wrapped_dek),
                     keyring._wrap_aad(row.tenant_id))
        return True
    except Exception:  # noqa: BLE001
        return False


async def cmd_status() -> int:
    crypto, keyring, TenantDekModel, sf = _parts()
    kek = crypto.get_kek()
    async with sf()() as session:
        rows = await _load(session, TenantDekModel)
        if not rows:
            print("No DEKs.")
            return 0
        for row in rows:
            tenant = keyring._ckey(row.tenant_id) or "<platform>"
            state = "readable" if _readable(crypto, keyring, kek, row) else "UNREADABLE"
            print(f"  {row.id}  {tenant:40}  {row.status:8}  {state}")
    return 0


async def cmd_retire(apply: bool) -> int:
    """Delete DEK rows that cannot be unwrapped — after backing them up.

    An unreadable row is not harmless: `get_or_create` finds it, fails to open
    it, and raises rather than creating a working key, so the tenant can never
    encrypt anything again. Deleting is what lets a fresh DEK be created.

    It is still a destroying operation, so the row is written to a backup file
    first. If the KEK it was wrapped under is ever recovered, the backup is what
    makes recovery possible.
    """
    crypto, keyring, TenantDekModel, sf = _parts()
    kek = crypto.get_kek()
    async with sf()() as session:
        rows = await _load(session, TenantDekModel)
        dead = [r for r in rows if not _readable(crypto, keyring, kek, r)]
        if not dead:
            print("Every DEK unwraps with the current KEK. Nothing to retire.")
            return 0
        for row in dead:
            print(f"  UNREADABLE  {row.id}  tenant={keyring._ckey(row.tenant_id) or '<platform>'}")
        if not apply:
            print(f"\n{len(dead)} row(s). Re-run with --apply to back up and delete them.")
            return 0

        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        backup = ROOT / f"tenant_deks-unreadable-{stamp}.json"
        backup.write_text(json.dumps([{
            "id": str(r.id),
            "tenant_id": str(r.tenant_id) if r.tenant_id else None,
            "key_version": r.key_version,
            "wrapped_dek": r.wrapped_dek,
            "status": r.status,
        } for r in dead], indent=2))
        backup.chmod(0o600)
        print(f"\n  backup written to {backup}")
        for row in dead:
            await session.delete(row)
        await session.commit()
        print(f"✓ {len(dead)} unreadable DEK row(s) removed. A fresh DEK is created on next use.")
    return 0


async def cmd_set_kek(apply: bool) -> int:
    """Re-wrap every readable DEK under a NEW random KEK and print it.

    Order is the whole point: re-wrap first, set the variable second. Setting
    `VELARIS_CASE_KEK` on a platform whose DEKs are wrapped under the derived
    KEK makes every one of them unreadable at the next restart.
    """
    crypto, keyring, TenantDekModel, sf = _parts()

    if os.getenv("VELARIS_CASE_KEK"):
        print("✗ VELARIS_CASE_KEK is already set in this environment.")
        print("  This command moves DEKs FROM the current KEK TO a new one; run it")
        print("  with the variable unset, exactly as the platform runs today.")
        return 1

    old_kek = crypto.get_kek()
    new_kek = secrets.token_bytes(crypto.KEY_LEN)

    async with sf()() as session:
        rows = await _load(session, TenantDekModel)
        active = [r for r in rows if r.status == "active"]
        unreadable = [r for r in active if not _readable(crypto, keyring, old_kek, r)]
        if unreadable:
            print("✗ Some active DEKs cannot be read with the current KEK:")
            for r in unreadable:
                print(f"    {r.id}  tenant={keyring._ckey(r.tenant_id) or '<platform>'}")
            print("  Retire them first (retire-unreadable) — re-wrapping cannot")
            print("  include a key it cannot open, and leaving them behind would")
            print("  mean two KEKs in play with no record of which is which.")
            return 1

        print(f"  {len(active)} active DEK(s) would be re-wrapped.")
        if not apply:
            print("\nRe-run with --apply. The new KEK is printed once and never stored.")
            return 0

        for row in active:
            dek = crypto.open_(old_kek, keyring._b64d(row.wrapped_dek),
                               keyring._wrap_aad(row.tenant_id))
            rewrapped = keyring._b64e(
                crypto.seal(new_kek, dek, keyring._wrap_aad(row.tenant_id)))
            # Verify before assigning: a DEK that does not open under the new
            # KEK must not replace one that opens under the old.
            check = crypto.open_(new_kek, keyring._b64d(rewrapped),
                                 keyring._wrap_aad(row.tenant_id))
            if check != dek:
                print(f"✗ {row.id} did not verify after re-wrap — nothing written.")
                await session.rollback()
                return 1
            row.wrapped_dek = rewrapped
        await session.commit()

    print(f"\n✓ {len(active)} DEK(s) re-wrapped. Set this now, then restart:\n")
    print(f"    ./scripts/secrets-push.sh VELARIS_CASE_KEK={new_kek.hex()}")
    print("    ./scripts/secrets-render.sh")
    print("\n  It is printed once. Until it is set, the platform still resolves the")
    print("  DERIVED KEK and every DEK above will fail to unwrap — so do it now.")
    return 0


def main() -> int:
    args = sys.argv[1:]
    apply = "--apply" in args
    cmd = next((a for a in args if not a.startswith("-")), "status")
    if cmd == "status":
        return asyncio.run(cmd_status())
    if cmd == "retire-unreadable":
        return asyncio.run(cmd_retire(apply))
    if cmd == "set-kek":
        return asyncio.run(cmd_set_kek(apply))
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main())
