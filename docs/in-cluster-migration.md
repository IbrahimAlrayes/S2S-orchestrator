# In-Cluster Agent Migration — Powering Nusuk's Voice API

**Status:** Plan, not yet executed
**Owner:** TBD
**Last verified against cluster:** 2026-05-10
**Related:** [eval/EXPERIMENT_LOG.md](../eval/EXPERIMENT_LOG.md) — 2026-05-10 baseline; [eval/network_breakdown.py](../eval/network_breakdown.py) — measurement script

---

## Product framing

The realtime voice agent is being shipped as **part of the Nusuk API product**. Nusuk customers (worldwide) will:

1. `POST dev.nusukai.com/voice/session` (or similar) authenticated with their Nusuk JWT.
2. Receive `{ livekit_url, room_name, participant_token }` in response — minted by `nlp-rag-app` using the LiveKit Server SDK.
3. Connect WebRTC to `wss://<nusuk-project>.livekit.cloud` with that token.
4. LiveKit Cloud dispatches our agent (registered as a worker from the nsk cluster) into the room.
5. Agent does STT → LLM → TTS, audio flows back via LiveKit Cloud's nearest edge SFU.

This framing collapses several earlier complications:

- **No public WebRTC infrastructure for us to operate.** LiveKit Cloud handles SFUs at the edge worldwide.
- **The private GKE cluster is no longer a blocker.** The agent only needs *outbound* connectivity to LiveKit Cloud (single wss:// connection) — Cloud NAT egress handles it. No inbound WebRTC, no public node IPs.
- **No separate token-server** to deploy. Token issuance is one new endpoint in `nlp-rag-app`, same Global ALB, same Nusuk JWT auth.
- **No demo-frontend** in the operated stack. That stays as a sample app for Nusuk's docs/SDK; Nusuk customers bring their own clients.

What you actually deploy from this repo: **just the agent.** One Deployment, one Secret, one ServiceAccount in a new `s2s-agent` namespace.

---

## TL;DR

The agent currently calls Nusuk STT/TTS over the public internet. **85% of STT wall time is network** — not compute. Moving the agent **into the same GKE cluster** as Nusuk eliminates the public-ingress hop on the agent ↔ Nusuk hot path. Agent → STT becomes pod-to-pod over the GKE VPC, sub-millisecond. **Predicted STT warm wall: 568 ms → ~110 ms** (within 25 ms of the 88 ms compute floor).

For the customer-facing media path, the agent connects outbound to **LiveKit Cloud**, which provides global edge SFUs. Nusuk customers connect to their nearest LiveKit Cloud edge with a token minted by `nlp-rag-app`. No customer ever talks to our cluster directly.

This document is the migration plan, with verified facts about the target cluster and concrete YAML to apply.

---

## 1. Why this is the right fix

### 1.1 The latency budget today (warm, measured 2026-05-10)

```
STT wall = 568 ms
├─ TCP RTT to GFE (request, ack)        ~110 ms   ← network
├─ TLS handshake (1 extra RTT, TLS 1.3) ~110 ms   ← amortized when warm
├─ Upload 128 KB body (TCP windowing)   ~150 ms   ← network
├─ Server inference (Triton)              88 ms   ← compute (real)
└─ Response trip back                    ~90 ms   ← network
                                        ──────
                                          85% network, 15% compute
```

### 1.2 Verified cluster topology (kubectl, corrected 2026-05-10)

- **Cluster:** `gke_hajj-umrah-nsk-dev_me-central2_nsk` — GKE in Dammam, Saudi Arabia.

- **Real data path inside the cluster:**

  ```
  dev.nusukai.com (Global ALB ingress, single backend)
        │
        ▼
  nlp-rag/nlp-rag-app  (FastAPI, ClusterIP 10.192.6.244:80 → pod:8080)
        │       │
        │       ├─ /auth/token:    handled internally, returns JWT
        │       ├─ /transcribe:    gRPC → asr-serving/triton:8001  (model = arabic_asr)
        │       └─ /synthesize:    gRPC → elm-tts/triton-server:8711  (model = f5_tts)
  ```

- **`nlp-rag-app` calls Triton servers directly via gRPC** — verified via the `TRITON_ASR_MODEL_NAME` and `ASR_SERVICE_URL` keys in its Secret, the wrapper-api args showing `--triton_url triton-server:8711`, and Triton startup logs.

- **`asr-serving/api` (FastAPI, port 80→8000) and `elm-tts/wrapper-api` (port 80→8080) are NOT in the production path.** They're standalone REST wrappers, likely for direct integration testing or unrelated external clients. The earlier version of this doc was wrong on this point.

- **Triton models loaded:**
  - `asr-serving/triton`: `arabic_asr` (ensemble — what `nlp-rag-app` calls), `parakeet` (nvidia/parakeet-tdt-0.6b-v3), `lid_speechbrain` (language ID). Ports: gRPC `8001`, HTTP `8000`, metrics `8002`.
  - `elm-tts/triton-server`: `f5_tts` (Arabic), `f5_tts_en` (English), `vocoder`. Ports: HTTP `8710`, gRPC `8711`, metrics `8712`.

- **Both Triton servers run on `gke-nsk-gpu-nvidia-l4-x2-min1-nsk-*` GPU nodes (NVIDIA L4 × 2).** Compute is on real GPUs.

- **Inter-namespace distance** — same VPC, same region, sub-millisecond pod-to-pod RTT. The 88 ms STT compute we measured includes everything: Triton inference + nlp-rag-app's Python REST wrapping + small LAN hops.

### 1.3 Why the public ingress is slow

`nlp-rag/nlp-rag-app-ingress-dev` is `class: gce` — a Global External Application Load Balancer at `35.190.5.146`. From a Saudi-Arabia client:

```
hop 5  51.39.229.65         30 ms    ← exit Saudi Arabia (STC)
hop 8  151.248.98.129      ~110 ms   ← +70 ms transit, GFE outside region
hop 12 35.190.5.146        ~110 ms   ← global anycast termination
```

Versus the **regional** LB on the same cluster (`elm-tts/wrapper-api` at `34.166.216.238`, ipinfo confirms Dammam): **24-52 ms ping**. Same cluster, same datacenter, three-times-faster path because TLS terminates regionally.

Network breakdown script: [eval/network_breakdown.py](../eval/network_breakdown.py).

### 1.4 Audience: global users

**The public API is consumed by clients worldwide.** This shapes the architecture decisions:

- **Public ingress stays Global** (`gce` class). Anycast routes each client to their nearest Google PoP, then Google's backbone delivers to me-central2. This is the correct shape for global traffic. A Regional ALB would force every non-Saudi user through Dammam, making them slower. Earlier versions of this doc proposed Regional ALB; that was wrong for this audience and has been removed from the plan.

- **The agent itself stays in me-central2** regardless of user geography. The agent ↔ Nusuk path runs ~50 times per turn (STT + LLM + TTS multiplied by retries and turn detection) and is geographically constrained — it must live near Nusuk's GPU servers. There is no benefit to running multiple agent regions; quite the opposite, it would add cross-region latency to every Triton call.

- **The user ↔ LiveKit SFU media path is a separate problem** that gets harder with global users. WebRTC media doesn't go through HTTP load balancers — it goes peer-to-SFU directly. A single SFU in me-central2 means a user in Tokyo pays ~150 ms RTT to reach it; a user in São Paulo pays ~250 ms. See §10.1 for the SFU placement options.

### 1.5 Two possible fixes, ranked

| | STT warm wall | Effort | Notes |
|---|---|---|---|
| **Run agent in-cluster** | **~100-130 ms** | **2-3 days** | **Primary path.** Eliminates public-ingress hops on the agent ↔ Nusuk hot path entirely. |
| Add streaming STT to Nusuk (gRPC bidi) | ~88 ms perceived | weeks | Upstream change. Or pursue from the agent side as Phase 5 — Triton supports streaming inference natively. |

These compose. The production target is in-cluster + streaming-STT (whether via Phase 5 in this repo or upstream API changes).

---

## 2. Target architecture

```
   Nusuk customer (any region)
        │
        │  1.  POST dev.nusukai.com/voice/session   (Nusuk JWT)
        │      ─→ Global ALB ─→ nlp-rag-app
        │      ←── { livekit_url, room_name, participant_token }
        │          minted by nlp-rag-app via livekit-api SDK
        │
        │  2.  WebRTC connect (TCP+UDP) to wss://<nusuk>.livekit.cloud
        ▼
   ┌──────── LiveKit Cloud ────────┐
   │  multi-region edge SFUs       │  ← managed by LiveKit
   │  customer connects to nearest │
   └───────────────┬───────────────┘
                   │  outbound wss:// (worker registration only)
                   │  no inbound traffic to the nsk cluster
                   ▼
   ┌────────────── nsk cluster (private GKE, me-central2 Dammam) ──────────────┐
   │                                                                            │
   │   agent pod (NEW, in s2s-agent namespace)                                  │
   │      │                                                                    │
   │      │ HTTP REST + Nusuk JWT (Path A)   /   gRPC (Path B, Phase 5)        │
   │      ▼                                                                    │
   │   nlp-rag/nlp-rag-app  (also serves /voice/session for token issuance)    │
   │      ↓ gRPC                                                               │
   │      ├──→ asr-serving/triton:8001    model = arabic_asr                   │
   │      └──→ elm-tts/triton-server:8711  model = f5_tts                      │
   │                                                                            │
   │   agent ──── outbound HTTPS ─────────────────── api.groq.com (LLM)        │
   └────────────────────────────────────────────────────────────────────────────┘
```

### Two paths for the STT/TTS calls (Phase 1-4 vs Phase 5)

Both paths eliminate the public-ingress hop on the agent ↔ Nusuk hot path. They differ in how directly the agent talks to Triton.

### Path A — agent → nlp-rag-app → Triton  (default, Phase 1-4)

The agent's STT/TTS adapters call `nlp-rag-app` via HTTP REST over ClusterIP, exactly as they call `dev.nusukai.com` today. nlp-rag-app dispatches to Triton via gRPC.

**Why Path A first:**

1. **Zero agent code change.** STT/TTS adapters keep speaking REST + Nusuk JWT. Only `.env` URLs change.
2. **Preserves auth boundary.** nlp-rag-app validates the JWT and enforces existing rate limits / observability. The agent stays as untrusted as it is today.
3. **No coordination needed with `asr-serving` team.** nlp-rag-app already has the allow-rule into `asr-serving`; we're not adding new clients to Triton.
4. **Same API surface as Nusuk's external customers.** Whatever `nlp-rag-app` does in `/transcribe` and `/synthesize` (resampling, language routing, telemetry, billing/usage tracking) we automatically inherit.

**What you give up:** one extra hop (~5-15 ms typical for a Python FastAPI handler invoking gRPC). Compute floor for STT stays at ~88 ms (the measured number includes nlp-rag-app overhead).

### Path B — agent → Triton directly (Phase 5, follow-on)

```
                                    ┌──────────────────────────────┐
                                    │  GKE me-central2 (Dammam)    │
                                    │                              │
                                    │   agent (NEW)                │
                                    │      ↓ gRPC                  │
                                    │      ├─→ asr-serving/triton  │  arabic_asr
                                    │      └─→ elm-tts/triton-srv  │  f5_tts
                                    │                              │
                                    │   nlp-rag/nlp-rag-app  ←─── kept for non-agent traffic
                                    └──────────────────────────────┘
```

**What this requires:**

1. **Triton gRPC client in the agent.** Add `tritonclient[grpc]` to `agent/requirements.txt`. Replace `CustomSTTAdapter`'s `_recognize_impl` HTTP POST with a Triton `infer()` call against `model_name='arabic_asr'`. Same surgery for TTS.
2. **NetworkPolicy in `asr-serving`** allowing ingress from `s2s-agent` namespace to `triton:8001`. Coordination with the asr-serving team. Three-line YAML — see §6.4.
3. **Replicate any preprocessing nlp-rag-app does.** Audio resampling, the language-ID hop via `lid_speechbrain` if it routes, and any post-processing on the transcript. We need to read nlp-rag-app's `/transcribe` handler to know what to keep.
4. **Drop JWT for the in-cluster path** (intra-cluster, NetworkPolicy enforces who can connect — JWT becomes redundant overhead). Keep the JWT path for external clients.

**Expected gain over Path A:** another 5-15 ms saved on STT, plus access to Triton's gRPC streaming features if the model configs support it (potentially the path to "perceived ~88 ms" without waiting on a separate streaming-STT API to be built).

**Path B is optional.** Path A is sufficient to declare success against the targets in §8 if the goal is "close to compute floor."

### Other architecture decisions (apply to both paths)

1. **LiveKit Cloud handles the SFU layer, not us.** Per the product framing: customer ↔ SFU media goes through LiveKit Cloud's global edges; the agent connects to LiveKit Cloud as an outbound worker via wss://. Practical consequences:
   - The nsk cluster (private GKE) needs only outbound egress to `*.livekit.cloud:443` — already permitted by Cloud NAT.
   - No `livekit-server` deployment in our cluster, no `hostNetwork` requirement, no public node IPs needed.
   - No multi-region operations on our side as Nusuk grows internationally — LiveKit Cloud handles that.

2. **Token issuance is folded into nlp-rag-app**, not a separate token-server we deploy. Add an endpoint (e.g. `POST /voice/session`) that uses the [LiveKit Server SDK](https://pypi.org/project/livekit-api/) to mint a participant token and create a `RoomConfiguration` with `agents: [{ agentName: "nusuk-agent" }]`. Same Global ALB, same Nusuk JWT auth as the rest of the API. This is a small change to nlp-rag-app, not part of this repo.

3. **No Redis dependency on our side.** Originally needed for self-hosted LiveKit's room state. With LiveKit Cloud, that state lives at LiveKit. Drop redis from the deploy unless the agent grows its own state requirements.

4. **No demo-frontend deployment.** Customer apps (browsers, mobile, server SDKs) connect directly to LiveKit Cloud with the token they get from `/voice/session`. The Next.js app in `demo/` stays as an internal sample / SDK reference but isn't operated as part of Nusuk's product.

5. **Groq LLM stays external** until Nusuk `/chat/stream` is fixed upstream. Groq's measured TTFT is 199 ms warm with only 26% network — it's not the bottleneck.

---

## 3. Cluster facts (verified, 2026-05-10)

| Aspect | Value | Source |
|---|---|---|
| Cluster | `gke_hajj-umrah-nsk-dev_me-central2_nsk` | `kubectl config current-context` |
| Region | `me-central2` (Dammam, SA) | cluster name + ipinfo on 34.166.216.238 |
| General-pool nodes | 3 × `e2-standard-8`, currently at 3-4% CPU / 17-28% memory | `kubectl top nodes` |
| GPU nodes | 3 × `gke-nsk-gpu-nvidia-l4-x2-min1-nsk` (NVIDIA L4 × 2) | `kubectl get nodes -o wide` |
| Image registry | `me-central2-docker.pkg.dev/researchdeployments/<repo>/<image>:build-<ts>` | existing pod images |
| ServiceAccount for image pull | `regpuller-sa` | `nlp-rag-app` deployment |
| Standard security context | `runAsNonRoot: true`, `runAsUser: 1000`, `readOnlyRootFilesystem: true`, drop `ALL`+`NET_RAW`, `seccompProfile: RuntimeDefault` | `nlp-rag-app` deployment |
| Standard scheduling | `nodeAffinity` to `general-e-standard-8-nsk` pool, `podAntiAffinity` (host), topology spread (host + zone) | `nlp-rag-app` deployment |
| Resource pattern (small service) | `requests: 2 cpu / 4 Gi`, `limits: 5 cpu / 5 Gi` (nlp-rag-app — will likely shrink for agent) | `nlp-rag-app` deployment |

### NetworkPolicy state

- **`asr-serving`** — `default-deny-ingress` + `allow-from-nlp-rag` only (port 8000 on `app=api`, port 8001 on `app=triton`). **Direct calls from a new namespace are blocked.**
- **`elm-tts`** — no NetworkPolicies. Open to any namespace.
- **`nlp-rag`** — milvus/etcd-internal policies only. `nlp-rag-app` is reachable from any namespace.

**Implications:**

- **For Path A** (agent → nlp-rag-app): no NetworkPolicy changes needed in `asr-serving`. nlp-rag-app already has the allow-rule and we're not adding new direct clients to Triton.
- **For Path B** (agent → Triton directly): need to add `allow-from-s2s-agent` to `asr-serving` (port 8001 on `app=triton`). `elm-tts` has no policies, so direct access works without new rules. See §6.4 for the YAML.

---

## 4. Implementation plan

### Phase 0 — pre-reqs (~half day)

- [ ] Confirm push access to `me-central2-docker.pkg.dev/researchdeployments` (or get a new repo in same project).
- [ ] Identify owner of `nlp-rag/nlp-rag-app-ingress-dev` for any cert/DNS coordination later.
- [ ] Pick the namespace name: **`s2s-agent`** (proposed).
- [ ] Decide whether dev-cluster only, or push the same manifests through to prod (`gke_hajj-umrah-nsk-prod_me-central2_nsk` — visible in `kubectl config get-contexts`).

### Phase 1 — image pipeline (~1 day)

- [ ] Add an amd64 build step for `agent/`. Existing `agent/Dockerfile` works; just need `docker buildx build --platform linux/amd64` + push to artifact registry. (No token-server build — that capability moves into nlp-rag-app.)
- [ ] Tag convention: match cluster norm `build-YYYYMMDDHHMMSS-<short-sha>` so deploy pipelines can reference by tag.
- [ ] Verify image runs on the cluster's container runtime (containerd 2.1.5, GKE 1.34) — should be a no-op since the agent already runs in vanilla Python 3.11 on Debian.

### Phase 2 — manifests (~1 day)

Build a Helm chart or kustomize overlay. The deployable surface is intentionally tiny — no token-server, no livekit-server, no redis, no demo-frontend (all moved to LiveKit Cloud or nlp-rag-app):

```
deploy/
├── chart/                       (or kustomize/)
│   ├── values-dev.yaml
│   ├── values-prod.yaml
│   └── templates/
│       ├── namespace.yaml
│       ├── serviceaccount.yaml
│       ├── secret-agent-env.yaml
│       ├── deployment-agent.yaml
│       └── networkpolicy-agent-egress.yaml
```

See [§6 concrete YAML](#6-concrete-yaml-snippets) below for the agent Deployment skeleton.

### Phase 3 — dev deploy + smoke test (~half day)

- [ ] `kubectl apply` to dev cluster.
- [ ] From a debug pod in `s2s-agent` namespace, curl test:
  ```
  curl -X POST http://nlp-rag-app.nlp-rag.svc.cluster.local/auth/token \
    -H 'Content-Type: application/json' \
    -d '{"client_id":"test@elm.sa","client_secret":"<secret>","user_id":"test@elm.sa"}'
  ```
  Expect: `200 OK` with JWT, < 50 ms total time.
- [ ] Run [eval/quick_speed.py](../eval/quick_speed.py) FROM the agent pod (kubectl exec) against in-cluster URLs. Expect STT warm wall < 130 ms.

### Phase 4 — validation + cutover (~1 day)

- [ ] Re-run the 20-file eval from inside the cluster. Append a new dated section to `eval/EXPERIMENT_LOG.md` with the in-cluster numbers.
- [ ] Confirm the agent's outbound LiveKit Cloud worker registration succeeds (look for `registered worker` log line on the wss:// connection to `*.livekit.cloud`).
- [ ] Coordinate with the nlp-rag-app team: their `/voice/session` endpoint must be live and minting tokens with a `RoomConfiguration.agents` entry naming the same `agentName` the agent is registered with (default `nusuk-agent`).
- [ ] End-to-end test: hit `POST dev.nusukai.com/voice/session` with a valid Nusuk JWT, take the returned `livekit_url` + `participant_token`, connect from a LiveKit JS test client, verify the agent joins the room and audio flows in both directions.
- [ ] Decommission local `docker compose` for production traffic. (The local stack stays useful for agent-code dev iteration — see §7 risks.)

---

## 5. `.env` mapping for in-cluster

Replace these lines in the in-cluster Secret (formerly `.env`):

| Old | New |
|---|---|
| `CUSTOM_STT_URL=https://dev.nusukai.com` | `CUSTOM_STT_URL=http://nlp-rag-app.nlp-rag.svc.cluster.local` |
| `CUSTOM_TTS_URL=https://dev.nusukai.com` | `CUSTOM_TTS_URL=http://nlp-rag-app.nlp-rag.svc.cluster.local` |
| `CUSTOM_LLM_URL=https://api.groq.com/openai/v1` | unchanged (Groq external) |
| `LIVEKIT_URL=ws://livekit-server:7880` | `LIVEKIT_URL=wss://<nusuk-project>.livekit.cloud` (LiveKit Cloud) |
| `LIVEKIT_PUBLIC_URL=ws://localhost:7880` | (drop — no longer relevant; nlp-rag-app's `/voice/session` issues this URL to clients) |
| `LIVEKIT_API_KEY=devkey` / `LIVEKIT_API_SECRET=secret` | Real LiveKit Cloud project credentials. Same key+secret must also be in nlp-rag-app's secret so it can mint participant tokens. |
| `PROMETHEUS_MULTIPROC_DIR=/tmp/prom_multiproc` | unchanged |
| `AGENT_METRICS_PORT=9090` | unchanged (scrape via GKE Managed Prometheus) |

Notes:

- HTTP (not HTTPS) intra-cluster is fine — same VPC, same trust boundary as the existing service-to-service calls. Keeps the agent off the TLS hot path entirely.
- `CUSTOM_STT_PROVIDER=nusuk` and `CUSTOM_TTS_PROVIDER=nusuk` stay the same — the agent still appends `/transcribe` and `/synthesize` correctly to the new URL.
- The Nusuk JWT is still required (the gateway validates it). `NusukTokenManager` works unchanged because it just hits `${base_url}/auth/token` — which now resolves to `nlp-rag-app` directly, saving the public-ingress hop on auth too.

---

## 6. Concrete YAML snippets

### 6.1 Namespace + ServiceAccount

```yaml
apiVersion: v1
kind: Namespace
metadata:
  name: s2s-agent
  labels:
    pod-security.kubernetes.io/enforce: restricted
---
apiVersion: v1
kind: ServiceAccount
metadata:
  name: regpuller-sa
  namespace: s2s-agent
  # configure imagePullSecret or Workload Identity to pull from
  # me-central2-docker.pkg.dev/researchdeployments/...
  # Match the convention used in nlp-rag namespace.
```

### 6.2 Secret (replaces `.env`)

```yaml
apiVersion: v1
kind: Secret
metadata:
  name: s2s-agent-env
  namespace: s2s-agent
type: Opaque
stringData:
  # — provider URLs (in-cluster) —
  CUSTOM_STT_URL: "http://nlp-rag-app.nlp-rag.svc.cluster.local"
  CUSTOM_STT_PROVIDER: "nusuk"
  CUSTOM_TTS_URL: "http://nlp-rag-app.nlp-rag.svc.cluster.local"
  CUSTOM_TTS_PROVIDER: "nusuk"
  CUSTOM_LLM_URL: "https://api.groq.com/openai/v1"
  CUSTOM_LLM_PROVIDER: "openai"
  CUSTOM_LLM_MODEL: "openai/gpt-oss-120b"
  CUSTOM_LLM_REASONING_EFFORT: "low"
  CUSTOM_LLM_MAX_TOKENS: "768"
  CUSTOM_LLM_TEMPERATURE: "0.2"
  # — credentials —
  GROQ_API_KEY: "<from secret manager>"
  CUSTOM_LLM_CLIENT_ID: "test@elm.sa"
  CUSTOM_LLM_CLIENT_SECRET: "<from secret manager>"
  # — livekit cloud (outbound worker registration) —
  LIVEKIT_URL: "wss://<nusuk-project>.livekit.cloud"
  LIVEKIT_API_KEY: "<from LiveKit Cloud project — same key in nlp-rag-app's secret>"
  LIVEKIT_API_SECRET: "<from LiveKit Cloud project — same secret in nlp-rag-app's secret>"
  # — agent —
  AGENT_NAME: "nusuk-agent"
  AGENT_MAX_JOBS_PER_WORKER: "10"
  AGENT_METRICS_PORT: "9090"
  AGENT_SYSTEM_PROMPT_FILE: "/app/system_prompt_rag.txt"
  PROMETHEUS_MULTIPROC_DIR: "/tmp/prom_multiproc"
  HF_HUB_OFFLINE: "1"
```

### 6.3 Agent Deployment

```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: agent
  namespace: s2s-agent
spec:
  replicas: 2                # scale by AGENT_MAX_JOBS_PER_WORKER × replicas
  revisionHistoryLimit: 2
  selector:
    matchLabels: { app: agent }
  template:
    metadata:
      labels: { app: agent }
    spec:
      serviceAccountName: regpuller-sa
      automountServiceAccountToken: false
      affinity:
        nodeAffinity:
          requiredDuringSchedulingIgnoredDuringExecution:
            nodeSelectorTerms:
              - matchExpressions:
                  - key: cloud.google.com/gke-nodepool
                    operator: In
                    values: [general-e-standard-8-nsk]
        podAntiAffinity:
          preferredDuringSchedulingIgnoredDuringExecution:
            - weight: 100
              podAffinityTerm:
                labelSelector: { matchLabels: { app: agent } }
                topologyKey: kubernetes.io/hostname
      topologySpreadConstraints:
        - labelSelector: { matchLabels: { app: agent } }
          maxSkew: 1
          topologyKey: kubernetes.io/hostname
          whenUnsatisfiable: ScheduleAnyway
      securityContext:
        runAsNonRoot: true
        runAsUser: 1000
        seccompProfile: { type: RuntimeDefault }
      containers:
        - name: agent
          image: me-central2-docker.pkg.dev/researchdeployments/<repo>/s2s-agent:<tag>
          imagePullPolicy: IfNotPresent
          command: ["python", "agent.py", "start"]
          envFrom:
            - secretRef: { name: s2s-agent-env }
          ports:
            - containerPort: 8081  # health
            - containerPort: 9090  # prometheus
          resources:
            requests: { cpu: "1000m", memory: "2Gi" }
            limits:   { cpu: "2000m", memory: "3Gi" }
          securityContext:
            allowPrivilegeEscalation: false
            capabilities: { drop: ["ALL", "NET_RAW"] }
            readOnlyRootFilesystem: true
          volumeMounts:
            - { name: tmp,    mountPath: /tmp }
            - { name: prom,   mountPath: /tmp/prom_multiproc }
          startupProbe:
            httpGet: { path: /, port: 8081 }
            failureThreshold: 30
            periodSeconds: 5
          readinessProbe:
            httpGet: { path: /, port: 8081 }
            periodSeconds: 10
          livenessProbe:
            tcpSocket: { port: 8081 }
            periodSeconds: 20
      volumes:
        - { name: tmp,  emptyDir: {} }
        - { name: prom, emptyDir: { medium: Memory } }
```

No additional in-cluster manifests for LiveKit / redis / token-server — those responsibilities now live with LiveKit Cloud and nlp-rag-app respectively.

### 6.4 NetworkPolicies

#### Path A — egress from agent (no asr-serving change)

The agent only needs to talk to:
- `nlp-rag/nlp-rag-app` (auth, STT, TTS via REST)
- `s2s-agent/livekit` and `s2s-agent/redis` (own namespace)
- `api.groq.com` (external — for now)
- DNS

```yaml
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: agent-egress
  namespace: s2s-agent
spec:
  podSelector: { matchLabels: { app: agent } }
  policyTypes: [Egress]
  egress:
    - to:
        - namespaceSelector:
            matchLabels: { kubernetes.io/metadata.name: nlp-rag }
          podSelector: { matchLabels: { app: nlp-rag-app } }
      ports: [{ port: 8080, protocol: TCP }]
    - to: [{ podSelector: {} }]   # within own namespace (currently empty besides agent itself; future-proofing)
    - to:
        - namespaceSelector:
            matchLabels: { kubernetes.io/metadata.name: kube-system }
          podSelector: { matchLabels: { k8s-app: kube-dns } }
      ports: [{ port: 53, protocol: UDP }]
    - to:
        - ipBlock: { cidr: 0.0.0.0/0, except: [10.0.0.0/8, 192.168.0.0/16, 172.16.0.0/12] }
      ports: [{ port: 443, protocol: TCP }]   # Groq + LiveKit Cloud (wss outbound)
```

The single 443/TCP rule covers both `api.groq.com` and `*.livekit.cloud:443`. If you want tighter egress, swap the broad `ipBlock` for an FQDN-based allowlist via Cloud Armor or Cloud NAT egress rules (cluster-level, not NetworkPolicy — K8s NetworkPolicy only matches IP CIDRs, not hostnames).

(No new policy needed in `asr-serving` because we route via `nlp-rag-app`.)

#### Path B addition — direct ingress into Triton (asr-serving + elm-tts)

When we move to Path B, two extra rules:

```yaml
# Add to asr-serving namespace (existing default-deny-ingress is in place;
# this is the third allow-rule, alongside allow-from-nlp-rag).
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: allow-from-s2s-agent
  namespace: asr-serving
spec:
  podSelector: { matchLabels: { app: triton } }
  policyTypes: [Ingress]
  ingress:
    - from:
        - namespaceSelector:
            matchLabels: { kubernetes.io/metadata.name: s2s-agent }
      ports: [{ port: 8001, protocol: TCP }]   # Triton gRPC
---
# elm-tts has no NetworkPolicies today, so a defensive policy is optional.
# If elm-tts adopts default-deny-ingress later, mirror the above pattern
# for triton-server:8711.
```

And update the agent's egress policy to allow:

```yaml
    - to:
        - namespaceSelector:
            matchLabels: { kubernetes.io/metadata.name: asr-serving }
          podSelector: { matchLabels: { app: triton } }
      ports: [{ port: 8001, protocol: TCP }]
    - to:
        - namespaceSelector:
            matchLabels: { kubernetes.io/metadata.name: elm-tts }
          podSelector: { matchLabels: { app: triton-server } }
      ports: [{ port: 8711, protocol: TCP }]
```

---

## 7. Risks and rollback

| Risk | Mitigation |
|---|---|
| `nlp-rag-app` becomes a single point of failure for the agent | Already has 5 replicas. Add HPA if traffic warrants. Long-term option (Phase 5): agent hits Triton directly. |
| LiveKit Cloud outage / connectivity loss | Agent worker reconnect logic should handle transient drops; LiveKit Cloud has its own SLO. Customer-facing degradation falls back to non-voice product surface. |
| LiveKit Cloud cost at scale | Per-participant-minute pricing — model the run rate at projected user volumes before broad launch. If cost outpaces revenue, fallback option is GCE VMs with public IPs running self-hosted livekit-server (loses multi-region edge benefit). |
| Audio data transits LiveKit Cloud (third-party) | Validate against any data-residency requirements for Nusuk customers. If HIPAA/sovereignty applies, self-hosted SFU may be required. |
| Cluster pull access for our image | Use `regpuller-sa` (existing pattern). |
| Slower agent dev iteration than docker-compose | Keep `docker compose` working for local code iteration. Cloud deploy is for prod traffic, not for dev edit-loop. Maintains two deploy modes — accept that cost. |
| Multiproc Prometheus dir on `emptyDir` (memory-backed) | Keep `medium: Memory`. Pod-level scrape via GKE Managed Prometheus. |
| `nlp-rag-app` change required (the new `/voice/session` endpoint) | Coordinate with that team. Until they ship it, the agent still works against externally-issued LiveKit tokens — useful for staging tests. |

**Rollback** is one `kubectl delete deployment/agent -n s2s-agent`. The agent stops registering as a worker; LiveKit Cloud reports no available agents to dispatch; `/voice/session` calls succeed but rooms have no agent. Customer-facing impact is "voice unavailable," not a broken cluster. Nusuk's existing REST APIs (`/transcribe`, `/synthesize`) are untouched throughout.

---

## 8. Success criteria

All measured warm, p50 across 20 clips, run from a pod inside the agent's namespace.

| Metric | Today | Path A target (Phase 4) | Path B target (Phase 5) |
|---|---:|---:|---:|
| STT wall | 568 ms | **< 130 ms** | **< 110 ms** (or < 100 ms perceived if streaming) |
| Auth POST | 199 ms | < 30 ms | n/a (drop JWT in-cluster) |
| TTS TTFA | 203 ms | < 50 ms | < 40 ms |
| LLM TTFT (Groq) | 199 ms | unchanged (external) | unchanged |
| **E2E first audio** | 990 ms | **< 250 ms** | **< 220 ms** |

If the Path A numbers don't hit these, the bottleneck has shifted — investigate compute scaling (Triton replicas, GPU utilization) before declaring the migration done.

Path A is sufficient to declare the migration goal achieved. Path B is an additional optimization that can be deferred indefinitely without losing the main win.

---

## 9. Phase 5 — Path B: bypass nlp-rag-app, talk to Triton directly

Once Path A is live and validated against the Phase 4 success criteria, Phase 5 collapses the remaining hop. The win on top of Path A is small in absolute terms (~5-15 ms saved per turn), but it also unlocks **Triton gRPC streaming inference**, which is the path to "perceived ~88 ms" without any upstream changes to a streaming-STT REST API.

**Pre-reqs (do these in parallel with Phase 4):**

1. Read `nlp-rag-app`'s `/transcribe` and `/synthesize` handler source. Document exactly what preprocessing/postprocessing happens. Candidates we already suspect: audio resampling to whatever rate `arabic_asr` expects, language-ID routing via `lid_speechbrain`, transcript cleanup. Whatever it does, the agent must replicate or replace.
2. Get `tritonclient[grpc]==2.40.0` (or whatever the current cluster version supports — check `kubectl describe pod -n asr-serving deploy/triton | grep Image` for the Triton server version, then match the client semver).
3. Coordinate the asr-serving NetworkPolicy addition with that team. Three-line YAML, but it's their namespace.

**Implementation outline:**

- Add a new `CustomSTTAdapter` provider, e.g. `provider=triton_direct`, that uses `tritonclient.grpc.aio.InferenceServerClient`. The model name (`arabic_asr`), input tensor names, and shape conventions come from `kubectl logs -n asr-serving deploy/triton | grep config` or by querying the model config endpoint at runtime.
- Same for TTS: a `provider=triton_direct` for `CustomTTS` calling `f5_tts` on `elm-tts/triton-server:8711`.
- Switch `.env` URLs:
  ```
  CUSTOM_STT_PROVIDER=triton_direct
  CUSTOM_STT_URL=triton.asr-serving.svc.cluster.local:8001
  CUSTOM_STT_MODEL=arabic_asr
  CUSTOM_TTS_PROVIDER=triton_direct
  CUSTOM_TTS_URL=triton-server.elm-tts.svc.cluster.local:8711
  CUSTOM_TTS_MODEL=f5_tts
  ```
- Drop the `NusukTokenManager` from the in-cluster path — JWT auth becomes redundant when NetworkPolicy enforces who can connect. Keep it for any remaining external code paths.

**Streaming STT bonus:** Triton supports gRPC streaming inference (`stream_infer`). If `arabic_asr`'s model config has `sequence_batching` enabled, the agent can send audio chunks as VAD captures them and receive partial transcripts incrementally. This is the architectural path to "perceived ~88 ms turn-end latency" that I previously described as needing weeks of upstream work — it can be done agent-side in Phase 5 if the model config supports it. Verify with:

```bash
kubectl logs -n asr-serving deploy/triton --tail=200 | grep -A2 -i "arabic_asr\|sequence"
```

## 10. LiveKit Cloud setup (the SFU layer)

**Decision: use LiveKit Cloud** ([livekit.io/cloud](https://livekit.io/cloud)) for the SFU. With global Nusuk customers + private nsk cluster, this is the only option that gives multi-region edge SFUs without us operating multi-region infrastructure.

### Setup checklist (one-time, ~half day)

- [ ] Create a LiveKit Cloud project for Nusuk. Capture the project URL (`wss://<project>.livekit.cloud`), API key, and API secret.
- [ ] Store the credentials in two places (same key/secret in both):
  - `s2s-agent/agent-secret` in the nsk cluster — agent uses them for worker registration.
  - `nlp-rag/nlp-rag-app-secret` (or wherever nlp-rag-app reads its env) — nlp-rag-app uses them to mint participant tokens.
- [ ] Verify outbound egress from the nsk cluster reaches `*.livekit.cloud:443`. (Default Cloud NAT egress permits HTTPS to the internet.)
- [ ] Configure LiveKit Cloud project settings: agent dispatch policy, max concurrent rooms (start low, raise as needed), region preferences if you want to constrain edge selection.

### Fallback options (only if LiveKit Cloud is rejected)

If cost, compliance, or vendor concerns rule out LiveKit Cloud:

- **GCE VMs with public IPs in each target region** — run `livekit-server` directly on VMs. Use the official [livekit-helm](https://github.com/livekit/livekit-helm) values as a starting config. For multi-region: configure as a [distributed LiveKit cluster](https://docs.livekit.io/transport/self-hosting/distributed/) sharing a Redis backbone. You operate VMs and Redis in every region you care about — significant ongoing burden.
- **Separate non-private GKE cluster** dedicated to LiveKit, one per region — same network shape as VMs but adds K8s overhead. Not recommended unless you already have strong K8s ops standardization.

Both fallbacks lose LiveKit Cloud's automatic edge-region selection — you'd need to manually deploy and load-balance per region. That's a substantial operational delta from the recommended path. Treat as last resort.

## 11. Other future work

- **Nusuk LLM revival:** when `/chat/stream` is fixed upstream, swap Groq for Nusuk LLM via `CUSTOM_LLM_PROVIDER=nusuk` — the in-cluster URL is the same `nlp-rag-app` service.
- **Edge expansion of Nusuk itself:** if Nusuk eventually grows beyond me-central2 (e.g. mirror clusters in EU/US/SEA), the agent should follow into each region and pick the nearest by zone label. Same in-cluster pattern, multiplied. Today, the agent stays single-region in me-central2.

---

## 12. Verification appendix

Commands used to gather the cluster facts in this doc (read-only, safe to repeat):

```bash
kubectl config current-context
kubectl get pods -A | grep -iE "nusuk|stt|tts|asr|rag"
kubectl get nodes -o wide
kubectl get svc -A | grep -iE "nusuk|stt|tts|asr|rag"
kubectl top nodes
kubectl get networkpolicy -A
kubectl get networkpolicy -n asr-serving -o yaml
kubectl get deploy -n nlp-rag nlp-rag-app -o yaml
kubectl get ingress -A

# Private-cluster check (critical for LiveKit placement decision)
kubectl get nodes -o jsonpath='{range .items[*]}{.metadata.name}{"\t"}externalIP={.status.addresses[?(@.type=="ExternalIP")].address}{"\n"}{end}'
kubectl get nodes -o jsonpath='{.items[0].metadata.labels}' | python3 -m json.tool | grep -i private
# Expected on a private cluster: externalIP=<empty>, label "cloud.google.com/private-node": "true"

# Triton model + protocol verification
kubectl describe deploy -n asr-serving triton    # init-container reveals which models download
kubectl logs -n asr-serving deploy/triton --tail=80 | grep -iE "model|grpc|http"
kubectl describe deploy -n elm-tts wrapper-api   # args show --triton_url + AR_MODEL/EN_MODEL env
```

External RTT verification:

```bash
ping -c 5 dev.nusukai.com                  # global LB, ~110 ms p50 from SA
ping -c 5 34.166.216.238                   # regional LB in cluster, ~36 ms p50 from SA
dig +short dev.nusukai.com                 # → 35.190.5.146 (anycast)
curl -s https://ipinfo.io/34.166.216.238   # → Dammam, SA
traceroute -n -w 1 dev.nusukai.com         # +70 ms transit between hop 5 (SA exit) and hop 8 (GFE)
```

Per-stage breakdown:

```bash
.venv/bin/python eval/network_breakdown.py
```
