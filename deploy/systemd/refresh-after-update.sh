#!/usr/bin/env bash
# Velaris under systemd — finish an update: the three things nothing else does.
#
# Why this exists
# ---------------
# `update-velaris.sh` ends by calling `start-velaris.sh`. On an install that
# systemd supervises, `start-velaris.sh` sees the running units and exits at
# once — correctly, a second copy from a shell would race them for the ports.
# But that early exit is BEFORE everything it does besides starting processes,
# so until 2026-10-11 an update on a systemd install:
#
#   1. never re-applied the release manifest (releases/manifest.sql);
#   2. never rebuilt Studio — the unit serves the built `dist` and builds only
#      when `dist` is MISSING, so Studio stayed on the previous release for good;
#   3. never restarted anything — new code on disk, old processes in memory —
#      and then passed its own health check against those old processes and
#      reported "updated".
#
# This script does those three, in that order, and says plainly when it could
# not do the last one. `update-velaris.sh` calls it instead of
# `start-velaris.sh` when the units are active. It is safe to run by hand, any
# number of times.
#
# Usage
# -----
#   deploy/systemd/refresh-after-update.sh                    # all three
#   deploy/systemd/refresh-after-update.sh --non-interactive  # never prompt (timer)
#   deploy/systemd/refresh-after-update.sh --no-restart       # 1 and 2 only
#
# Exit status
# -----------
#   0  done (or nothing to do: systemd does not run Velaris here)
#   3  manifest and Studio handled, but the services could NOT be restarted —
#      the command to run is printed. The update is NOT live until it is.
#
# Copyright (c) 2024-2026 Velaris Contributors
# SPDX-License-Identifier: BSL-1.1

set -uo pipefail

SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VELARIS_DIR="$(cd "$SRC_DIR/../.." && pwd)"
UNIT_DIR="${VELARIS_UNIT_DIR:-/etc/systemd/system}"
# Scratch files and error logs go to a directory only this user can write. A
# fixed name under /tmp can be pre-created, or pointed somewhere else, by any
# other local user before this script writes to it.
VELARIS_SCRATCH="${VELARIS_SCRATCH:-${XDG_STATE_HOME:-${HOME:-}/.local/state}/velaris}"
if ! { mkdir -p "$VELARIS_SCRATCH" && chmod 700 "$VELARIS_SCRATCH" && [ -w "$VELARIS_SCRATCH" ]; } 2>/dev/null; then
  VELARIS_SCRATCH="$(mktemp -d "${TMPDIR:-/tmp}/velaris.XXXXXX")"
fi
UNITS=(velaris-engine velaris-case-service velaris-analytics-service
       velaris-hxgraph-service velaris-hxmigrate-service velaris-studio)

INTERACTIVE=true
DO_RESTART=true
for arg in "$@"; do
  case "$arg" in
    --non-interactive) INTERACTIVE=false ;;
    --no-restart)      DO_RESTART=false ;;
    -h|--help) sed -n '2,36p' "${BASH_SOURCE[0]}"; exit 0 ;;
    *) echo "unknown option: $arg" >&2; exit 2 ;;
  esac
done

green()  { echo -e "\033[0;32m$*\033[0m"; }
yellow() { echo -e "\033[0;33m$*\033[0m"; }
red()    { echo -e "\033[0;31m$*\033[0m"; }

ACTIVE=()
for u in "${UNITS[@]}"; do
  systemctl is-active --quiet "$u" 2>/dev/null && ACTIVE+=("$u")
done
if [ "${#ACTIVE[@]}" -eq 0 ]; then
  echo "  systemd is not running Velaris here — nothing to refresh."
  exit 0
fi

# ── 1. Release manifest ───────────────────────────────────────────
# The same idempotent upsert start-velaris.sh applies on every start. A failure
# is reported and does not stop the refresh: a feature row that did not land is
# not a reason to leave the services on old code.
DB_BACKEND="$(grep -E '^\s+database:' "$VELARIS_DIR/velaris.yaml" 2>/dev/null | head -1 \
              | sed -E 's/.*database:[[:space:]]*"?([a-z]+)"?.*/\1/')"
DB_CONTAINER="${DB_CONTAINER:-docker-compose-velaris-db-1}"
DB_USER="${DB_USER:-velaris}"
DB_NAME="${DB_NAME:-velaris}"
echo "▶ Release manifest..."
case "${DB_BACKEND:-postgresql}" in
  postgresql)
    if [ -f "$VELARIS_DIR/releases/manifest.sql" ]; then
      if docker exec -i "$DB_CONTAINER" psql -U "$DB_USER" -d "$DB_NAME" -q \
           < "$VELARIS_DIR/releases/manifest.sql" 2>"$VELARIS_SCRATCH/velaris-manifest-err.log" \
         && ! grep -q "ERROR" "$VELARIS_SCRATCH/velaris-manifest-err.log"; then
        green "  ✓ release manifest applied"
      else
        yellow "  ⚠ release manifest had errors — feature versions may be stale:"
        head -5 "$VELARIS_SCRATCH/velaris-manifest-err.log"
      fi
    fi ;;
  *)
    # The MySQL/MariaDB manifest needs the client wrapper start-velaris.sh
    # builds from .env. Named rather than half-done.
    yellow "  ⚠ database is ${DB_BACKEND}: apply releases/manifest.mysql.sql by hand" ;;
esac

# ── 2. Studio ─────────────────────────────────────────────────────
# Built beside the live copy and swapped in, so Studio is never served from a
# half-written directory, and a failed build leaves the working one in place.
build_studio() {
  local studio="$VELARIS_DIR/studio" npm_bin
  [ -f "$studio/package.json" ] || return 0
  npm_bin="$(command -v npm 2>/dev/null || true)"
  if [ -z "$npm_bin" ] && [ -f "$UNIT_DIR/velaris-studio.service" ]; then
    # systemd has no shell profile; the unit carries the absolute path.
    npm_bin="$(grep -E '^ExecStart=' "$UNIT_DIR/velaris-studio.service" \
               | grep -oE '[^ ]*/npm( |$)' | head -1 | tr -d ' ')"
  fi
  if [ -z "$npm_bin" ] || [ ! -x "$npm_bin" ]; then
    red "  ✗ npm not found — Studio NOT rebuilt; it still serves the previous release"
    return 1
  fi
  rm -rf "$studio/dist.new"
  if ( cd "$studio" && PATH="$(dirname "$npm_bin"):$PATH" \
         "$npm_bin" run build -- --outDir dist.new --emptyOutDir ) \
       >"$VELARIS_SCRATCH/velaris-studio-build.log" 2>&1 && [ -f "$studio/dist.new/index.html" ]; then
    rm -rf "$studio/dist.old"
    [ -d "$studio/dist" ] && mv "$studio/dist" "$studio/dist.old"
    mv "$studio/dist.new" "$studio/dist"
    rm -rf "$studio/dist.old"
    green "  ✓ Studio rebuilt"
  else
    rm -rf "$studio/dist.new"
    red "  ✗ Studio build FAILED — still serving the previous release."
    red "    Log: $VELARIS_SCRATCH/velaris-studio-build.log"
    tail -5 "$VELARIS_SCRATCH/velaris-studio-build.log"
    return 1
  fi
}
STUDIO_OK=true
if printf '%s\n' "${ACTIVE[@]}" | grep -qx velaris-studio; then
  echo "▶ Studio..."
  build_studio || STUDIO_OK=false
fi

# ── 3. Restart ────────────────────────────────────────────────────
[ "$DO_RESTART" = true ] || { echo "  (--no-restart: services left running)"; exit 0; }

RESTART_CMD="sudo systemctl restart ${ACTIVE[*]}"
echo "▶ Restarting: ${ACTIVE[*]}"

restarted=false
if [ "$(id -u)" -eq 0 ]; then
  systemctl restart "${ACTIVE[@]}" && restarted=true
elif sudo -n true 2>/dev/null; then
  sudo -n systemctl restart "${ACTIVE[@]}" && restarted=true
elif [ "$INTERACTIVE" = true ] && [ -t 0 ]; then
  sudo systemctl restart "${ACTIVE[@]}" && restarted=true
fi

# No root and no prompt (the update timer runs as the install's user). The
# units are `Restart=always` and their processes are that user's own, so ending
# each main process hands the restart to systemd. Only done when BOTH hold for
# a unit; otherwise it is left running and named below.
if [ "$restarted" = false ]; then
  restarted=true
  for u in "${ACTIVE[@]}"; do
    pid="$(systemctl show -p MainPID --value "$u" 2>/dev/null)"
    policy="$(systemctl show -p Restart --value "$u" 2>/dev/null)"
    if [ "$policy" != "always" ] || [ -z "$pid" ] || [ "$pid" = "0" ] \
       || [ "$(stat -c %u "/proc/$pid" 2>/dev/null)" != "$(id -u)" ]; then
      restarted=false; continue
    fi
    kill -TERM "$pid" 2>/dev/null || { restarted=false; continue; }
    new="$pid"
    for _ in $(seq 1 30); do
      sleep 1
      new="$(systemctl show -p MainPID --value "$u" 2>/dev/null)"
      [ -n "$new" ] && [ "$new" != "0" ] && [ "$new" != "$pid" ] && break
    done
    if [ "$new" = "$pid" ] || [ -z "$new" ] || [ "$new" = "0" ]; then
      red "  ✗ $u did not come back after being stopped"; restarted=false
    fi
  done
fi

if [ "$restarted" = true ]; then
  green "  ✓ services restarted"
  [ "$STUDIO_OK" = true ] || yellow "  ⚠ …but Studio was not rebuilt (see above)"
  exit 0
fi
red "  ✗ The services were NOT all restarted — they are still running the previous code."
red "    Run:  $RESTART_CMD"
exit 3
