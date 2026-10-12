#!/bin/bash
# ═══════════════════════════════════════════════════════════════════
#  VELARIS Startup Script
#  Run after every reboot to bring the full environment up.
#
#  Usage:  ./start-velaris.sh
# ═══════════════════════════════════════════════════════════════════

set -euo pipefail

VELARIS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
COMPOSE_FILE="$VELARIS_DIR/deploy/docker-compose/docker-compose.yml"
# Scratch files and error logs go to a directory only this user can write. A
# fixed name under /tmp can be pre-created, or pointed somewhere else, by any
# other local user before this script writes to it.
VELARIS_SCRATCH="${VELARIS_SCRATCH:-${XDG_STATE_HOME:-${HOME:-}/.local/state}/velaris}"
if ! { mkdir -p "$VELARIS_SCRATCH" && chmod 700 "$VELARIS_SCRATCH" && [ -w "$VELARIS_SCRATCH" ]; } 2>/dev/null; then
  VELARIS_SCRATCH="$(mktemp -d "${TMPDIR:-/tmp}/velaris.XXXXXX")"
fi
# Which optional compose services this install runs (the bundled MinIO is kept
# only where it already exists) — see the file for the rule.
# shellcheck source=/dev/null
[ -f "$VELARIS_DIR/deploy/docker-compose/compose-profiles.sh" ] && . "$VELARIS_DIR/deploy/docker-compose/compose-profiles.sh"

# uv installs itself to ~/.local/bin, which is NOT on PATH in a fresh non-login
# shell. Add it here rather than making the operator export it — a missing uv
# used to surface as "product key verification failed", which is a lie.
case ":$PATH:" in
  *":$HOME/.local/bin:"*) ;;
  *) PATH="$HOME/.local/bin:$PATH"; export PATH ;;
esac
DB_CONTAINER="docker-compose-velaris-db-1"
DB_USER="velaris"
DB_NAME="velaris"

# ── Step 0: OpenBao secrets (Group K, opt-in) ─────────────────────
# Enabled by the sentinel file deploy/openbao/enabled. When active, .env is
# RENDERED from OpenBao before anything reads it. Render is fail-closed: on
# any problem the existing .env stays untouched and we continue on the
# last-known-good secrets (warning printed). No sentinel = no change at all.
if [ -f "$VELARIS_DIR/deploy/openbao/enabled" ]; then
  echo "▶ Step 0: OpenBao secrets..."
  [ -f "$VELARIS_DIR/.env" ] || touch "$VELARIS_DIR/.env"   # compose interpolation needs the file to exist
  # Every failure below degrades to "continue on the existing .env" — Step 0
  # must never be the reason the platform fails to start (set -e is active).
  if ! docker compose -f "$COMPOSE_FILE" --env-file "$VELARIS_DIR/.env" up -d openbao 2>/dev/null; then
    echo -e "  \033[0;33m⚠ Could not start the OpenBao container\033[0m"
  fi
  BAO_HEALTH="000"
  for i in $(seq 1 15); do
    BAO_HEALTH=$(curl -s -m 2 -o /dev/null -w '%{http_code}' \
      "http://127.0.0.1:8350/v1/sys/health?sealedcode=472&uninitcode=471" || echo "000")
    [ "$BAO_HEALTH" != "000" ] && break
    sleep 1
  done
  if [ "$BAO_HEALTH" = "472" ]; then
    echo "  Unsealing OpenBao from local keyfile..."
    if "$VELARIS_DIR/scripts/secrets-unseal.sh" >/dev/null 2>&1; then
      BAO_HEALTH=200
    else
      echo -e "  \033[0;33m⚠ Unseal failed (bad or unreadable keyfile)\033[0m"
    fi
  fi
  if [ "$BAO_HEALTH" = "200" ] || [ "$BAO_HEALTH" = "429" ]; then
    if "$VELARIS_DIR/scripts/secrets-render.sh"; then
      echo -e "  \033[0;32m✓ .env rendered from OpenBao\033[0m"
    else
      echo -e "  \033[0;33m⚠ Render failed — continuing on the existing .env\033[0m"
    fi
  else
    echo -e "  \033[0;33m⚠ OpenBao unavailable (health=$BAO_HEALTH) — continuing on the existing .env\033[0m"
  fi
  echo ""
fi

# Load .env early so VELARIS_DB_PASSWORD is available for docker compose
if [ -f "$VELARIS_DIR/.env" ]; then
  set -o allexport; source "$VELARIS_DIR/.env"; set +o allexport
fi

green()  { echo -e "\033[0;32m$*\033[0m"; }
yellow() { echo -e "\033[0;33m$*\033[0m"; }
red()    { echo -e "\033[0;31m$*\033[0m"; }

echo "╔══════════════════════════════════════════╗"
echo "║         VELARIS Startup                  ║"
echo "╚══════════════════════════════════════════╝"
echo ""

# ════════════════════════════════════════════════════════════════════
# GATE 1: Product key verification (MUST PASS — first check, always)
# ════════════════════════════════════════════════════════════════════
# If systemd already supervises Velaris, starting a second copy from a shell
# gives two processes racing for 8100/8201/8202/8203/8204/5173. The nohup copy usually wins
# the bind, systemd's restarts fail quietly, and the whole thing disappears at
# the next logout. Refuse instead.
if systemctl is-active --quiet velaris-case-service 2>/dev/null \
   || systemctl is-active --quiet velaris-engine 2>/dev/null; then
  yellow "  ⚠ Velaris is already running under systemd."
  echo   "    Status:   sudo systemctl status velaris-engine velaris-case-service velaris-analytics-service velaris-hxgraph-service velaris-hxmigrate-service velaris-studio"
  echo   "    Restart:  sudo systemctl restart velaris-engine velaris-case-service velaris-analytics-service velaris-hxgraph-service velaris-hxmigrate-service velaris-studio"
  echo   "    Logs:     sudo journalctl -u velaris-case-service -f"
  echo ""
  echo   "    To run from this shell instead:"
  echo   "      sudo systemctl stop velaris-engine velaris-case-service velaris-analytics-service velaris-hxgraph-service velaris-hxmigrate-service velaris-studio"
  exit 0
fi

# A missing toolchain is a toolchain problem — report it as one instead of
# blaming the licence, which sent the operator hunting the wrong thing.
if ! command -v uv >/dev/null 2>&1; then
  red "  ✗ 'uv' is not installed (or not on PATH)."
  echo "    Install it with:  curl -LsSf https://astral.sh/uv/install.sh | sh"
  echo "    Then re-run ./start-velaris.sh"
  exit 1
fi

echo "▶ Verifying product key…"
set +e
uv run python "$VELARIS_DIR/scripts/verify_key.py"
KEY_STATUS=$?
set -e
if [ "$KEY_STATUS" -eq 1 ]; then
  echo ""
  red "╔═══════════════════════════════════════════════════════╗"
  red "║  STARTUP BLOCKED: product key verification failed.   ║"
  red "║  Run ./setup-velaris.sh if this is a new install.    ║"
  red "║  Contact support: velaris.app.os@gmail.com                   ║"
  red "╚═══════════════════════════════════════════════════════╝"
  exit 1
elif [ "$KEY_STATUS" -eq 2 ]; then
  # Not activated yet. Starting is REQUIRED here: the registration website
  # calls this platform's API to create the customer's user, so the key
  # cannot exist until these services are up. See scripts/verify_key.py.
  yellow "  ⚠ Running unactivated — activate with ./activate-velaris.sh once you have a key."
fi
echo ""

# ── Update check (non-blocking notice) ──────────────────────────
_yaml_val() {
  grep -E "^\s+${1}:" "$VELARIS_DIR/velaris.yaml" 2>/dev/null \
    | head -1 | sed -E "s/.*${1}:[[:space:]]*[\"']?([^\"'#]+)[\"']?.*/\1/" | xargs || echo "${2:-}"
}
_CURRENT_VER=$(_yaml_val "version" "")
_UPDATE_SOURCE=$(_yaml_val "source" "github")
_GITHUB_REPO=$(_yaml_val "github_repo" "")
_SERVER_URL=$(_yaml_val "server_url" "")
if [ -n "$_CURRENT_VER" ]; then
  _LATEST_VER=""
  if [ "$_UPDATE_SOURCE" = "github" ] && [ -n "$_GITHUB_REPO" ]; then
    _LATEST_VER=$(curl -sf --max-time 5 \
      -H "Accept: application/vnd.github+json" \
      "https://api.github.com/repos/${_GITHUB_REPO}/releases/latest" 2>/dev/null \
      | python3 -c "import sys,json; print(json.load(sys.stdin)['tag_name'].lstrip('v'))" 2>/dev/null || echo "")
  elif [ "$_UPDATE_SOURCE" = "server" ] && [ -n "$_SERVER_URL" ]; then
    _LATEST_VER=$(curl -sf --max-time 5 "$_SERVER_URL" 2>/dev/null \
      | python3 -c "import sys,json; print(json.load(sys.stdin)['version'])" 2>/dev/null || echo "")
  fi
  if [ -n "$_LATEST_VER" ] && [ "$_LATEST_VER" != "$_CURRENT_VER" ]; then
    yellow "  ⚠ Update available: v$_CURRENT_VER → v$_LATEST_VER"
    yellow "    Run ./update-velaris.sh to upgrade"
    echo ""
  fi
fi

# ── DB SDK: select the database backend early ────────────────────
# Steps 3/4/4b and the superadmin gate branch on this. PostgreSQL keeps the exact
# original docker-exec/psql path (bundled container). MySQL is a BYO external DB:
# reached over the network by a throwaway mysql:8 client container (no host mysql
# binary needed; password via MYSQL_PWD, never on the command line, from
# VELARIS_DB_PASSWORD/OpenBao — never velaris.yaml plaintext).
DATABASE_BACKEND=$(_yaml_val "database" "postgresql")
# DB_FAMILY normalises the runner path: MariaDB reuses the entire MySQL path (same
# migrations/mysql baseline, schema_migrations DDL, manifest, client protocol) and
# differs only in the client image. All MySQL-path branches below test DB_FAMILY.
case "$DATABASE_BACKEND" in
  postgresql)    DB_FAMILY=postgresql ;;
  mysql|mariadb) DB_FAMILY=mysql ;;
  *)
    red "STARTUP BLOCKED: unsupported database backend '$DATABASE_BACKEND' in velaris.yaml (allowed: postgresql, mysql, mariadb)."
    exit 1
    ;;
esac
export DATABASE_BACKEND
if [ "$DB_FAMILY" = "mysql" ]; then
  DB_HOST=$(_yaml_val "db_host" "127.0.0.1")
  DB_PORT=$(_yaml_val "db_port" "3306")
  DB_NAME=$(_yaml_val "db_name" "velaris")
  DB_USER=$(_yaml_val "db_user" "velaris")
  DB_PASSWORD="${VELARIS_DB_PASSWORD:-}"
  # The mysql:8 client speaks to both MySQL 8 and MariaDB 10.6+/11 servers (the mariadb
  # image dropped the `mysql` command, so mysql:8 is the portable client for both).
  mysql_client() {
    docker run --rm -i --network host -e MYSQL_PWD="$DB_PASSWORD" mysql:8 \
      mysql --connect-timeout=10 -h "$DB_HOST" -P "$DB_PORT" -u "$DB_USER" "$@"
  }
fi

# ── Guard: Docker accessible? ────────────────────────────────────
if ! docker info &>/dev/null 2>&1; then
  red "ERROR: Docker not accessible. Run setup-velaris.sh first, then log out/in."
  exit 1
fi

# ── Step 0: Free ports and clean old containers ──────────────────
echo "▶ Preparing ports..."
# --remove-orphans is load-bearing. A plain `down` only touches services still
# named in the compose file, so anything that gets renamed leaves its old
# container running and holding its published port. That is exactly what the
# 2026-08-07 helix-*→velaris-* rename did: docker-compose-helix-db-1 survived
# the down and kept 127.0.0.1:5432, and the next `up` died with "port is
# already allocated".
docker compose -f "$COMPOSE_FILE" --env-file "$VELARIS_DIR/.env" down --remove-orphans 2>/dev/null || true
sudo systemctl stop postgresql 2>/dev/null || true

# Host processes only — these ports belong to uvicorn/vite, not to Docker.
for port in 8100 8201 8202 8203 8204 5173; do
  fuser -k "${port}/tcp" 2>/dev/null || true
done

# 5432 is handled separately and never blind-killed. When Docker holds it the
# listener is docker-proxy, and killing that does NOT release the daemon's port
# reservation — it just strands it, so every later `up` fails with "port is
# already allocated" until dockerd restarts. The `down` above is what frees a
# container-held 5432; only a genuine host Postgres gets killed here.
if fuser 5432/tcp &>/dev/null 2>&1; then
  if docker ps --format '{{.Ports}}' 2>/dev/null | grep -q ':5432->'; then
    yellow "  ⚠ Port 5432 is held by a running container — leaving it to compose"
  else
    fuser -k 5432/tcp 2>/dev/null || true
  fi
fi
sleep 2
green "  ✓ Ports ready"
echo ""

# ── Step 1: Start Docker infrastructure ──────────────────────────
echo "▶ Starting Docker infrastructure..."
docker compose -f "$COMPOSE_FILE" --env-file "$VELARIS_DIR/.env" up -d
echo "  Waiting for services to initialise..."
sleep 10

# Group K: the global down/up above restarted OpenBao sealed. .env is already
# rendered (Step 0); re-unseal so day-2 secret operations work without manual
# intervention. Failure is non-fatal — the platform runs fine sealed.
if [ -f "$VELARIS_DIR/deploy/openbao/enabled" ]; then
  "$VELARIS_DIR/scripts/secrets-unseal.sh" 2>/dev/null \
    && green "  ✓ OpenBao unsealed" \
    || yellow "  ⚠ OpenBao still sealed — run ./scripts/secrets-unseal.sh before pushing secrets"
fi

echo ""
echo "  Checking containers:"
_CHECK_SVCS="velaris-db temporal temporal-ui cache mailpit"
[ "${VELARIS_BUNDLED_MINIO_ACTIVE:-false}" = true ] && _CHECK_SVCS="$_CHECK_SVCS minio"
for svc in $_CHECK_SVCS; do
  if docker ps --format '{{.Names}}' | grep -q "$svc"; then
    green "    ✓ $svc running"
  else
    yellow "    ⚠ $svc not found (may be optional or slow to start)"
  fi
done
echo ""

# ── Step 2: Wait for Temporal ────────────────────────────────────
echo "▶ Waiting for Temporal (port 7233)..."
for i in $(seq 1 30); do
  if nc -z localhost 7233 2>/dev/null; then
    green "  ✓ Temporal ready"; break
  fi
  [ "$i" -eq 30 ] && yellow "  ⚠ Temporal not ready after 30s — continuing"
  sleep 1
done

# ── Step 3: Wait for the database ────────────────────────────────
if [ "$DB_FAMILY" = "mysql" ]; then
  echo "▶ Waiting for MySQL ($DB_HOST:$DB_PORT)..."
  for i in $(seq 1 20); do
    if mysql_client -e "SELECT 1" >/dev/null 2>&1; then
      green "  ✓ MySQL ready"; break
    fi
    [ "$i" -eq 20 ] && { red "  ✗ MySQL not ready at $DB_HOST:$DB_PORT — check the DB and VELARIS_DB_PASSWORD"; exit 1; }
    sleep 1
  done
else
  echo "▶ Waiting for PostgreSQL (port 5432)..."
  for i in $(seq 1 20); do
    if docker exec "$DB_CONTAINER" pg_isready -U "$DB_USER" -q 2>/dev/null; then
      green "  ✓ PostgreSQL ready"; break
    fi
    [ "$i" -eq 20 ] && { red "  ✗ PostgreSQL not ready — check: docker logs $DB_CONTAINER"; exit 1; }
    sleep 1
  done
fi
echo ""

# ── Step 4: Run all migrations ────────────────────────────────────
echo "▶ Running database migrations..."

if [ "$DB_FAMILY" = "mysql" ]; then
  # MySQL track: the consolidated baseline under migrations/mysql/ (Velaris ships
  # fresh on MySQL — no incremental PG files). schema_migrations is the MySQL
  # variant (VARCHAR PK / DATETIME(6) / INSERT IGNORE).
  # Ensure the database exists, as utf8mb4 — required for correctness (emoji/non-Latin
  # text) and for the InnoDB 3072-byte key-limit math the schema is bounded against.
  # Idempotent; a no-op if a DBA pre-created it. Issued without a default DB selected.
  mysql_client -e "CREATE DATABASE IF NOT EXISTS \`$DB_NAME\` CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;"
  mysql_client "$DB_NAME" -e "CREATE TABLE IF NOT EXISTS schema_migrations (
  filename   VARCHAR(255) PRIMARY KEY,
  applied_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6)
);"
  APPLIED=0; SKIPPED=0
  for migration_file in $(find "$VELARIS_DIR/migrations/mysql" -maxdepth 1 -name "*.sql" | sort); do
    filename="$(basename "$migration_file")"
    EXISTS=$(mysql_client "$DB_NAME" -N -e "SELECT 1 FROM schema_migrations WHERE filename='$filename';" 2>/dev/null || echo "")
    if [ "$EXISTS" = "1" ]; then
      echo "    skip  $filename"; SKIPPED=$((SKIPPED + 1)); continue
    fi
    echo -n "    apply $filename ... "
    if mysql_client "$DB_NAME" < "$migration_file" 2>"$VELARIS_SCRATCH/migration_err.log"; then
      mysql_client "$DB_NAME" -e "INSERT IGNORE INTO schema_migrations(filename) VALUES('$filename');"
      green "✓"; APPLIED=$((APPLIED + 1))
    else
      red "FAILED"; cat "$VELARIS_SCRATCH/migration_err.log"
      red "  Migration failed — stopping."; exit 1
    fi
  done
  echo ""
  green "  ✓ Migrations: $APPLIED applied, $SKIPPED already up-to-date"
  echo ""
else
docker exec -i "$DB_CONTAINER" psql -U "$DB_USER" -d "$DB_NAME" -q <<'SQL'
CREATE TABLE IF NOT EXISTS schema_migrations (
  filename   TEXT PRIMARY KEY,
  applied_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
SQL

APPLIED=0; SKIPPED=0; FAILED=0

for migration_file in $(find "$VELARIS_DIR/migrations/postgresql" -maxdepth 1 -name "*.sql" | sort); do
  filename="$(basename "$migration_file")"
  EXISTS=$(docker exec "$DB_CONTAINER" psql -U "$DB_USER" -d "$DB_NAME" -tAq \
    -c "SELECT 1 FROM schema_migrations WHERE filename='$filename';" 2>/dev/null || echo "")
  if [ "$EXISTS" = "1" ]; then
    echo "    skip  $filename"; SKIPPED=$((SKIPPED + 1)); continue
  fi
  echo -n "    apply $filename ... "
  # ON_ERROR_STOP is what makes a failure a failure. Without it psql runs the
  # rest of the file, exits 0, and the migration is recorded as applied — which
  # is how an install ended up with 080 "applied" and its column unchanged
  # (repaired by 217). setup-velaris.sh and update-velaris.sh already had it;
  # this runner was missed until 2026-10-11.
  if docker exec -i "$DB_CONTAINER" psql -U "$DB_USER" -d "$DB_NAME" -q \
      -v ON_ERROR_STOP=1 < "$migration_file" 2>"$VELARIS_SCRATCH/migration_err.log"; then
    docker exec "$DB_CONTAINER" psql -U "$DB_USER" -d "$DB_NAME" -q \
      -c "INSERT INTO schema_migrations(filename) VALUES('$filename') ON CONFLICT DO NOTHING;"
    green "✓"; APPLIED=$((APPLIED + 1))
  else
    red "FAILED"; cat "$VELARIS_SCRATCH/migration_err.log"; FAILED=$((FAILED + 1))
    red "  Migration failed — stopping."; exit 1
  fi
done
echo ""
green "  ✓ Migrations: $APPLIED applied, $SKIPPED already up-to-date"
echo ""
fi

# ── Step 4b: Sync release manifest ───────────────────────────────
# Applies releases/manifest.sql on EVERY start. The file is an idempotent
# upsert, so this is cheap and self-correcting.
#
# It used to run only when the INSERT count differed from the row count in
# scheduled_releases. That made a version bump unreachable: adding a feature
# changed the count and applied, but editing an existing feature's version left
# the counts equal, so the manifest was never re-applied — and even if it had
# been, ON CONFLICT DO NOTHING would have skipped the row. Both halves of that
# are fixed; the count is now reported, not used as a gate.
# MySQL uses the dialect sibling manifest.mysql.sql (UUID() / ON DUPLICATE KEY).
if [ "$DB_FAMILY" = "mysql" ]; then
  MANIFEST_FILE="$VELARIS_DIR/releases/manifest.mysql.sql"
else
  MANIFEST_FILE="$VELARIS_DIR/releases/manifest.sql"
fi
if [ -f "$MANIFEST_FILE" ]; then
  MANIFEST_COUNT=$(grep -c "^INSERT" "$MANIFEST_FILE" 2>/dev/null || echo 0)
  if [ "$DB_FAMILY" = "mysql" ]; then
    DB_COUNT=$(mysql_client "$DB_NAME" -N -e "SELECT COUNT(*) FROM scheduled_releases;" 2>/dev/null | tr -d '[:space:]' || echo 0)
  else
    DB_COUNT=$(docker exec "$DB_CONTAINER" psql -U "$DB_USER" -d "$DB_NAME" -tAq \
      -c "SELECT COUNT(*) FROM scheduled_releases;" 2>/dev/null | tr -d '[:space:]' || echo 0)
  fi
  echo "▶ Syncing release manifest ($DB_COUNT features in DB, $MANIFEST_COUNT in manifest)..."
  if [ "$DB_FAMILY" = "mysql" ]; then
    _man_ok=0; mysql_client "$DB_NAME" < "$MANIFEST_FILE" 2>"$VELARIS_SCRATCH/manifest_err.log" && _man_ok=1
  else
    _man_ok=0; docker exec -i "$DB_CONTAINER" psql -U "$DB_USER" -d "$DB_NAME" -q < "$MANIFEST_FILE" 2>"$VELARIS_SCRATCH/manifest_err.log" && _man_ok=1
  fi
  if [ "$_man_ok" = "1" ]; then
    if [ "$MANIFEST_COUNT" -gt "$DB_COUNT" ]; then
      green "  ✓ Release manifest synced — $((MANIFEST_COUNT - DB_COUNT)) new feature(s) activated"
    else
      green "  ✓ Release manifest synced ($MANIFEST_COUNT features, versions refreshed)"
    fi
  else
    yellow "  ⚠ Release manifest sync had warnings:"; cat "$VELARIS_SCRATCH/manifest_err.log"
  fi
  echo ""
fi

# ════════════════════════════════════════════════════════════════════
# GATE 2: Superadmin DB presence check
# ════════════════════════════════════════════════════════════════════
echo "▶ Verifying superadmin account…"
if [ "$DB_FAMILY" = "mysql" ]; then
  # No password in the URL — check_superadmin.py reads VELARIS_DB_PASSWORD from the
  # env (sourced from .env above), so a special-char password can't corrupt the URL.
  SUPERADMIN_DB_URL="mysql://${DB_USER}@${DB_HOST}:${DB_PORT}/${DB_NAME}"
else
  SUPERADMIN_DB_URL="postgresql://velaris:${VELARIS_DB_PASSWORD:-velaris_dev_password}@localhost:5432/velaris"
fi
DATABASE_URL="$SUPERADMIN_DB_URL" \
  uv run python "$VELARIS_DIR/scripts/check_superadmin.py" || {
  echo ""
  red "╔═══════════════════════════════════════════════════════╗"
  red "║  STARTUP BLOCKED: no superadmin account found.       ║"
  red "║  Run ./setup-velaris.sh to create one.               ║"
  red "╚═══════════════════════════════════════════════════════╝"
  exit 1
}
green "  ✓ Superadmin verified"
echo ""

# ── Step 4b: Sync Python workspace dependencies ──────────────────
echo "▶ Syncing Python dependencies..."
cd "$VELARIS_DIR"
# Optional ML extras are NOT part of the default install (a base box stays lean,
# no model weights). But `uv sync` is exact by default — it prunes anything not
# declared, so an operator who installed an extra would silently lose it on the
# next run. Detect what is already present and ask for it again.
_EXTRAS=""
uv run python -c "import faster_whisper" >/dev/null 2>&1 && _EXTRAS="$_EXTRAS --extra intelligence"
uv run python -c "import av" >/dev/null 2>&1 && _EXTRAS="$_EXTRAS --extra kyc"
uv sync --all-packages $_EXTRAS --quiet 2>/dev/null && green "  ✓ Dependencies up to date$([ -n "$_EXTRAS" ] && echo " (extras preserved:$_EXTRAS)")" || yellow "  ⚠ uv sync had warnings — continuing"
echo ""

# ── Step 5: Set environment variables ────────────────────────────
export VELARIS_SERVICE_REGISTRY='{"order-service":"http://localhost:3001","notification-service":"http://localhost:3002"}'

# DB SDK: the database backend was already detected, allowlist-gated, and exported
# near the top (so Steps 3/4/4b/Gate-2 could branch). The Python layer
# (velaris_shared.db.backends) enforces the same allowlist — fail-closed both sides.
green "  ✓ Environment ready (database: $DATABASE_BACKEND)"

# ── Step 5b: Check Ollama ─────────────────────────────────────────
echo "▶ Checking Ollama (HxNexus AI backend, port 11434)..."
if curl -s --max-time 2 http://localhost:11434/api/tags > /dev/null 2>&1; then
  # An online Ollama with NO models pulled makes every AI feature (chat,
  # case Q&A, session summaries) fail soft with empty answers — warn loudly.
  MODEL_COUNT=$(curl -s --max-time 2 http://localhost:11434/api/tags \
    | python3 -c "import sys,json; print(len(json.load(sys.stdin).get('models',[])))" 2>/dev/null || echo "?")
  if [ "$MODEL_COUNT" = "0" ]; then
    yellow "  ⚠ Ollama online but NO models pulled — HxNexus answers will be empty."
    yellow "    Pull the defaults:  docker exec docker-compose-ollama-1 ollama pull ${VELARIS_CASE_AI_OLLAMA_MODEL:-llama3.2}"
    yellow "                        docker exec docker-compose-ollama-1 ollama pull ${VELARIS_CASE_AI_OLLAMA_EMBED_MODEL:-nomic-embed-text}"
  else
    green "  ✓ Ollama online ($MODEL_COUNT model(s)) — HxNexus ready"
  fi
else
  yellow "  ⚠ Ollama not reachable — HxNexus will show offline"
fi
echo ""

# ── Internal TLS (roadmap #18) ────────────────────────────────────
# Off by default. `_velaris_tls_args` prints nothing unless
# VELARIS_INTERNAL_TLS_ENABLED=true, so the launch commands below are byte-for-
# byte what they were. Defined as a no-op FIRST so the script still runs if the
# helper file is absent — a missing optional file must not stop the platform.
# One source for the ports (2026-08-24). Defaults are the historical values, so
# an install that sets none of these keeps exactly the ports it had.
# shellcheck source=/dev/null
[ -f "$VELARIS_DIR/deploy/ports/velaris-ports.sh" ] && . "$VELARIS_DIR/deploy/ports/velaris-ports.sh"
: "${VELARIS_ENGINE_PORT:=8100}" "${VELARIS_CASE_PORT:=8201}" "${VELARIS_ANALYTICS_PORT:=8202}"
: "${VELARIS_HXGRAPH_PORT:=8203}" "${VELARIS_HXMIGRATE_PORT:=8204}" "${VELARIS_STUDIO_PORT:=5173}"

_velaris_tls_args() { :; }
# Same reasoning: a plain http probe is exactly what the script did before.
_velaris_probe() { curl -sf --max-time 3 "http://localhost:$1/health" >/dev/null 2>&1; }
# shellcheck source=/dev/null
[ -f "$VELARIS_DIR/deploy/tls/uvicorn-tls-args.sh" ] && . "$VELARIS_DIR/deploy/tls/uvicorn-tls-args.sh"

# ── Step 6: Start Engine ──────────────────────────────────────────
echo "▶ Starting Velaris Engine (port 8100)..."
cd "$VELARIS_DIR"
# Loopback-only: the engine is called by case-service (VELARIS_CASE_ENGINE_API_URL)
# and by Studio's vite proxy, both from this host. Bound to 0.0.0.0 it was a
# public, unauthenticated port on any server whose firewall was not up.
# Watch only this service's own source and the shared package. Without
# --reload-dir the reloader watches the whole repo — .venv, node_modules,
# Velaris/, dist — which is ~18,600 inotify watches EACH. Since #13b-e made
# this four processes that is 65,000+ of the kernel's 65,536 default, and the
# next thing to start (Studio's vite) dies with ENOSPC. Seen 2026-08-24.
nohup uv run uvicorn helix_engine.main:app \
  --host 127.0.0.1 --port "${VELARIS_ENGINE_PORT}" --reload \
  --reload-dir engine \
  --proxy-headers \
  > /tmp/velaris-engine.log 2>&1 &
echo "  PID: $! | Logs: tail -f /tmp/velaris-engine.log"
for i in $(seq 1 20); do
  if curl -s http://localhost:8100/health > /dev/null 2>&1; then
    green "  ✓ Engine ready on port 8100"; break
  fi
  [ "$i" -eq 20 ] && yellow "  ⚠ Engine not ready after 20s"
  sleep 1
done
echo ""

# ── Step 7: Start Case Service ────────────────────────────────────
echo "▶ Starting Case Service (loopback 127.0.0.1:8201, only reachable via API gateway on 8200)..."
# shellcheck disable=SC2046 - deliberate word splitting; the values are ours
nohup uv run uvicorn case_service.main:app $(_velaris_tls_args case-service) \
  --host 127.0.0.1 --port "${VELARIS_CASE_PORT}" --reload \
  --reload-dir services/case-service --reload-dir services/_shared \
  --proxy-headers \
  --app-dir services/case-service \
  > /tmp/velaris-case-service.log 2>&1 &
echo "  PID: $! | Logs: tail -f /tmp/velaris-case-service.log"
for i in $(seq 1 20); do
  if _velaris_probe "${VELARIS_CASE_PORT}"; then
    green "  ✓ Case Service ready on port 8201"; break
  fi
  [ "$i" -eq 20 ] && yellow "  ⚠ Case Service not ready after 20s"
  sleep 1
done
echo ""

# ── Step 7b: Start Analytics Service ──────────────────────────────
# Extracted from case-service at roadmap 13c. Loopback-only for the same
# reason case-service is: the API gateway on 8200 is the only door, and a
# second door on 0.0.0.0 would go around it.
echo "▶ Starting Analytics Service (loopback 127.0.0.1:8202, reachable via the gateway on 8200)..."
cd "$VELARIS_DIR"
# shellcheck disable=SC2046 - deliberate word splitting; the values are ours
nohup uv run uvicorn analytics_service.main:app $(_velaris_tls_args analytics-service) \
  --host 127.0.0.1 --port "${VELARIS_ANALYTICS_PORT}" --reload \
  --reload-dir services/analytics-service --reload-dir services/_shared \
  --proxy-headers \
  --app-dir services/analytics-service \
  > /tmp/velaris-analytics-service.log 2>&1 &
echo "  PID: $! | Logs: tail -f /tmp/velaris-analytics-service.log"
for i in $(seq 1 20); do
  if _velaris_probe "${VELARIS_ANALYTICS_PORT}"; then
    green "  ✓ Analytics Service ready on port 8202"; break
  fi
  [ "$i" -eq 20 ] && yellow "  ⚠ Analytics Service not ready after 20s"
  sleep 1
done
echo ""

# ── Step 7c: Start HxGraph Service ────────────────────────────────
# Extracted from case-service at roadmap 13d. Loopback-only, like the rest.
echo "▶ Starting HxGraph Service (loopback 127.0.0.1:8203, reachable via the gateway on 8200)..."
cd "$VELARIS_DIR"
# shellcheck disable=SC2046 - deliberate word splitting; the values are ours
nohup uv run uvicorn hxgraph_service.main:app $(_velaris_tls_args hxgraph-service) \
  --host 127.0.0.1 --port "${VELARIS_HXGRAPH_PORT}" --reload \
  --reload-dir services/hxgraph-service --reload-dir services/_shared \
  --proxy-headers \
  --app-dir services/hxgraph-service \
  > /tmp/velaris-hxgraph-service.log 2>&1 &
echo "  PID: $! | Logs: tail -f /tmp/velaris-hxgraph-service.log"
for i in $(seq 1 20); do
  if _velaris_probe "${VELARIS_HXGRAPH_PORT}"; then
    green "  ✓ HxGraph Service ready on port 8203"; break
  fi
  [ "$i" -eq 20 ] && yellow "  ⚠ HxGraph Service not ready after 20s"
  sleep 1
done
echo ""

# ── Step 7d: Start HxMigrate Service ──────────────────────────────
# Extracted from case-service at roadmap 13e — the migration domain
# (hxmigrate + bpm_importer + scout + orchestrator). Loopback-only.
echo "▶ Starting HxMigrate Service (loopback 127.0.0.1:8204, reachable via the gateway on 8200)..."
cd "$VELARIS_DIR"
# shellcheck disable=SC2046 - deliberate word splitting; the values are ours
nohup uv run uvicorn hxmigrate_service.main:app $(_velaris_tls_args hxmigrate-service) \
  --host 127.0.0.1 --port "${VELARIS_HXMIGRATE_PORT}" --reload \
  --reload-dir services/hxmigrate-service --reload-dir services/_shared \
  --proxy-headers \
  --app-dir services/hxmigrate-service \
  > /tmp/velaris-hxmigrate-service.log 2>&1 &
echo "  PID: $! | Logs: tail -f /tmp/velaris-hxmigrate-service.log"
for i in $(seq 1 20); do
  if _velaris_probe "${VELARIS_HXMIGRATE_PORT}"; then
    green "  ✓ HxMigrate Service ready on port 8204"; break
  fi
  [ "$i" -eq 20 ] && yellow "  ⚠ HxMigrate Service not ready after 20s"
  sleep 1
done
echo ""

# ── Step 8: Start Studio ─────────────────────────────────────────
echo "▶ Starting Velaris Studio (port 5173)..."
cd "$VELARIS_DIR/studio"

# Reinstall npm deps if package.json changed since the last install, OR if the
# installed tree doesn't actually satisfy package.json (a release can add
# packages, e.g. livekit-client in 2.1.0; a plain `git pull` + start would
# otherwise leave node_modules stale and Vite fails to resolve imports).
# node_modules lives at the workspace root in dev (npm workspaces) or inside
# studio/ in a standalone clone — check whichever marker exists.
_NM_MARK=""
for _m in "$VELARIS_DIR/node_modules/.package-lock.json" "$VELARIS_DIR/studio/node_modules/.package-lock.json"; do
  [ -f "$_m" ] && _NM_MARK="$_m" && break
done
_NEED_NPM=0
if [ -z "$_NM_MARK" ] \
   || [ "$VELARIS_DIR/studio/package.json" -nt "$_NM_MARK" ] \
   || { [ -f "$VELARIS_DIR/package.json" ] && [ "$VELARIS_DIR/package.json" -nt "$_NM_MARK" ]; }; then
  _NEED_NPM=1
elif ! npm ls --depth=0 >/dev/null 2>&1; then
  # mtimes look fine but the tree is broken/incomplete (failed earlier install)
  _NEED_NPM=1
fi
if [ "$_NEED_NPM" = 1 ]; then
  echo "  Studio dependencies missing/stale — running npm install..."
  _NPM_LOG="$VELARIS_SCRATCH/velaris-npm-install.log"
  if npm install --prefer-offline >"$_NPM_LOG" 2>&1 \
     || npm install >"$_NPM_LOG" 2>&1; then   # retry online if offline-first failed
    rm -rf "$VELARIS_DIR/studio/node_modules/.vite"
    green "  ✓ Studio dependencies installed"
  else
    tail -5 "$_NPM_LOG"
    yellow "  ⚠ npm install FAILED — Studio may not load. Full log: $_NPM_LOG"
  fi
else
  green "  ✓ Studio dependencies up to date"
fi

nohup npm run dev -- --host 0.0.0.0 --port "$VELARIS_STUDIO_PORT" --strictPort \
  > /tmp/velaris-studio.log 2>&1 &
echo "  PID: $! | Logs: tail -f /tmp/velaris-studio.log"
sleep 3
echo ""
cd "$VELARIS_DIR"

# ── Heartbeat ping (background, non-blocking) ─────────────────────
if [ -f "$VELARIS_DIR/.velaris-key" ]; then
  KEY_ID=$(python3 -c "import json; d=json.load(open('$VELARIS_DIR/.velaris-key')); print(d.get('key_id',''))" 2>/dev/null || echo "")
  if [ -n "$KEY_ID" ]; then
    curl -s -X POST "${VELARIS_REGISTER_URL:-https://register.velaris.io}/heartbeat" \
      -H "Content-Type: application/json" \
      -d "{\"key_id\":\"$KEY_ID\",\"mac\":\"$(cat /sys/class/net/$(ip route show default | awk '/default/{print $5}')/address 2>/dev/null || echo unknown)\",\"version\":\"1.0.0\"}" \
      --max-time 5 > /dev/null 2>&1 &
  fi
fi

# ── Speech capability notice ─────────────────────────────────────
# Voice input ships enabled (releases/manifest.sql) but needs BOTH halves:
#   1. a local Whisper engine — the optional `intelligence` extra. Without it
#      /stt/status reports no engine and the mic renders nothing at all (an
#      absent capability is absent, never a dead button), so this is a notice
#      rather than a failure.
#   2. a SECURE CONTEXT in the browser. `navigator.mediaDevices` is undefined
#      on a plain-http origin, so the mic works at http://localhost but is
#      silently unavailable to anyone opening Studio or the customer portal
#      over http://<LAN-IP> — most obviously from a phone. Put a TLS
#      terminator in front for those users.
if ! uv run python -c "import faster_whisper" >/dev/null 2>&1; then
  yellow "  ⚠ Speech-to-text engine not installed — voice input will be hidden."
  echo   "    Enable it with:  uv sync --all-packages --extra intelligence"
  echo ""
fi

# ── Done ─────────────────────────────────────────────────────────
echo "╔═══════════════════════════════════════════════════════╗"
green "║              VELARIS is running!                      ║"
echo "╠═══════════════════════════════════════════════════════╣"
echo "║  Studio:        http://localhost:5173                 ║"
echo "║  Engine API:    http://localhost:8100                 ║"
echo "║  Case Service:  http://localhost:8200                 ║"
echo "║  Temporal UI:   http://localhost:8233                 ║"
[ "${VELARIS_BUNDLED_MINIO_ACTIVE:-false}" = true ] && \
echo "║  MinIO Console: http://localhost:9001                 ║"
echo "║  Mailpit UI:    http://localhost:8025                 ║"
echo "╠═══════════════════════════════════════════════════════╣"
echo "║  Engine logs:   tail -f /tmp/velaris-engine.log       ║"
echo "║  Case logs:     tail -f /tmp/velaris-case-service.log ║"
echo "║  Studio logs:   tail -f /tmp/velaris-studio.log       ║"
echo "╠═══════════════════════════════════════════════════════╣"
echo "║  Stop:          ./stop-velaris.sh                     ║"
echo "╠═══════════════════════════════════════════════════════╣"
echo "║  Microphone features (voice input, HxMeet captions)   ║"
echo "║  need https or localhost — browsers refuse mic access ║"
echo "║  over plain http on a LAN address.                    ║"
echo "╚═══════════════════════════════════════════════════════╝"
