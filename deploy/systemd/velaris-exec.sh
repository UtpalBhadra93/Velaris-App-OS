#!/usr/bin/env bash
# Derive the environment start-velaris.sh computes, then exec the real command.
#
#   velaris-exec.sh <VELARIS_DIR> <command...>
#
# The units load .env via EnvironmentFile=, but two variables are NOT in .env —
# start-velaris.sh derives them at runtime, and a service that skipped them
# would behave differently under systemd than under the script:
#
#   DATABASE_BACKEND       read from velaris.yaml. velaris_shared/config.py reads
#                          it (AliasChoices DATABASE_BACKEND) and DEFAULTS TO
#                          postgresql when absent — so on a MySQL/MariaDB
#                          install, omitting it would silently dispatch the
#                          Postgres paths (e.g. the case-number sequence uses
#                          nextval, which no-ops against MySQL).
#   VELARIS_SERVICE_REGISTRY the engine's HTTP task resolver reads it; absent
#                          means the built-in demo endpoints.
#
# Deriving them here rather than baking them in at install time means editing
# velaris.yaml and restarting the unit is enough — no reinstall, no drift.
#
# Copyright (c) 2024-2026 Velaris Contributors
# SPDX-License-Identifier: BSL-1.1
set -euo pipefail

VELARIS_DIR="${1:?usage: velaris-exec.sh <VELARIS_DIR> <command...>}"
shift

# Same parsing as start-velaris.sh::_yaml_val, same fallback.
_yaml_val() {
  grep -E "^\s+${1}:" "$VELARIS_DIR/velaris.yaml" 2>/dev/null \
    | head -1 | sed -E "s/.*${1}:[[:space:]]*[\"']?([^\"'#]+)[\"']?.*/\1/" | xargs || echo "${2:-}"
}

DATABASE_BACKEND="$(_yaml_val "database" "postgresql")"
case "$DATABASE_BACKEND" in
  postgresql|mysql|mariadb) ;;
  *)
    echo "STARTUP BLOCKED: unsupported database backend '$DATABASE_BACKEND' in velaris.yaml" >&2
    exit 1
    ;;
esac
export DATABASE_BACKEND

export VELARIS_SERVICE_REGISTRY="${VELARIS_SERVICE_REGISTRY:-{\"order-service\":\"http://localhost:3001\",\"notification-service\":\"http://localhost:3002\"}}"

# Internal TLS (#18) — appended here rather than baked into each unit's
# ExecStart, so turning it on is one .env line and not five unit edits.
# The service name comes from the uvicorn module argument, which is the one
# thing every unit's command line already carries.
_svc=""
for _a in "$@"; do
  case "$_a" in
    case_service.main:app)      _svc="case-service" ;;
    analytics_service.main:app) _svc="analytics-service" ;;
    hxgraph_service.main:app)   _svc="hxgraph-service" ;;
    hxmigrate_service.main:app) _svc="hxmigrate-service" ;;
  esac
done
if [ -n "$_svc" ] && [ -f "$VELARIS_DIR/deploy/tls/uvicorn-tls-args.sh" ]; then
  # shellcheck source=/dev/null
  . "$VELARIS_DIR/deploy/tls/uvicorn-tls-args.sh"
  _tls_args="$(VELARIS_DIR="$VELARIS_DIR" _velaris_tls_args "$_svc")"
  if [ -n "$_tls_args" ]; then
    echo "Internal TLS enabled for $_svc" >&2
    # shellcheck disable=SC2086 - deliberate word splitting; the values are ours
    exec "$@" $_tls_args
  fi
fi

exec "$@"
