#!/usr/bin/env bash
set -euo pipefail

# Configure HTTPS for the realtime-asr k8s deployment on cloud.
#
# Two modes:
#   SELF_SIGNED (default) — generates a self-signed cert; no domain required.
#                           Browser shows a security warning; microphone still works.
#   LETSENCRYPT            — free trusted cert via Let's Encrypt; requires a domain
#                           that resolves to this ECS public IP and a valid email.
#
# Usage:
#   # Self-signed (works immediately, no domain required):
#   bash scripts/setup_https.sh
#
#   # Let's Encrypt (requires domain + email):
#   DOMAIN=tts.example.com EMAIL=you@example.com bash scripts/setup_https.sh
#
# Optional env vars:
#   MODE=SELF_SIGNED|LETSENCRYPT   (auto-detected from DOMAIN/EMAIL if not set)
#   DOMAIN                          hostname pointing to this server's public IP
#   EMAIL                           contact email for Let's Encrypt account
#   TLS_SECRET_NAME                 k8s secret name to store the cert (default: realtime-asr-tls)
#   NGINX_INGRESS_VERSION           nginx ingress controller chart version (default: 4.10.1)
#   CERT_MANAGER_VERSION            cert-manager version (default: v1.14.5)

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"

DOMAIN="${DOMAIN:-}"
EMAIL="${EMAIL:-}"
TLS_SECRET_NAME="${TLS_SECRET_NAME:-realtime-asr-tls}"
NAMESPACE="${NAMESPACE:-realtime-asr}"
INGRESS_NAME="${INGRESS_NAME:-gateway-ingress}"
LEGACY_NAMESPACE="${LEGACY_NAMESPACE:-realtime-tts}"
CLEANUP_LEGACY_INGRESS="${CLEANUP_LEGACY_INGRESS:-true}"
NGINX_INGRESS_VERSION="${NGINX_INGRESS_VERSION:-4.10.1}"
# Use NodePort on a local PC/cluster; LoadBalancer on cloud (ECS/EC2).
NGINX_INGRESS_SERVICE_TYPE="${NGINX_INGRESS_SERVICE_TYPE:-LoadBalancer}"
CERT_MANAGER_VERSION="${CERT_MANAGER_VERSION:-v1.14.5}"

# Auto-detect mode.
if [[ -z "${MODE:-}" ]]; then
  if [[ -n "$DOMAIN" && -n "$EMAIL" ]]; then
    MODE="LETSENCRYPT"
  else
    MODE="SELF_SIGNED"
  fi
fi

export KUBECONFIG=/etc/rancher/k3s/k3s.yaml

SUDO=""
if [[ "${EUID}" -ne 0 ]]; then
  SUDO="sudo"
fi

log() {
  printf '\n[%s] %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*"
}

need_cmd() {
  command -v "$1" >/dev/null 2>&1
}

install_helm() {
  if need_cmd helm; then
    log "helm already installed"
    return
  fi

  log "Installing helm"
  curl -fsSL https://raw.githubusercontent.com/helm/helm/main/scripts/get-helm-3 | bash
}

install_nginx_ingress() {
  log "Installing nginx ingress controller (service type: ${NGINX_INGRESS_SERVICE_TYPE})"
  helm repo add ingress-nginx https://kubernetes.github.io/ingress-nginx --force-update
  helm repo update

  # Do NOT use --wait here: on local clusters the LoadBalancer service never
  # receives an external IP, which causes Helm to time out even though the
  # controller pods are perfectly healthy. We wait for pod readiness separately.
  helm upgrade --install ingress-nginx ingress-nginx/ingress-nginx \
    --namespace ingress-nginx \
    --create-namespace \
    --version "${NGINX_INGRESS_VERSION}" \
    --set controller.service.type="${NGINX_INGRESS_SERVICE_TYPE}" \
    --set controller.config.use-forwarded-headers="true"

  log "Waiting for ingress-nginx controller pod to be ready ..."
  kubectl -n ingress-nginx rollout status deployment/ingress-nginx-controller --timeout=5m
}

cleanup_legacy_ingress_conflict() {
  if [[ "${CLEANUP_LEGACY_INGRESS}" != "true" ]]; then
    return
  fi

  if [[ "${LEGACY_NAMESPACE}" == "${NAMESPACE}" ]]; then
    return
  fi

  if kubectl -n "${LEGACY_NAMESPACE}" get ingress "${INGRESS_NAME}" >/dev/null 2>&1; then
    log "Removing legacy ingress ${LEGACY_NAMESPACE}/${INGRESS_NAME} to avoid host/path conflicts"
    kubectl -n "${LEGACY_NAMESPACE}" delete ingress "${INGRESS_NAME}" --ignore-not-found
  fi
}

generate_self_signed_cert() {
  log "Generating self-signed TLS certificate"

  local subject_cn="${DOMAIN:-realtime-asr.local}"

  # Generate private key + self-signed cert valid for 2 years.
  openssl req -x509 -nodes -days 730 \
    -newkey rsa:2048 \
    -keyout /tmp/tts-tls.key \
    -out /tmp/tts-tls.crt \
    -subj "/CN=${subject_cn}/O=realtime-asr" \
    -addext "subjectAltName=IP:$(curl -fsS http://100.100.100.200/latest/meta-data/eipv4 2>/dev/null || curl -fsS http://checkip.amazonaws.com 2>/dev/null || echo '127.0.0.1'),DNS:${subject_cn}"

  kubectl -n "${NAMESPACE}" create secret tls "${TLS_SECRET_NAME}" \
    --cert=/tmp/tts-tls.crt \
    --key=/tmp/tts-tls.key \
    --dry-run=client -o yaml | kubectl apply -f -

  rm -f /tmp/tts-tls.crt /tmp/tts-tls.key
  log "Self-signed TLS secret '${TLS_SECRET_NAME}' applied"
}

install_cert_manager() {
  log "Installing cert-manager ${CERT_MANAGER_VERSION}"
  kubectl apply -f "https://github.com/cert-manager/cert-manager/releases/download/${CERT_MANAGER_VERSION}/cert-manager.yaml"

  log "Waiting for cert-manager to be ready"
  kubectl -n cert-manager rollout status deployment/cert-manager --timeout=180s
  kubectl -n cert-manager rollout status deployment/cert-manager-webhook --timeout=180s
  # Allow the webhook to become fully ready before creating issuers.
  sleep 15
}

create_letsencrypt_issuer() {
  log "Creating Let's Encrypt ClusterIssuer"
  kubectl apply -f - <<EOF
apiVersion: cert-manager.io/v1
kind: ClusterIssuer
metadata:
  name: letsencrypt-prod
spec:
  acme:
    server: https://acme-v02.api.letsencrypt.org/directory
    email: ${EMAIL}
    privateKeySecretRef:
      name: letsencrypt-prod-account-key
    solvers:
      - http01:
          ingress:
            ingressClassName: nginx
EOF
}

patch_gateway_to_clusterip() {
  # Remove the gateway's own LoadBalancer so it no longer competes
  # with the ingress controller on port 80, which causes redirect loops.
  log "Patching gateway service to ClusterIP (traffic now flows via ingress only)"
  kubectl -n "${NAMESPACE}" patch svc gateway -p '{"spec":{"type":"ClusterIP"}}' 2>/dev/null || true
}

apply_self_signed_ingress() {
  local secret="$1"

  log "Applying catch-all HTTPS ingress (IP-based access, no hostname required)"

  # No 'host' field = nginx ingress matches all hostnames/IPs.
  # ssl-redirect false = no HTTP->HTTPS loop when the browser hits port 80.
  kubectl apply -f - <<EOF
apiVersion: networking.k8s.io/v1
kind: Ingress
metadata:
  name: ${INGRESS_NAME}
  namespace: ${NAMESPACE}
  annotations:
    nginx.ingress.kubernetes.io/proxy-read-timeout: "3600"
    nginx.ingress.kubernetes.io/proxy-send-timeout: "3600"
    nginx.ingress.kubernetes.io/proxy-http-version: "1.1"
    nginx.ingress.kubernetes.io/ssl-redirect: "false"
spec:
  ingressClassName: nginx
  tls:
    - secretName: ${secret}
  rules:
    - http:
        paths:
          - path: /grafana
            pathType: Prefix
            backend:
              service:
                name: grafana
                port:
                  number: 3000
          - path: /
            pathType: Prefix
            backend:
              service:
                name: gateway
                port:
                  number: 80
EOF
}

apply_letsencrypt_ingress() {
  local domain="$1"
  local secret="$2"

  log "Applying Let's Encrypt-managed ingress for domain: ${domain}"

  kubectl apply -f - <<EOF
apiVersion: networking.k8s.io/v1
kind: Ingress
metadata:
  name: ${INGRESS_NAME}
  namespace: ${NAMESPACE}
  annotations:
    cert-manager.io/cluster-issuer: letsencrypt-prod
    nginx.ingress.kubernetes.io/proxy-read-timeout: "3600"
    nginx.ingress.kubernetes.io/proxy-send-timeout: "3600"
    nginx.ingress.kubernetes.io/proxy-http-version: "1.1"
    nginx.ingress.kubernetes.io/ssl-redirect: "true"
spec:
  ingressClassName: nginx
  tls:
    - hosts:
        - ${domain}
      secretName: ${secret}
  rules:
    - host: ${domain}
      http:
        paths:
          - path: /grafana
            pathType: Prefix
            backend:
              service:
                name: grafana
                port:
                  number: 3000
          - path: /
            pathType: Prefix
            backend:
              service:
                name: gateway
                port:
                  number: 80
EOF
}

print_access_info() {
  local domain="${1:-}"
  local ingress_ip

  # Give ingress controller a moment to acquire an external IP.
  sleep 5
  ingress_ip="$(kubectl -n ingress-nginx get svc ingress-nginx-controller \
    -o jsonpath='{.status.loadBalancer.ingress[0].ip}' 2>/dev/null || true)"

  log "HTTPS setup complete"
  printf '\n'

  if [[ -n "$domain" ]]; then
    printf 'App URL:   https://%s\n' "$domain"
    printf 'Grafana:   https://%s/grafana/\n' "$domain"
    printf 'WebSocket: wss://%s/ws/transcribe\n' "$domain"
    if [[ -n "$ingress_ip" ]]; then
      printf '\nDNS:       Point %s -> %s (if not done yet)\n' "$domain" "$ingress_ip"
    fi
  else
    local public_ip
    public_ip="$(curl -fsS http://100.100.100.200/latest/meta-data/eipv4 2>/dev/null \
      || curl -fsS http://checkip.amazonaws.com 2>/dev/null || true)"

    printf 'Ingress IP: %s\n' "${ingress_ip:-<pending>}"
    if [[ -n "$public_ip" ]]; then
      printf '\nOpen in browser (accept the security warning for self-signed cert):\n'
      printf '  https://%s\n' "$public_ip"
      printf '  https://%s/grafana/\n' "$public_ip"
    fi
    printf '\nNote: Chrome/Firefox require accepting the warning once.\n'
    printf 'Microphone will work after you accept the certificate.\n'
  fi

  printf '\nGrafana credentials (default): admin / admin\n'

  printf '\nEnsure Cloud Security Group allows inbound:\n'
  printf '  TCP 443 (HTTPS)\n'
  printf '  TCP 80  (HTTP — for Let'\''s Encrypt ACME challenge)\n'
}

main() {
  log "HTTPS setup mode: ${MODE}"

  install_helm
  install_nginx_ingress

  case "${MODE}" in
    SELF_SIGNED)
      cleanup_legacy_ingress_conflict
      generate_self_signed_cert
      apply_self_signed_ingress "${TLS_SECRET_NAME}"
      patch_gateway_to_clusterip
      print_access_info
      ;;

    LETSENCRYPT)
      if [[ -z "$DOMAIN" || -z "$EMAIL" ]]; then
        echo "Error: DOMAIN and EMAIL must be set for LETSENCRYPT mode." >&2
        exit 1
      fi
      cleanup_legacy_ingress_conflict
      install_cert_manager
      create_letsencrypt_issuer
      apply_letsencrypt_ingress "$DOMAIN" "${TLS_SECRET_NAME}"
      patch_gateway_to_clusterip
      print_access_info "$DOMAIN"
      ;;

    *)
      echo "Error: Unknown MODE '${MODE}'. Use SELF_SIGNED or LETSENCRYPT." >&2
      exit 1
      ;;
  esac
}

main "$@"
