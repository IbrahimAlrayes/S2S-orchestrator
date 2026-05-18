# S2S Orchestrator — Deployment Guide

## Table of Contents

1. [Infrastructure Overview](#1-infrastructure-overview)
2. [Repository Layout](#2-repository-layout)
3. [Kubernetes Manifests](#3-kubernetes-manifests)
4. [Cloud Build](#4-cloud-build)
5. [CI/CD Workflow](#5-cicd-workflow)
6. [GitHub Secrets Reference](#6-github-secrets-reference)
7. [First-Time Setup Runbook](#7-first-time-setup-runbook)
8. [Routine Deployment Runbook](#8-routine-deployment-runbook)
9. [Post-Deploy Checklist](#9-post-deploy-checklist)
10. [Rollback Procedure](#10-rollback-procedure)
11. [Troubleshooting](#11-troubleshooting)

---

## 1. Infrastructure Overview

### Google Cloud projects

| Project | Purpose |
|---|---|
| `researchdeployments` | Artifact Registry — stores all Docker images |
| `hajj-umrah-nsk-dev` | GKE cluster — runs all workloads |

### GKE cluster

| Field | Value |
|---|---|
| Name | `nsk` |
| Region | `me-central2` |
| Kubernetes version | 1.35.3-gke.1389000 |

### Node pools

| Pool | Machine | Disk | Purpose |
|---|---|---|---|
| `general-e-standard-8-nsk` | e2-standard-8 | 200 GB | All S2S workloads (agent, livekit, token-server, demo) |
| `stateful-pool` | e2-standard-4 | 100 GB | Redis |
| `gpu-nvidia-l4-x2-min1-nsk` | g2-standard-24 | — | GPU (not used by S2S; taint excludes all S2S pods) |

### Namespace

All S2S resources live in the `elm-s2s` namespace.

### Artifact Registry

| Field | Value |
|---|---|
| Project | `researchdeployments` |
| Location | `me-central2` |
| Repository | `nsk-s2s` |
| Registry prefix | `me-central2-docker.pkg.dev/researchdeployments/nsk-s2s` |

### Images

| Image name | Source | Description |
|---|---|---|
| `s2s-agent` | `agent/` | Python LiveKit worker (STT → LLM → TTS pipeline) |
| `s2s-token-server` | `token-server/` | FastAPI token-signing service |
| `s2s-demo` | `demo/` | Next.js standalone frontend |

### Static IP

| Name | IP | Owner |
|---|---|---|
| `elm-s2s-ingress-ip` | `34.54.4.30` | GKE HTTP/S Global LB Ingress for this deployment |

### Cross-namespace service dependencies

These services run on the **same cluster** in different namespaces. Internal cluster DNS resolves them with zero network egress.

| Service | Namespace | Cluster DNS pattern |
|---|---|---|
| STT (ASR) | `asr-serving` | `http://<svc>.asr-serving.svc.cluster.local:<port>` |
| LLM / RAG | `nlp-rag` | `http://<svc>.nlp-rag.svc.cluster.local:<port>` |
| TTS | `elm-tts` | `http://<svc>.elm-tts.svc.cluster.local:<port>` |

### Image pull mechanism

Every namespace on the cluster pulls private images via a Kubernetes Secret named `regcred` (type `kubernetes.io/dockerconfigjson`). This secret holds a JSON key for `ar-puller@researchdeployments.iam.gserviceaccount.com`, which has `roles/artifactregistry.reader` on `researchdeployments`.

The `regcred` secret in `elm-s2s` was bootstrapped by copying it from the `nlp-rag` namespace:

```bash
kubectl get secret regcred -n nlp-rag -o json \
  | python3 -c "
import sys, json
d = json.load(sys.stdin)
d['metadata'] = {'name': 'regcred', 'namespace': 'elm-s2s'}
d.pop('status', None)
print(json.dumps(d))
" \
  | kubectl apply -f -
```

---

## 2. Repository Layout

```
.
├── agent/                     # Python LiveKit worker
│   ├── Dockerfile
│   ├── agent.py
│   ├── config.py
│   ├── metrics.py
│   ├── requirements.txt
│   └── plugins/
├── token-server/              # FastAPI token server
│   ├── Dockerfile
│   └── server.py
├── demo/                      # Next.js frontend (output: standalone)
│   ├── Dockerfile
│   └── ...
├── k8s/                       # Kubernetes manifests
│   ├── namespace.yaml
│   ├── configmap.yaml
│   ├── secret.yaml.template   # copy → secret.yaml, fill values, apply
│   ├── ingress.yaml
│   ├── redis/
│   │   ├── deployment.yaml
│   │   └── service.yaml
│   ├── livekit/
│   │   ├── deployment.yaml
│   │   └── service.yaml
│   ├── agent/
│   │   ├── deployment.yaml
│   │   └── service.yaml
│   ├── token-server/
│   │   ├── deployment.yaml
│   │   └── service.yaml
│   ├── demo/
│   │   ├── deployment.yaml
│   │   └── service.yaml
│   ├── build-push.sh          # local build + push helper (uses absurahio@gmail.com)
│   ├── deploy.sh              # local kubectl apply helper (uses mebnouf@elm.sa)
│   └── setup-gar-pull-access.sh  # one-time regcred copy helper
├── cloudbuild.yaml            # Cloud Build pipeline (3 parallel image builds)
└── .github/
    └── workflows/
        └── ci-cd.yml          # GitHub Actions CI/CD
```

---

## 3. Kubernetes Manifests

### 3.1 Manifest dependency order

Apply in this order (respects ConfigMap/Secret must exist before Deployments reference them):

```
namespace → configmap → secret → redis → livekit → agent → token-server → demo → ingress
```

### 3.2 namespace.yaml

Creates the `elm-s2s` namespace with labels `project: s2s-orchestrator` and `env: dev`.

```bash
kubectl apply -f k8s/namespace.yaml
```

### 3.3 configmap.yaml — `s2s-config`

Non-sensitive configuration consumed by the agent and token-server via `envFrom`. Key groups:

| Group | Notable keys |
|---|---|
| LiveKit (internal) | `LIVEKIT_URL=ws://livekit-server:7880` |
| Agent behaviour | `AGENT_NAME`, `AGENT_SYSTEM_PROMPT`, `AGENT_GREETING`, `AGENT_USE_TURN_DETECTOR`, `AGENT_ALLOW_INTERRUPTIONS`, interruption/endpointing tuning knobs |
| STT defaults | `CUSTOM_STT_PROVIDER`, `CUSTOM_STT_MODEL`, `CUSTOM_STT_LANGUAGE`, `CUSTOM_STT_TARGET_SAMPLE_RATE` |
| LLM defaults | `CUSTOM_LLM_PROVIDER`, `CUSTOM_LLM_MODEL`, `CUSTOM_LLM_TEMPERATURE`, `CUSTOM_LLM_MAX_TOKENS` |
| TTS defaults | `CUSTOM_TTS_PROVIDER`, `CUSTOM_TTS_VOICE`, `CUSTOM_TTS_SAMPLE_RATE`, `CUSTOM_TTS_AUDIO_FORMAT` |
| Token server | `TOKEN_SERVER_PORT=8080`, `TOKEN_TTL_MINUTES=60` |
| Observability | `PROMETHEUS_MULTIPROC_DIR=/tmp/prom_multiproc`, `AGENT_METRICS_PORT=9090` |

### 3.4 secret.yaml.template → secret.yaml — `s2s-secrets`

Sensitive values. `k8s/secret.yaml` is gitignored. Copy and fill before first deploy:

```bash
cp k8s/secret.yaml.template k8s/secret.yaml
# edit k8s/secret.yaml — replace every REPLACE_WITH_* value
kubectl apply -f k8s/secret.yaml
```

| Key | Notes |
|---|---|
| `LIVEKIT_API_KEY` / `LIVEKIT_API_SECRET` | Must match the `keys:` block in `livekit.yaml` below |
| `LIVEKIT_PUBLIC_URL` | `wss://<livekit-lb-external-ip>:7880` — fill after first deploy |
| `livekit.yaml` | Full LiveKit server config mounted as a file. Contains API keys, RTC port range (50000-50020), Redis address, Prometheus port. |
| `CUSTOM_STT_URL` | Internal cluster DNS URL for ASR service |
| `CUSTOM_STT_ACCESS_TOKEN` | Bearer token for ASR (empty if no auth) |
| `CUSTOM_LLM_URL` | Internal cluster DNS URL for LLM/RAG service |
| `CUSTOM_LLM_ACCESS_TOKEN` | Bearer token for LLM |
| `CUSTOM_LLM_CLIENT_ID` / `CUSTOM_LLM_CLIENT_SECRET` | Nusuk OAuth credentials (leave empty if unused) |
| `CUSTOM_TTS_URL` | Internal cluster DNS URL for TTS service |
| `CUSTOM_TTS_ACCESS_TOKEN` | Bearer token for TTS (empty if no auth) |
| `TOKEN_CORS_ORIGINS` | Comma-separated allowed origins for the token server |
| `GRAFANA_ADMIN_PASSWORD` | Grafana admin password (observability profile) |

### 3.5 redis/

| Resource | Details |
|---|---|
| PVC | `redis-data`, 5 Gi, `storageClassName: standard-rwo` (GKE SSD), `ReadWriteOnce` |
| Deployment | `redis:7-alpine`, 1 replica, `nodeSelector: stateful-pool`, `runAsUser: 999`, health via `redis-cli ping` |
| Service | ClusterIP, port 6379 |

**Persistence:** RDB snapshot enabled (`--save 60 1`) — Redis writes a point-in-time snapshot to the `/data` PVC if at least one key changed in the last 60 seconds. The GKE persistent disk survives pod restarts, node reboots, and rolling updates. Data is only lost if the PVC itself is deleted.

To resize the volume:
```bash
kubectl patch pvc redis-data -n elm-s2s \
  -p '{"spec":{"resources":{"requests":{"storage":"10Gi"}}}}'
```

### 3.6 livekit/

**Deployment**

| Setting | Value | Reason |
|---|---|---|
| Image | `livekit/livekit-server:latest` | Public Docker Hub — no `imagePullSecrets` needed |
| `replicas` | 1 | `hostNetwork: true` binds ports directly; multiple replicas on the same node would conflict |
| `strategy` | `Recreate` | Ensures clean port release before new pod starts |
| `hostNetwork` | `true` | UDP media ports bypass kube-proxy; required for `use_external_ip: true` to work |
| `dnsPolicy` | `ClusterFirstWithHostNet` | Retains cluster DNS resolution while using host network |
| `nodeSelector` | `general-e-standard-8-nsk` | CPU node pool |
| Config mount | `livekit.yaml` from `s2s-secrets` Secret → `/etc/livekit/livekit.yaml` | Config contains API keys; stored in Secret not ConfigMap |

**Services**

| Service | Type | Ports | Purpose |
|---|---|---|---|
| `livekit-server` | LoadBalancer (TCP) | 7880 (WS), 7881 (RTC-over-TCP) | WebSocket signalling + TCP fallback |
| `livekit-server-udp` | LoadBalancer (UDP) | 50000–50020 (21 individual ports) | UDP media (WebRTC) |

Both use `externalTrafficPolicy: Local` so the GCP passthrough LB only routes to the node running the LiveKit pod.

> **Note:** Get the LiveKit LoadBalancer external IP after first deploy and set `LIVEKIT_PUBLIC_URL` in the secret:
> ```bash
> kubectl get svc livekit-server -n elm-s2s
> ```

### 3.7 agent/

| Resource | Details |
|---|---|
| Deployment | `s2s-agent:latest` from GAR, 1 replica, `nodeSelector: general-e-standard-8-nsk`, `imagePullSecrets: [regcred]` |
| Config | `envFrom: s2s-config` (ConfigMap) + individual `secretKeyRef` from `s2s-secrets` |
| Volumes | `emptyDir` at `/tmp/prom_multiproc` for Prometheus multiprocess mode |
| Ports | 9090 (Prometheus metrics), 8081 (health) |
| Service | ClusterIP, ports 9090 + 8081 |

### 3.8 token-server/

| Resource | Details |
|---|---|
| Deployment | `s2s-token-server:latest` from GAR, 1 replica, `nodeSelector: general-e-standard-8-nsk`, `imagePullSecrets: [regcred]` |
| Config | `envFrom: s2s-config` + secret env vars (API key/secret, public URL, CORS origins) |
| Health | `GET /health` on port 8080 |
| Service | ClusterIP, port 8080, annotation `cloud.google.com/neg: '{"ingress": true}'` (NEG for Ingress) |

### 3.9 demo/

| Resource | Details |
|---|---|
| Deployment | `s2s-demo:latest` from GAR, 1 replica, `nodeSelector: general-e-standard-8-nsk`, `imagePullSecrets: [regcred]` |
| Config | `LIVEKIT_API_KEY`, `LIVEKIT_API_SECRET` from secret; `LIVEKIT_URL` mapped from secret key `LIVEKIT_PUBLIC_URL` (public `wss://` URL for browsers); `AGENT_NAME` from configmap |
| Port | 3000 |
| Service | ClusterIP, port 3000, annotation `cloud.google.com/neg: '{"ingress": true}'` (NEG for Ingress) |

### 3.10 ingress.yaml

| Setting | Value |
|---|---|
| Type | GKE HTTP/S Global Load Balancer |
| Static IP | `elm-s2s-ingress-ip` → `34.54.4.30` |
| Default backend | `s2s-demo:3000` |
| Path `/` | `s2s-demo:3000` |
| Path `/token` | `s2s-token-server:8080` |
| TLS | ManagedCertificate block present but commented out — uncomment after DNS A record is live |

To enable TLS after DNS propagates:
1. Create an A record: `s2s-dev.nusukai.com` → `34.54.4.30`
2. Uncomment the `ManagedCertificate` block in `k8s/ingress.yaml`
3. Uncomment the `networking.gke.io/managed-certificates` and `kubernetes.io/ingress.allow-http: "false"` annotations
4. `kubectl apply -f k8s/ingress.yaml`
5. Wait ~15 minutes for certificate provisioning

---

## 4. Cloud Build

**File:** `cloudbuild.yaml`

**Invoked by** the GitHub Actions `build-and-push` job (or manually via `gcloud builds submit`).

**Machine type:** `E2_HIGHCPU_8` — provides enough CPU for three concurrent Docker builds.

### Build steps (parallel)

```
build-agent  ──┐
               ├──▶ push-agent
               │
build-token-server ──▶ push-token-server
               │
build-demo   ──┘──▶ push-demo
```

All three builds start simultaneously (`waitFor: ['-']`). Each push starts as soon as its corresponding build completes — no serial bottleneck.

### Substitutions

| Variable | Default | Description |
|---|---|---|
| `_REGISTRY` | `me-central2-docker.pkg.dev/researchdeployments/nsk-s2s` | Full registry path |
| `_TAG` | `latest` | Unique build tag set by CI (format: `build-YYYYMMDDHHMMSS-<sha8>`) |

Each image is tagged with both `$_TAG` (pinned version) and `latest` (convenience pointer).

### Manual trigger

```bash
gcloud builds submit \
  --project researchdeployments \
  --region  me-central2 \
  --config  cloudbuild.yaml \
  --substitutions _REGISTRY=me-central2-docker.pkg.dev/researchdeployments/nsk-s2s,_TAG=my-tag
```

### Layer caching

Each build step pulls `--cache-from :latest` before building. On a warm cache (no Dockerfile changes), builds complete in ~2 minutes instead of ~10.

---

## 5. CI/CD Workflow

**File:** `.github/workflows/ci-cd.yml`

Trigger: **manual only** (`workflow_dispatch`) — no push-triggered deploys.

### Jobs

```
validate ──▶ build-and-push ──▶ security-scan ──▶ deploy
                         └────▶ verify-gar-pull (optional)
```

### Job 0 — validate

Runs unless `skip_validate=true`.

- **relaxed mode** (default): only fatal syntax errors (`ruff --select E9,F63,F7,F82`); mypy and pip-audit are non-blocking.
- **strict mode**: full ruff lint, strict mypy, and blocking pip-audit.

Scopes: `agent/` and `token-server/` Python code only.

### Job 1-2 — build-and-push

Authenticates to `researchdeployments` via Workload Identity, submits a Cloud Build job, polls until complete, then outputs the three image references and the computed tag.

Tag format: `build-<YYYYMMDDHHMMSS>-<sha8>`

### Job 3 — verify-gar-pull (optional)

Launches a temporary pod (`gar-pull-test`) in `elm-s2s` to confirm the GKE project can pull the freshly-built image from the `researchdeployments` GAR. Only runs when `verify_pull=true`.

### security-scan

Runs Trivy against the `s2s-agent` image. Non-blocking for DEV (`exit-code: 0`). Only executes Trivy when `run_trivy_scan=true`.

### Job 4 — deploy

1. Gets GKE credentials (`mebnouf@elm.sa` equivalent via `GKE_SA_EMAIL`)
2. Applies `k8s/namespace.yaml`
3. Copies `regcred` from `nlp-rag` if not already present in `elm-s2s`
4. Injects the Kubernetes secret `s2s-secrets` from GitHub Secrets (idempotent: `--dry-run=client -o yaml | kubectl apply -f -`)
5. Patches image tags in the three deployment manifests (`sed`)
6. Applies manifests in dependency order
7. Pins exact image tag with `kubectl set image` on all three deployments
8. Waits for rollout of `s2s-token-server`, `s2s-demo`, `s2s-agent`
9. Smoke tests `GET /health` on `s2s-token-server`
10. Rolls back all three deployments if smoke test fails
11. Commits the updated manifest image tags back to `main`

### Workflow inputs

| Input | Default | Description |
|---|---|---|
| `skip_validate` | `false` | Skip lint/test job entirely |
| `validate_mode` | `relaxed` | `relaxed` = fatal errors only; `strict` = full lint + type check |
| `build` | `true` | Run Cloud Build and push images |
| `verify_pull` | `false` | Run cross-project GAR pull verification pod |
| `deploy` | `true` | Deploy to GKE after build |
| `image_tag` | — | Existing tag to deploy when `build=false` |
| `run_trivy_scan` | `false` | Run Trivy vulnerability scan |

---

## 6. GitHub Secrets Reference

Configure these in **Settings → Secrets and variables → Actions** on the repository.

> **Secret ownership model:**
> CI/CD only needs secrets for its own infrastructure auth (WIF) and email notifications.
> **Application secrets** (LiveKit, STT/LLM/TTS, CORS, Grafana, Milvus) are managed
> exclusively via `k8s/secret.yaml` (gitignored) and applied manually with
> `kubectl apply -f k8s/secret.yaml`. CI/CD never reads or overwrites them.

### GCP authentication (Workload Identity Federation)

> Both service accounts are bound to `IbrahimAlrayes/S2S-orchestrator` on the `main` branch.
> No further GCP-side setup is needed.

| Secret | Exact value |
|---|---|
| `GAR_PROVIDER` | `projects/89433675168/locations/global/workloadIdentityPools/gha-rag-nusuk-pool/providers/gha-rag-nusuk-provider` |
| `GAR_SA_EMAIL` | `gha-cloudbuild-submitter@researchdeployments.iam.gserviceaccount.com` |
| `GKE_PROVIDER` | `projects/184617603524/locations/global/workloadIdentityPools/gha-rag-nusuk-gke-pool/providers/gha-rag-nusuk-gke-provider` |
| `GKE_SA_EMAIL` | `gha-gke-deployer@hajj-umrah-nsk-dev.iam.gserviceaccount.com` |

Roles granted:
- `gha-cloudbuild-submitter` → `roles/cloudbuild.builds.editor` + `roles/cloudbuild.builds.builder` on `researchdeployments`
- `gha-gke-deployer` → `roles/container.developer` on `hajj-umrah-nsk-dev`

### Email notifications (main-notify.yml)

| Secret | Description |
|---|---|
| `MAIL_USERNAME` | Gmail address to send from and receive notifications |
| `MAIL_PASSWORD` | Gmail App Password — generate at [myaccount.google.com/apppasswords](https://myaccount.google.com/apppasswords) (not your login password) |

### Application secrets — managed via k8s/secret.yaml only

These are **not** GitHub secrets. Fill in `k8s/secret.yaml` (from `k8s/secret.yaml.template`) and apply manually:

```bash
cp k8s/secret.yaml.template k8s/secret.yaml
# edit k8s/secret.yaml with real values
kubectl apply -f k8s/secret.yaml
```

| Key in k8s/secret.yaml | Purpose |
|---|---|
| `LIVEKIT_API_KEY` / `LIVEKIT_API_SECRET` | LiveKit server auth |
| `LIVEKIT_PUBLIC_URL` | WebSocket URL for demo frontend (`ws://34.166.12.170:7880`) |
| `livekit.yaml` | Full LiveKit server config (keys block must match API key/secret above) |
| `CUSTOM_STT_URL` / `CUSTOM_STT_ACCESS_TOKEN` | In-cluster AST service |
| `CUSTOM_LLM_URL` / `CUSTOM_LLM_ACCESS_TOKEN` / `CUSTOM_LLM_CLIENT_ID` / `CUSTOM_LLM_CLIENT_SECRET` | In-cluster LLM / RAG service |
| `CUSTOM_TTS_URL` / `CUSTOM_TTS_ACCESS_TOKEN` | In-cluster TTS service |
| `TOKEN_CORS_ORIGINS` | Allowed CORS origins for token server |
| `GRAFANA_ADMIN_PASSWORD` | Grafana dashboard admin password |
| `MILVUS_TOKEN` | Milvus auth token (default `root:Milvus`; only used with `CUSTOM_LLM_PROVIDER=nusuk_rag`) |

---

## 7. First-Time Setup Runbook

Follow these steps once per environment. After this, use the [Routine Deployment Runbook](#8-routine-deployment-runbook).

### Step 1 — Authenticate

```bash
# For GAR (image push / Cloud Build):
gcloud auth login absurahio@gmail.com
gcloud config set project researchdeployments

# For GKE (kubectl):
gcloud auth login mebnouf@elm.sa
gcloud config set project hajj-umrah-nsk-dev
gcloud container clusters get-credentials nsk --region me-central2 --project hajj-umrah-nsk-dev
```

### Step 2 — Create the namespace (already done)

```bash
kubectl apply -f k8s/namespace.yaml
# Expected: namespace/elm-s2s created (or unchanged)
```

### Step 3 — Copy regcred (already done)

```bash
./k8s/setup-gar-pull-access.sh
# Idempotent — skips if regcred already present in elm-s2s
```

### Step 4 — Fill in secret.yaml

```bash
cp k8s/secret.yaml.template k8s/secret.yaml
```

Edit `k8s/secret.yaml` and replace every `REPLACE_WITH_*` placeholder:

- `LIVEKIT_API_KEY` / `LIVEKIT_API_SECRET` — choose any strong key/secret pair
- `livekit.yaml` → update the `keys:` block to match the above
- `CUSTOM_STT_URL` — get the ASR service name: `kubectl get svc -n asr-serving`
- `CUSTOM_LLM_URL` — get the LLM service name: `kubectl get svc -n nlp-rag`
- `CUSTOM_TTS_URL` — get the TTS service name: `kubectl get svc -n elm-tts`
- Leave `LIVEKIT_PUBLIC_URL` as a placeholder for now (fill after Step 8)

### Step 5 — Build and push images

```bash
# Switch to absurahio@gmail.com first
gcloud auth login absurahio@gmail.com

./k8s/build-push.sh
# Builds s2s-agent, s2s-token-server, s2s-demo
# Pushes to me-central2-docker.pkg.dev/researchdeployments/nsk-s2s
```

### Step 6 — Deploy to GKE

```bash
# Switch back to mebnouf@elm.sa
gcloud auth login mebnouf@elm.sa

./k8s/deploy.sh
# Applies manifests in order; waits for rollouts
```

### Step 7 — Wait for LiveKit LoadBalancer IP

```bash
kubectl get svc livekit-server -n elm-s2s --watch
# Wait until EXTERNAL-IP column is populated (usually < 2 minutes)
```

### Step 8 — Update LIVEKIT_PUBLIC_URL

Once the external IP is assigned (e.g. `1.2.3.4`):

```bash
# Edit k8s/secret.yaml
# Set: LIVEKIT_PUBLIC_URL: "wss://1.2.3.4:7880"

kubectl apply -f k8s/secret.yaml

# Restart token-server and demo so they pick up the new value
kubectl rollout restart deployment/s2s-token-server -n elm-s2s
kubectl rollout restart deployment/s2s-demo -n elm-s2s
```

### Step 9 — Verify services are healthy

```bash
kubectl get pods -n elm-s2s
# All pods should be Running / Ready

# Token server health
kubectl exec deployment/s2s-token-server -n elm-s2s -- curl -fsS http://localhost:8080/health

# Agent logs
kubectl logs deployment/s2s-agent -n elm-s2s --tail=50
```

### Step 10 — DNS and TLS (optional but recommended)

1. Add an A record: `s2s-dev.nusukai.com` → `34.54.4.30`
2. Wait for DNS propagation (~5 min typical)
3. Uncomment the `ManagedCertificate` block and associated annotations in `k8s/ingress.yaml`
4. `kubectl apply -f k8s/ingress.yaml`
5. Certificate provisioning takes ~15 minutes

---

## 8. Routine Deployment Runbook

### Via GitHub Actions (preferred)

1. Go to **Actions → CI-CD → Run workflow**
2. Select inputs:

   | Input | Typical value |
   |---|---|
   | `skip_validate` | `false` |
   | `validate_mode` | `relaxed` |
   | `build` | `true` |
   | `deploy` | `true` |
   | All others | defaults |

3. Click **Run workflow**
4. Monitor the run: `validate` → `build-and-push` → `security-scan` → `deploy`
5. On success, the workflow commits the updated image tags to `main`

### Deploy-only (re-deploy an existing tag without rebuilding)

1. Note the tag to deploy (e.g. `build-20260517142300-abc12345`)
2. Run workflow with:
   - `build = false`
   - `image_tag = build-20260517142300-abc12345`
   - `deploy = true`

### Via local scripts (manual fallback)

```bash
# Build and push
gcloud auth login absurahio@gmail.com
./k8s/build-push.sh

# Deploy
gcloud auth login mebnouf@elm.sa
./k8s/deploy.sh

# Or pin a specific tag:
IMAGE_TAG=build-20260517142300-abc12345 ./k8s/deploy.sh
```

---

## 9. Post-Deploy Checklist

```
[ ] kubectl get pods -n elm-s2s          — all pods Running
[ ] kubectl get svc  -n elm-s2s          — LB IPs assigned
[ ] Token server /health returns 200
[ ] Agent logs show "connected to LiveKit"
[ ] Demo frontend loads at http(s)://<ingress-ip or domain>
[ ] Demo can start a voice session (microphone → agent responds)
[ ] Prometheus metrics visible at :9090/metrics on agent pod
[ ] (If TLS) Certificate status Active: kubectl get managedcertificate -n elm-s2s
```

---

## 10. Rollback Procedure

### Automated (CI/CD smoke-test failure)

The deploy job automatically rolls back all three deployments if the `s2s-token-server /health` smoke test fails:

```bash
kubectl rollout undo deploy/s2s-agent        -n elm-s2s
kubectl rollout undo deploy/s2s-token-server -n elm-s2s
kubectl rollout undo deploy/s2s-demo         -n elm-s2s
```

### Manual rollback

```bash
# Rollback a single deployment to the previous ReplicaSet
kubectl rollout undo deploy/s2s-agent -n elm-s2s

# Rollback to a specific revision
kubectl rollout history deploy/s2s-agent -n elm-s2s
kubectl rollout undo deploy/s2s-agent -n elm-s2s --to-revision=<N>

# Pin to an older image tag directly
kubectl set image deploy/s2s-agent \
  s2s-agent=me-central2-docker.pkg.dev/researchdeployments/nsk-s2s/s2s-agent:<old-tag> \
  -n elm-s2s
```

---

## 11. Troubleshooting

### ImagePullBackOff

```bash
kubectl describe pod <pod-name> -n elm-s2s | grep -A5 Events
```

- **`regcred` missing**: `kubectl get secret regcred -n elm-s2s` — if absent, run `./k8s/setup-gar-pull-access.sh`
- **Image not pushed**: verify the image exists in GAR: `gcloud artifacts docker images list me-central2-docker.pkg.dev/researchdeployments/nsk-s2s`

### Agent not connecting to LiveKit

```bash
kubectl logs deployment/s2s-agent -n elm-s2s --tail=100
```

- Check `LIVEKIT_URL` in ConfigMap is `ws://livekit-server:7880`
- Check LiveKit pod is running: `kubectl get pod -n elm-s2s -l app=livekit-server`

### LIVEKIT_PUBLIC_URL not set correctly

Symptom: browser clients cannot establish WebRTC connection.

```bash
kubectl get secret s2s-secrets -n elm-s2s -o jsonpath='{.data.LIVEKIT_PUBLIC_URL}' | base64 -d
# Should print: wss://<livekit-lb-ip>:7880
```

If wrong, update `k8s/secret.yaml` and re-apply, then restart token-server and demo.

### LiveKit UDP media not working

Confirm the UDP LoadBalancer external IP matches the IP LiveKit advertises:

```bash
kubectl get svc livekit-server-udp -n elm-s2s
kubectl logs deployment/livekit-server -n elm-s2s | grep "ICE\|external"
```

If the node has no external IP (Cloud NAT only), set `use_external_ip: false` in `livekit.yaml` inside the secret and set `node_ip: <udp-lb-ip>` explicitly.

### Token server CORS errors

```bash
kubectl get secret s2s-secrets -n elm-s2s -o jsonpath='{.data.TOKEN_CORS_ORIGINS}' | base64 -d
```

Add the frontend origin to `TOKEN_CORS_ORIGINS` in `k8s/secret.yaml`, re-apply, and restart token-server.

### Cloud Build fails

- Check logs in Cloud Console: `https://console.cloud.google.com/cloud-build/builds?project=researchdeployments`
- Common cause: `--cache-from` pull fails on a brand-new image name — safe to ignore, Cloud Build continues without cache on first run.

### CI/CD deploy job fails on `git push`

The commit step rebases on `origin/main` before pushing. If two workflows run in parallel and both try to push, one will fail. Re-run the failed workflow — it will pick up the other's commit and push successfully.
