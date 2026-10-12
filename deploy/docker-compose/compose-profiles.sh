#!/usr/bin/env bash
# Velaris — which optional parts of the compose file this install runs.
#
# SOURCE this before any `docker compose` call against docker-compose.yml:
#
#   . "$VELARIS_DIR/deploy/docker-compose/compose-profiles.sh"
#
# It exports COMPOSE_PROFILES, which every later `docker compose` in the same
# shell reads. One line per script, rather than a flag on every call — a flag
# forgotten on one call is a container that silently is not started, or is not
# stopped.
#
# The bundled MinIO (profile `minio`)
# -----------------------------------
# Until v3.1.2 every install ran a MinIO container for documents. MinIO's images
# were withdrawn from Docker Hub around 2026-09-11, so a NEW install can no
# longer pull one: from v3.1.2 a new install stores documents on local disk, and
# object storage is an S3 endpoint the operator points at
# (VELARIS_CASE_STORAGE_BACKEND=s3 + VELARIS_CASE_S3_*).
#
# An install that ALREADY runs the bundled MinIO must keep it: its documents are
# in that volume. So the profile is switched on by what the host holds, not by a
# setting somebody has to remember to add:
#
#   - the `velaris-minio` container exists, or
#   - a `…_velaris-minio-data` volume exists.
#
# The storage setting is deliberately NOT the test. `minio`/`s3` with an external
# endpoint is a legitimate new install, and starting the bundled container for
# it would fail on an image that cannot be pulled.
#
# Override, in the environment or in .env:
#   VELARIS_BUNDLED_MINIO=1   run it regardless (a restore onto a new host that
#                             already has the images)
#   VELARIS_BUNDLED_MINIO=0   never run it (after moving the documents off it —
#                             scripts/storage-migrate.py)
#
# Anything already in COMPOSE_PROFILES (e.g. `observability`) is kept.
#
# Copyright (c) 2024-2026 Velaris Contributors
# SPDX-License-Identifier: BSL-1.1

_vcp_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
_vcp_env="${VELARIS_DIR:-$(cd "$_vcp_dir/../.." && pwd)}/.env"

_vcp_setting="${VELARIS_BUNDLED_MINIO:-}"
if [ -z "$_vcp_setting" ] && [ -f "$_vcp_env" ]; then
  _vcp_setting="$(grep -E '^VELARIS_BUNDLED_MINIO=' "$_vcp_env" 2>/dev/null | tail -1 | cut -d= -f2- | tr -d "\"' " || true)"
fi

_vcp_minio=false
case "$(printf '%s' "$_vcp_setting" | tr '[:upper:]' '[:lower:]')" in
  1|true|yes|on)  _vcp_minio=true ;;
  0|false|no|off) _vcp_minio=false ;;
  *)
    # Unset, empty or unrecognised: decide from what this host already holds.
    if docker ps -a --format '{{.Names}}' 2>/dev/null | grep -qx 'velaris-minio' \
       || docker volume ls --format '{{.Name}}' 2>/dev/null | grep -qE '(^|_)velaris-minio-data$'; then
      _vcp_minio=true
    fi ;;
esac

# The shell's COMPOSE_PROFILES wins over the one in --env-file, so exporting ours
# would silently drop a profile an operator set in .env. Start from theirs.
if [ -z "${COMPOSE_PROFILES:-}" ] && [ -f "$_vcp_env" ]; then
  COMPOSE_PROFILES="$(grep -E '^COMPOSE_PROFILES=' "$_vcp_env" 2>/dev/null | tail -1 | cut -d= -f2- | tr -d "\"' " || true)"
fi
if [ "$_vcp_minio" = true ]; then
  case ",${COMPOSE_PROFILES:-}," in
    *,minio,*) ;;
    *) COMPOSE_PROFILES="${COMPOSE_PROFILES:+$COMPOSE_PROFILES,}minio" ;;
  esac
fi
# Exported only when there is something to say: an empty export is not "unset".
if [ -n "${COMPOSE_PROFILES:-}" ]; then export COMPOSE_PROFILES; else unset COMPOSE_PROFILES; fi
VELARIS_BUNDLED_MINIO_ACTIVE="$_vcp_minio"
unset _vcp_dir _vcp_env _vcp_setting _vcp_minio
