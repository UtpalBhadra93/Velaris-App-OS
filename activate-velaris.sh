#!/bin/bash
# ═══════════════════════════════════════════════════════════════════
#  VELARIS Product Key Activation
#
#  Run this once you have a product key from your Velaris website.
#  A fresh install runs unactivated on purpose: the website calls this
#  platform's API to create your user, so the platform has to be up
#  BEFORE a key can be issued. This script closes that loop.
#
#  Usage:  ./activate-velaris.sh
# ═══════════════════════════════════════════════════════════════════

set -euo pipefail

HELIX_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

case ":$PATH:" in
  *":$HOME/.local/bin:"*) ;;
  *) PATH="$HOME/.local/bin:$PATH"; export PATH ;;
esac

green()  { echo -e "\033[0;32m$*\033[0m"; }
yellow() { echo -e "\033[0;33m$*\033[0m"; }
red()    { echo -e "\033[0;31m$*\033[0m"; }

if ! command -v uv >/dev/null 2>&1; then
  red "  ✗ 'uv' is not installed. Install with: curl -LsSf https://astral.sh/uv/install.sh | sh"
  exit 1
fi

cd "$HELIX_DIR"

# Read the registration server from .env when present so a self-hosted
# website is used instead of the vendor default.
REGISTER_URL="${VELARIS_REGISTER_URL:-}"
if [ -z "$REGISTER_URL" ] && [ -f "$HELIX_DIR/.env" ]; then
  REGISTER_URL=$(grep -E "^VELARIS_REGISTER_URL=" "$HELIX_DIR/.env" 2>/dev/null | cut -d= -f2- | tr -d '"' | xargs || true)
fi

# The activation call itself needs no DB, but setup_superadmin.py imports the
# driver up front — install it when absent so a standalone run cannot fail the
# way it did before (No module named 'psycopg2').
uv pip install psycopg2-binary bcrypt httpx cryptography --quiet 2>/dev/null || true

VELARIS_REGISTER_URL="${REGISTER_URL:-https://velaris-app-os.duckdns.org}" \
  uv run python "$HELIX_DIR/scripts/setup_superadmin.py" --key-only

echo ""
if uv run python "$HELIX_DIR/scripts/verify_key.py"; then
  green "  ✓ Activation verified."
else
  yellow "  ⚠ Key written but verification did not pass — see the message above."
fi
