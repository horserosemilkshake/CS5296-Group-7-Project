#!/usr/bin/env bash
# Stop the realtime-asr Kubernetes application.
#
# What this does:
#   1. Deletes the kustomize-managed resources (gateway, nlp, ollama, monitoring, HPA, …).
#   2. Removes the optional HTTPS ingress if it was applied by setup_https.sh.
#   3. Optionally deletes the entire namespace (and every remaining object in it).
#
# Usage:
#   bash scripts/stop_all.sh              # stop workloads, keep namespace & PVCs
#   bash scripts/stop_all.sh --all        # also delete the namespace (wipes PVCs/data)
#
# Optional env vars:
#   K8S_DIR=path/to/k8s   (default: <repo-root>/k8s)
#   NAMESPACE=realtime-asr (default: realtime-asr)

set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
K8S_DIR="${K8S_DIR:-${REPO_DIR}/k8s}"
NAMESPACE="${NAMESPACE:-realtime-asr}"
DELETE_NAMESPACE=false

for arg in "$@"; do
  case "$arg" in
    --all) DELETE_NAMESPACE=true ;;
    *) echo "Unknown argument: $arg" >&2; exit 1 ;;
  esac
done

log() {
  printf '\n[%s] %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*"
}

# ── 1. Pre-flight ────────────────────────────────────────────────────────────
if ! command -v kubectl >/dev/null 2>&1; then
  echo "kubectl not found. Is k3s/kubectl installed and on PATH?" >&2
  exit 1
fi

log "Targeting namespace: ${NAMESPACE}"
log "Cluster: $(kubectl config current-context 2>/dev/null || echo '<unknown>')"

# ── 2. Delete kustomize-managed resources ────────────────────────────────────
if kubectl get namespace "${NAMESPACE}" >/dev/null 2>&1; then
  log "Deleting kustomize-managed resources from ${K8S_DIR} ..."
  kubectl delete -k "${K8S_DIR}" --ignore-not-found=true --wait=false \
    --namespace "${NAMESPACE}" 2>/dev/null || true

  # ── 3. Delete optional HTTPS ingress (applied by setup_https.sh) ──────────
  log "Removing ingress objects (if any) ..."
  kubectl delete ingress --all -n "${NAMESPACE}" --ignore-not-found=true 2>/dev/null || true

  # ── 4. Scale all remaining workloads to zero immediately ─────────────────
  log "Scaling all Deployments to 0 replicas ..."
  kubectl get deployments -n "${NAMESPACE}" -o name 2>/dev/null \
    | xargs -r kubectl scale --replicas=0 -n "${NAMESPACE}" || true

  log "Scaling all StatefulSets to 0 replicas ..."
  kubectl get statefulsets -n "${NAMESPACE}" -o name 2>/dev/null \
    | xargs -r kubectl scale --replicas=0 -n "${NAMESPACE}" || true

  log "Waiting for all pods in ${NAMESPACE} to terminate ..."
  kubectl wait pod --all -n "${NAMESPACE}" \
    --for=delete --timeout=120s 2>/dev/null || true

  log "All workloads stopped."
else
  log "Namespace '${NAMESPACE}' does not exist — nothing to stop."
fi

# ── 5. Optional: delete the entire namespace ─────────────────────────────────
if [[ "${DELETE_NAMESPACE}" == "true" ]]; then
  log "Deleting namespace '${NAMESPACE}' and all remaining objects ..."
  kubectl delete namespace "${NAMESPACE}" --ignore-not-found=true
  log "Namespace deleted. PersistentVolumeClaims and data are gone."
else
  log "Namespace '${NAMESPACE}' retained. Re-run with --all to also remove it."
  log ""
  log "To restart the application later:"
  log "  kubectl apply -k ${K8S_DIR}"
fi
