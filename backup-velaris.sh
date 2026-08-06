#!/usr/bin/env bash
# Velaris — take a database backup AND record it in HxDR, in one command.
#
# Why this exists
# ---------------
# `update-velaris.sh` already dumps the database and records the result, but it
# only runs during an update. Every other backup — a cron job, a systemd timer,
# an operator taking a dump before a risky change — had to be recorded by hand
# through the API, which in practice means it was not recorded at all. The
# consequence is specific and bad: `GET /dr/posture` reports `no_backup` while
# backups are in fact being taken, so the one screen that exists to say whether
# you can recover is wrong in the direction that invites people to ignore it.
#
# The fix is not another reminder. It is making the recording part of taking the
# backup, so forgetting is not an option that exists.
#
# Usage
# -----
#   ./backup-velaris.sh                       # dump + record
#   ./backup-velaris.sh --dir /mnt/backups    # somewhere other than .backups/
#   ./backup-velaris.sh --keep 14             # prune dumps older than N days
#   ./backup-velaris.sh --quiet               # cron-friendly: errors only
#
# Cron / systemd timer:
#   0 2 * * *  /opt/velaris/backup-velaris.sh --quiet --keep 30
#
# Exit status
# -----------
# Non-zero ONLY when the backup itself failed. A failure to record is reported
# and does not change the exit code — a successful backup must never be
# reported as a failed one because a bookkeeping row did not land. That is the
# same trade `update-velaris.sh` makes, for the same reason.
#
# Copyright (c) 2024-2026 Velaris Contributors
# SPDX-License-Identifier: BSL-1.1
set -euo pipefail

VELARIS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BACKUP_DIR="$VELARIS_DIR/.backups"
KEEP_DAYS=""
QUIET=false

while [[ $# -gt 0 ]]; do
  case "$1" in
    --dir)   BACKUP_DIR="$2"; shift 2 ;;
    --keep)  KEEP_DAYS="$2";  shift 2 ;;
    --quiet) QUIET=true;      shift ;;
    -h|--help) sed -n '2,32p' "${BASH_SOURCE[0]}"; exit 0 ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
done

say()  { [[ "$QUIET" == true ]] || echo "$@"; }
warn() { echo "$@" >&2; }

# Config comes from the same .env the service reads, so a backup can never be
# taken against a different database than the one running.
if [[ -f "$VELARIS_DIR/.env" ]]; then
  set -a; . "$VELARIS_DIR/.env"; set +a
fi

DB_CONTAINER="${DB_CONTAINER:-docker-compose-velaris-db-1}"
DB_USER="${POSTGRES_USER:-velaris}"
DB_NAME="${POSTGRES_DB:-velaris}"

mkdir -p "$BACKUP_DIR"
STAMP="$(date +%Y%m%d-%H%M%S)"
BACKUP_FILE="$BACKUP_DIR/velaris-${STAMP}.sql"

# ── Take the dump ─────────────────────────────────────────────────
say "▶ Backing up $DB_NAME ..."
if ! docker exec "$DB_CONTAINER" pg_dump -U "$DB_USER" "$DB_NAME" > "$BACKUP_FILE" 2>/tmp/velaris-backup-err.log; then
  warn "✗ Backup FAILED — see /tmp/velaris-backup-err.log"
  # A truncated dump is more dangerous than none: it looks like a backup.
  rm -f "$BACKUP_FILE"
  # Recorded as failed, so the posture shows the gap rather than an old
  # success. Best-effort; a failed backup is already the headline.
  if [[ -x "$VELARIS_DIR/.venv/bin/python" ]]; then
    (cd "$VELARIS_DIR" && PYTHONPATH="$VELARIS_DIR/services/case-service" \
      "$VELARIS_DIR/.venv/bin/python" -m case_service.compliance.dr_cli \
        --location "$BACKUP_FILE" --scope database --status failed \
        --recorded-by "backup-velaris.sh") >/dev/null 2>&1 || true
  fi
  exit 1
fi
say "  ✓ $BACKUP_FILE ($(du -h "$BACKUP_FILE" | cut -f1))"

# ── Record it ─────────────────────────────────────────────────────
# `set -e` is suspended: bookkeeping must not turn a good backup into a failure.
if [[ -x "$VELARIS_DIR/.venv/bin/python" ]]; then
  set +e
  (cd "$VELARIS_DIR" && PYTHONPATH="$VELARIS_DIR/services/case-service" \
   "$VELARIS_DIR/.venv/bin/python" -m case_service.compliance.dr_cli \
     --location "$BACKUP_FILE" \
     --scope database \
     --status succeeded \
     --recorded-by "backup-velaris.sh")
  RC=$?
  set -e
  [[ $RC -eq 0 ]] || warn "! backup recorded to disk but NOT to HxDR — /dr/posture will under-report"
else
  warn "! .venv not found — backup taken but not recorded in /dr/posture"
fi

# ── Prune ─────────────────────────────────────────────────────────
# Only ever prunes files this script's own naming produces, and only when asked.
# A retention sweep that guesses which files are backups is how unrelated data
# gets deleted from a directory somebody repurposed.
if [[ -n "$KEEP_DAYS" ]]; then
  PRUNED=$(find "$BACKUP_DIR" -maxdepth 1 -name 'velaris-*.sql' -type f \
             -mtime +"$KEEP_DAYS" -print -delete | wc -l)
  [[ "$PRUNED" -gt 0 ]] && say "  pruned $PRUNED backup(s) older than ${KEEP_DAYS}d"
fi

say "✓ Done."
