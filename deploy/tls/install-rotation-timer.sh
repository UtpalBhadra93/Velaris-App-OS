#!/usr/bin/env bash
# Install the certificate rotation timer (roadmap #18). Run with sudo.
#
# Separate from deploy/systemd/install.sh on purpose: internal TLS is off by
# default, so installing a timer that rotates certificates nothing uses would be
# noise. Run this when you turn internal TLS on — and if you do not run it,
# rotation is manual and the certificates expire in 90 days.
#
# Copyright (c) 2024-2026 Velaris Contributors
# SPDX-License-Identifier: BSL-1.1
set -euo pipefail
[ "$(id -u)" = 0 ] || { echo "Run with sudo." >&2; exit 1; }

VELARIS_DIR="${VELARIS_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}"
VELARIS_USER="${SUDO_USER:-$(stat -c '%U' "$VELARIS_DIR")}"
VELARIS_HOME="$(getent passwd "$VELARIS_USER" | cut -d: -f6)"
SRC_DIR="$VELARIS_DIR/deploy/systemd"
UNIT_DIR=/etc/systemd/system

[ -f "$VELARIS_DIR/deploy/tls/certs/ca.crt" ] || {
  echo "No certificates in deploy/tls/certs — run deploy/tls/make-internal-ca.sh first." >&2
  exit 1
}

for unit in velaris-cert-rotate.service velaris-cert-rotate.timer; do
  sed -e "s|__VELARIS_USER__|$VELARIS_USER|g" \
      -e "s|__VELARIS_DIR__|$VELARIS_DIR|g" \
      -e "s|__VELARIS_HOME__|$VELARIS_HOME|g" \
      "$SRC_DIR/$unit" > "$UNIT_DIR/$unit"
  echo "  installed $UNIT_DIR/$unit"
done

systemctl daemon-reload
systemctl enable --now velaris-cert-rotate.timer
echo
echo "✓ Rotation timer enabled. Next run:"
systemctl list-timers velaris-cert-rotate.timer --no-pager | tail -2
echo
echo "The timer is not the safety net — it is the thing that can quietly stop."
echo "Point a monitor at the exit code of:"
echo "  $VELARIS_DIR/deploy/tls/rotate-internal-certs.sh --check"
