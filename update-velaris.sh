#!/bin/bash
# ═══════════════════════════════════════════════════════════════════
#  VELARIS Update Script
#  Checks for a new release, backs up the DB, pulls new code/images,
#  runs migrations, and restarts all services.
#
#  Usage:  ./update-velaris.sh [--yes]   (--yes skips confirmation)
#
#  ── Switching update source ─────────────────────────────────────
#  Currently using GitHub Releases API. When moving to your own
#  server, change UPDATE_SOURCE to "server" and set UPDATE_SERVER_URL.
#  Everything else stays the same.
# ═══════════════════════════════════════════════════════════════════

set -euo pipefail

VELARIS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
COMPOSE_FILE="$VELARIS_DIR/deploy/docker-compose/docker-compose.yml"
DB_CONTAINER="docker-compose-velaris-db-1"
DB_USER="velaris"
DB_NAME="velaris"
BACKUP_DIR="$VELARIS_DIR/.backups"

# ── UPDATE SOURCE CONFIG — read from velaris.yaml ───────────────
# To switch from GitHub to your own server, edit velaris.yaml:
#   updates.source: "server"
#   updates.server_url: "https://updates.velaris.io/latest"
_read_yaml() {
  local key="$1" default="${2:-}"
  grep -E "^\s+${key}:" "$VELARIS_DIR/velaris.yaml" 2>/dev/null \
    | head -1 \
    | sed -E "s/.*${key}:[[:space:]]*[\"']?([^\"'#]+)[\"']?.*/\1/" \
    | xargs \
    || echo "$default"
}
UPDATE_SOURCE=$(_read_yaml "source" "github")
GITHUB_REPO=$(_read_yaml "github_repo" "your-org/velaris")
UPDATE_SERVER_URL=$(_read_yaml "server_url" "https://updates.velaris.io/latest")
UPDATE_CHANNEL=$(_read_yaml "channel" "stable")
AUTO_UPDATE=$(_read_yaml "auto_update" "false")
UPDATE_WINDOW=$(_read_yaml "update_window" "")
MANIFEST_BRANCH=$(_read_yaml "manifest_branch" "main")

# ── Backend guard ────────────────────────────────────────────────
# This updater assumes the bundled Postgres topology: it starts the `velaris-db`
# container and runs migrations via `docker exec … psql` against migrations/postgresql/.
# MySQL/MariaDB installs are bring-your-own external DBs (different client, consolidated
# baseline — no incremental upgrade track), so a Postgres-only run here would execute
# psql against a container that doesn't exist. Fail fast with guidance rather than a
# confusing mid-upgrade failure. Automated MySQL/MariaDB upgrades are a known limitation.
_DB_BACKEND=$(_read_yaml "database" "postgresql")
if [ "$_DB_BACKEND" != "postgresql" ]; then
  echo "ERROR: update-velaris.sh supports Postgres installs only." >&2
  echo "  Detected backend '$_DB_BACKEND' (bring-your-own external DB)." >&2
  echo "  Automated upgrades for MySQL/MariaDB are not yet supported — apply schema" >&2
  echo "  changes to your database manually, then restart services. (Known limitation.)" >&2
  exit 1
fi

# PUO Phase 1: UI-approved update request written by Studio (platform_updates.py)
# Phase 2: requests may carry scheduled_for (next after-hours slot from the
# /admin business calendar) — the agent waits for that exact moment.
UPDATE_REQUEST_FILE="$VELARIS_DIR/.update-request"
REQUEST_MODE=""
REQUEST_SCHEDULED_FOR=""
REQUEST_ACTION=""
REQUEST_TO_VERSION=""
if [[ -f "$UPDATE_REQUEST_FILE" ]]; then
  REQUEST_MODE=$(python3 -c "import json; print(json.load(open('$UPDATE_REQUEST_FILE')).get('mode','window'))" 2>/dev/null || echo "window")
  REQUEST_SCHEDULED_FOR=$(python3 -c "import json; print(json.load(open('$UPDATE_REQUEST_FILE')).get('scheduled_for') or '')" 2>/dev/null || echo "")
  REQUEST_ACTION=$(python3 -c "import json; print(json.load(open('$UPDATE_REQUEST_FILE')).get('action') or '')" 2>/dev/null || echo "")
  REQUEST_TO_VERSION=$(python3 -c "import json; print(json.load(open('$UPDATE_REQUEST_FILE')).get('to_version') or '')" 2>/dev/null || echo "")
fi

# Service port for the post-update health gate (default 8200)
SERVICE_PORT=$(grep -E "^VELARIS_CASE_SERVICE_PORT=" "$VELARIS_DIR/.env" 2>/dev/null | cut -d= -f2 || true)
SERVICE_PORT="${SERVICE_PORT:-8200}"

# ─────────────────────────────────────────────────────────────────

green()  { echo -e "\033[0;32m$*\033[0m"; }
yellow() { echo -e "\033[0;33m$*\033[0m"; }
red()    { echo -e "\033[0;31m$*\033[0m"; }

# ── System binaries, checked BEFORE anything is touched ──────────
#
# The update syncs Python and Node dependencies and pulls container images. It
# cannot install an apt package, so a release that starts using a new system
# tool would install cleanly on a NEW machine and break on every existing one —
# at runtime, in whichever component called it first.
#
# It halts rather than installing: apt needs root, this runs unattended on a
# timer, and a package manager running unwatched on a client's server is not a
# decision an update gets to make.
if [ -f "$VELARIS_DIR/deploy/deps/velaris-deps.sh" ]; then
  # shellcheck source=/dev/null
  . "$VELARIS_DIR/deploy/deps/velaris-deps.sh"
  _MISSING_PKGS="$(_velaris_deps_missing)"
  if [ -n "$_MISSING_PKGS" ]; then
    red "✗ This release needs system packages that are not installed:"
    _velaris_deps_report
    red "  Nothing has been changed. Install them and re-run the update:"
    red "    sudo apt-get update && sudo apt-get install -y $_MISSING_PKGS"
    exit 1
  fi
fi

# ── Ports, checked BEFORE anything is touched ────────────────────
#
# An update can add a component, and a component needs a port the client may
# already be using. Finding that out after the pull, the migrations and the
# restart means diagnosing a half-applied update; finding it out here costs
# nothing. Ports held by Velaris itself are expected — the platform is running
# while this runs — so only a port held by something ELSE stops us.
#
# This never prompts and never reassigns: the timer runs this unattended, and a
# port chosen by a script at 3am is a port nobody knows about.
if [ -f "$VELARIS_DIR/deploy/ports/velaris-ports.sh" ]; then
  # shellcheck source=/dev/null
  set -a; . "$VELARIS_DIR/.env" 2>/dev/null || true; set +a
  . "$VELARIS_DIR/deploy/ports/velaris-ports.sh"
  if ! _velaris_ports_preflight --quiet; then
    red "✗ A port this release needs is held by something that is not Velaris (above)."
    red "  Nothing has been changed. Set the port in .env and re-run the update."
    exit 1
  fi
fi

# ── Gateway limits, checked BEFORE anything is touched ───────────
#
# The gateway's rate and connection ceilings are substituted into
# nginx.conf.template from .env (2026-09-09). nginx validates them at START,
# not at write time, so a typo here is not a bad limit — it is a gateway that
# refuses to boot, which is the whole platform. That failure has happened once
# already (an empty identity secret, 2026-08-24), so it is checked here where
# it costs nothing and nothing has been touched yet.
#
# UNSET OR EMPTY IS ALWAYS FINE: compose supplies the shipped default, which is
# the ceiling the gateway already enforces. Only a value the operator actually
# typed can be wrong.
_velaris_check_gateway_limits() {
  local bad=0 v
  for k in VELARIS_GW_RATE_API VELARIS_GW_RATE_PORTAL; do
    eval "v=\${$k:-}"
    [ -z "$v" ] && continue
    if ! printf '%s' "$v" | grep -Eq '^[1-9][0-9]*r/[sm]$'; then
      red "  ✗ $k=\"$v\" is not an nginx rate (expected e.g. 20r/s or 30r/m)"
      bad=1
    fi
  done
  v="${VELARIS_GW_WS_CONN_MAX:-}"
  if [ -n "$v" ] && ! printf '%s' "$v" | grep -Eq '^[1-9][0-9]*$'; then
    red "  ✗ VELARIS_GW_WS_CONN_MAX=\"$v\" is not a positive whole number"
    bad=1
  fi
  return $bad
}
if ! _velaris_check_gateway_limits; then
  red "✗ A gateway limit in .env would stop nginx from starting."
  red "  Nothing has been changed. Correct the value, or delete the line to"
  red "  take the shipped default, and re-run."
  exit 1
fi
bold()   { echo -e "\033[1m$*\033[0m"; }
step()   { echo ""; bold "▶ $*"; }

# ── Version beacon — fleet visibility for ops ────────────────────
# previous_version is the rollback target — carry it across beacon writes
# so an hourly "current" status never erases it.
PREVIOUS_VERSION=$(python3 -c "import json; print(json.load(open('$VELARIS_DIR/.update-status')).get('previous_version') or '')" 2>/dev/null || echo "")
_write_status() {
  # args: result message
  cat > "$VELARIS_DIR/.update-status" <<EOF
{"version": "${CURRENT_VERSION:-unknown}", "previous_version": "${PREVIOUS_VERSION}", "channel": "$UPDATE_CHANNEL", "result": "$1", "message": "$2", "timestamp": "$(date -Iseconds)"}
EOF
}

# ── Maintenance window check (HH:MM-HH:MM, wrap-around supported) ─
_in_window() {
  local win="$1"
  [[ -z "$win" ]] && return 0
  local start="${win%-*}" end="${win#*-}" now
  now=$(date +%H:%M)
  if [[ "$start" < "$end" ]]; then
    [[ "$now" > "$start" && "$now" < "$end" ]]
  else
    [[ "$now" > "$start" || "$now" < "$end" ]]
  fi
}

AUTO_YES=false
AUTO_MODE=false
for arg in "$@"; do
  case "$arg" in
    --yes)  AUTO_YES=true ;;
    --auto) AUTO_MODE=true; AUTO_YES=true ;;
    --install-timer)
      # Install a systemd timer that polls hourly; the script itself enforces
      # the maintenance window and exits fast when already up to date.
      sudo tee /etc/systemd/system/velaris-update.service > /dev/null <<EOF
[Unit]
Description=Velaris platform auto-update (channel: follows velaris.yaml)
After=network-online.target docker.service

[Service]
Type=oneshot
User=$USER
WorkingDirectory=$VELARIS_DIR
ExecStart=$VELARIS_DIR/update-velaris.sh --auto
EOF
      sudo tee /etc/systemd/system/velaris-update.timer > /dev/null <<EOF
[Unit]
Description=Hourly Velaris update check

[Timer]
OnCalendar=hourly
RandomizedDelaySec=600
Persistent=true

[Install]
WantedBy=timers.target
EOF
      sudo systemctl daemon-reload
      sudo systemctl enable --now velaris-update.timer
      green "✓ velaris-update.timer installed and started (hourly check; window: ${UPDATE_WINDOW:-anytime})"
      exit 0
      ;;
  esac
done

# --auto honours the operator's settings: opt-in flag + maintenance window.
# A pending UI-approved request (.update-request) overrides the auto_update
# opt-in — the admin explicitly approved this update in Studio. mode="now"
# also skips the maintenance window; mode="window" waits for it.
if [[ "$AUTO_MODE" == true ]]; then
  if [[ -n "$REQUEST_MODE" ]]; then
    AUTO_YES=true
    if [[ "$REQUEST_MODE" != "now" ]]; then
      if [[ -n "$REQUEST_SCHEDULED_FOR" ]]; then
        # Calendar-scheduled slot wins over the static update_window
        NOW_EPOCH=$(date +%s)
        SLOT_EPOCH=$(date -d "$REQUEST_SCHEDULED_FOR" +%s 2>/dev/null || echo 0)
        if [[ "$SLOT_EPOCH" -gt 0 && "$NOW_EPOCH" -lt "$SLOT_EPOCH" ]]; then
          echo "UI update request pending — scheduled after business hours ($REQUEST_SCHEDULED_FOR)"
          exit 0
        fi
      elif ! _in_window "$UPDATE_WINDOW"; then
        echo "UI update request pending — waiting for maintenance window ($UPDATE_WINDOW)"
        exit 0
      fi
    fi
    echo "UI-approved update request found (mode: $REQUEST_MODE) — proceeding"
  else
    if [[ "$AUTO_UPDATE" != "true" ]]; then
      echo "auto_update is disabled in velaris.yaml — nothing to do"
      exit 0
    fi
    if ! _in_window "$UPDATE_WINDOW"; then
      echo "outside maintenance window ($UPDATE_WINDOW) — skipping"
      exit 0
    fi
  fi
fi

echo "╔══════════════════════════════════════════╗"
echo "║         VELARIS Updater                  ║"
echo "╚══════════════════════════════════════════╝"

# ── Step 1: Read current version ─────────────────────────────────
step "Step 1/7 — Reading current version..."
CURRENT_VERSION=$(grep 'version:' "$VELARIS_DIR/velaris.yaml" | head -1 | sed 's/.*version: *"\([^"]*\)".*/\1/')
echo "  Installed: v$CURRENT_VERSION"

# ── Step 2: Fetch target version ─────────────────────────────────
step "Step 2/7 — Checking for updates ($UPDATE_SOURCE, channel: $UPDATE_CHANNEL)..."

# Channel manifest: channels.json maps each channel to the version it should
# run. Promotion = the Velaris team moves a pointer; every env converges on
# its own schedule. Falls back to "latest release" when the manifest is
# unreachable (manual mode only — --auto never guesses).
#
# PUO Phase 4: when deploy/release-signing.pub exists, the manifest MUST
# carry a valid cosign signature (channels.json.sig) — fail closed on any
# verification problem. Without the key file, verification is skipped
# (not configured).
MANIFEST_FILE="/tmp/velaris-channels.json"

_manifest_url() {
  if [[ "$UPDATE_SOURCE" == "github" ]]; then
    echo "https://raw.githubusercontent.com/${GITHUB_REPO}/${MANIFEST_BRANCH}/channels.json"
  else
    echo "${UPDATE_SERVER_URL%/latest}/channels.json"
  fi
}

fetch_manifest() {
  # returns: 0 = ok, 1 = unreachable, 2 = signature verification failure
  local url; url=$(_manifest_url)
  curl -sf --max-time 10 "$url" -o "$MANIFEST_FILE" 2>/dev/null || return 1

  local pubkey="$VELARIS_DIR/deploy/release-signing.pub"
  if [[ -f "$pubkey" ]]; then
    if ! command -v cosign >/dev/null 2>&1; then
      red "  ✗ release-signing.pub configured but cosign is not installed — refusing unverified manifest"
      return 2
    fi
    if ! curl -sf --max-time 10 "${url}.sig" -o "${MANIFEST_FILE}.sig" 2>/dev/null; then
      red "  ✗ channels.json.sig missing — refusing unverified manifest"
      return 2
    fi
    if ! cosign verify-blob --key "$pubkey" --signature "${MANIFEST_FILE}.sig" "$MANIFEST_FILE" >/dev/null 2>&1; then
      red "  ✗ manifest signature verification FAILED — possible tampering"
      return 2
    fi
    green "  ✓ manifest signature verified (cosign)"
  fi
  return 0
}

fetch_latest_version() {
  if [[ "$UPDATE_SOURCE" == "github" ]]; then
    # GitHub Releases API — returns tag_name like "v2.1.0"
    local response
    response=$(curl -sf --max-time 10 \
      -H "Accept: application/vnd.github+json" \
      "https://api.github.com/repos/${GITHUB_REPO}/releases/latest" 2>/dev/null) || {
      yellow "  ⚠ Could not reach GitHub — skipping update check" >&2
      echo "$CURRENT_VERSION"
      return
    }
    echo "$response" | python3 -c "import sys,json; print(json.load(sys.stdin)['tag_name'].lstrip('v'))"

  elif [[ "$UPDATE_SOURCE" == "server" ]]; then
    # Own server — expects JSON: { "version": "2.1.0", "notes": "..." }
    local response
    response=$(curl -sf --max-time 10 "$UPDATE_SERVER_URL" 2>/dev/null) || {
      yellow "  ⚠ Could not reach update server — skipping update check" >&2
      echo "$CURRENT_VERSION"
      return
    }
    echo "$response" | python3 -c "import sys,json; print(json.load(sys.stdin)['version'])"
  fi
}

MANIFEST_RC=0
fetch_manifest || MANIFEST_RC=$?
if [[ "$MANIFEST_RC" -eq 2 ]]; then
  _write_status "manifest-unverified" "channel manifest failed signature verification"
  exit 1
fi

if [[ "$REQUEST_ACTION" == "rollback" ]]; then
  # ── PUO Phase 4: admin-approved rollback (code + images only) ────
  if [[ -z "$REQUEST_TO_VERSION" ]]; then
    red "  ✗ rollback request without to_version — ignoring"
    _write_status "failed" "rollback request missing to_version"
    rm -f "$UPDATE_REQUEST_FILE"
    exit 1
  fi
  LATEST_VERSION="$REQUEST_TO_VERSION"
  yellow "  Rollback requested → v$LATEST_VERSION"
  yellow "  Code and images are reverted; DB migrations are NOT reverted"
  yellow "  (expand/contract migrations make old code run on new schema)."
else
  LATEST_VERSION=""
  if [[ "$MANIFEST_RC" -eq 0 ]]; then
    LATEST_VERSION=$(python3 -c "import json; print(json.load(open('$MANIFEST_FILE')).get('$UPDATE_CHANNEL','') or '')" 2>/dev/null || echo "")
  fi

  if [[ -n "$LATEST_VERSION" ]]; then
    echo "  Channel '$UPDATE_CHANNEL' pins: v$LATEST_VERSION"
  else
    if [[ "$AUTO_MODE" == true ]]; then
      yellow "  ⚠ Channel manifest unreachable — auto mode never guesses; skipping"
      _write_status "skipped" "channel manifest unreachable"
      exit 0
    fi
    yellow "  ⚠ Channel manifest unreachable — falling back to latest release"
    LATEST_VERSION=$(fetch_latest_version)
  fi

  if [[ "$LATEST_VERSION" == "$CURRENT_VERSION" ]]; then
    green "  ✓ Already on the channel version (v$CURRENT_VERSION)"
    _write_status "current" "already on v$CURRENT_VERSION"
    rm -f "$UPDATE_REQUEST_FILE"   # request satisfied — nothing newer pinned
    exit 0
  fi

  # Never downgrade automatically — a manifest behind the installed version
  # means this env ran ahead (e.g. dev box switched to prod channel).
  if [[ "$(printf '%s\n%s\n' "$LATEST_VERSION" "$CURRENT_VERSION" | sort -V | tail -1)" == "$CURRENT_VERSION" ]]; then
    yellow "  ⚠ Channel pins v$LATEST_VERSION but v$CURRENT_VERSION is installed — not downgrading"
    _write_status "skipped" "channel v$LATEST_VERSION behind installed v$CURRENT_VERSION"
    rm -f "$UPDATE_REQUEST_FILE"   # unsatisfiable request
    exit 0
  fi

  # PUO Phase 4: min_upgrade_from — never skip required migration steps
  # (e.g. 1.0 must go through 1.5 before 2.0).
  if [[ "$MANIFEST_RC" -eq 0 ]]; then
    MIN_FROM=$(python3 -c "import json; r=json.load(open('$MANIFEST_FILE')).get('releases',{}).get('$LATEST_VERSION',{}) or {}; print(r.get('min_upgrade_from') or '')" 2>/dev/null || echo "")
    if [[ -n "$MIN_FROM" ]] \
       && [[ "$(printf '%s\n%s\n' "$MIN_FROM" "$CURRENT_VERSION" | sort -V | head -1)" == "$CURRENT_VERSION" ]] \
       && [[ "$CURRENT_VERSION" != "$MIN_FROM" ]]; then
      red "  ✗ v$LATEST_VERSION requires at least v$MIN_FROM (installed: v$CURRENT_VERSION)"
      red "    Upgrade to v$MIN_FROM first — version path enforcement."
      _write_status "blocked" "v$LATEST_VERSION requires upgrading via v$MIN_FROM first"
      rm -f "$UPDATE_REQUEST_FILE"
      exit 1
    fi
  fi
fi

echo ""
green "  New version available: v$CURRENT_VERSION → v$LATEST_VERSION"
echo ""

# ── Confirm ───────────────────────────────────────────────────────
if [[ "$AUTO_YES" == false ]]; then
  read -rp "  Proceed with update? [y/N]: " CONFIRM
  [[ "$CONFIRM" =~ ^[Yy]$ ]] || { echo "  Aborted."; exit 0; }
fi

# ── Step 3: Backup database ───────────────────────────────────────
step "Step 3/7 — Backing up database..."
mkdir -p "$BACKUP_DIR"
BACKUP_FILE="$BACKUP_DIR/velaris-v${CURRENT_VERSION}-$(date +%Y%m%d-%H%M%S).sql"

if docker exec "$DB_CONTAINER" pg_dump -U "$DB_USER" "$DB_NAME" > "$BACKUP_FILE" 2>/dev/null; then
  green "  ✓ Backup saved: $BACKUP_FILE"
else
  red "  ✗ DB backup failed — aborting to protect your data"
  red "    Check: docker logs $DB_CONTAINER"
  exit 1
fi

# ── Record the backup in HxDR (§8.9) ──────────────────────────────
# This script is the one place that reliably knows a good backup exists, and
# until it said so `GET /dr/posture` reported `no_backup` for every region
# while backups were in fact being taken.
#
# Runs BEFORE the code pull in Step 4 deliberately — the old tree is still
# intact here, so the CLI is guaranteed importable.
#
# Never fatal, and `set -e` is suspended for it: a bookkeeping row must not
# turn a successful backup into a failed update. The CLI exits 0 on its own
# failures too; `|| true` covers the interpreter not being there at all.
#
# Runs from $VELARIS_DIR, not from services/case-service: `env_file: ".env"` is
# resolved against the working directory, and the .env lives at the root. This
# mirrors how start-velaris.sh launches the service (root + --app-dir).
if [[ -x "$VELARIS_DIR/.venv/bin/python" ]]; then
  set +e
  (cd "$VELARIS_DIR" && \
   PYTHONPATH="$VELARIS_DIR/services/case-service" \
   "$VELARIS_DIR/.venv/bin/python" -m case_service.compliance.dr_cli \
     --location "$BACKUP_FILE" \
     --scope database \
     --status succeeded \
     --recorded-by "update-velaris.sh v${CURRENT_VERSION}") || true
  set -e
else
  yellow "  ! DR: .venv not found — backup taken but not recorded in /dr/posture"
fi

# ── Step 4: Pull latest code ──────────────────────────────────────
step "Step 4/7 — Pulling latest code..."
cd "$VELARIS_DIR"

# Protect per-host OpenBao runtime secrets (unseal keys, root token, AppRole)
# from the update. They live in the tree but are generated per-server; while any
# stay git-tracked, a checkout/pull would abort, and a tarball extract would
# overwrite them. Strategy: back up → clear local mods so git won't refuse →
# update → restore the host's live copies → verify → drop the backup.
_SECRET_FILES=(
  "deploy/openbao/.bao-init.json"
  "deploy/openbao/agent/role_id"
  "deploy/openbao/agent/secret_id"
)
_SECRET_BACKUP="$(mktemp -d "${TMPDIR:-/tmp}/velaris-secrets.XXXXXX")"
for _f in "${_SECRET_FILES[@]}"; do
  if [ -f "$_f" ]; then
    mkdir -p "$_SECRET_BACKUP/$(dirname "$_f")"
    cp -p "$_f" "$_SECRET_BACKUP/$_f"
    if git ls-files --error-unmatch "$_f" >/dev/null 2>&1; then
      git checkout -- "$_f" 2>/dev/null || true   # tracked + modified → drop local edit
    fi
  fi
done

if [[ "$UPDATE_SOURCE" == "github" ]]; then
  git fetch origin --tags
  git checkout "v${LATEST_VERSION}" 2>/dev/null || git checkout "tags/v${LATEST_VERSION}" 2>/dev/null || {
    # If no tag, just pull latest main
    yellow "  ⚠ No tag v${LATEST_VERSION} found — pulling latest main"
    git pull origin main
  }
elif [[ "$UPDATE_SOURCE" == "server" ]]; then
  # Own server: download and extract tarball
  TARBALL_URL="${UPDATE_SERVER_URL%/latest}/download/v${LATEST_VERSION}.tar.gz"
  curl -sf --max-time 120 -o /tmp/velaris-update.tar.gz "$TARBALL_URL"
  tar -xzf /tmp/velaris-update.tar.gz -C "$VELARIS_DIR" --strip-components=1
  rm -f /tmp/velaris-update.tar.gz
fi

# Restore the host's live secrets (overwrites whatever the update brought),
# verify each, and only drop the backup on full success.
_secrets_ok=1
for _f in "${_SECRET_FILES[@]}"; do
  if [ -f "$_SECRET_BACKUP/$_f" ]; then
    mkdir -p "$(dirname "$_f")"
    if cp -p "$_SECRET_BACKUP/$_f" "$_f" && cmp -s "$_SECRET_BACKUP/$_f" "$_f"; then :; else
      _secrets_ok=0; red "  ✗ Failed to restore OpenBao secret: $_f"
    fi
  fi
done
if [ "$_secrets_ok" = 1 ]; then
  rm -rf "$_SECRET_BACKUP"
else
  red "  Kept secret backup at: $_SECRET_BACKUP — restore manually before restarting."
fi

green "  ✓ Code updated to v$LATEST_VERSION"

# ── Step 4b: Refresh dependencies ─────────────────────────────────
# New releases can add Python/npm packages (e.g. livekit-client in 2.1.0);
# stale .venv/node_modules break services and Vite until reinstalled.
step "Step 4b — Refreshing dependencies..."
if command -v uv >/dev/null 2>&1; then
  # Optional ML extras are NOT part of the default install (a base box stays lean,
  # no model weights). But `uv sync` is exact by default — it prunes anything not
  # declared, so an operator who installed an extra would silently lose it on the
  # next run. Detect what is already present and ask for it again.
  _EXTRAS=""
  uv run python -c "import faster_whisper" >/dev/null 2>&1 && _EXTRAS="$_EXTRAS --extra intelligence"
  uv run python -c "import av" >/dev/null 2>&1 && _EXTRAS="$_EXTRAS --extra kyc"
  uv sync --all-packages $_EXTRAS 2>&1 | tail -3
  green "  ✓ Python dependencies synced"
else
  yellow "  ⚠ uv not found — skipping Python dependency sync"
fi
if [ -d "$VELARIS_DIR/studio" ]; then
  if ( cd "$VELARIS_DIR/studio" && { npm install --prefer-offline >/tmp/velaris-npm-install.log 2>&1 \
         || npm install >/tmp/velaris-npm-install.log 2>&1; } \
       && rm -rf node_modules/.vite ); then
    green "  ✓ Studio npm dependencies installed"
  else
    tail -5 /tmp/velaris-npm-install.log
    yellow "  ⚠ npm install FAILED — Studio may not load. Full log: /tmp/velaris-npm-install.log"
  fi
fi

# ── Step 5: Pull new Docker images ───────────────────────────────
step "Step 5/7 — Pulling new Docker images..."
# --env-file is REQUIRED: compose interpolates the whole file for every
# subcommand, and two services declare secrets with `${VAR:?...}`. Without it
# this aborts with "required variable VELARIS_MINIO_PASSWORD is missing" on any
# host that lacks a stray .env beside the compose file — i.e. every real server.
docker compose -f "$COMPOSE_FILE" --env-file "$VELARIS_DIR/.env" pull
green "  ✓ Images updated"

# ── Step 5b: Reclaim superseded layers + keep the cache cap in place ──
# Every update supersedes image layers and (where a service builds) leaves the
# previous build cache behind. Docker expires neither on its own, so an install
# that is updated often grows without bound — this is where that is paid back.
step "Step 5b — Reclaiming Docker disk..."
# shellcheck source=scripts/docker-storage.sh
[ -f "$VELARIS_DIR/scripts/docker-storage.sh" ] && . "$VELARIS_DIR/scripts/docker-storage.sh"

if type vds_write_daemon_json >/dev/null 2>&1; then
  # Self-heal: an install predating the cap, or one whose daemon.json was
  # replaced, gets it back here rather than only on a fresh setup.
  if vds_write_daemon_json "" "$(vds_current_root)"; then
    vds_restart_docker && green "  ✓ Build-cache cap applied" \
      || yellow "  ⚠ Docker restart failed — cap applies on the next restart"
  fi
  vds_prune_cache 5GB
else
  # Helper absent (older tree): fall back to the one safe reclaim.
  docker builder prune -f >/dev/null 2>&1 || true
  green "  ✓ Build cache trimmed"
fi

# Dangling IMAGES only — layers no tag and no container references. Volumes are
# deliberately NOT pruned: an unreferenced volume here can still be real data.
docker image prune -f >/dev/null 2>&1 || true
green "  ✓ Superseded image layers removed"

# ── Step 6: Run migrations ────────────────────────────────────────
if [[ "$REQUEST_ACTION" == "rollback" ]]; then
  step "Step 6/7 — Skipping migrations (rollback: schema stays, old code runs on it)"
else
step "Step 6/7 — Running database migrations..."

# Ensure DB is up before migrating (--env-file required — see Step 5)
docker compose -f "$COMPOSE_FILE" --env-file "$VELARIS_DIR/.env" up -d velaris-db
for i in $(seq 1 20); do
  docker exec "$DB_CONTAINER" pg_isready -U "$DB_USER" -q 2>/dev/null && break
  [ "$i" -eq 20 ] && { red "  ✗ PostgreSQL not ready"; exit 1; }
  sleep 1
done

docker exec -i "$DB_CONTAINER" psql -U "$DB_USER" -d "$DB_NAME" -q <<'SQL'
CREATE TABLE IF NOT EXISTS schema_migrations (
  filename   TEXT PRIMARY KEY,
  applied_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
SQL

APPLIED=0; SKIPPED=0

for migration_file in $(find "$VELARIS_DIR/migrations/postgresql" -maxdepth 1 -name "*.sql" | sort); do
  filename="$(basename "$migration_file")"
  EXISTS=$(docker exec "$DB_CONTAINER" psql -U "$DB_USER" -d "$DB_NAME" -tAq \
    -c "SELECT 1 FROM schema_migrations WHERE filename='$filename';" 2>/dev/null || echo "")
  if [[ "$EXISTS" == "1" ]]; then
    SKIPPED=$((SKIPPED + 1)); continue
  fi
  echo -n "    apply $filename ... "
  # ON_ERROR_STOP is what makes a failure a failure — the same line
  # setup-velaris.sh carries, and for the same reason. Without it psql runs the
  # remaining statements after a rejected one, exits 0, and the file is recorded
  # in schema_migrations as applied: a half-applied migration that never runs
  # again, on an EXISTING install, unattended. `setup` learned this from
  # migration 080; the update path never had the fix.
  if docker exec -i "$DB_CONTAINER" psql -U "$DB_USER" -d "$DB_NAME" -q \
      -v ON_ERROR_STOP=1 < "$migration_file" 2>/tmp/migration_err.log; then
    docker exec "$DB_CONTAINER" psql -U "$DB_USER" -d "$DB_NAME" -q \
      -c "INSERT INTO schema_migrations(filename) VALUES('$filename') ON CONFLICT DO NOTHING;"
    green "✓"; APPLIED=$((APPLIED + 1))
  else
    red "FAILED"
    cat /tmp/migration_err.log
    red "  Migration failed — rolling back to v$CURRENT_VERSION"
    red "  Restore backup with: psql -U $DB_USER $DB_NAME < $BACKUP_FILE"
    _write_status "failed" "migration $filename failed during update to v$LATEST_VERSION"
    rm -f "$UPDATE_REQUEST_FILE"   # consumed — never auto-retry a failing update
    exit 1
  fi
done
green "  ✓ Migrations: $APPLIED applied, $SKIPPED already up-to-date"
fi

# ── Step 6b: Sync .env with new variables from .env.example ──────
# New releases may introduce new configuration variables. The user's .env
# is never overwritten — variables it already has (active OR commented)
# are left untouched; only genuinely new ones are appended verbatim:
#   - active lines from .env.example arrive active (shipped default)
#   - commented lines arrive commented (documentation of the new knob)
step "Step 6b — Syncing .env with new variables..."

ENV_FILE="$VELARIS_DIR/.env"
EXAMPLE_FILE="$VELARIS_DIR/.env.example"
ENV_ADDED=0

if [[ -f "$ENV_FILE" && -f "$EXAMPLE_FILE" ]]; then
  while IFS= read -r line; do
    key=""
    if [[ "$line" =~ ^([A-Z][A-Z0-9_]+)= ]]; then
      key="${BASH_REMATCH[1]}"
    elif [[ "$line" =~ ^#[[:space:]]([A-Z][A-Z0-9_]+)= ]]; then
      key="${BASH_REMATCH[1]}"
    fi
    [[ -z "$key" ]] && continue
    grep -qE "^#?[[:space:]]*${key}=" "$ENV_FILE" && continue

    if [[ "$ENV_ADDED" -eq 0 ]]; then
      {
        echo ""
        echo "# ── Added by update-velaris.sh → v${LATEST_VERSION} ($(date +%Y-%m-%d)) ──"
      } >> "$ENV_FILE"
    fi
    echo "$line" >> "$ENV_FILE"
    echo "    + $key"
    ENV_ADDED=$((ENV_ADDED + 1))
  done < "$EXAMPLE_FILE"

  # Newly appended secrets ship empty — generate real values for known ones
  _gen_if_empty() {
    local key="$1" val="$2"
    if grep -q "^${key}=$" "$ENV_FILE"; then
      sed -i "s|^${key}=$|${key}=${val}|" "$ENV_FILE"
      echo "    generated ${key}"
    fi
  }
  _gen_if_empty VELARIS_CASE_AUTH_SECRET        "$(openssl rand -hex 32)"
  _gen_if_empty VELARIS_CASE_STORAGE_MASTER_KEY "$(openssl rand -hex 32)"
  _gen_if_empty VELARIS_DB_PASSWORD           "$(openssl rand -hex 16)"
  _gen_if_empty VELARIS_ADMIN_PASSWORD        "$(openssl rand -hex 16)"
  _gen_if_empty VELARIS_SEARCH_PASSWORD       "$(openssl rand -hex 16)"
  # VELARIS_GATEWAY_IDENTITY_SECRET is deliberately NOT generated here.
  #
  # Generating it would switch workload identity (#18) ON during an unattended
  # update — the timer runs this on its own — and enforcement that arrives
  # without anyone watching is enforcement nobody can diagnose at 3am. An
  # existing install therefore keeps the check off until an operator turns it on
  # deliberately (deploy/tls/README.md); `setup-velaris.sh` generates one for
  # NEW installs, where there is nothing to lock out.
  #
  # This is a decision, not an oversight — do not "fix" it by adding a line here.

  if [[ "$ENV_ADDED" -gt 0 ]]; then
    green "  ✓ Added $ENV_ADDED new variable(s) to .env — review them after the update"
  else
    green "  ✓ .env already has every variable — nothing to sync"
  fi
else
  yellow "  ⚠ .env or .env.example missing — skipping env sync"
fi

# ── Step 7: Update version + restart services ─────────────────────
step "Step 7/7 — Restarting services..."

# Stamp new version into velaris.yaml
sed -i "s/version: \"$CURRENT_VERSION\"/version: \"$LATEST_VERSION\"/" "$VELARIS_DIR/velaris.yaml"

# Restart. Two ways, because an install runs one of two ways.
#
# Under systemd, start-velaris.sh refuses to start a second copy and exits at
# once — before its manifest sync, and without restarting anything. Until
# 2026-10-11 that was the whole of this step there: new code on disk, old
# processes in memory, Studio still serving the previous build, and the health
# gate below passing against the old processes. refresh-after-update.sh does
# what was skipped: manifest, Studio rebuild, restart.
RESTART_PENDING=false
if systemctl is-active --quiet velaris-case-service 2>/dev/null \
   || systemctl is-active --quiet velaris-engine 2>/dev/null; then
  _refresh_args=()
  [[ "$AUTO_MODE" == true ]] && _refresh_args+=(--non-interactive)
  _refresh_rc=0
  DB_CONTAINER="$DB_CONTAINER" DB_USER="$DB_USER" DB_NAME="$DB_NAME" \
    bash "$VELARIS_DIR/deploy/systemd/refresh-after-update.sh" "${_refresh_args[@]}" || _refresh_rc=$?
  [[ "$_refresh_rc" -ne 0 ]] && RESTART_PENDING=true
else
  # Restart via the standard startup script (handles all service health checks)
  bash "$VELARIS_DIR/start-velaris.sh"
fi

# ── Health gate: never report success on a broken env ─────────────
# Every API service, not just the gateway. Since 13c-e the platform is four
# processes: the gateway answers /health from case-service, so a dead
# analytics-, hxgraph- or hxmigrate-service would have left this gate green
# while a quarter of the API returned 502.
# Probe the way the services actually came up. With internal TLS on they demand
# the gateway's client certificate at the handshake, so a plain http probe would
# fail against a perfectly healthy platform — and this gate is what decides
# whether the update is considered successful.
_velaris_probe() { curl -sf --max-time 3 "http://localhost:$1/health" >/dev/null 2>&1; }
# shellcheck source=/dev/null
[ -f "$VELARIS_DIR/deploy/tls/uvicorn-tls-args.sh" ] && . "$VELARIS_DIR/deploy/tls/uvicorn-tls-args.sh"

step "Post-update health check..."
HEALTH_OK=false
HEALTH_FAILED=""
for i in $(seq 1 30); do
  HEALTH_FAILED=""
  for _svc in "${SERVICE_PORT}:gateway" "${VELARIS_CASE_PORT:-8201}:case-service" \
              "${VELARIS_ANALYTICS_PORT:-8202}:analytics-service" \
              "${VELARIS_HXGRAPH_PORT:-8203}:hxgraph-service" \
              "${VELARIS_HXMIGRATE_PORT:-8204}:hxmigrate-service"; do
    _port="${_svc%%:*}"; _name="${_svc##*:}"
    _velaris_probe "${_port}" \
      || HEALTH_FAILED="${HEALTH_FAILED} ${_name}"
  done
  if [[ -z "$HEALTH_FAILED" ]]; then
    HEALTH_OK=true
    break
  fi
  sleep 2
done

PREVIOUS_VERSION="$CURRENT_VERSION"  # beacon records where we came from (rollback target)
CURRENT_VERSION="$LATEST_VERSION"    # beacon reports the new version
rm -f "$UPDATE_REQUEST_FILE"         # request consumed (success or halt — admin re-requests)

if [[ "$HEALTH_OK" == false ]]; then
  red "  ✗ health check failed after the update:${HEALTH_FAILED}"
  red "    This environment is HALTED for human attention — backup at: $BACKUP_FILE"
  red "    Logs: tail -f /tmp/velaris-<service>.log"
  _write_status "unhealthy" "updated to v$LATEST_VERSION but health check failed"
  exit 1
fi
green "  ✓ all services healthy"
if [[ "$RESTART_PENDING" == true ]]; then
  # Healthy, but on the PREVIOUS code: say so rather than report an update.
  red "  ✗ v$LATEST_VERSION is installed but NOT running — the services could not be restarted."
  red "    Run:  sudo systemctl restart velaris-engine velaris-case-service velaris-analytics-service velaris-hxgraph-service velaris-hxmigrate-service velaris-studio"
  _write_status "restart_required" "v$LATEST_VERSION installed; services still run the previous code — restart them"
  exit 1
fi
if [[ "$REQUEST_ACTION" == "rollback" ]]; then
  _write_status "rolled_back" "rolled back to v$LATEST_VERSION"
else
  _write_status "updated" "updated to v$LATEST_VERSION"
fi

echo ""
echo "╔══════════════════════════════════════════════════════╗"
green "║   Velaris updated → v$LATEST_VERSION (channel: $UPDATE_CHANNEL)"
echo "║   Backup kept at: .backups/"
echo "╚══════════════════════════════════════════════════════╝"
