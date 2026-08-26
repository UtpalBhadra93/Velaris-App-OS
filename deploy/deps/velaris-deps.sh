# The system binaries the platform needs, named once.
#
# `setup-velaris.sh` has always installed these. `update-velaris.sh` never
# checked them, and that is the gap this closes: a release that starts using a
# new system tool installs fine on a NEW machine and breaks on every existing
# one, at runtime, in whatever component happened to call it first. The update
# pulls code, syncs Python and Node dependencies and pulls container images —
# none of which can install an apt package.
#
# The updater does not install them either, deliberately. apt needs root, the
# updater runs unattended on a timer, and a package manager running unwatched
# on someone else's server is not a decision an update gets to make. It halts
# and prints the exact command instead, before anything has been changed.
#
# Keep this list in step with what the code actually calls. A binary that is
# only used by an optional feature does not belong here — a hard requirement
# that stops an update must be one the platform genuinely cannot run without.
#
# Copyright (c) 2024-2026 Velaris Contributors
# SPDX-License-Identifier: BSL-1.1

# command:apt-package:human label
VELARIS_REQUIRED_COMMANDS="\
curl:curl:curl
git:git:git
nc:netcat-openbsd:netcat (nc)
fuser:psmisc:fuser (psmisc)
node:nodejs:Node.js
npm:npm:npm
pg_isready:postgresql-client:postgresql-client
rsync:rsync:rsync
openssl:openssl:openssl
docker:docker.io:Docker"

# _velaris_deps_missing → prints "pkg pkg…" for whatever is absent, empty if all
# present. Only the apt package names, so the caller can build one install line
# rather than ten.
_velaris_deps_missing() {
  local line cmd pkg missing=""
  while IFS= read -r line; do
    [ -n "$line" ] || continue
    cmd="${line%%:*}"
    pkg="${line#*:}"; pkg="${pkg%%:*}"
    command -v "$cmd" >/dev/null 2>&1 || missing="$missing $pkg"
  done <<VELARIS_DEPS_EOF
$VELARIS_REQUIRED_COMMANDS
VELARIS_DEPS_EOF
  echo "${missing# }"
}

# _velaris_deps_report → human list of what is missing, one line each.
_velaris_deps_report() {
  local line cmd pkg label
  while IFS= read -r line; do
    [ -n "$line" ] || continue
    cmd="${line%%:*}"
    pkg="${line#*:}"; label="${pkg#*:}"; pkg="${pkg%%:*}"
    command -v "$cmd" >/dev/null 2>&1 \
      || printf '  %-22s missing — apt package: %s\n' "$label" "$pkg" >&2
  done <<VELARIS_DEPS_EOF
$VELARIS_REQUIRED_COMMANDS
VELARIS_DEPS_EOF
}
