#!/bin/bash
# ═══════════════════════════════════════════════════════════════════
#  VELARIS on Kubernetes (MariaDB) — convert an existing database to utf8mb4
#
#  Only for installs created BEFORE the setup script began forcing
#  utf8mb4 as the server default. A utf8mb3 database rejects every
#  4-byte character (emoji, extended CJK) with VEL-DB-UNAVAILABLE-503
#  instead of a validation error.
#
#  DRY RUN BY DEFAULT. Nothing is altered until --apply is given.
#
#  This rewrites every affected table (ALTER TABLE ... CONVERT TO
#  CHARACTER SET), which on MySQL rebuilds the table: it is slow on
#  large data and holds locks. Take a backup and run it in a window.
#
#  Usage:  ./convert-utf8mb4.sh            # report what would change
#          ./convert-utf8mb4.sh --apply    # convert
#
#  Copyright (c) 2024-2026 Velaris Contributors
#  SPDX-License-Identifier: BSL-1.1
# ═══════════════════════════════════════════════════════════════════
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/_common.sh"

APPLY=0
while [ $# -gt 0 ]; do
  case "$1" in
    --apply) APPLY=1; shift ;;
    -h|--help) sed -n '2,20p' "$0"; exit 0 ;;
    *) die "unknown option: $1" ;;
  esac
done

require_cluster
db_bundled || die "this script supports the bundled $DB_ENGINE only."

step "Current character sets"
CURRENT_DB_CS="$(sql_q "SELECT @@character_set_database" | tr -d '[:space:]')"
ok "database charset: ${CURRENT_DB_CS:-unknown}"

TABLES="$(sql_q "SELECT DISTINCT t.table_name
                 FROM information_schema.tables t
                 JOIN information_schema.columns c
                   ON c.table_schema = t.table_schema AND c.table_name = t.table_name
                 WHERE t.table_schema = DATABASE()
                   AND t.table_type = 'BASE TABLE'
                   AND c.character_set_name IS NOT NULL
                   AND c.character_set_name <> 'utf8mb4'
                 ORDER BY t.table_name")"
COUNT="$(printf '%s\n' "$TABLES" | grep -c . || true)"

if [ "${COUNT:-0}" = "0" ] && [ "$CURRENT_DB_CS" = "utf8mb4" ]; then
  ok "already utf8mb4 — nothing to do"
  exit 0
fi
warn "$COUNT table(s) hold non-utf8mb4 text columns"

# An index over a VARCHAR that fits in utf8mb3 can exceed the key limit at
# 4 bytes per character. Name them before touching anything: these are the
# rows that make a conversion fail half way.
step "Index length risks"
RISKY="$(sql_q "SELECT CONCAT(s.table_name, '.', s.index_name, ' (', c.column_name,
                              ' varchar(', c.character_maximum_length, '))')
                FROM information_schema.statistics s
                JOIN information_schema.columns c
                  ON c.table_schema = s.table_schema
                 AND c.table_name  = s.table_name
                 AND c.column_name = s.column_name
                WHERE s.table_schema = DATABASE()
                  AND c.data_type IN ('varchar','char')
                  AND c.character_maximum_length > 191
                  AND c.character_set_name <> 'utf8mb4'
                ORDER BY 1")"
if [ -n "$RISKY" ]; then
  printf '%s\n' "$RISKY" | while read -r r; do [ -n "$r" ] && warn "$r"; done
  warn "these indexes may exceed the key length at 4 bytes/char"
else
  ok "no index exceeds the safe length"
fi

if [ "$APPLY" = "0" ]; then
  echo
  echo "${BOLD}Dry run — nothing was changed.${NC}"
  echo "  Tables that would be converted:"
  printf '%s\n' "$TABLES" | while read -r t; do [ -n "$t" ] && echo "    $t"; done
  echo
  echo "  Re-run with --apply to convert. Take a backup first:"
  echo "    ./backup-velaris.sh"
  exit 0
fi

step "Converting (this rebuilds each table)"
warn "the database should not be serving traffic while this runs"
sql_q "ALTER DATABASE \`$(db_name)\` CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci" >/dev/null \
  && ok "database default set to utf8mb4"

FAILED=0; DONE=0
printf '%s\n' "$TABLES" | while read -r t; do
  [ -z "$t" ] && continue
  echo -n "    convert $t ... "
  if sql_q "ALTER TABLE \`$t\` CONVERT TO CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci" >/dev/null 2>&1; then
    echo "${GREEN}✓${NC}"; DONE=$((DONE+1))
  else
    echo "${RED}✗${NC}"; FAILED=$((FAILED+1))
    err "  $t could not be converted — most likely an index over the key limit."
    err "  Shorten the column or index prefix, then re-run; converted tables are skipped."
  fi
done

step "Result"
LEFT="$(sql_q "SELECT count(DISTINCT table_name) FROM information_schema.columns
               WHERE table_schema = DATABASE() AND character_set_name IS NOT NULL
                 AND character_set_name <> 'utf8mb4'" | tr -d '[:space:]')"
if [ "${LEFT:-0}" = "0" ]; then
  ok "every table is utf8mb4"
else
  err "$LEFT table(s) still not utf8mb4 — see the failures above"
  exit 1
fi
