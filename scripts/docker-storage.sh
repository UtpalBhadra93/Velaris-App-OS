#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════════
#  Velaris — Docker storage location + build-cache policy
#
#  Sourced by setup-velaris.sh, update-velaris.sh, uninstall-velaris.sh
#  and website/setup-website.sh. Defines vds_* helpers; runs nothing on
#  its own so it is safe to source anywhere.
#
#  WHY THIS EXISTS
#  ---------------
#  Two unbounded growers ship with Docker's defaults:
#
#    1. BuildKit keeps every superseded layer from every `compose build`
#       forever. Nothing expires it. Observed in the wild: 44 GB.
#    2. Docker's data-root defaults to /var/lib/docker — on the ROOT
#       filesystem. On a box with a separate, larger /home that fills the
#       small partition first, and a full root filesystem wedges the OS
#       (journald, apt and logs all lose their write target), not just
#       Velaris.
#
#  So: let the operator put data-root on the roomier filesystem at install
#  time, and cap the build cache so it can never repeat.
#
#  Build cache is disposable by definition — evicting it can never destroy
#  a container, image or volume, only make the next build slower.
# ═══════════════════════════════════════════════════════════════════

VDS_DAEMON_JSON="/etc/docker/daemon.json"

# ── tiny output helpers (only if the caller has not defined them) ──
type ok   >/dev/null 2>&1 || ok()   { echo -e "\033[0;32m  ✓ $*\033[0m"; }
type warn >/dev/null 2>&1 || warn() { echo -e "\033[0;33m  ⚠ $*\033[0m"; }
type red  >/dev/null 2>&1 || red()  { echo -e "\033[0;31m$*\033[0m"; }

# ── vds_fs_of <path> ──────────────────────────────────────────────
# Backing device of the filesystem holding <path> (nearest existing parent).
vds_fs_of() {
  local p="$1"
  while [ ! -e "$p" ] && [ "$p" != "/" ]; do p="$(dirname "$p")"; done
  df -P "$p" 2>/dev/null | awk 'NR==2{print $1}'
}

# ── vds_same_fs <a> <b> ───────────────────────────────────────────
# True when both paths live on the SAME filesystem — in which case moving
# data-root between them frees nothing and is pure risk.
vds_same_fs() { [ "$(vds_fs_of "$1")" = "$(vds_fs_of "$2")" ]; }

# Sizes resolve to the nearest EXISTING parent. A target that does not exist
# yet (the whole point at install time) would otherwise make df fail and the
# caps fall back to a wrong default.
vds_total_gb() {
  local p="$1"; while [ ! -e "$p" ] && [ "$p" != "/" ]; do p="$(dirname "$p")"; done
  df -PBG "$p" 2>/dev/null | awk 'NR==2{gsub("G","",$2); print $2+0}'
}
vds_free_gb() {
  local p="$1"; while [ ! -e "$p" ] && [ "$p" != "/" ]; do p="$(dirname "$p")"; done
  df -PBG "$p" 2>/dev/null | awk 'NR==2{gsub("G","",$4); print $4+0}'
}

# ── vds_clamp <value> <min> <max> ─────────────────────────────────
vds_clamp() {
  local v="$1" lo="$2" hi="$3"
  [ "$v" -lt "$lo" ] 2>/dev/null && v="$lo"
  [ "$v" -gt "$hi" ] 2>/dev/null && v="$hi"
  echo "$v"
}

# ── vds_current_root ──────────────────────────────────────────────
vds_current_root() {
  docker info --format '{{.DockerRootDir}}' 2>/dev/null || echo "/var/lib/docker"
}

# ── vds_caps_for <path> ───────────────────────────────────────────
# Size the build-cache caps off the filesystem that will hold them, and
# echo "reserved max minfree" in GB. Percentages, not fixed numbers: a
# 40 GB VPS and a 2 TB server should not get the same ceiling.
vds_caps_for() {
  local target="$1" total reserved maxused minfree
  total="$(vds_total_gb "$target")"
  [ -n "$total" ] && [ "$total" -gt 0 ] 2>/dev/null || total=100

  reserved=$(vds_clamp $(( total * 10 / 100 ))  5 25)
  maxused=$( vds_clamp $(( total * 20 / 100 )) 10 50)
  minfree=$( vds_clamp $(( total * 15 / 100 )) 10 40)

  # maxUsedSpace must exceed reservedSpace or GC thrashes.
  [ "$maxused" -le "$reserved" ] && maxused=$(( reserved * 2 ))
  echo "$reserved $maxused $minfree"
}

# ── vds_write_daemon_json <data_root_or_empty> <caps_target_path> ──
# Writes /etc/docker/daemon.json. MERGES into any existing file (never
# clobbers a config the operator or another product put there) and keeps a
# timestamped backup. Empty data_root = leave Docker's default in place.
# Returns 0 if the file changed, 1 if it was already correct.
vds_write_daemon_json() {
  local data_root="$1" caps_target="${2:-/}" caps reserved maxused minfree tmp
  caps="$(vds_caps_for "$caps_target")"
  reserved="${caps%% *}"; maxused="$(echo "$caps" | awk '{print $2}')"; minfree="${caps##* }"

  tmp="$(mktemp)"
  DATA_ROOT="$data_root" RESERVED="${reserved}GB" MAXUSED="${maxused}GB" MINFREE="${minfree}GB" \
  EXISTING="$VDS_DAEMON_JSON" python3 - "$tmp" <<'PY'
import json, os, sys

out_path  = sys.argv[1]
existing  = os.environ["EXISTING"]
data_root = os.environ["DATA_ROOT"]

cfg = {}
if os.path.exists(existing):
    try:
        with open(existing) as fh:
            cfg = json.load(fh) or {}
    except Exception:
        # An unparseable daemon.json must not be silently discarded.
        # Signal via exit code only; the caller prints the operator message.
        sys.exit(3)

if data_root:
    cfg["data-root"] = data_root

# Merge into builder.gc so unrelated builder settings survive.
builder = cfg.setdefault("builder", {})
gc = builder.setdefault("gc", {})
gc["enabled"] = True
gc["reservedSpace"] = os.environ["RESERVED"]
gc["maxUsedSpace"]  = os.environ["MAXUSED"]
gc["minFreeSpace"]  = os.environ["MINFREE"]

# NOTE: defaultKeepStorage is deprecated (Docker 28+). If an older install
# left it behind, drop it so it cannot fight the new keys.
gc.pop("defaultKeepStorage", None)

with open(out_path, "w") as fh:
    json.dump(cfg, fh, indent=2)
    fh.write("\n")
PY
  local rc=$?
  if [ "$rc" -eq 3 ]; then
    rm -f "$tmp"
    warn "$VDS_DAEMON_JSON exists but is not valid JSON — leaving it untouched."
    warn "Fix it by hand, then re-run. Build cache stays UNCAPPED until then."
    return 1
  elif [ "$rc" -ne 0 ]; then
    rm -f "$tmp"; warn "Could not compose daemon.json (python3 missing?)"; return 1
  fi

  if [ -f "$VDS_DAEMON_JSON" ] && cmp -s "$tmp" "$VDS_DAEMON_JSON"; then
    rm -f "$tmp"; return 1                      # already correct — no restart needed
  fi

  sudo mkdir -p /etc/docker
  [ -f "$VDS_DAEMON_JSON" ] && \
    sudo cp -a "$VDS_DAEMON_JSON" "$VDS_DAEMON_JSON.bak.$(date +%Y%m%d%H%M%S)"
  # Must be checked: without a usable sudo (no tty, no rights) install fails and
  # reporting success here would leave the cache uncapped while claiming it was
  # capped — and would trigger a pointless Docker restart on top.
  if ! sudo install -m 0644 "$tmp" "$VDS_DAEMON_JSON"; then
    rm -f "$tmp"
    warn "Could not write $VDS_DAEMON_JSON (needs sudo) — build cache stays UNCAPPED."
    return 1
  fi
  rm -f "$tmp"
  ok "Wrote $VDS_DAEMON_JSON (build cache capped at ${maxused}GB)"
  return 0
}

# ── vds_migrate_root <new_root> ───────────────────────────────────
# Move an EXISTING data-root. Stops Docker, copies, restarts, verifies.
# The old tree is left in place — the caller decides when to reclaim it.
vds_migrate_root() {
  local new_root="$1" old_root need_kb free_kb
  old_root="$(vds_current_root)"

  [ "$old_root" = "$new_root" ] && return 0

  if [ -d "$new_root/image" ]; then
    warn "$new_root already holds a Docker tree — not overwriting it."
    return 1
  fi
  command -v rsync >/dev/null 2>&1 || { warn "rsync missing — skipping data-root move."; return 1; }

  need_kb="$(sudo du -sk "$old_root" 2>/dev/null | awk '{print $1}')"; need_kb="${need_kb:-0}"
  free_kb="$(df -Pk "$(dirname "$new_root")" 2>/dev/null | awk 'NR==2{print $4}')"; free_kb="${free_kb:-0}"
  if [ "$free_kb" -lt $(( need_kb * 12 / 10 )) ]; then
    warn "Not enough space at $new_root for $(( need_kb / 1024 )) MB — keeping $old_root."
    return 1
  fi

  echo "  Moving Docker data: $old_root → $new_root  ($(( need_kb / 1024 )) MB)"
  sudo systemctl stop docker.socket 2>/dev/null || true
  sudo systemctl stop docker 2>/dev/null || true
  sleep 2
  if pgrep -x dockerd >/dev/null 2>&1; then
    warn "dockerd still running — aborting the move (a live copy corrupts the tree)."
    sudo systemctl start docker 2>/dev/null || true
    return 1
  fi

  sudo mkdir -p "$new_root"; sudo chmod 710 "$new_root"
  # -aHAX is REQUIRED: overlayfs relies on hardlinks and xattrs. Plain -a
  # silently produces a tree whose images will not run.
  sudo rsync -aHAX "$old_root/" "$new_root/" || {
    warn "rsync failed — leaving Docker on $old_root."
    sudo systemctl start docker 2>/dev/null || true
    return 1
  }
  sudo systemctl start docker 2>/dev/null || true
  sleep 3

  local now; now="$(vds_current_root)"
  if [ "$now" != "$new_root" ]; then
    warn "data-root is still $now — config not applied. Old data intact at $old_root."
    return 1
  fi
  ok "Docker data-root is now $new_root"
  echo "  Old copy left at $old_root — reclaim it once Velaris is verified:"
  echo "      sudo rm -rf $old_root"
  return 0
}

# ── vds_containerd_root ───────────────────────────────────────────
# Docker 29 keeps IMAGE LAYERS in containerd's own root, which `data-root`
# does NOT govern. Moving data-root alone leaves images on the original disk.
# Reads the CONFIG FILE first (world-readable) rather than `containerd config
# dump`, which needs root. Falling back to the compiled-in default whenever
# sudo is unavailable reports "/var/lib/containerd" for a host that was
# already moved — which makes a SUCCESSFUL migration look like a failure.
vds_containerd_root() {
  local conf="/etc/containerd/config.toml" val=""
  if [ -r "$conf" ]; then
    # Top-level `root` only: stop at the first [section] so a key belonging to
    # a plugin cannot be mistaken for it.
    val="$(awk '/^[[:space:]]*\[/{exit}
                /^[[:space:]]*root[[:space:]]*=/{
                  sub(/^[^=]*=[[:space:]]*/,""); gsub(/["'\'']/,"");
                  sub(/[[:space:]]*(#.*)?$/,""); print; exit}' "$conf")"
  fi
  if [ -z "$val" ]; then
    val="$(sudo -n containerd config dump 2>/dev/null \
            | awk -F"'" '/^root = /{print $2; exit}')"
  fi
  echo "${val:-/var/lib/containerd}"
}

# ── vds_migrate_containerd <new_root> ─────────────────────────────
# Move containerd's root (image layers) and point its config at the new path.
vds_migrate_containerd() {
  local new_root="$1" old_root conf="/etc/containerd/config.toml" need_kb free_kb tmp
  old_root="$(vds_containerd_root)"
  [ "$old_root" = "$new_root" ] && return 0

  systemctl list-unit-files containerd.service >/dev/null 2>&1 || {
    warn "No containerd service — skipping image-layer move."; return 1; }
  command -v rsync >/dev/null 2>&1 || { warn "rsync missing — skipping image-layer move."; return 1; }

  need_kb="$(sudo du -sk "$old_root" 2>/dev/null | awk '{print $1}')"; need_kb="${need_kb:-0}"
  free_kb="$(df -Pk "$(dirname "$new_root")" 2>/dev/null | awk 'NR==2{print $4}')"; free_kb="${free_kb:-0}"
  if [ "$free_kb" -lt $(( need_kb * 12 / 10 )) ]; then
    warn "Not enough space for containerd ($(( need_kb / 1024 )) MB) — leaving $old_root."
    return 1
  fi

  echo "  Moving image layers: $old_root → $new_root  ($(( need_kb / 1024 )) MB)"
  # Docker talks to containerd, so it has to go down first and come up last.
  sudo systemctl stop docker.socket 2>/dev/null || true
  sudo systemctl stop docker 2>/dev/null || true
  sudo systemctl stop containerd 2>/dev/null || true
  sleep 2
  if pgrep -x containerd >/dev/null 2>&1; then
    warn "containerd still running — aborting the move."
    sudo systemctl start containerd 2>/dev/null || true
    sudo systemctl start docker 2>/dev/null || true
    return 1
  fi

  sudo mkdir -p "$new_root"; sudo chmod 711 "$new_root"
  sudo rsync -aHAX "$old_root/" "$new_root/" || {
    warn "rsync failed — leaving containerd on $old_root."
    sudo systemctl start containerd 2>/dev/null || true
    sudo systemctl start docker 2>/dev/null || true
    return 1
  }

  # Set the top-level `root` key. The shipped config has it commented out; a
  # key placed after a [section] header would belong to that section, so the
  # edit stays inside the preamble.
  tmp="$(mktemp)"
  NEW_ROOT="$new_root" python3 - "$conf" "$tmp" <<'PY'
import os, re, sys
src, out = sys.argv[1], sys.argv[2]
new_root = os.environ["NEW_ROOT"]
lines = open(src).read().splitlines(True) if os.path.exists(src) else []

end = len(lines)                       # preamble ends at the first [section]
for i, ln in enumerate(lines):
    if re.match(r'^\s*\[', ln):
        end = i
        break

line = 'root = "%s"\n' % new_root
for i in range(end):                   # replace an active root =
    if re.match(r'^\s*root\s*=', lines[i]):
        lines[i] = line
        break
else:
    for i in range(end):               # else place it at the commented one
        if re.match(r'^\s*#\s*root\s*=', lines[i]):
            lines.insert(i + 1, line)
            break
    else:
        lines.insert(end, line)        # else at the end of the preamble

open(out, "w").write("".join(lines))
PY
  if [ $? -ne 0 ]; then
    rm -f "$tmp"; warn "Could not edit $conf — reverting to $old_root."
    sudo systemctl start containerd 2>/dev/null || true
    sudo systemctl start docker 2>/dev/null || true
    return 1
  fi
  sudo mkdir -p /etc/containerd
  [ -f "$conf" ] && sudo cp -a "$conf" "$conf.bak.$(date +%Y%m%d%H%M%S)"
  sudo install -m 0644 "$tmp" "$conf"; rm -f "$tmp"

  sudo systemctl start containerd 2>/dev/null || true
  sleep 2
  sudo systemctl start docker 2>/dev/null || true
  sleep 3

  local now; now="$(vds_containerd_root)"
  if [ "$now" != "$new_root" ]; then
    warn "containerd root is still $now — config not applied. Old data intact at $old_root."
    return 1
  fi
  ok "Image layers now at $new_root"
  echo "  Old copy left at $old_root — reclaim it once Velaris is verified:"
  echo "      sudo rm -rf $old_root"
  return 0
}

# ── vds_restart_docker ────────────────────────────────────────────
vds_restart_docker() {
  sudo systemctl restart docker 2>/dev/null || return 1
  sleep 3
  docker info >/dev/null 2>&1
}

# ── vds_prune_cache [keep] ────────────────────────────────────────
# Trim build cache. Safe unconditionally: never touches containers,
# images or volumes. Default keeps 5 GB of warm cache for fast rebuilds.
vds_prune_cache() {
  local keep="${1:-5GB}" before after
  docker info >/dev/null 2>&1 || return 0
  before="$(docker system df --format '{{.BuildCache}}' 2>/dev/null | head -1)"
  docker builder prune -af --keep-storage "$keep" >/dev/null 2>&1 \
    || docker builder prune -f >/dev/null 2>&1 || true
  after="$(docker system df --format '{{.BuildCache}}' 2>/dev/null | head -1)"
  ok "Build cache trimmed (kept ≤ $keep)"
}

# ── vds_apply <data_root_or_empty> ────────────────────────────────
# Write the config and make it live, migrating first when data-root moves.
# ── vds_can_migrate <new_root> ────────────────────────────────────
# Everything the move needs, checked BEFORE anything is written.
vds_can_migrate() {
  local new_root="$1" old_root need_kb free_kb
  old_root="$(vds_current_root)"
  [ "$old_root" = "$new_root" ] && return 0

  command -v rsync >/dev/null 2>&1 || { warn "rsync is not installed."; return 1; }
  [ -d "$new_root/image" ] && { warn "$new_root already holds a Docker tree."; return 1; }

  need_kb="$(sudo du -sk "$old_root" 2>/dev/null | awk '{print $1}')"
  free_kb="$(df -Pk "$(dirname "$new_root")" 2>/dev/null | awk 'NR==2{print $4}')"; free_kb="${free_kb:-0}"

  # An unmeasurable source must NOT read as "needs 0 bytes" — that turns the
  # space check into a no-op that approves any destination.
  if [ -z "$need_kb" ] || [ "$need_kb" -le 0 ] 2>/dev/null; then
    warn "Could not measure $old_root — requiring 10 GB free as a floor."
    [ "$free_kb" -ge 10485760 ] \
      || { warn "Less than 10 GB free at $new_root."; return 1; }
    return 0
  fi

  [ "$free_kb" -ge $(( need_kb * 12 / 10 )) ] \
    || { warn "Not enough space at $new_root for $(( need_kb / 1024 )) MB."; return 1; }
  return 0
}

vds_apply() {
  local data_root="$1" caps_target changed=0

  # Pre-flight BEFORE writing anything. A daemon.json naming a data-root we
  # then failed to populate is the worst outcome available here: Docker keeps
  # running on the old path, setup fills it, and the NEXT restart silently
  # switches to an empty directory — indistinguishable from total data loss.
  # Falling back to cap-only is always safe.
  if [ -n "$data_root" ] && [ "$(vds_current_root)" != "$data_root" ]; then
    if ! vds_can_migrate "$data_root"; then
      warn "Keeping Docker's current storage location ($(vds_current_root))."
      data_root=""
    fi
  fi
  # Size the cap off the filesystem that will actually HOLD the cache: the new
  # root when moving, otherwise wherever Docker already keeps it (which is not
  # necessarily /var/lib/docker on a box configured by an earlier run).
  caps_target="${data_root:-$(vds_current_root)}"

  vds_write_daemon_json "$data_root" "$caps_target" && changed=1

  if [ -n "$data_root" ] && [ "$(vds_current_root)" != "$data_root" ]; then
    vds_migrate_root "$data_root" || return 1     # migrate restarts Docker itself
  elif [ "$changed" = "1" ]; then
    vds_restart_docker || warn "Docker restart failed — caps apply after the next restart."
  fi

  # Image layers live under containerd's root, which data-root does not cover.
  # Without this the operator picks /home and images stay on /, which is not
  # what the choice appeared to offer. Sibling of data-root, so both land on
  # the chosen disk under one predictable parent.
  if [ -n "$data_root" ]; then
    vds_migrate_containerd "$(dirname "$data_root")/containerd" \
      || warn "Image layers stay at $(vds_containerd_root)."
  fi
  return 0
}
