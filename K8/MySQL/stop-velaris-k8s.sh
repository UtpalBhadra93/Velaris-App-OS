#!/bin/bash
# ═══════════════════════════════════════════════════════════════════
#  VELARIS on Kubernetes (MySQL) — stop
#
#  By default this STOPS the platform without destroying anything:
#  every workload is scaled to zero, the database and cache keep
#  running, and all data stays where it is. --start puts the previous
#  replica counts back.
#
#  Destructive modes are opt-in and never implied:
#    --uninstall  remove the Helm release (PersistentVolumeClaims kept)
#    --purge      also delete the PVCs — THIS DELETES THE DATABASE
#
#  Usage:  ./stop-velaris-k8s.sh              # scale to zero
#          ./stop-velaris-k8s.sh --with-data  # also stop the database/valkey
#          ./stop-velaris-k8s.sh --start      # resume
#          ./stop-velaris-k8s.sh --uninstall
#          ./stop-velaris-k8s.sh --purge
#
# Copyright (c) 2024-2026 Velaris Contributors
# SPDX-License-Identifier: BSL-1.1
# ═══════════════════════════════════════════════════════════════════
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/_common.sh"

MODE="stop"; WITH_DATA=0; ASSUME_YES=0
STATE_FILE="${VELARIS_K8S_STATE:-$K8_DIR/.stopped-scale.$NAMESPACE.$RELEASE}"

while [ $# -gt 0 ]; do
  case "$1" in
    --start)     MODE="start"; shift ;;
    --uninstall) MODE="uninstall"; shift ;;
    --purge)     MODE="purge"; shift ;;
    --with-data) WITH_DATA=1; shift ;;
    -y|--yes)    ASSUME_YES=1; shift ;;
    -h|--help)   sed -n '2,26p' "$0"; exit 0 ;;
    *) die "unknown option: $1" ;;
  esac
done

require_cluster
release_exists || die "no release '$RELEASE' in namespace '$NAMESPACE'."

confirm() {  # confirm <prompt>
  [ "$ASSUME_YES" = "1" ] && return 0
  read -r -p "  $1 [type 'yes' to continue]: " a
  [ "$a" = "yes" ]
}

case "$MODE" in

  stop)
    step "Stopping VELARIS (data is kept)"
    : > "$STATE_FILE"
    for svc in "${WORKLOADS[@]}"; do
      d="$(deploy_name "$svc")"
      n="$(kubectl -n "$NAMESPACE" get deploy "$d" -o jsonpath='{.spec.replicas}' 2>/dev/null)"
      [ -z "$n" ] && continue
      echo "$d $n" >> "$STATE_FILE"
      kubectl -n "$NAMESPACE" scale deploy "$d" --replicas=0 >/dev/null 2>&1 \
        && ok "$svc: $n -> 0"
    done

    # A HorizontalPodAutoscaler does not act on a deployment that sits at
    # zero replicas, so scaling to zero is a real stop and needs no further
    # handling. On --start the HPA takes control again from the restored
    # count, which is why the recorded number is a starting point, not a cap.
    if [ -n "$(kubectl -n "$NAMESPACE" get hpa -o name 2>/dev/null)" ]; then
      ok "HPAs left untouched (they do not scale a deployment that is at zero)"
    fi

    if [ "$WITH_DATA" = "1" ]; then
      for s in "${RELEASE}-${DB_ENGINE}" "${RELEASE}-valkey-primary"; do
        n="$(kubectl -n "$NAMESPACE" get statefulset "$s" -o jsonpath='{.spec.replicas}' 2>/dev/null)"
        [ -z "$n" ] && continue
        echo "sts/$s $n" >> "$STATE_FILE"
        kubectl -n "$NAMESPACE" scale statefulset "$s" --replicas=0 >/dev/null 2>&1 && ok "$s: $n -> 0"
      done
    else
      ok "database and cache left running (--with-data stops them too)"
    fi
    echo; echo "${GREEN}Stopped.${NC} Scale recorded in $STATE_FILE — resume with --start"
    ;;

  start)
    step "Starting VELARIS"
    [ -f "$STATE_FILE" ] || die "no recorded scale at $STATE_FILE; scale the deployments by hand."
    # Data stores first: a service that starts without its database crash-loops
    # and burns its restart budget before the database is ever up.
    grep '^sts/' "$STATE_FILE" | while read -r name n; do
      kubectl -n "$NAMESPACE" scale statefulset "${name#sts/}" --replicas="$n" >/dev/null 2>&1 \
        && ok "${name#sts/}: -> $n"
    done
    grep -v '^sts/' "$STATE_FILE" | while read -r d n; do
      kubectl -n "$NAMESPACE" scale deploy "$d" --replicas="$n" >/dev/null 2>&1 && ok "$d: -> $n"
    done
    if [ -n "$(kubectl -n "$NAMESPACE" get hpa -o name 2>/dev/null)" ]; then
      warn "deployments with an HPA will settle at whatever it decides, not"
      warn "necessarily the restored count above"
    fi
    wait_ready 600 && rm -f "$STATE_FILE"
    ;;

  uninstall)
    step "Uninstalling the release"
    warn "This removes the Helm release from namespace '$NAMESPACE'."
    warn "PersistentVolumeClaims are NOT deleted, so the database survives."
    confirm "Uninstall '$RELEASE'?" || { echo "  cancelled"; exit 0; }
    helm -n "$NAMESPACE" uninstall "$RELEASE" >/dev/null && ok "release removed"
    echo "  remaining PVCs:"
    kubectl -n "$NAMESPACE" get pvc --no-headers 2>/dev/null | awk '{print "    "$1"  "$4}'
    ;;

  purge)
    step "PURGE — this deletes the database"
    err "Every PersistentVolumeClaim in namespace '$NAMESPACE' will be deleted."
    err "All case data, users and audit history go with them. This cannot be undone."
    kubectl -n "$NAMESPACE" get pvc --no-headers 2>/dev/null | awk '{print "    "$1"  "$4}'
    confirm "Delete the release AND all data?" || { echo "  cancelled"; exit 0; }
    helm -n "$NAMESPACE" uninstall "$RELEASE" >/dev/null 2>&1
    kubectl -n "$NAMESPACE" delete pvc --all >/dev/null 2>&1 && ok "PVCs deleted"
    rm -f "$STATE_FILE"
    ;;
esac
