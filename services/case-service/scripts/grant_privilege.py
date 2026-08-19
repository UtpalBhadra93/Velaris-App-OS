#!/usr/bin/env python3
"""Break-glass: grant platform access directly in the database.

For exactly one situation — nobody can reach the screen that grants access. The
API refuses to create that state (`hxguard/lockout.py`), so reaching it means
rows were edited behind the API, a migration went wrong, or the last superadmin
was deleted at the database level. In all three the API is no longer a way in,
which is why this talks to the database instead.

    python scripts/grant_privilege.py --list
    python scripts/grant_privilege.py --username alice --make-superadmin
    python scripts/grant_privilege.py --username alice --grant identity.groups.write

It prints what it will do and asks, unless `--yes` is passed. Every run writes a
`breakglass_grant` SecurityEvent: an emergency that leaves no trace is
indistinguishable from an attack, and this is the one tool in the platform that
can manufacture an administrator.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


async def _main() -> int:
    from sqlalchemy import select

    from case_service.db.models import HelixUserModel
    from case_service.db.session import get_session_factory
    from case_service.hxguard.lockout import BOOTSTRAP_ACTION, _holders_of

    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--list", action="store_true",
                    help="who can currently grant access")
    ap.add_argument("--username", help="account to repair")
    ap.add_argument("--make-superadmin", action="store_true",
                    help="the widest possible grant; prefer --grant")
    ap.add_argument("--grant", metavar="ACTION",
                    help="add one platform action via the admin role")
    ap.add_argument("--yes", action="store_true", help="skip the confirmation")
    args = ap.parse_args()

    factory = get_session_factory()
    async with factory() as session:
        if args.list or not args.username:
            holders = await _holders_of(session, BOOTSTRAP_ACTION)
            print(f"accounts able to perform {BOOTSTRAP_ACTION}: {len(holders)}")
            for uid in sorted(holders):
                user = await session.get(HelixUserModel, uid)
                if user:
                    print(f"  {user.username:<30} roles={user.roles}")
            if not holders:
                print("  (none — the platform is locked out; repair with --username)")
            return 0

        user = (await session.execute(
            select(HelixUserModel).where(HelixUserModel.username == args.username)
        )).scalar_one_or_none()
        if user is None:
            print(f"no such user: {args.username}", file=sys.stderr)
            return 1

        if args.make_superadmin:
            change, new_roles = "superadmin", sorted(set(user.roles or []) | {"superadmin"})
        elif args.grant:
            from case_service.hxguard.actions import ACTIONS
            if args.grant not in ACTIONS:
                print(f"unknown action: {args.grant}", file=sys.stderr)
                return 1
            # Deliberately grants through the `admin` role rather than writing a
            # bespoke privilege row: break-glass should restore a state the
            # normal UI can then edit, not invent one only this script
            # understands.
            change, new_roles = f"admin (for {args.grant})", sorted(set(user.roles or []) | {"admin"})
        else:
            print("nothing to do — pass --make-superadmin or --grant", file=sys.stderr)
            return 1

        print(f"user     : {user.username} ({user.id})")
        print(f"roles    : {user.roles} -> {new_roles}")
        print(f"granting : {change}")
        if not args.yes and input("proceed? [y/N] ").strip().lower() != "y":
            print("aborted")
            return 1

        user.roles = new_roles
        if args.make_superadmin:
            user.is_superadmin = True
        user.is_active = True

        from case_service.enterprise.security_events import log_security_event
        await log_security_event(
            session, event_type="breakglass_grant", severity="critical",
            user_id=str(user.id), action="breakglass", outcome="success",
            details={"username": user.username, "granted": change,
                     "roles": new_roles, "operator": os.environ.get("USER", "?")},
        )
        await session.commit()
        print("done — this is recorded as a critical security event")
        return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_main()))
