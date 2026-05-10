# Architecture

![System diagram](diagrams/system.png)

> Source: [diagrams/system.excalidraw](diagrams/system.excalidraw) — open in [excalidraw.com](https://excalidraw.com) or the VS Code Excalidraw extension. The PNG above is a pre-rendered snapshot; regenerate with `python3 scripts/render_excalidraw.py docs/diagrams/system.excalidraw docs/diagrams/system.png` after edits. The ASCII below is a quick reference.

## Component Diagram

```
Browser (WebRTC)
      │
      │ wss://  (signaling + media)
      ▼
┌─────────────────────────────────┐
│         LiveKit Server          │  ← SFU: routes audio between browser and agent
│  port 7880 (WS) / 7881 (TCP)   │
│  ports 50000–50100 (UDP/WebRTC) │
└──────────────┬──────────────────┘
               │ room events + audio frames
               ▼
┌─────────────────────────────────┐
│       Agent Worker Pool         │  ← Python, one process per room
│       (livekit-agents SDK)      │
│                                 │
│  VAD (Silero, preloaded)        │
│  STT adapter  ──► Nusuk /transcribe (shared NusukTokenManager)
│  LLM adapter  ──► Groq /chat/completions (gpt-oss-120b, OpenAI-compat) │
│  TTS adapter  ──► Nusuk /synthesize  (streaming PCM)                   │
│  Shared httpx.AsyncClient(http2=True) — one per worker                 │
└─────────────────────────────────┘

┌─────────────────────────────────┐
│         Token Server            │  ← FastAPI, mints LiveKit JWTs
│         port 8080               │
└─────────────────────────────────┘

┌─────────────────────────────────┐
│         Redis                   │  ← LiveKit state store (internal only)
│         port 6379               │
└─────────────────────────────────┘

┌─────────────────────────────────┐   (optional, --profile demo)
│       Demo Frontend             │  ← Next.js 15, App Router
│       port 3000                 │
│                                 │
│  /          LiveKit demo        │
│  /ptt       Push-to-talk demo   │
│  /api/token  token proxy        │
│  /api/ptt/*  PTT API proxies    │
└─────────────────────────────────┘
```

## Data Flow — LiveKit Realtime Path

```
1.  Browser calls GET /token (token-server)
      → receives {token, url, room, identity}

2.  Browser connects to LiveKit server via WebRTC (wss://livekit-server:7880)
      → LiveKit dispatches a job to the agent worker pool

3.  Agent worker joins the room
      → Agent sends greeting audio via TTS
      → AgentSession starts listening for user audio

4.  User speaks  →  VAD segments audio  →  STT adapter posts WAV to Nusuk /transcribe
      → returns {transcription_text, language}

5.  Transcript → LLM adapter (Groq /chat/completions, OpenAI-compat SSE)
      → SSE tokens stream back; reasoning fields ignored, legacy <think> blocks stripped
      → AgentSession buffers by sentence (.,،,؟,!,\n)
      → Each complete sentence triggers TTS immediately
      → preemptive_tts=True also fires speculative TTS during endpointing window

6.  TTS adapter posts text to Nusuk /synthesize
      → streams WAV (chunked) → parses RIFF header from prefix → pushes PCM frames as they arrive
      → AgentSession publishes frames to LiveKit room
      → Browser receives and plays audio
```

## Data Flow — Push-to-Talk Path

```
1.  User holds button  →  MediaRecorder captures audio (webm/opus)

2.  On release: browser POSTs blob to /api/ptt/transcribe
      → Next.js proxy forwards to ASR service with Bearer token
      → Returns {transcription_text, language}

3.  Browser POSTs {query, session_id} to /api/ptt/chat
      → Next.js proxy prepends NUSUK_QUERY_PREFIX
      → Fetches Nusuk token (server-side, cached in module scope)
      → POSTs to Nusuk /chat (non-streaming)
      → Returns {response, session_id}

4.  Browser POSTs {text} to /api/ptt/tts
      → Next.js proxy strips markdown from text
      → POSTs to TTS wrapper service
      → Returns WAV audio buffer
      → Browser plays via Audio(URL.createObjectURL(blob))
```

## Machine Split (Recommended for Production)

```
CPU Machine
───────────────────────────────────────────
livekit-server
agent workers
token-server
redis
demo-frontend (optional)

External
───────────────────────────────────────────
STT   Nusuk https://dev.nusukai.com   POST /transcribe
LLM   Groq  https://api.groq.com/openai/v1   POST /chat/completions  (gpt-oss-120b)
TTS   Nusuk https://dev.nusukai.com   POST /synthesize
```

All model inference is handled by external APIs. No GPU machine is needed in this repo. See [troubleshooting.md](troubleshooting.md#livekit-public-ip) for the public IP requirement.

## GKE Sizing (Middle East Regions)

This repo is **orchestration-only** — no GPU inference. The binding resources are:
- **RAM**: each Python agent worker loads PyTorch + Silero VAD once (~700–900 MB fixed per process, shared across 10 sessions via `AGENT_MAX_JOBS_PER_WORKER`)
- **CPU**: Silero VAD runs ~1–3 ms inference per 50 ms audio frame → ~0.03–0.07 vCPU per concurrent session on average (sessions are ~85% I/O-idle waiting on Nusuk API)

**Recommended VM family: `n2d-standard-*`** (General Purpose, AMD EPYC, 8 GB/vCPU).
No GPU needed. C2/C3 compute-optimized is overkill for 1:1 audio rooms. Memory-optimized (M-series) is unnecessary.

### Node pool layout

LiveKit requires host networking — only **one pod per node**. Use two node pools:

```
Node Pool: "livekit"  (1–2 nodes, fixed)
  n2d-standard-4  →  livekit-server + redis
  Handles hundreds of 1:1 audio rooms; SFU is not the bottleneck here.

Node Pool: "agent"  (autoscales)
  n2d-standard-8  →  agent pods + token-server
  Each node: ~8 agent pods × 10 sessions = ~80 concurrent sessions
  HPA trigger: agent_active_sessions_total (Prometheus, port 9090)
```

### Sessions per node (n2d-standard, me-central2 Dammam)

| VM Type | vCPU | RAM | Concurrent Sessions | $/hr | $/month |
|---|---|---|---|---|---|
| `n2d-standard-2` | 2 | 8 GB | 10–15 | $0.135 | $99 |
| `n2d-standard-4` | 4 | 16 GB | 30–40 | $0.270 | $197 |
| `n2d-standard-8` | 8 | 32 GB | 70–90 | $0.541 | $395 |
| `n2d-standard-16` | 16 | 64 GB | 140–180 | $1.082 | $789 |
| `n2d-standard-32` | 32 | 128 GB | 280–350 | $2.163 | $1,579 |

### Cost per concurrent session (me-central2)

Cost per session flattens after `n2d-standard-8` — no efficiency gain going larger. Scale horizontally instead.

| VM Type | Sessions | $/month | $/session/month |
|---|---|---|---|
| `n2d-standard-4` | 35 | $197 | $5.63 |
| `n2d-standard-8` | 80 | $395 | $4.94 |
| `n2d-standard-16` | 160 | $789 | $4.93 |
| `n2d-standard-32` | 315 | $1,579 | $5.01 |

### Pod resource specs

| Pod | CPU request | CPU limit | Memory request | Memory limit |
|---|---|---|---|---|
| `agent` | `800m` | `2000m` | `900Mi` | `1.5Gi` |
| `livekit-server` | `1000m` | `4000m` | `512Mi` | `2Gi` |
| `token-server` | `100m` | `500m` | `128Mi` | `256Mi` |
| `redis` | `200m` | `500m` | `256Mi` | `512Mi` |

Prices are on-demand (pay-as-you-go) as of May 2026. Apply 1-year committed use for ~37% discount on sustained production workloads.

## Configuration Entry Points

| Layer | Config source |
|---|---|
| All services | `.env` (loaded by Docker Compose) |
| LiveKit server | `livekit-server/livekit.yaml` |
| Agent | `agent/config.py` (Pydantic settings, env-prefix mapped) |
| Token server | `token-server/server.py` (Pydantic settings) |
| Demo frontend | `docker-compose.yml` environment block + Next.js `process.env` |

## Dependency Graph (startup order)

```
redis  →  livekit-server  →  agent
                          →  token-server
                          →  demo-frontend
```

All `depends_on` use `condition: service_healthy` for LiveKit so the agent does not register before the server is ready.
