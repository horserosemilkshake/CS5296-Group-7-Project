#!/usr/bin/env bash
set -euo pipefail

# One-click Kubernetes deployment for this repo on a fresh cloud Ubuntu host.
# Usage:
#   bash scripts/deploy.sh
# Optional env vars:
#   REPO_DIR=/path/to/realtime-asr
#   LB_ALGORITHM=round-robin|least-conn|consistent-hash (default: consistent-hash)
#   INSTALL_METRICS_SERVER=true|false (default: true)
#   WAIT_TIMEOUT_SECONDS=1800

REPO_DIR="${REPO_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
INSTALL_METRICS_SERVER="${INSTALL_METRICS_SERVER:-true}"
WAIT_TIMEOUT_SECONDS="${WAIT_TIMEOUT_SECONDS:-1800}"
# Load balancing algorithm: round-robin, least-conn, consistent-hash (default: consistent-hash)
LB_ALGORITHM="${LB_ALGORITHM:-consistent-hash}"
# Gateway defaults tuned for maximum deployment stability on single-node hosts.
GATEWAY_REPLICAS="${GATEWAY_REPLICAS:-1}"
NLP_REPLICAS="${NLP_REPLICAS:-1}"
WEB_CONCURRENCY="${WEB_CONCURRENCY:-1}"
SUMMARIZE_MAX_CONCURRENCY="${SUMMARIZE_MAX_CONCURRENCY:-1}"
GATEWAY_CPU_REQUEST="${GATEWAY_CPU_REQUEST:-300m}"
GATEWAY_MEM_REQUEST="${GATEWAY_MEM_REQUEST:-2Gi}"
GATEWAY_CPU_LIMIT="${GATEWAY_CPU_LIMIT:-2000m}"
GATEWAY_MEM_LIMIT="${GATEWAY_MEM_LIMIT:-24Gi}"
NLP_CPU_REQUEST="${NLP_CPU_REQUEST:-200m}"
NLP_MEM_REQUEST="${NLP_MEM_REQUEST:-512Mi}"
NLP_CPU_LIMIT="${NLP_CPU_LIMIT:-2000m}"
NLP_MEM_LIMIT="${NLP_MEM_LIMIT:-2Gi}"
OLLAMA_CPU_REQUEST="${OLLAMA_CPU_REQUEST:-500m}"
OLLAMA_MEM_REQUEST="${OLLAMA_MEM_REQUEST:-1Gi}"
OLLAMA_CPU_LIMIT="${OLLAMA_CPU_LIMIT:-2000m}"
OLLAMA_MEM_LIMIT="${OLLAMA_MEM_LIMIT:-4Gi}"

log() {
  printf '\n[%s] %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*"
}

need_cmd() {
  command -v "$1" >/dev/null 2>&1
}

SUDO=""
if [[ "${EUID}" -ne 0 ]]; then
  SUDO="sudo"
fi

retry() {
  local attempts="$1"
  local sleep_seconds="$2"
  shift 2
  local i
  for ((i = 1; i <= attempts; i++)); do
    if "$@"; then
      return 0
    fi
    if [[ "$i" -lt "$attempts" ]]; then
      sleep "$sleep_seconds"
    fi
  done
  return 1
}

set_gateway_affinity_mode() {
  local mode="$1"
  local gateway_file="${REPO_DIR}/k8s/gateway.yaml"

  python3 - "$gateway_file" "$mode" <<'PY'
from pathlib import Path
import sys

path = Path(sys.argv[1])
mode = sys.argv[2]
text = path.read_text(encoding="utf-8")
lines = text.splitlines()

try:
    service_index = next(i for i, line in enumerate(lines) if line.strip() == "kind: Service")
except StopIteration:
    raise SystemExit("kind: Service not found in gateway.yaml")

try:
    spec_index = next(i for i in range(service_index, len(lines)) if lines[i].strip() == "spec:")
except StopIteration:
    raise SystemExit("Service spec not found in gateway.yaml")

insert_at = None
end_at = len(lines)
filtered: list[str] = []
skip_block = False
skip_indent = None

for idx, line in enumerate(lines):
    if idx > spec_index and line.startswith("---"):
        end_at = idx
        break

for idx, line in enumerate(lines):
    if idx <= spec_index or idx >= end_at:
        filtered.append(line)
        continue

    stripped = line.lstrip()
    indent = len(line) - len(stripped)

    if skip_block:
        if stripped and indent <= skip_indent:
            skip_block = False
            skip_indent = None
        else:
            continue

    if indent == 2 and (
        stripped.startswith("sessionAffinity:")
        or stripped.startswith("sessionAffinityConfig:")
    ):
        if insert_at is None:
            insert_at = len(filtered)

        # sessionAffinityConfig is a nested mapping; skip until next top-level key in Service spec.
        if stripped.startswith("sessionAffinityConfig:"):
            skip_block = True
            skip_indent = indent
        continue

    filtered.append(line)

if insert_at is None:
    try:
        insert_at = next(i for i, line in enumerate(filtered) if i > spec_index and line.strip() == "type: LoadBalancer") + 1
    except StopIteration:
        raise SystemExit("type: LoadBalancer not found in gateway.yaml")

replacement = ["  sessionAffinity: None"]
if mode == "clientip":
    replacement = [
        "  sessionAffinity: ClientIP",
        "  sessionAffinityConfig:",
        "    clientIP:",
        "      timeoutSeconds: 10800",
    ]

filtered[insert_at:insert_at] = replacement
path.write_text("\n".join(filtered) + "\n", encoding="utf-8")
PY
}

set_nginx_lb_mode() {
  local mode="$1"
  local nginx_file="${REPO_DIR}/nginx/nginx.conf"

  python3 - "$nginx_file" "$mode" <<'PY'
from pathlib import Path
import sys

path = Path(sys.argv[1])
mode = sys.argv[2]
lines = path.read_text(encoding="utf-8").splitlines()

start = None
for idx, line in enumerate(lines):
    if line.strip() == "upstream gateway_upstream {":
        start = idx
        break

if start is None:
    raise SystemExit("upstream gateway_upstream block not found in nginx.conf")

end = None
for idx in range(start + 1, len(lines)):
    if lines[idx].strip() == "}":
        end = idx
        break

if end is None:
    raise SystemExit("upstream gateway_upstream block is not closed")

directive = {
    "round-robin": "        # Load balancing: round-robin (default)",
    "least-conn": "        least_conn;",
    "consistent-hash": "        hash $remote_addr consistent;",
}[mode]

directive_index = None
for idx in range(start + 1, end):
    stripped = lines[idx].strip()
    if stripped in {
        "# Load balancing: round-robin (default)",
        "least_conn;",
        "hash $remote_addr consistent;",
        "hash \\$remote_addr consistent;",
    }:
        directive_index = idx
        break

if directive_index is None:
    try:
        directive_index = next(i for i in range(start + 1, end) if lines[i].strip().startswith("server gateway:8765"))
    except StopIteration:
        raise SystemExit("server gateway:8765 line not found in nginx.conf")

    lines[directive_index:directive_index] = [directive]
else:
    lines[directive_index] = directive

path.write_text("\n".join(lines) + "\n", encoding="utf-8")
PY
}

configure_load_balancing() {
  log "Configuring load balancing algorithm: ${LB_ALGORITHM}"

  case "${LB_ALGORITHM}" in
    round-robin)
      set_nginx_lb_mode "round-robin"
      set_gateway_affinity_mode "none"
      log "  ✓ NGINX: round-robin (default behavior, no directive)"
      log "  ✓ Kubernetes: sessionAffinity: None"
      ;;
    least-conn)
      set_nginx_lb_mode "least-conn"
      set_gateway_affinity_mode "none"
      log "  ✓ NGINX: least_conn (fewest active connections)"
      log "  ✓ Kubernetes: sessionAffinity: None"
      ;;
    consistent-hash)
      set_nginx_lb_mode "consistent-hash"
      set_gateway_affinity_mode "clientip"
      log "  ✓ NGINX: consistent hash on client IP (stable routing)"
      log "  ✓ Kubernetes: sessionAffinity: ClientIP (3 hours, then re-assigned)"
      ;;
    *)
      log "ERROR: Unknown LB_ALGORITHM='${LB_ALGORITHM}'. Use: round-robin, least-conn, or consistent-hash" >&2
      exit 1
      ;;
  esac
}

retry() {
  local attempts="$1"
  local sleep_seconds="$2"
  shift 2
  local i
  for ((i = 1; i <= attempts; i++)); do
    if "$@"; then
      return 0
    fi
    if [[ "$i" -lt "$attempts" ]]; then
      sleep "$sleep_seconds"
    fi
  done
  return 1
}

install_base_packages() {
  log "Installing base packages"
  ${SUDO} apt-get update -y
  ${SUDO} env DEBIAN_FRONTEND=noninteractive apt-get install -y \
    ca-certificates \
    curl \
    jq \
    conntrack \
    socat \
    unzip \
    tar
}

install_docker() {
  if need_cmd docker; then
    log "Docker already installed"
  else
    log "Installing Docker"
    ${SUDO} env DEBIAN_FRONTEND=noninteractive apt-get install -y docker.io
  fi

  log "Enabling Docker service"
  ${SUDO} systemctl enable --now docker

  # Allow current user to access docker without sudo in future sessions.
  if [[ -n "${SUDO}" ]]; then
    ${SUDO} usermod -aG docker "$USER" || true
  fi
}

install_k3s_and_kubectl() {
  if need_cmd k3s; then
    log "k3s already installed"
  else
    log "Installing k3s"
    curl -sfL https://get.k3s.io | ${SUDO} env INSTALL_K3S_EXEC="--write-kubeconfig-mode 644 --disable traefik" sh -
  fi

  if ! need_cmd kubectl && [[ -x /usr/local/bin/kubectl ]]; then
    ${SUDO} ln -sf /usr/local/bin/kubectl /usr/bin/kubectl
  fi

  export KUBECONFIG=/etc/rancher/k3s/k3s.yaml

  log "Waiting for Kubernetes node readiness"
  retry 60 5 kubectl get nodes >/dev/null
  kubectl wait --for=condition=Ready node --all --timeout=300s
}

install_metrics_server() {
  if [[ "${INSTALL_METRICS_SERVER}" != "true" ]]; then
    log "Skipping metrics-server installation"
    return 0
  fi

  log "Installing metrics-server for HPA support"
  kubectl apply -f https://github.com/kubernetes-sigs/metrics-server/releases/latest/download/components.yaml

  # k3s commonly requires insecure kubelet TLS for metrics-server in single-node setups.
  kubectl -n kube-system patch deployment metrics-server --type='json' -p='[
    {"op":"add","path":"/spec/template/spec/containers/0/args/-","value":"--kubelet-insecure-tls"},
    {"op":"add","path":"/spec/template/spec/containers/0/args/-","value":"--kubelet-preferred-address-types=InternalIP,ExternalIP,Hostname"}
  ]' || true

  kubectl -n kube-system rollout status deployment/metrics-server --timeout=240s || true
}

build_and_import_images() {
  log "Building app images"
  cd "$REPO_DIR"
  ${SUDO} docker build -t realtime-asr-gateway:latest .
  ${SUDO} docker build -t realtime-asr-nlp:latest .

  log "Importing images into k3s containerd"
  ${SUDO} docker save realtime-asr-gateway:latest | ${SUDO} k3s ctr images import -
  ${SUDO} docker save realtime-asr-nlp:latest | ${SUDO} k3s ctr images import -
}

deploy_manifests() {
  log "Applying Kubernetes manifests"
  cd "$REPO_DIR"
  kubectl apply -k k8s
}

tune_for_single_node() {
  log "Applying single-node scaling and resource tuning"

  kubectl -n realtime-asr scale deployment/gateway --replicas="$GATEWAY_REPLICAS"
  kubectl -n realtime-asr scale deployment/nlp --replicas="$NLP_REPLICAS"

  kubectl -n realtime-asr set resources deployment/gateway -c gateway \
    --requests="cpu=${GATEWAY_CPU_REQUEST},memory=${GATEWAY_MEM_REQUEST}" \
    --limits="cpu=${GATEWAY_CPU_LIMIT},memory=${GATEWAY_MEM_LIMIT}"

  kubectl -n realtime-asr set resources deployment/nlp -c nlp \
    --requests="cpu=${NLP_CPU_REQUEST},memory=${NLP_MEM_REQUEST}" \
    --limits="cpu=${NLP_CPU_LIMIT},memory=${NLP_MEM_LIMIT}"

  kubectl -n realtime-asr set resources deployment/ollama -c ollama \
    --requests="cpu=${OLLAMA_CPU_REQUEST},memory=${OLLAMA_MEM_REQUEST}" \
    --limits="cpu=${OLLAMA_CPU_LIMIT},memory=${OLLAMA_MEM_LIMIT}"

  # Keep gateway worker count minimal for large Vosk model memory footprint.
  kubectl -n realtime-asr set env deployment/gateway \
    "WEB_CONCURRENCY=${WEB_CONCURRENCY}"

  # Control NLP summarize/rewrite parallelism using semaphore size.
  kubectl -n realtime-asr set env deployment/nlp \
    "SUMMARIZE_MAX_CONCURRENCY=${SUMMARIZE_MAX_CONCURRENCY}"
}

diagnose_rollout_failure() {
  local deploy_name="$1"

  log "Rollout failed for deployment/${deploy_name}. Collecting diagnostics..."
  kubectl -n realtime-asr get deploy "$deploy_name" -o wide || true
  kubectl -n realtime-asr describe deployment "$deploy_name" || true
  kubectl -n realtime-asr get pods -l "app=${deploy_name}" -o wide || true

  local pod_names
  pod_names="$(kubectl -n realtime-asr get pods -l "app=${deploy_name}" -o jsonpath='{range .items[*]}{.metadata.name}{"\n"}{end}' 2>/dev/null || true)"
  if [[ -n "$pod_names" ]]; then
    while IFS= read -r pod; do
      [[ -z "$pod" ]] && continue
      printf '\n----- describe pod/%s -----\n' "$pod"
      kubectl -n realtime-asr describe pod "$pod" || true
      printf '\n----- logs pod/%s (current) -----\n' "$pod"
      kubectl -n realtime-asr logs "$pod" --all-containers --tail=120 || true
      printf '\n----- logs pod/%s (previous) -----\n' "$pod"
      kubectl -n realtime-asr logs "$pod" --all-containers --tail=120 --previous || true
    done <<< "$pod_names"
  fi

  printf '\nRecent namespace events:\n'
  kubectl -n realtime-asr get events --sort-by=.lastTimestamp | tail -n 40 || true
}

rollout_or_fail() {
  local deploy_name="$1"
  local timeout="$2"

  if ! kubectl -n realtime-asr rollout status "deployment/${deploy_name}" --timeout="$timeout"; then
    diagnose_rollout_failure "$deploy_name"
    return 1
  fi
}

wait_for_workloads() {
  local timeout="${WAIT_TIMEOUT_SECONDS}s"

  log "Waiting for core deployments"
  rollout_or_fail gateway "$timeout"
  rollout_or_fail nlp "$timeout"
  rollout_or_fail ollama "$timeout"

  log "Waiting for ollama model init job (can take several minutes on first run)"
  kubectl -n realtime-asr wait --for=condition=complete job/ollama-model-init --timeout="$timeout"
}

get_ecs_public_ip() {
  # Cloud metadata endpoint.
  curl -fsS -m 2 http://100.100.100.200/latest/meta-data/eipv4 2>/dev/null || true
}

print_access_info() {
  local public_ip
  local lb_ip
  local node_port

  public_ip="$(get_ecs_public_ip)"
  if [[ -z "$public_ip" ]]; then
    public_ip="$(curl -fsS http://checkip.amazonaws.com 2>/dev/null || true)"
  fi

  lb_ip="$(kubectl -n realtime-asr get svc gateway -o jsonpath='{.status.loadBalancer.ingress[0].ip}' 2>/dev/null || true)"
  node_port="$(kubectl -n realtime-asr get svc gateway -o jsonpath='{.spec.ports[0].nodePort}' 2>/dev/null || true)"

  log "Deployment complete"
  kubectl get pods -n realtime-asr

  printf '\nApp URLs (ensure Cloud Security Group allows inbound HTTP):\n'
  if [[ -n "$lb_ip" ]]; then
    printf '  - http://%s\n' "$lb_ip"
  fi
  if [[ -n "$public_ip" ]]; then
    printf '  - http://%s\n' "$public_ip"
    if [[ -n "$node_port" ]]; then
      printf '  - http://%s:%s\n' "$public_ip" "$node_port"
    fi
  fi

  printf '\nUseful checks:\n'
  printf '  - kubectl get pods -n realtime-asr\n'
  printf '  - kubectl get svc -n realtime-asr\n'
  printf '  - kubectl get hpa -n realtime-asr\n'
  printf '  - kubectl -n realtime-asr logs job/ollama-model-init\n'
}

main() {
  if [[ ! -f "$REPO_DIR/k8s/kustomization.yaml" ]]; then
    echo "Error: REPO_DIR does not look like the realtime-asr repository: $REPO_DIR" >&2
    exit 1
  fi

  log "Load balancing algorithm: ${LB_ALGORITHM}"
  configure_load_balancing

  install_base_packages
  install_docker
  install_k3s_and_kubectl
  install_metrics_server
  build_and_import_images
  deploy_manifests
  tune_for_single_node
  wait_for_workloads
  print_access_info
}

main "$@"
