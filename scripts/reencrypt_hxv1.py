#!/usr/bin/env python3
"""Re-encrypt legacy hxv1 credentials as hxv3 (AES-256-GCM).

hxv1 is a hand-rolled SHA-256 keystream with an encrypt-then-MAC tag. It is
authenticated — it was never trivially forgeable — but it is cipher code
written by hand in a codebase that already depends on `cryptography` and
already uses AES-256-GCM in `hxvault/`, and it derives both the keystream and
the MAC from one key. Since 2026-08-24 nothing WRITES it; this moves what is
already stored.

A secret that is never rewritten would otherwise stay hxv1 forever, which is
how a "legacy format" outlives the thing that replaced it.

    python scripts/reencrypt_hxv1.py            # dry run — counts only
    python scripts/reencrypt_hxv1.py --apply    # rewrite

Dry-run by default, matching retention: a tool that rewrites secret columns
should have to be asked twice.

Run from the repository root — `.env` is read relative to the working
directory, and a run from services/case-service silently misses it.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
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


#: (table, column, shape) — shape is "json" for a JSON/JSONB column holding
#: {"_enc": …}, or "text" for a TEXT column holding that dict serialised.
#:
#: Every column written through `encrypt_credentials`. Listed explicitly rather
#: than discovered by name: a column that merely LOOKS like a secret (a token
#: hash, an OIDC client registration) must not be rewritten by a script that
#: guessed.
COLUMNS = [
    ("sso_providers",                 "client_secret_enc",  "json"),
    ("channel_endpoints",             "signing_secret_enc", "json"),
    ("bot_credentials",               "value_enc",          "json"),
    ("connector_registry",            "credentials",        "json"),
    ("hxdbmigrate_sources",           "credentials",        "json"),
    ("environment_registry",          "api_token_enc",      "json"),
    ("velaris_users",                 "mfa_secret_enc",     "json"),
    ("checkout_webhook_integrations", "hmac_secret_enc",    "text"),
    ("payment_disbursements",         "bank_reference",     "text"),
]


def _is_hxv1(value) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (ValueError, TypeError):
            return False
    return isinstance(value, dict) and str(value.get("_enc", "")).startswith("hxv1:")


async def main(apply: bool) -> int:
    from sqlalchemy import text

    from case_service.hxbridge.encryption import (
        decrypt_credentials,
        encrypt_credentials,
    )
    from velaris_shared.db.session import get_session_factory

    total = converted = failed = 0
    async with get_session_factory()() as session:
        for table, column, shape in COLUMNS:
            try:
                rows = (await session.execute(
                    text(f"SELECT id, {column} FROM {table} WHERE {column} IS NOT NULL")
                )).all()
            except Exception as exc:  # noqa: BLE001
                # A table this release does not have is not an error — the same
                # script has to run against installs at different versions.
                print(f"  {table}.{column}: skipped ({type(exc).__name__})")
                await session.rollback()
                continue

            hits = [(rid, val) for rid, val in rows if _is_hxv1(val)]
            total += len(hits)
            if not hits:
                continue
            print(f"  {table}.{column}: {len(hits)} hxv1 value(s)")
            if not apply:
                continue

            for rid, val in hits:
                stored = json.loads(val) if isinstance(val, str) else val
                try:
                    plain = decrypt_credentials(stored)
                except Exception as exc:  # noqa: BLE001
                    # Never destroy what cannot be read back. A row that fails
                    # here keeps its hxv1 value and is reported.
                    print(f"    ! {table}.{rid}: cannot decrypt ({exc}) — left as is")
                    failed += 1
                    continue
                new = encrypt_credentials(plain)
                # Prove the new value reads back BEFORE it replaces the old one.
                if decrypt_credentials(new) != plain:
                    print(f"    ! {table}.{rid}: re-encrypted value did not verify — left as is")
                    failed += 1
                    continue
                payload = json.dumps(new) if shape == "text" else new
                await session.execute(
                    text(f"UPDATE {table} SET {column} = :v WHERE id = :i").bindparams(
                        **({"v": payload, "i": rid} if shape == "text" else {})
                    ) if shape == "text" else
                    text(f"UPDATE {table} SET {column} = CAST(:v AS JSONB) WHERE id = :i"),
                    {"v": payload if shape == "text" else json.dumps(new), "i": rid},
                )
                converted += 1
        if apply:
            await session.commit()

    print()
    if apply:
        print(f"✓ {converted} value(s) re-encrypted as hxv3, {failed} left untouched")
    else:
        print(f"{total} hxv1 value(s) found. Re-run with --apply to rewrite them.")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main("--apply" in sys.argv)))
