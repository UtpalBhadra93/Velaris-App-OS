#!/usr/bin/env bash
# Block until the backing services a Velaris unit needs are actually accepting
# connections, then exit 0. Used as ExecStartPre= in the engine and case-service
# units.
#
#   velaris-wait-deps.sh <VELARIS_DIR>
#
# Why this exists (2026-08-05): the units already ordered themselves After=
# docker.service, but that only means the DAEMON is up — not that Postgres
# inside a container has finished booting and bound its port. On that day the
# containers had no restart policy at all, so the daemon came back alone and
# case-service started against a database that would never arrive: it stayed
# "active (running)" and answered every login with a 500 for 30 minutes.
#
# Ordering alone cannot express "the port is open". This can. Failing here is
# the point: a failed ExecStartPre puts the unit in failed state, and with
# Restart=always + StartLimitIntervalSec=0 systemd retries every RestartSec
# forever — so a slow or briefly-missing database self-heals into a working
# platform instead of a permanently broken one.
#
# Copyright (c) 2024-2026 Velaris Contributors
# SPDX-License-Identifier: BSL-1.1
set -uo pipefail

VELARIS_DIR="${1:?usage: velaris-wait-deps.sh <VELARIS_DIR>}"
TIMEOUT="${VELARIS_WAIT_DEPS_TIMEOUT:-90}"

# The unit loads .env as EnvironmentFile, but this script also runs by hand
# during triage, when nothing is exported. Read it directly if we must.
if [ -z "${VELARIS_CASE_DATABASE_URL:-}" ] && [ -f "$VELARIS_DIR/.env" ]; then
  VELARIS_CASE_DATABASE_URL="$(grep -E '^VELARIS_CASE_DATABASE_URL=' "$VELARIS_DIR/.env" \
    | tail -1 | cut -d= -f2- | tr -d '"'"'"'')"
fi

if [ -z "${VELARIS_CASE_DATABASE_URL:-}" ]; then
  echo "wait-deps: no VELARIS_CASE_DATABASE_URL — cannot verify the database, continuing" >&2
  exit 0
fi

# postgresql+asyncpg://user:pass@host:port/db  ->  host, port
# Strip the scheme, then everything up to the LAST '@' — passwords legitimately
# contain '@' and ':', so anything anchored on the first one parses the wrong
# host on a machine with a strong password and works fine on a weak one.
_netloc="${VELARIS_CASE_DATABASE_URL#*://}"
_netloc="${_netloc%%/*}"
_hostport="${_netloc##*@}"
DB_HOST="${_hostport%%:*}"
DB_PORT="${_hostport##*:}"

if [ "$DB_PORT" = "$DB_HOST" ]; then           # no ':port' in the URL
  case "$VELARIS_CASE_DATABASE_URL" in
    mysql*|mariadb*) DB_PORT=3306 ;;
    *)               DB_PORT=5432 ;;
  esac
fi
[ -n "$DB_HOST" ] || DB_HOST=127.0.0.1

# bash's /dev/tcp rather than nc: one less package to depend on inside a unit
# that runs with a minimal PATH and no shell profile.
_port_open() {
  timeout 2 bash -c "exec 3<>/dev/tcp/$1/$2" 2>/dev/null
}

echo "wait-deps: waiting up to ${TIMEOUT}s for database $DB_HOST:$DB_PORT"
_deadline=$(( SECONDS + TIMEOUT ))
until _port_open "$DB_HOST" "$DB_PORT"; do
  if [ "$SECONDS" -ge "$_deadline" ]; then
    echo "wait-deps: database $DB_HOST:$DB_PORT still refusing after ${TIMEOUT}s." >&2
    echo "wait-deps: check  docker ps -a --format '{{.Names}}\t{{.Status}}'  — if velaris-db is" >&2
    echo "wait-deps: Exited, bring the stack up:  docker compose -f $VELARIS_DIR/deploy/docker-compose/docker-compose.yml --env-file $VELARIS_DIR/.env up -d" >&2
    exit 1
  fi
  sleep 2
done
echo "wait-deps: database $DB_HOST:$DB_PORT is accepting connections"

# Temporal is a soft dependency: the workflow poller retries on its own and the
# API serves requests without it, so a missing Temporal must not block boot the
# way a missing database does. Wait briefly, then note it and move on.
_deadline=$(( SECONDS + 30 ))
until _port_open 127.0.0.1 7233; do
  if [ "$SECONDS" -ge "$_deadline" ]; then
    echo "wait-deps: Temporal 127.0.0.1:7233 not up after 30s — starting anyway (poller will retry)" >&2
    break
  fi
  sleep 2
done

exit 0
