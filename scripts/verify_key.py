#!/usr/bin/env python3
"""
Velaris startup key verification gate.
Called as the very first step of start-velaris.sh.

Exit codes:
  0 — key is valid, proceed with startup
  1 — key is revoked, invalid, or grace period expired — BLOCK startup
  2 — not activated yet (no setup marker / no key cache) — startup may continue

Why 2 exists: a brand-new install cannot obtain a key until the platform API is
running, because the registration website calls it to create the customer's
user. Blocking startup on a key the operator has no way to get yet is a
deadlock, so "never activated" is a warning and "was activated, now revoked"
stays a hard block.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import uuid
import hashlib
import socket
from datetime import datetime, timezone, timedelta
from pathlib import Path

REGISTER_URL = os.environ.get("VELARIS_REGISTER_URL", "https://velaris-app-os.duckdns.org")
VELARIS_DIR    = Path(__file__).parent.parent
KEY_CACHE    = VELARIS_DIR / ".velaris-key"
GRACE_DAYS   = int(os.environ.get("KEY_GRACE_DAYS", "7"))


def red(s):    return f"\033[0;31m{s}\033[0m"
def yellow(s): return f"\033[0;33m{s}\033[0m"
def green(s):  return f"\033[0;32m{s}\033[0m"


def get_mac() -> str:
    override = os.environ.get("VELARIS_HOST_MAC")
    if override:
        return override.lower()
    try:
        result = subprocess.run(["ip", "route", "show", "default"],
                                capture_output=True, text=True, timeout=5)
        if result.returncode == 0 and result.stdout.strip():
            iface = result.stdout.split()[4]
            mac_file = Path(f"/sys/class/net/{iface}/address")
            if mac_file.exists():
                return mac_file.read_text().strip().lower()
    except Exception:
        pass
    mac_int = uuid.getnode()
    return ":".join(f"{(mac_int >> (8 * i)) & 0xff:02x}" for i in range(5, -1, -1))


def read_cache() -> dict | None:
    if not KEY_CACHE.exists():
        return None
    try:
        return json.loads(KEY_CACHE.read_text())
    except Exception:
        return None


def write_cache(cache: dict) -> None:
    KEY_CACHE.write_text(json.dumps(cache, indent=2))


def call_verify(key_id: str, mac: str) -> dict | None:
    """Call the registration server. Returns response dict or None if unreachable."""
    try:
        import urllib.request, urllib.error
        url = f"{REGISTER_URL}/api/verify?key_id={key_id}&mac={mac}&version=1.0.0"
        req = urllib.request.Request(url, headers={"User-Agent": "velaris-startup/1.0"})
        with urllib.request.urlopen(req, timeout=8) as resp:
            return json.loads(resp.read())
    except Exception:
        return None


def main() -> int:
    # ── 1. Check setup marker exists ──────────────────────────────────
    if not (VELARIS_DIR / ".velaris-setup-complete").exists():
        print(red("  ✗  Velaris has not been set up."))
        print(red("     Run ./setup-velaris.sh first."))
        return 1

    # ── 2. Read key cache ──────────────────────────────────────────────
    cache = read_cache()
    if not cache:
        print(yellow("  ⚠  No product key activated yet."))
        print(yellow("     Register on your Velaris website, then run ./activate-velaris.sh"))
        return 2

    key_id = cache.get("key_id", "")
    if not key_id:
        print(yellow("  ⚠  Key cache is unreadable — treating this install as not activated."))
        print(yellow("     Re-activate with ./activate-velaris.sh"))
        return 2

    # ── 3. Immediate local revocation check (cached) ──────────────────
    if cache.get("status") == "revoked":
        print(red(f"  ✗  Your Velaris product key ({key_id}) has been revoked."))
        print(red("     Contact support: velaris.app.os@gmail.com"))
        return 1

    # ── 4. Determine if a server check is due (every 7 days) ──────────
    CHECK_INTERVAL = 7
    last_verified_str = cache.get("last_verified")
    grace = int(cache.get("grace_days", GRACE_DAYS))

    if last_verified_str:
        last_verified = datetime.fromisoformat(last_verified_str)
        if last_verified.tzinfo is None:
            last_verified = last_verified.replace(tzinfo=timezone.utc)
        age_days = (datetime.now(timezone.utc) - last_verified).days
    else:
        age_days = 999  # force a check if never verified

    if last_verified_str and age_days < CHECK_INTERVAL:
        next_check = CHECK_INTERVAL - age_days
        # A pending key must be said out loud EVERY startup, not once a week.
        # The 7-day short circuit is why "registered but never activated" could
        # stay invisible: the check that would have mentioned it only ran on the
        # eighth day, and printed "valid" on the other seven.
        if cache.get("status") == "pending":
            print(yellow(f"  ⚠  Key {key_id} is registered but NOT activated "
                         f"(re-checking in {next_check} day(s))."))
            print(yellow("     Run ./activate-velaris.sh to activate it."))
            return 0
        print(green(f"  ✓  Key {key_id} valid (next check in {next_check} day(s))."))
        return 0

    # ── 5. Call registration server ───────────────────────────────────
    mac    = get_mac()
    result = call_verify(key_id, mac)

    if result is not None:
        status = result.get("status", "")

        if status == "revoked":
            cache["status"] = "revoked"
            write_cache(cache)
            print(red(f"  ✗  Key {key_id} has been revoked."))
            print(red("     Contact support: velaris.app.os@gmail.com"))
            return 1

        if status == "mac_mismatch":
            cache["status"] = "revoked"
            write_cache(cache)
            print(red("  ✗  Key is registered to a different machine (MAC address changed)."))
            print(red("     Contact support: velaris.app.os@gmail.com"))
            return 1

        if status == "not_found":
            print(red(f"  ✗  Key {key_id} not found in registration server."))
            print(red("     Re-run ./setup-velaris.sh with a valid key."))
            return 1

        if status == "pending":
            # Registered, never activated. The platform still runs — refusing to
            # start over a status column would be worse than the problem — but
            # it must say so every time, because the old behaviour was to
            # believe a hardcoded "active" and never mention it again.
            cache["status"]        = "pending"
            cache["last_verified"] = datetime.now(timezone.utc).isoformat()
            cache["mac"]           = mac
            write_cache(cache)
            print(yellow(f"  ⚠  Key {key_id} is registered but NOT activated."))
            print(yellow("     Your dashboard will show it as pending until you run:"))
            print(yellow("       ./activate-velaris.sh"))
            print(yellow("     That call sends the signed key; this check cannot."))
            return 0

        if status == "active":
            cache["status"]        = "active"
            cache["last_verified"] = datetime.now(timezone.utc).isoformat()
            cache["mac"]           = mac
            write_cache(cache)
            print(green(f"  ✓  Key {key_id} verified (active)."))
            return 0

        print(yellow(f"  ⚠  Unexpected status from server: {status}. Applying grace period."))

    # ── 6. Server unreachable — grace period logic ────────────────────
    if not last_verified_str:
        print(red("  ✗  Could not reach registration server and key has never been verified online."))
        print(red(f"     Ensure {REGISTER_URL} is reachable and re-run."))
        return 1

    days_overdue = age_days - CHECK_INTERVAL
    if days_overdue <= grace:
        remaining = grace - days_overdue
        print(yellow(f"  ⚠  Registration server unreachable. Grace period: {remaining} day(s) remaining."))
        print(yellow(f"     Last verified: {last_verified.strftime('%Y-%m-%d %H:%M UTC')}"))
        return 0
    else:
        print(red(f"  ✗  Registration server unreachable and grace period expired ({age_days} day(s) since last check)."))
        print(red("     Contact support: velaris.app.os@gmail.com"))
        return 1


if __name__ == "__main__":
    sys.exit(main())
