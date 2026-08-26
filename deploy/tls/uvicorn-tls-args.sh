# Shared by start-velaris.sh and the systemd exec wrapper (roadmap #18).
# Source it, then:  _velaris_tls_args case-service   → echoes the uvicorn flags.
#
# Prints nothing at all when internal TLS is off, which is the default — so both
# launch paths keep exactly the command line they had, and there is one place
# that decides rather than two that can disagree.
#
# `--ssl-cert-reqs 2` is ssl.CERT_REQUIRED: the service demands a client
# certificate and verifies it against the internal CA. Without it the service
# would encrypt the hop and accept a connection from anyone — the direction of
# authentication that actually matters here is the gateway proving itself to the
# service, not the other way round.
#
# Copyright (c) 2024-2026 Velaris Contributors
# SPDX-License-Identifier: BSL-1.1

_velaris_tls_args() {
  local service="$1"
  [ "${VELARIS_INTERNAL_TLS_ENABLED:-false}" = "true" ] || return 0

  local dir="${VELARIS_INTERNAL_TLS_DIR:-deploy/tls/certs}"
  case "$dir" in /*) ;; *) dir="${VELARIS_DIR:-$PWD}/$dir" ;; esac

  if [ ! -f "$dir/$service.crt" ] || [ ! -f "$dir/$service.key" ]; then
    # Loud, and NOT fatal: a service that refuses to start because a certificate
    # is missing takes the platform down over a setting nobody meant to enable.
    # It starts in plaintext and says so; the gateway, which speaks https when
    # the same flag is set, then fails to connect — visibly, in one place.
    echo "WARNING: VELARIS_INTERNAL_TLS_ENABLED=true but $dir/$service.crt is missing." >&2
    echo "         Starting $service WITHOUT TLS. Run deploy/tls/make-internal-ca.sh." >&2
    return 0
  fi

  printf '%s' "--ssl-keyfile $dir/$service.key --ssl-certfile $dir/$service.crt --ssl-ca-certs $dir/ca.crt --ssl-cert-reqs 2"
}

# ── Health probes ─────────────────────────────────────────────────────────────
#
# The gateway is not the only thing that opens a service port: `start-velaris.sh`
# waits on /health after each launch, and `update-velaris.sh` gates the whole
# update on the same four endpoints. Those probes speak plain http, and with
# `--ssl-cert-reqs 2` a service refuses anything that does not present the
# gateway's CLIENT certificate — at the handshake, before /health is ever
# reached. Left alone, turning mTLS on would make a perfectly healthy platform
# report every service dead, and would make update-velaris.sh call a good update
# a failed one and act on it.
#
# So the same file that decides how a service LISTENS also decides how we KNOCK.
# Prints a plain http probe when TLS is off, so both scripts keep the behaviour
# they had.
#
#   _velaris_probe 8201  → curl … http(s)://localhost:8201/health
#
# `localhost` and not 127.0.0.1 because that is the name in every service's SAN
# list; the hostname check stays ON rather than being disabled to make it work.
_velaris_probe() {
  local port="$1"
  local timeout="${VELARIS_PROBE_TIMEOUT:-3}"

  if [ "${VELARIS_INTERNAL_TLS_ENABLED:-false}" != "true" ]; then
    curl -sf --max-time "$timeout" "http://localhost:${port}/health" >/dev/null 2>&1
    return $?
  fi

  local dir="${VELARIS_INTERNAL_TLS_DIR:-deploy/tls/certs}"
  case "$dir" in /*) ;; *) dir="${VELARIS_DIR:-$PWD}/$dir" ;; esac

  # Same fallback as the launch flags: if the certificates are not there the
  # service started in plaintext and said so, so probe it the way it actually
  # came up rather than reporting it dead for a second reason.
  if [ ! -f "$dir/gateway.crt" ] || [ ! -f "$dir/ca.crt" ]; then
    curl -sf --max-time "$timeout" "http://localhost:${port}/health" >/dev/null 2>&1
    return $?
  fi

  curl -sf --max-time "$timeout" \
    --cacert "$dir/ca.crt" --cert "$dir/gateway.crt" --key "$dir/gateway.key" \
    "https://localhost:${port}/health" >/dev/null 2>&1
}
