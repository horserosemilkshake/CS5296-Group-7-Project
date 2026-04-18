#!/usr/bin/env bash
set -euo pipefail

# Sweep SUMMARIZE_MAX_CONCURRENCY and run stress tests for each value.
#
# Example:
#   bash scripts/benchmark/benchmark_nlp_semaphore.sh \
#     --base-url https://192.168.1.11 \
#     --insecure-tls \
#     --audio-file audio/cat.wav \
#     --scenarios summarize,rewrite,mixed-all \
#     --concurrency 1,2,4,8,16,32 \
#     --semaphores 1,2,4,8 \
#     --output-dir test_results/semaphore-sweep

NAMESPACE="realtime-asr"
BASE_URL="auto"
SEMAPHORES="1,2,4,8"
OUTPUT_DIR="test_results/semaphore-sweep"
SCENARIOS="summarize,rewrite,mixed-all"
CONCURRENCY="1,2,4,8,16,32"
REQUESTS_PER_SCENARIO="60"
TIMEOUT="240"
AUDIO_FILE=""
INSECURE_TLS="false"
EXTRA_ARGS=()

log() {
  printf '\n[%s] %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*"
}

usage() {
  cat <<'EOF'
Usage: bash scripts/benchmark/benchmark_nlp_semaphore.sh [options]

Options:
  --namespace <ns>              Kubernetes namespace (default: realtime-asr)
  --base-url <url>              Gateway base URL or auto (default: auto)
  --semaphores <csv>            CSV values, e.g. 1,2,4,8
  --output-dir <dir>            Output directory for JSON reports
  --scenarios <csv>             Stress scenarios CSV
  --concurrency <csv>           Concurrency CSV
  --requests-per-scenario <n>   Requests per scenario/concurrency
  --timeout <sec>               Request timeout seconds
  --audio-file <path>           WAV file for transcribe/mixed-all
  --insecure-tls                Disable TLS verification
  --extra-arg <arg>             Extra arg passed to stress script (repeatable)
  -h, --help                    Show this help

Notes:
  - Requires an existing deployment: deployment/nlp in the selected namespace.
  - For each semaphore value:
      1) set env SUMMARIZE_MAX_CONCURRENCY on deployment/nlp
      2) wait for rollout
      3) run stress_test_backend.py
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --namespace)
      NAMESPACE="$2"; shift 2 ;;
    --base-url)
      BASE_URL="$2"; shift 2 ;;
    --semaphores)
      SEMAPHORES="$2"; shift 2 ;;
    --output-dir)
      OUTPUT_DIR="$2"; shift 2 ;;
    --scenarios)
      SCENARIOS="$2"; shift 2 ;;
    --concurrency)
      CONCURRENCY="$2"; shift 2 ;;
    --requests-per-scenario)
      REQUESTS_PER_SCENARIO="$2"; shift 2 ;;
    --timeout)
      TIMEOUT="$2"; shift 2 ;;
    --audio-file)
      AUDIO_FILE="$2"; shift 2 ;;
    --insecure-tls)
      INSECURE_TLS="true"; shift ;;
    --extra-arg)
      EXTRA_ARGS+=("$2"); shift 2 ;;
    -h|--help)
      usage; exit 0 ;;
    *)
      echo "Unknown argument: $1" >&2
      usage
      exit 1 ;;
  esac
done

if ! command -v kubectl >/dev/null 2>&1; then
  echo "kubectl not found" >&2
  exit 1
fi
if ! command -v python3 >/dev/null 2>&1; then
  echo "python3 not found" >&2
  exit 1
fi

kubectl -n "$NAMESPACE" get deployment nlp >/dev/null
mkdir -p "$OUTPUT_DIR"

timestamp="$(date -u +%Y%m%dT%H%M%SZ)"
IFS=',' read -r -a sem_values <<< "$SEMAPHORES"

for raw in "${sem_values[@]}"; do
  sem="$(echo "$raw" | xargs)"
  [[ -z "$sem" ]] && continue
  if ! [[ "$sem" =~ ^[0-9]+$ ]]; then
    echo "Invalid semaphore value: $sem" >&2
    exit 1
  fi

  log "Setting SUMMARIZE_MAX_CONCURRENCY=$sem"
  kubectl -n "$NAMESPACE" set env deployment/nlp "SUMMARIZE_MAX_CONCURRENCY=$sem" >/dev/null
  kubectl -n "$NAMESPACE" rollout status deployment/nlp --timeout=600s

  output_file="${OUTPUT_DIR}/stress-semaphore-${sem}-${timestamp}.json"
  cmd=(
    python3 scripts/stress_test_backend.py
    --base-url "$BASE_URL"
    --k8s-namespace "$NAMESPACE"
    --scenarios "$SCENARIOS"
    --concurrency "$CONCURRENCY"
    --requests-per-scenario "$REQUESTS_PER_SCENARIO"
    --timeout "$TIMEOUT"
    --nlp-semaphore "$sem"
    --output "$output_file"
  )

  if [[ "$INSECURE_TLS" == "true" ]]; then
    cmd+=(--insecure-tls)
  fi
  if [[ -n "$AUDIO_FILE" ]]; then
    cmd+=(--audio-file "$AUDIO_FILE")
  fi
  if [[ "${#EXTRA_ARGS[@]}" -gt 0 ]]; then
    cmd+=("${EXTRA_ARGS[@]}")
  fi

  log "Running stress test for semaphore=$sem"
  "${cmd[@]}"

  log "Saved: $output_file"
  python3 - <<PY
import json
from pathlib import Path
p = Path(${output_file@Q})
report = json.loads(p.read_text(encoding="utf-8"))
results = report.get("results", [])
if not results:
    print("  summary: no results")
else:
    errs = sum(int(r.get("failed", 0)) for r in results)
    reqs = sum(int(r.get("requests", 0)) for r in results)
    print(f"  summary: failed={errs} / requests={reqs}")
PY

done

log "Semaphore sweep complete: $OUTPUT_DIR"
