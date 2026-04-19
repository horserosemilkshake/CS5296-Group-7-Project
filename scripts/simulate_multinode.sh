#!/usr/bin/env bash
# Simulate a 2-node Kubernetes cluster on a single machine using kind
# (Kubernetes IN Docker). Each "node" is a Docker container that acts as
# an independent k8s node, so scheduling, affinity, and resource isolation
# all behave like a real multi-node cluster.
#
# Node layout simulated:
#   node-1  (control-plane + worker role=frontend)  → gateway, nlp pods
#   node-2  (worker         role=backend)           → ollama, monitoring pods
#
# Usage:
#   bash scripts/simulate_multinode.sh up       # create cluster and deploy
#   bash scripts/simulate_multinode.sh down     # destroy cluster
#   bash scripts/simulate_multinode.sh status   # show pod/node distribution
#
# Existing cluster/ECS mode:
#   CLUSTER_MODE=existing bash scripts/simulate_multinode.sh up
#   CLUSTER_MODE=existing bash scripts/simulate_multinode.sh status
#
# CLUSTER_MODE values:
#   kind     -> force kind simulation mode (default)
#   existing -> use current kubectl context (for ECS/k3s/any existing cluster)
#   auto     -> use kind if available, otherwise existing
#
# Requirements: kind, docker, kubectl
# kind install: https://kind.sigs.k8s.io/docs/user/quick-start/#installation

set -euo pipefail

CLUSTER_NAME="realtime-asr-multi"
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
NAMESPACE="realtime-asr"
CLUSTER_MODE="${CLUSTER_MODE:-kind}"
# NodePort on the kind control-plane container, exposed as localhost:8080
GATEWAY_NODE_PORT=30765
GATEWAY_HOST_PORT=8080

ACTION="${1:-up}"

log() {
  printf '\n[%s] %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*"
}

check_requirements() {
  local missing=()
  command -v docker  >/dev/null 2>&1 || missing+=(docker)
  command -v kubectl >/dev/null 2>&1 || missing+=(kubectl)

  if [[ "${CLUSTER_MODE}" == "kind" ]]; then
    command -v kind >/dev/null 2>&1 || missing+=(kind)
  fi

  if [[ ${#missing[@]} -gt 0 ]]; then
    echo "Missing required tools: ${missing[*]}" >&2
    echo "Install kind: https://kind.sigs.k8s.io/docs/user/quick-start/#installation" >&2
    exit 1
  fi
}

resolve_cluster_mode() {
  case "${CLUSTER_MODE}" in
    kind|existing)
      ;;
    auto)
      if command -v kind >/dev/null 2>&1; then
        CLUSTER_MODE="kind"
      else
        CLUSTER_MODE="existing"
      fi
      ;;
    *)
      echo "Invalid CLUSTER_MODE='${CLUSTER_MODE}'. Use: kind | existing | auto" >&2
      exit 1
      ;;
  esac

  log "Cluster mode: ${CLUSTER_MODE}"
}

ensure_context_for_existing_cluster() {
  local ctx
  ctx="$(kubectl config current-context 2>/dev/null || true)"
  if [[ -z "${ctx}" ]]; then
    echo "No active kubectl context. Configure kubectl for your ECS cluster first." >&2
    exit 1
  fi

  log "Using existing kubectl context: ${ctx}"
}

create_cluster() {
  if [[ "${CLUSTER_MODE}" != "kind" ]]; then
    ensure_context_for_existing_cluster
    return
  fi

  if kind get clusters 2>/dev/null | grep -q "^${CLUSTER_NAME}$"; then
    log "Cluster '${CLUSTER_NAME}' already exists — skipping creation."
    kubectl config use-context "kind-${CLUSTER_NAME}"
    return
  fi

  log "Creating 2-node kind cluster: ${CLUSTER_NAME}"
  log "  node-1: control-plane  (role=frontend label: gateway + nlp)"
  log "  node-2: worker         (role=backend  label: ollama + monitoring)"

  # extraPortMappings maps NodePort 30765 inside the cluster
  # to localhost:8080, so the app is directly accessible on this machine.
  cat <<EOF | kind create cluster --name "${CLUSTER_NAME}" --config -
kind: Cluster
apiVersion: kind.x-k8s.io/v1alpha4
nodes:
  - role: control-plane
    extraPortMappings:
      - containerPort: ${GATEWAY_NODE_PORT}
        hostPort: ${GATEWAY_HOST_PORT}
        protocol: TCP
  - role: worker
EOF

  kubectl config use-context "kind-${CLUSTER_NAME}"
  log "Cluster ready. Nodes:"
  kubectl get nodes -o wide
}

label_nodes() {
  log "Labelling nodes to simulate frontend/backend machine separation ..."

  local cp_node worker_node first_node second_node

  if [[ "${CLUSTER_MODE}" == "kind" ]]; then
    cp_node="$(kubectl get nodes --no-headers | grep control-plane | awk '{print $1}')"
    worker_node="$(kubectl get nodes --no-headers | grep -v control-plane | awk '{print $1}')"
  else
    first_node="$(kubectl get nodes --no-headers | awk 'NR==1 {print $1}')"
    second_node="$(kubectl get nodes --no-headers | awk 'NR==2 {print $1}')"

    cp_node="${first_node}"
    if [[ -n "${second_node}" ]]; then
      worker_node="${second_node}"
    else
      worker_node="${first_node}"
      log "Only one node detected; applying both roles to the same node."
    fi
  fi

  if [[ -z "${cp_node}" || -z "${worker_node}" ]]; then
    echo "Failed to determine cluster nodes for labeling." >&2
    exit 1
  fi

  kubectl label node "${cp_node}"     node-role=frontend --overwrite
  kubectl label node "${worker_node}" node-role=backend  --overwrite

  log "  ${cp_node}   → node-role=frontend  (gateway, nlp)"
  log "  ${worker_node} → node-role=backend   (ollama, monitoring)"
}

load_custom_images() {
  if [[ "${CLUSTER_MODE}" != "kind" ]]; then
    log "Skipping kind image load in existing-cluster mode."
    return
  fi

  log "Loading custom Docker images into the kind cluster ..."
  local images=(
    "realtime-asr-gateway:latest"
    "realtime-asr-nlp:latest"
  )
  for img in "${images[@]}"; do
    if docker image inspect "${img}" >/dev/null 2>&1; then
      log "  Loading ${img}"
      kind load docker-image "${img}" --name "${CLUSTER_NAME}"
    else
      log "  WARNING: ${img} not found locally — build images first with:"
      log "    docker build -t realtime-asr-gateway:latest ."
      log "    docker build -t realtime-asr-nlp:latest --target nlp . (if separate Dockerfile)"
    fi
  done
}

patch_manifests_and_deploy() {
  log "Deploying kustomize manifests ..."

  # Apply base manifests.
  kubectl apply -k "${REPO_DIR}/k8s"

  # ── Patch 1: service exposure mode differs by cluster mode ──────────────
  if [[ "${CLUSTER_MODE}" == "kind" ]]; then
    # kind has no cloud LoadBalancer; NodePort with extraPortMappings gives
    # direct localhost access.
    kubectl -n "${NAMESPACE}" patch svc gateway \
      -p "{\"spec\":{\"type\":\"NodePort\",\"ports\":[{\"port\":80,\"targetPort\":8765,\"nodePort\":${GATEWAY_NODE_PORT}}]}}"
  else
    # On ECS/existing clusters keep cloud-native exposure.
    kubectl -n "${NAMESPACE}" patch svc gateway -p '{"spec":{"type":"LoadBalancer"}}' || true
  fi

  # ── Patch 2: add nodeSelector to pin workloads across the two nodes ─────
  # gateway and nlp → frontend node (control-plane)
  kubectl -n "${NAMESPACE}" patch deployment gateway --type=merge -p '
    {"spec":{"template":{"spec":{"nodeSelector":{"node-role":"frontend"}}}}}'
  kubectl -n "${NAMESPACE}" patch deployment nlp --type=merge -p '
    {"spec":{"template":{"spec":{"nodeSelector":{"node-role":"frontend"}}}}}'

  # ollama and monitoring → backend node (worker)
  kubectl -n "${NAMESPACE}" patch deployment ollama --type=merge -p '
    {"spec":{"template":{"spec":{"nodeSelector":{"node-role":"backend"}}}}}'
  kubectl -n "${NAMESPACE}" patch deployment prometheus --type=merge -p '
    {"spec":{"template":{"spec":{"nodeSelector":{"node-role":"backend"}}}}}' 2>/dev/null || true
  kubectl -n "${NAMESPACE}" patch deployment grafana --type=merge -p '
    {"spec":{"template":{"spec":{"nodeSelector":{"node-role":"backend"}}}}}' 2>/dev/null || true

  log "Waiting for gateway and nlp to be ready (this may take 1-2 min) ..."
  kubectl -n "${NAMESPACE}" rollout status deployment/gateway --timeout=180s || true
  kubectl -n "${NAMESPACE}" rollout status deployment/nlp    --timeout=180s || true
}

show_distribution() {
  log "=== 2-node simulation layout ==="
  echo ""
  kubectl get nodes -o wide
  echo ""
  log "Pod-to-node distribution:"
  kubectl get pods -n "${NAMESPACE}" -o wide \
    --sort-by='{.spec.nodeName}' 2>/dev/null || kubectl get pods -n "${NAMESPACE}" -o wide
}

print_access_info() {
  printf '\n'
  log "=== Access Information ==="

  if [[ "${CLUSTER_MODE}" == "kind" ]]; then
    printf '  App URL:   http://localhost:%s\n'          "${GATEWAY_HOST_PORT}"
    printf '  WebSocket: ws://localhost:%s/ws/transcribe\n' "${GATEWAY_HOST_PORT}"
    printf '\nNote: microphone requires HTTPS context. For HTTPS on kind, use kubectl port-forward\n'
    printf '      or expose via a self-signed ingress added manually.\n'
  else
    printf '  Service:   gateway (type LoadBalancer) in namespace %s\n' "${NAMESPACE}"
    printf '  Inspect:   kubectl -n %s get svc gateway -o wide\n' "${NAMESPACE}"
    printf '  Ingress:   If enabled, use your configured domain over HTTPS.\n'
  fi

  printf '\nUseful commands:\n'
  if [[ "${CLUSTER_MODE}" == "kind" ]]; then
    printf '  kubectl config use-context kind-%s\n'  "${CLUSTER_NAME}"
  fi
  printf '  kubectl get pods -n %s -o wide\n'      "${NAMESPACE}"
  printf '  kubectl logs -n %s deploy/gateway\n'   "${NAMESPACE}"
  printf '  bash scripts/simulate_multinode.sh down   # stop everything\n'
}

teardown() {
  if [[ "${CLUSTER_MODE}" != "kind" ]]; then
    log "Existing-cluster mode: down does not delete your ECS cluster."
    log "Use: kubectl delete -k ${REPO_DIR}/k8s  (if you want to remove app resources)"
    return
  fi

  log "Deleting kind cluster '${CLUSTER_NAME}' (removes all simulated nodes + data) ..."
  kind delete cluster --name "${CLUSTER_NAME}"
  log "Done. Your original kubectl context has been restored."
}

# ── Entrypoint ───────────────────────────────────────────────────────────────
resolve_cluster_mode
check_requirements

case "${ACTION}" in
  up)
    create_cluster
    label_nodes
    load_custom_images
    patch_manifests_and_deploy
    show_distribution
    print_access_info
    ;;
  down)
    teardown
    ;;
  status)
    kubectl config use-context "kind-${CLUSTER_NAME}" 2>/dev/null || true
    show_distribution
    ;;
  *)
    echo "Usage: $0 [up|down|status]" >&2
    exit 1
    ;;
esac
