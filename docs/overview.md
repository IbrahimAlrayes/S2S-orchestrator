# S2S-Orchestrator — System Overview

## What This Is

A self-hosted, real-time **speech-to-speech orchestration control plane** built on LiveKit. It connects a voice user (browser) to three backend AI services — ASR, LLM, and TTS — through a managed session layer.

This repository owns **orchestration only**. It does not run any AI models. All model inference lives in separate services and is called over HTTP.

## What It Is Not

- Not a model server (no Triton, no GPU workloads here)
- Not a frontend application (a demo frontend is included but is optional)
- Not a session persistence layer (conversation history lives in the LiveKit session in memory only)

## Two User-Facing Demos

### 1. LiveKit Realtime Demo (`/`)

Full duplex voice conversation. The browser sends microphone audio over WebRTC; the agent processes it through STT → LLM → TTS and plays back synthesized speech in real time. VAD and semantic turn detection control when the pipeline triggers.

### 2. Push-to-Talk Demo (`/ptt`)

Modular pipeline driven by a hold-to-talk button. The browser records audio, then sequentially calls ASR → Nusuk chat → TTS via Next.js API proxy routes. No WebRTC. Useful as a debugging fallback and for latency comparison.

## Core Services

| Service | Role | Default Port |
|---|---|---|
| `livekit-server` | WebRTC media relay (SFU) | 7880 (WS), 7881 (TCP), 50000–50100 (UDP), 6789 (Prometheus, internal) |
| `agent` | Python LiveKit agent worker pool | 9090 (Prometheus), 8081 (internal) |
| `token-server` | LiveKit JWT issuer | 8080 |
| `redis` | LiveKit state store | 6379 (internal) |
| `demo-frontend` | Optional Next.js demo UI (`--profile demo`) | 3000 |
| `prometheus` | Metrics scraper (`--profile observability`) | 9091 |
| `grafana` | Dashboards (`--profile observability`) | 3001 |

## External Services (not in this repo)

STT and TTS are hosted by Nusuk at `https://dev.nusukai.com` and share one `NusukTokenManager` (client_id + secret → cached JWT, refreshed on 401, reused across all sessions on a worker). LLM is currently routed to Groq while Nusuk `/chat/stream` is broken upstream; Nusuk credentials remain in `.env` so swapping back is one line.

| Service | Endpoint | Protocol | Auth |
|---|---|---|---|
| STT | Nusuk `POST /transcribe` | multipart WAV (16 kHz) → `{transcription_text, language}` | shared NusukTokenManager |
| LLM | Groq `POST /openai/v1/chat/completions` (model `openai/gpt-oss-120b`, `reasoning_effort=low`) | OpenAI-compatible SSE stream | `GROQ_API_KEY` static bearer |
| TTS | Nusuk `POST /synthesize` | JSON `{text}` → WAV (24 kHz, chunked transfer; PCM streamed to LiveKit as it arrives) | shared NusukTokenManager |

## Key Design Decisions

- Turn detection is **always on** (`MultilingualModel` when installed, VAD-only fallback for unsupported languages e.g. Arabic)
- `preemptive_tts=True` — TTS speculatively fires during the endpointing-delay window; cancelled if the user keeps talking
- TTS input is stripped of markdown before synthesis (LLM responses contain `**bold**`, `[1]` citations)
- TTS streams PCM to LiveKit chunk-by-chunk as bytes arrive from `/synthesize` (per-sentence audio flushed before later sentences finish rendering)
- One `httpx.AsyncClient(http2=True)` is built in `prewarm()` and shared across STT / LLM / TTS / Nusuk auth — process-scoped, reused across all sessions on the worker
- Room I/O defaults (16 kHz mono input, `tts_settings.sample_rate` output, 50 ms frames, pre-connect audio) are **hard-coded** in `agent.py` — not env-configurable
- Long system prompts can be loaded from disk via `AGENT_SYSTEM_PROMPT_FILE` (current deployment uses a `RAG_VOICE` prompt at `/app/system_prompt_rag.txt`)
- Per-turn metrics (`agent_turn_e2e_latency_seconds`, etc.) are walked from `session.history` `ChatMessage.metrics` at session end into multiproc-safe Prometheus histograms

## Related Docs

- [diagrams/](diagrams/) — Excalidraw system diagrams (canonical visual)
- [architecture.md](architecture.md) — component diagram and data flow
- [agents.md](agents.md) — agent session lifecycle and behavior
- [livekit.md](livekit.md) — LiveKit SDK patterns used here
- [functions.md](functions.md) — internal function reference
- [workflows.md](workflows.md) — end-to-end execution paths
- [troubleshooting.md](troubleshooting.md) — known issues and fixes
- [changelog.md](changelog.md) — ongoing updates and decisions log
