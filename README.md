# Realtime ASR (Microservice Architecture)

Microservice-based web app that:

1. Captures microphone audio in the browser.
2. Streams audio to a FastAPI backend over WebSocket.
3. Uses Vosk for real-time transcription.
4. Displays transcript live on the frontend.
5. Offers one-click summarization using Ollama or a llama.cpp-compatible API after speaking ends.
6. Supports prompt-based transcript rewriting after recording, updating the live transcript text.

## Service Architecture

- `nginx`:
  - Public entrypoint on port `8765`
  - Reverse proxy and load balancer for `gateway`
- `gateway` (FastAPI):
  - Serves frontend assets (`/` + `/static/*`)
  - Handles realtime transcription websocket (`/ws/transcribe`)
  - Proxies NLP endpoints (`/api/summarize`, `/api/rewrite`) to `nlp`
- `nlp` (FastAPI):
  - Handles summarization and rewrite requests
  - Connects to LLM provider (`ollama` or OpenAI-compatible API)
- `ollama`:
  - Runs model server for generation
- `ollama-init`:
  - One-shot model warmup (`pull <model>`)
- `kafka`:
  - Async job queue for summarize/rewrite requests
- `prometheus`:
  - Scrapes `/metrics` from `gateway` and `nlp`
  - Scrapes container metrics from `cadvisor`
- `grafana`:
  - Visualization UI with pre-provisioned Prometheus datasource and dashboard
- `cadvisor`:
  - Exposes Docker container CPU/memory metrics for Prometheus

## Stack

- Frontend: HTML/CSS/JavaScript
- Gateway: FastAPI + WebSocket
- NLP Service: FastAPI
- Speech recognition: Vosk
- Summarization: Ollama API or OpenAI-compatible llama.cpp API
- Containerization: Docker + Docker Compose (nginx + gateway + nlp + ollama)

## Quick Start (Docker)

```bash
docker compose up --build
```

Open:

- `http://localhost:8765`
- `http://localhost:3000` (Grafana, default `admin/admin`)
- `http://localhost:9090` (Prometheus)

## Cloud Deploy + HTTPS (Quick)

Use the provided scripts for one-click deploy on Ubuntu ECS and then enable HTTPS.

Prerequisites:
- Inbound Security Group rules: TCP `22`, `80`, `443`
- Outbound internet access from ECS
- For trusted certs: a domain A record pointing to ECS public IP

### 1. Deploy to Cloud (k3s + app)

```bash
ssh <user>@<ecs-public-ip>
sudo apt-get update -y && sudo apt-get install -y git
cd /opt
sudo git clone <your-repo-url> realtime-asr
cd /opt/realtime-asr
sudo chmod +x scripts/*.sh

# Optional: load balancing mode (default: consistent-hash)
export LB_ALGORITHM=consistent-hash   # round-robin | least-conn | consistent-hash

bash scripts/deploy.sh
```

### 2. Enable HTTPS

Self-signed (fastest, no domain):

```bash
cd /opt/realtime-asr
bash scripts/setup_https.sh
```

Let's Encrypt (trusted cert):

```bash
cd /opt/realtime-asr
DOMAIN=tts.example.com EMAIL=you@example.com bash scripts/setup_https.sh
```

### 3. Verify

```bash
export KUBECONFIG=/etc/rancher/k3s/k3s.yaml
kubectl get pods -n realtime-asr
kubectl get svc -n realtime-asr
kubectl get ingress -n realtime-asr
kubectl -n realtime-asr logs job/ollama-model-init
```

Open:
- Self-signed: `https://<ecs-public-ip>`
- Let's Encrypt: `https://<your-domain>`

Notes:
- Keep TCP `80` open for ACME challenge when using Let's Encrypt.
- Self-signed mode will show a browser warning; accept once to continue.

### HTTPS troubleshooting (localhost / k3s)

If `bash scripts/setup_https.sh` fails with an ingress webhook error like:

- `host "_" and path "/grafana" is already defined in ingress <ns>/gateway-ingress`

you likely have a leftover `gateway-ingress` from an older namespace.

Clean up and retry:

```bash
export KUBECONFIG=/etc/rancher/k3s/k3s.yaml
kubectl get ingress -A
kubectl -n realtime-tts delete ingress gateway-ingress --ignore-not-found
kubectl -n realtime-asr delete ingress gateway-ingress --ignore-not-found
bash scripts/setup_https.sh
```

For local-only clusters, you can also use NodePort for ingress controller service:

```bash
NGINX_INGRESS_SERVICE_TYPE=NodePort bash scripts/setup_https.sh
```

The script supports namespace overrides if needed:

```bash
NAMESPACE=realtime-asr LEGACY_NAMESPACE=realtime-tts bash scripts/setup_https.sh
```

## Kubernetes Cluster Deployment (High Load)

For sustained high load, run this app on Kubernetes with multiple `gateway` and `nlp` pods plus autoscaling.

### 1. Build images

Build the two app images used by the manifests:

```bash
docker build -t realtime-asr-gateway:latest .
docker build -t realtime-asr-nlp:latest .
```

If you use `kind`, load images into the cluster:

```bash
kind load docker-image realtime-asr-gateway:latest
kind load docker-image realtime-asr-nlp:latest
```

If you use `minikube`, use:

```bash
minikube image load realtime-asr-gateway:latest
minikube image load realtime-asr-nlp:latest
```

### 2. Apply manifests

```bash
kubectl apply -k k8s
```

This deploys:
- `gateway` Deployment + Service (3 replicas by default)
- `nlp` Deployment + Service (2 replicas by default)
- `ollama` Deployment + PVC
- `ollama-model-init` Job (pulls `llama3.2` into Ollama)
- HPA for `gateway` and `nlp`

### 3. Verify cluster health

```bash
kubectl get pods -n realtime-asr
kubectl get svc -n realtime-asr
kubectl get hpa -n realtime-asr
kubectl get job -n realtime-asr ollama-model-init
```

Wait until:
- `ollama` pod is `Running` and `READY 1/1`
- `ollama-model-init` job shows `COMPLETIONS 1/1`

Before model pull completes, summarize/rewrite requests can return temporary upstream errors.

### 4. Access the app

If your cluster supports external load balancers, use the `gateway` service external IP.

For local clusters, port-forward:

```bash
kubectl port-forward -n realtime-asr svc/gateway 8765:80
```

Open:
- `http://localhost:8765`

### 5. Scale manually (optional)

```bash
kubectl scale deploy/gateway -n realtime-asr --replicas=6
kubectl scale deploy/nlp -n realtime-asr --replicas=3
```

Notes:
- HPAs require `metrics-server` in the cluster.
- Default model storage uses the cluster default StorageClass via PVC `ollama-data`.
- Manifests are located in `k8s/` and applied via `k8s/kustomization.yaml`.

## Use a Different Port

Yes. `0.0.0.0` is the bind address (all interfaces), while `8765` is the default port in this project.

Run with custom host/container ports:

```bash
HOST_PORT=8080 APP_PORT=8080 docker compose up --build
```

Then open:

- `http://localhost:8080`

If only host port needs to change (container stays on 8765):

```bash
HOST_PORT=8080 docker compose up --build
```

## Docker Services Behavior

```bash
docker compose up --build
```

The `gateway` container handles UI + websocket + API proxying.
The `nlp` container handles summarize/rewrite APIs.
When `NLP_ASYNC_ENABLED=true`, summarize/rewrite are queued through Kafka and processed asynchronously.
The `nginx` container load-balances inbound traffic across one or more `gateway` instances.
The `ollama` container runs Ollama.
The `prometheus` container scrapes service/container metrics.
The `grafana` container visualizes health and latency dashboards.
The one-shot `ollama-init` service pulls `llama3.2` before the app starts.
On first run, summarization becomes available after the model pull completes.

### Kafka Async NLP Jobs

With Docker Compose, async mode is enabled by default via `NLP_ASYNC_ENABLED=true`.

Flow:

1. Frontend calls `POST /api/summarize` or `POST /api/rewrite`.
2. Gateway enqueues job into Kafka and returns `job_id` with `status=queued`.
3. NLP worker consumes job, runs LLM, and updates in-memory job status.
4. Frontend polls `GET /api/jobs/{job_id}` until `completed` or `failed`.

Relevant topics:

1. `nlp.jobs.request`
2. `nlp.jobs.result`

Important:

1. Async job status is currently in-memory in the NLP service.
2. For multi-instance production deployments, replace in-memory job state with shared storage.

### Monitoring

1. Start the full stack:

```bash
docker compose up -d --build
```

2. Open Grafana:
  - `http://localhost:3000`
  - Username: `admin`
  - Password: `admin`

3. Open Prometheus targets page:
  - `http://localhost:9090/targets`

The dashboard `Realtime ASR Overview` is provisioned automatically and includes:
- Gateway/NLP request rate
- Gateway/NLP p95 latency
- Gateway/NLP 5xx rates
- Per-service container CPU and memory usage

### Scale Gateway Replicas

Use this to run multiple gateway instances behind nginx:

```bash
docker compose up -d --build --scale gateway=3
```

## Environment Variables

- `VOSK_MODEL_PATH` (default: `/app/models/vosk-model-small-en-us-0.15`)
- `VOSK_MODEL_URL` (default model download URL)
- `NLP_API_BASE` (gateway -> nlp, default in Docker: `http://nlp:8766`)
- `NLP_PORT` (default: `8766`)
- `NLP_WEB_CONCURRENCY` (default: `1`)
- `LLM_API_MODE`
  - `ollama` for `/api/generate`
  - `openai` for `/v1/chat/completions` (llama.cpp OpenAI-compatible mode)
- `LLM_API_BASE` (default in Docker: `http://ollama:11434`)
- `LLM_MODEL` (for example `llama3.2`)

### Semaphore Performance Sweep

To compare NLP throughput/latency/error behavior for different semaphore sizes
(`SUMMARIZE_MAX_CONCURRENCY`), use:

```bash
bash scripts/benchmark/benchmark_nlp_semaphore.sh \
  --base-url https://<gateway-ip> \
  --insecure-tls \
  --audio-file audio/cat.wav \
  --scenarios summarize,rewrite,mixed-all \
  --concurrency 1,2,4,8,16,32 \
  --semaphores 1,2,4,8 \
  --output-dir test_results/semaphore-sweep
```

What this does for each semaphore value:

1. Sets `SUMMARIZE_MAX_CONCURRENCY=<value>` on `deployment/nlp`.
2. Waits for the NLP rollout to complete.
3. Runs `scripts/stress_test_backend.py` and writes a separate JSON result file.

Each output file includes `config.nlp_semaphore` so results are easy to compare.

## EC2 + Kubernetes Deployment Guide (EKS)

This section shows a production-style deployment of this app on AWS EC2-backed Kubernetes using EKS managed node groups.

## One-Click Kubernetes Deploy (Single Host)

For a newly initialized Ubuntu host where this repository is already present, run:

```bash
bash scripts/deploy.sh
```

The script will:
- install required system packages
- install/start Docker
- install k3s + kubectl
- install metrics-server (for HPA)
- build and import `realtime-asr-gateway:latest` and `realtime-asr-nlp:latest`
- apply `k8s/` manifests and wait for readiness

After completion, it prints service URLs and verification commands.

Important:
- Ensure EC2 security group allows inbound HTTP (`80`) and/or the Kubernetes node port for `gateway`.
- First startup can take a while because `ollama-model-init` pulls the model.

### Architecture

1. EKS control plane (managed by AWS).
2. EC2 worker nodes (managed node group) run `gateway`, `nlp`, and `ollama` pods.
3. ECR stores the `realtime-asr-gateway` and `realtime-asr-nlp` images.
4. Kubernetes `Service` + `Ingress` expose HTTP(S) traffic.
5. HPA scales app pods during load (requires `metrics-server`).

### 1. AWS prerequisites

1. Create or choose an AWS account/region.
2. Install tools locally: `aws`, `kubectl`, `eksctl`, `helm`.
3. Configure AWS CLI credentials:

```bash
aws configure
```

4. Verify identity:

```bash
aws sts get-caller-identity
```

### 2. Create ECR repositories

```bash
export AWS_REGION=ap-southeast-1
export AWS_ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)

aws ecr create-repository --repository-name realtime-asr-gateway --region $AWS_REGION || true
aws ecr create-repository --repository-name realtime-asr-nlp --region $AWS_REGION || true
```

Login Docker to ECR:

```bash
aws ecr get-login-password --region $AWS_REGION | docker login --username AWS --password-stdin $AWS_ACCOUNT_ID.dkr.ecr.$AWS_REGION.amazonaws.com
```

### 3. Build and push images to ECR

```bash
export IMAGE_TAG=$(date +%Y%m%d-%H%M%S)

docker build -t realtime-asr-gateway:$IMAGE_TAG .
docker build -t realtime-asr-nlp:$IMAGE_TAG .

docker tag realtime-asr-gateway:$IMAGE_TAG $AWS_ACCOUNT_ID.dkr.ecr.$AWS_REGION.amazonaws.com/realtime-asr-gateway:$IMAGE_TAG
docker tag realtime-asr-nlp:$IMAGE_TAG $AWS_ACCOUNT_ID.dkr.ecr.$AWS_REGION.amazonaws.com/realtime-asr-nlp:$IMAGE_TAG

docker push $AWS_ACCOUNT_ID.dkr.ecr.$AWS_REGION.amazonaws.com/realtime-asr-gateway:$IMAGE_TAG
docker push $AWS_ACCOUNT_ID.dkr.ecr.$AWS_REGION.amazonaws.com/realtime-asr-nlp:$IMAGE_TAG
```

### 4. Create EKS cluster (EC2 worker nodes)

```bash
export CLUSTER_NAME=realtime-asr-eks

eksctl create cluster \
  --name $CLUSTER_NAME \
  --region $AWS_REGION \
  --nodegroup-name tts-ng \
  --node-type m6i.2xlarge \
  --nodes 3 \
  --nodes-min 3 \
  --nodes-max 8 \
  --managed
```

Update kubeconfig:

```bash
aws eks update-kubeconfig --region $AWS_REGION --name $CLUSTER_NAME
kubectl get nodes
```

### 5. Install required cluster add-ons

Install metrics server (required for HPA):

```bash
helm repo add metrics-server https://kubernetes-sigs.github.io/metrics-server/
helm repo update
helm upgrade --install metrics-server metrics-server/metrics-server -n kube-system
```

Install NGINX ingress controller:

```bash
helm repo add ingress-nginx https://kubernetes.github.io/ingress-nginx
helm repo update
helm upgrade --install ingress-nginx ingress-nginx/ingress-nginx -n ingress-nginx --create-namespace
```

Optional for TLS automation:

```bash
helm repo add jetstack https://charts.jetstack.io
helm repo update
helm upgrade --install cert-manager jetstack/cert-manager -n cert-manager --create-namespace --set crds.enabled=true
```

### 6. Point manifests to ECR images

Edit `k8s/gateway.yaml` and `k8s/nlp.yaml`:

1. Replace `image: realtime-asr-gateway:latest` with:

```text
image: <account>.dkr.ecr.<region>.amazonaws.com/realtime-asr-gateway:<tag>
```

2. Replace `image: realtime-asr-nlp:latest` with:

```text
image: <account>.dkr.ecr.<region>.amazonaws.com/realtime-asr-nlp:<tag>
```

3. Set `imagePullPolicy: IfNotPresent` or `Always` for frequent releases.

### 7. Deploy application

```bash
kubectl apply -k k8s
```

Validate startup:

```bash
kubectl get pods -n realtime-asr
kubectl get svc -n realtime-asr
kubectl get hpa -n realtime-asr
kubectl get job -n realtime-asr ollama-model-init
```

Wait for:

1. `ollama` pod `READY 1/1`.
2. `ollama-model-init` job `COMPLETIONS 1/1`.

### 8. Expose app publicly

You have two common options:

1. Use `Service type: LoadBalancer` on `gateway` (already in manifests).
2. Use ingress with a DNS hostname.

If using ingress, create `k8s/ingress.yaml` targeting `gateway` service on port `80`, then apply it and map your domain DNS to the ingress load balancer.

### 9. Security groups and networking

Open inbound only what you need:

1. `80` and `443` for app traffic.
2. Restrict SSH (`22`) to your admin IP only.
3. Do not expose internal pod/service ports directly (`8766`, `11434`) to the internet.

### 10. Monitoring and observability on EKS

For Kubernetes-native monitoring, you can:

1. Keep current app `/metrics` endpoints.
2. Deploy `kube-prometheus-stack` via Helm for cluster and app dashboards.
3. Add alerts for:
  - high `5xx` rates on `gateway`/`nlp`
  - high latency on summarize/rewrite paths
  - high memory on `ollama`

### 11. Rolling updates

1. Build/push new image tags to ECR.
2. Update image tags in manifests.
3. Apply:

```bash
kubectl apply -k k8s
kubectl rollout status deploy/gateway -n realtime-asr
kubectl rollout status deploy/nlp -n realtime-asr
```

### 12. Common issues and fixes

1. `502` on summarize/rewrite right after deploy:
  - `ollama-model-init` is still pulling the model.
  - Wait until job completes.
2. HPA targets show `<unknown>`:
  - Install/fix `metrics-server`.
3. Pods cannot pull images:
  - Check ECR image URI/tag and node IAM permissions.
4. Load balancer not receiving traffic:
  - Check ingress/controller status and AWS security groups.

## Local Development (without Docker)

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r backend/requirements.txt
# Ensure Vosk model exists locally and VOSK_MODEL_PATH points to it.
uvicorn backend.app.main:app --reload --port 8765

# Run NLP service separately in another terminal
uvicorn backend.app.nlp_service:app --reload --port 8766
```

## Backend Stress Testing (Concurrency + Backpressure)

Use `scripts/stress_test_backend.py` to stress `/api/summarize` and `/api/rewrite` under high concurrency and mixed parallel access.
Use `transcribe` scenarios to stress `/ws/transcribe` session handling as well.

What it measures:

1. Request throughput (RPS).
2. Latency (`p50`, `p95`, `p99`, max).
3. Failure/timeout rates and status code distribution.
4. Backpressure index (0-100) derived from overload errors and p95 latency.
5. CPU and memory samples during the test:
   - Docker mode: sampled via `docker stats`
   - Kubernetes mode: sampled via `kubectl top pods`

### Prerequisites

1. App must be reachable via a gateway URL (for example `http://localhost:8765`).
2. Python 3 installed locally.
3. For Kubernetes resource sampling, `metrics-server` must be installed.

### Quick run

```bash
python3 scripts/stress_test_backend.py \
  --base-url http://localhost:8765 \
  --scenarios summarize,rewrite,mixed,transcribe \
  --concurrency 1,4,8,16,32 \
  --requests-per-scenario 60 \
  --monitor-mode auto \
  --k8s-namespace realtime-asr
```

### Example for heavier load

```bash
python3 scripts/stress_test_backend.py \
  --base-url http://localhost:8765 \
  --scenarios mixed-all \
  --concurrency 16,32,48,64 \
  --requests-per-scenario 120 \
  --timeout 180 \
  --monitor-mode k8s \
  --ws-chunks 40 \
  --ws-chunk-bytes 3200 \
  --ws-chunk-delay-ms 8
```

### Output

The script prints a per-scenario table and writes a JSON report file such as:

```text
stress-results-20260318T000000Z.json
```

JSON includes:

1. Scenario metrics by concurrency.
2. Status code breakdown and sample errors.
3. Aggregated resource metrics per service (`gateway`, `nlp`, `ollama`, etc).

WebSocket scenarios:

1. `transcribe`: only `/ws/transcribe` sessions.
2. `mixed-all`: randomized mix of summarize + rewrite + transcribe in parallel.

If you use websocket scenarios, install dependency locally:

```bash
pip install websockets
```

### Interpreting backpressure

Treat these as overload signs:

1. Rising `p95/p99` latency with flat or falling throughput.
2. Increasing `502/503/504/429` or timeout counts.
3. High backpressure index (for example `>40` moderate, `>70` severe).
4. `nlp` and/or `ollama` memory/CPU saturation near limits.

Tune with:

1. More `gateway`/`nlp` replicas.
2. Higher node capacity for `ollama`.
3. Adjusted concurrency/timeouts and HPA thresholds.
4. Queue size and worker settings (`MAX_AUDIO_QUEUE_SIZE`, `WEB_CONCURRENCY`, `NLP_WEB_CONCURRENCY`, `SUMMARIZE_MAX_CONCURRENCY`).

## Notes

- Browser microphone access requires HTTPS in production.
- This app sends PCM chunks from browser to backend and resamples to 16 kHz for Vosk.
- Keep model size small for faster startup and lower memory usage.
