# Changelog

Ongoing record of significant changes, decisions, and findings. Most recent first.

---

## 2026-05-17

### Validator banner + ValidationError shape on prompt-file failure (closes R-030)
When R-025's regen fallback also failed (file missing AND `agent/prompts/prompts.json` unimportable), the validator raised a bare `FileNotFoundError` inside the LiveKit IPC fork — the actionable line was buried at the bottom of a ~50-line `DuplexClosed` traceback that the supervisor printed on every respawn. Fixed two ways: **(1)** the validator now writes a 72-column stderr banner (`====` block with `FATAL: agent system prompt unavailable` + the offending path + three numbered fix options) BEFORE raising, so the message survives any later wrapping; **(2)** raises `ValueError` instead of `FileNotFoundError` — pydantic catches it cleanly and surfaces a `ValidationError` with `Value error, system_prompt_file=...` keyed to the field name. Smoke-tested in container by moving `agent/prompts/prompts.json` aside and pointing `system_prompt_file` at `/nonexistent/path.txt`: banner appears prominently on stderr; exception type is `ValidationError` with the actionable summary at the top.

### `AGENT_SYSTEM_PROMPT_FILE` default baked into config (closes R-026)
The env var was set in the running container (from 25h ago) but missing from both `.env` and `.env.example`. A fresh `docker compose up` on another machine would silently fall back to the short inline Arabic bootstrap prompt — degraded responses with no visible failure. Fix: removed the env-var dependency. `AgentSettings.system_prompt_file` now defaults to `Path(__file__).resolve().parent / "system_prompt_rag.txt"`, which resolves to `/app/system_prompt_rag.txt` inside Docker and `agent/system_prompt_rag.txt` outside. The env var still overrides when set, so testing layouts and non-standard mounts still work. Combined with R-025 (self-healing regen) and R-006 (durable source at `agent/prompts/prompts.json`), the file is guaranteed to be present with the correct content on every cold start, irrespective of `.env` state. Smoke-tested in container three ways: no env override → default loads; bad override path → regen creates the file there; missing default file → regen restores it. `.env.example` updated with an explanatory comment noting the override is optional.

### Prompt versioning + golden-set regression check (closes R-006)
Three changes shipped together. **(1)** `prompts.json` moved out of the temp RAG subtree at [`agent/plugins/rag/config/prompts.json`](../agent/plugins/rag/config/prompts.json) to the durable location [`agent/prompts/prompts.json`](../agent/prompts/prompts.json). The file was split: durable `RAG_VOICE_SECTIONS` + a new top-level `prompt_version` field go to the new home; only `RERANKER` (consumed by `rerank.py`) stays in the RAG subtree and dies with it when the temp adapter is removed. `voice_prompt.py` moved with it, now stdlib-only (no `plugins.rag.config_loader` dep), exposing `RAG_VOICE_PROMPT`, `PROMPT_HASH`, `PROMPT_VERSION`. **(2)** Per-turn logging in [`custom_llm._run_nusuk_rag`](../agent/plugins/custom_llm.py) now includes `prompt_version` + `prompt_hash` + `query_lang`, so any production response is forensically attributable to a specific prompt state. **(3)** Golden-set eval landed at [`eval/rag_qa/golden/`](../eval/rag_qa/golden/) — the frozen `pilot100_v4` 100-row sample (one cell per Category × Language × 10) plus a baseline JSON with alignment + judge thresholds (~10% headroom below observed). New script [`eval/rag_qa/regression.py`](../eval/rag_qa/regression.py) runs the golden set against the live pipeline and diffs vs baseline, exits non-zero on regression. Self-check against the source `pilot100_v4` run reports OK. Versioning strategy: git is the source, hash is the runtime fingerprint, `prompt_version` is the human label — bumped together on every prompt edit. See [S2S-roadmap/06-risks-issues.md](../../S2S-roadmap/docs/06-risks-issues.md) §4 (R-006).

### Self-healing `agent/system_prompt_rag.txt` regeneration on startup (closes R-025)
`AgentSettings._load_prompt_file` no longer crashes when `AGENT_SYSTEM_PROMPT_FILE` points at a missing file. The validator now falls back to importing `plugins.rag.voice_prompt.RAG_VOICE_PROMPT` (assembled from `plugins/rag/config/prompts.json` at module import) and atomically rewrites the file via a tempfile+`os.replace` pattern (mode preserved at `0o644`). A clear `FileNotFoundError` is raised only when *both* the file is missing *and* the RAG plugin source is unimportable. Why this matters: the file was regenerated locally on 2026-05-12 but was never committed, so any `git clean -fdx` or fresh checkout would brick every agent job spawn with a 50-line LiveKit IPC traceback ending in a bare `FileNotFoundError`. The file is now derived from in-repo sources (`prompts.json` → `voice_prompt._assemble()`), making it self-healing without committing the 14 KB artifact. Smoke-tested in-container: deleting `/app/system_prompt_rag.txt` then constructing `AgentSettings()` regenerates it (13,770 chars, hash unchanged at `sha256:0da47283a405de98`). Note: the file's content never reaches the LLM in the live RAG path — `custom_llm._run_nusuk_rag` strips the chat_ctx system message and re-fuses `RAG_VOICE_PROMPT` per turn — so its only roles are satisfying the validator (so `Agent(instructions=...)` isn't empty) and ops convenience (`cat`-able prompt on disk). See [S2S-roadmap/06-risks-issues.md](../../S2S-roadmap/docs/06-risks-issues.md) §4 (R-025).

---

## 2026-05-12

### Temp local-RAG LLM provider (`nusuk_rag`)
Added `agent/plugins/rag/` — a self-contained Milvus + reranker stack ported from `rag-nusuk-ai` so the agent can produce RAG-augmented responses without depending on Nusuk's `/chat/stream` (broken upstream: `GenericRAG.stream_search() got an unexpected keyword argument 'prompt_key'`). New provider `CustomLLM._run_nusuk_rag()` does: query → embed (`embed.llmtests.org`) → Milvus hybrid search → generative reranker (`ranker.llmtests.org`) → top-12 docs fused into the system prompt Nusuk-style → delegate to the existing Groq OpenAI streaming path. Activated via `CUSTOM_LLM_PROVIDER=nusuk_rag`. Module-scope singletons for `VectorClient` and `GenerativeRerankerModelAsync` are lazy-initialised on first turn with a 2-retry connect budget so failure falls back to plain Groq within seconds rather than blocking voice. Refactored `_run_openai` to accept an optional `messages=` kwarg so the RAG provider can reuse its streaming loop without duplication. Also tightened TTS `_strip_markdown`'s citation regex from `\[\d+\]` to `\[[^\]]*\]` so Arabic and non-numeric bracket markers don't leak into spoken audio. **This whole subtree is temporary** — see `agent/plugins/rag/README.md` for the removal checklist; removal will be a 6-file PR when Nusuk `/chat/stream` is restored.

### Documented in-cluster migration plan
Added `docs/in-cluster-migration.md` after diagnosing that 85% of STT wall time is network, not compute (`eval/network_breakdown.py`). The Nusuk cluster is in me-central2 (Dammam) — same city as the test client — but the public ingress is a global anycast ALB that routes Saudi-Arabia clients through a European GFE, adding ~70 ms RTT × 3-4 round trips per STT call. Plan moves the agent into the same GKE cluster, talking to `nlp-rag-app` via ClusterIP, predicted STT wall 568 → ~110 ms. LiveKit SFU goes to LiveKit Cloud (private cluster blocks self-hosted LiveKit per [LiveKit docs](https://docs.livekit.io/transport/self-hosting/kubernetes/)), and `nlp-rag-app` gains a `/voice/session` endpoint that mints LiveKit participant tokens. Two paths documented: Path A (agent → nlp-rag-app, REST) is the safe cut-over; Path B (agent → Triton directly, gRPC) unlocks streaming STT to the compute floor.

### Prewarm `RuntimeError: Event loop is closed` fix
Prewarm built a process-shared `httpx.AsyncClient`, then ran `asyncio.run(token_manager.get_token())` using that same client for the Nusuk JWT prefetch. `asyncio.run` creates a temporary loop, the HTTP/2 connection to `dev.nusukai.com` bound to it, then the loop closed — but the connection lived on in httpx's pool. First STT call from the worker's persistent loop tried to recycle it, `transport.close()` called `loop.call_soon(...)` on the dead prewarm loop → crash. Fix: in `agent/agent.py`, the prefetch now uses a throwaway `httpx.AsyncClient` opened inside the `asyncio.run` coroutine and closed via `async with` before the loop dies. The long-lived shared client (bound to the worker's job loop on first use) is then seeded with the cached JWT via a new `NusukTokenManager.seed_cache()` method. Bug was latent on Linux (worked by timing luck), bit on macOS where Docker's network stack triggered the recycle path on first reuse.

### LiveKit RTC UDP range moved 50000–50100 → 30000–30100
macOS reserves 49152–65535 as the ephemeral port range. With the LiveKit RTC range sitting in 50000–50100 (or even 60000–60100), Docker's bulk UDP-port bind raced every outbound network connection on the Mac. Symptom: `bind: address already in use` on a port `lsof` showed as free — because the kernel won and freed it before our investigation. Updated `livekit-server/livekit.yaml` and `docker-compose.yml` to 30000–30100 (below the ephemeral floor). Also bumped `TOKEN_SERVER_PORT` 8080 → 8090 to avoid a collision with an unrelated `nlp-rag-fastapi` container.

---

## 2026-05-07

### Per-turn pipeline metrics from `ChatMessage.metrics` → Prometheus
The LiveKit SDK populates a `MetricsReport` on every `ChatMessage` with `e2e_latency`, `llm_node_ttft`, `tts_node_ttfb`, `transcription_delay`, `end_of_turn_delay`. These are the same data points the SDK exports through OpenTelemetry (`lk.agents.turn.*`). Added five matching Prometheus histograms in `agent/metrics.py` (`agent_turn_e2e_latency_seconds`, `agent_turn_llm_node_ttft_seconds`, `agent_turn_tts_node_ttfb_seconds`, `agent_turn_transcription_delay_seconds`, `agent_turn_end_of_turn_delay_seconds`) and a `record_turn_metrics(history)` helper that walks `session.history` and observes them. Called from the `entrypoint` `finally` block at session end. Skipped the OTel→Prom bridge because `opentelemetry-exporter-prometheus` is not multi-process-aware — only the worker bound to port 9090 would emit; other forked workers' samples would silently disappear. Reading the SDK's data structure directly into `prometheus_client` multiproc histograms keeps all workers' samples aggregated correctly and uses our existing dashboards.

### Enabled `preemptive_tts=True`
Set `turn_handling={"preemptive_generation": {"preemptive_tts": True}}` on `AgentSession`. The SDK already runs the LLM speculatively during the endpointing-delay window (this is on by default); flipping `preemptive_tts` makes TTS also fire speculatively as soon as sentence 1 of the speculative LLM stream is ready. If the user keeps talking (false turn-end), the in-flight LLM+TTS calls are cancelled. Capped at `max_retries=3` per turn, skipped for utterances `> max_speech_duration=10s`. Expected ~500–1000 ms TTFA win on confidently-detected short turns; cost is up to 3× speculative LLM/TTS calls per turn that get cancelled when speculation is wrong (rare when turn detection is well-tuned).

### Streaming TTS: PCM pushed as it arrives
`CustomTTSChunkedStream._run` now uses `httpx.stream("POST", …)` and pushes PCM chunks to the LiveKit `output_emitter` as they arrive, instead of awaiting the full WAV body. New `_parse_wav_header()` walks `RIFF`/`fmt `/`data` markers in the prefix buffer to extract sample rate and channel count without invoking the `wave` module on a partial stream. Added `ttfa_s` field to `tts_done` log line — time from request start to first PCM chunk hitting the emitter, vs. `duration_s` (full body received). Measured savings on first call: ~360 ms TTFA reduction per sentence. Bigger wins on long replies because Nusuk flushes per-sentence audio while later sentences are still rendering.

### Shared `httpx.AsyncClient` across STT / LLM / TTS / Nusuk auth
LiveKit's idiom is `utils.http_context.http_session()` — a process-scoped singleton — but it's `aiohttp` and would force us off HTTP/2 and our streaming TTS. Applied the same pattern with `httpx`: one `AsyncClient(http2=True, max_keepalive_connections=20, keepalive_expiry=120s)` built in `prewarm()` and stored in `proc.userdata["http_client"]`. Plugins accept it as a constructor arg and track `_owns_client` so they only close the client they created themselves (process-scoped lifetime; one warm TCP+TLS connection to `dev.nusukai.com` reused by all three plugins instead of three separate ones).

### Enabled HTTP/2 on all `httpx.AsyncClient` instances
Added `httpx[http2]` to `agent/requirements.txt` (pulls in `h2`). All four `AsyncClient` constructions now pass `http2=True`. Verified: `dev.nusukai.com` negotiates ALPN h2 (`HTTP/2 200`). Benefits: header compression on every request, stream multiplexing on a single socket — pays off when sentence-buffered TTS calls overlap or once we move to one-shot full-reply TTS.

### Fixed Prometheus multi-process metrics
The agent forks 5 worker processes; `prewarm()` ran `metrics.start_server(9090)` in each. Only the first worker won the bind; other workers' metric emissions went to private process memory and never reached the `/metrics` endpoint. Symptom: every Grafana panel was empty even after successful turns. Fix: enabled `prometheus_client` multi-process mode — set `PROMETHEUS_MULTIPROC_DIR=/tmp/prom_multiproc` (with a `tmpfs` mount in `docker-compose.yml`), updated `metrics.start_server` to register a `MultiProcessCollector`, set `multiprocess_mode="livesum"` on the active-sessions `Gauge`. Now every worker writes to its own `.db` file in the multiproc dir and the bound HTTP server aggregates them on scrape.

### Fixed shared Nusuk token-manager gating
`prewarm()` only built the shared `NusukTokenManager` when **LLM provider == nusuk**. After switching LLM to Groq, the manager was no longer created — but STT and TTS still hit `dev.nusukai.com`, fired without `Authorization: Bearer …`, and Nusuk returned `422` to every transcribe call. Symptom: ASR worked once, then every subsequent turn returned no response. Fix: gate now triggers when **any** of STT/LLM/TTS is `nusuk`. Same client_id/secret pair used; nothing else changed.

### LLM temporarily switched to Groq (`openai/gpt-oss-120b`)
Nusuk `/chat/stream` is broken upstream (`GenericRAG.stream_search() got an unexpected keyword argument 'prompt_key'` → 500/403). Switched LLM provider to Groq via the existing OpenAI-compatible path (`_run_openai`). `.env` now sets `CUSTOM_LLM_URL=https://api.groq.com/openai/v1`, `CUSTOM_LLM_PROVIDER=openai`, `CUSTOM_LLM_MODEL=openai/gpt-oss-120b`. `CUSTOM_LLM_MAX_TOKENS` raised 96 → 768 because `gpt-oss-120b` is a reasoning model and reasoning tokens consume the budget before any visible content is emitted. Added `GROQ_API_KEY` to `LLMSettings.access_token` `AliasChoices`. Nusuk credentials kept in `.env` so the swap back is one-line when `/chat/stream` is restored.

### Added `CUSTOM_LLM_REASONING_EFFORT`
New optional field on `LLMSettings`. When set, `_run_openai` injects `reasoning_effort` in the payload (Groq `gpt-oss-*` accepts `low|medium|high`). Production `.env` uses `low`: collapsed reasoning trace from ~78 tokens → ~7 tokens, dropped TTFT measurably without hurting reply quality. Field is opt-in — unset means provider default and no key sent (safe with non-reasoning models).

### Added `AGENT_SYSTEM_PROMPT_FILE` (file-based system prompt loader)
Long system prompts (e.g. the full Nusuk RAG prompt at ~24 KB) don't fit cleanly in `.env` because `env_file` parsing is line-oriented. Added a `system_prompt_file: str | None` field on `AgentSettings` plus a `model_validator(mode="after")` that, when set, replaces `system_prompt` with `Path(...).read_text(encoding="utf-8")`. Inline `AGENT_SYSTEM_PROMPT` still works when set; the file path takes precedence. Current deployment uses `AGENT_SYSTEM_PROMPT_FILE=/app/system_prompt_rag.txt` populated from the `RAG_VOICE` entry in `prompts.json`.

### LLM provider benchmark — Groq vs OpenAI (RAG_VOICE prompt, ~916 B)
Same 6 Arabic queries, post-warmup, `max_completion_tokens=768`. Streaming TTFT measured to first content delta (reasoning deltas excluded since the agent ignores them).

| Model | TTFT median | TTLT median | Generation throughput | Notes |
|---|---|---|---|---|
| Groq `openai/gpt-oss-120b` (`reasoning_effort=low`, `T=0.2`) | **422 ms** | **483 ms** | ~1347 ch/s | Current production choice |
| OpenAI `gpt-5.3-chat-latest` (non-reasoning, `T=1` forced default) | 1765 ms | 2220 ms | ~234 ch/s | Does not accept `temperature` or `max_tokens` |
| OpenAI `gpt-5.4-mini-2026-03-17` (`reasoning_effort=low`) | 1676 ms | 1932 ms | ~383 ch/s | Cold-cache outlier observed (5.2 s TTFT once) |

**Headlines:**
- Groq is ~4× faster on TTFT and ~3.5–5.7× faster on throughput than either OpenAI model.
- Groq's full reply (TTLT ~480 ms) lands before OpenAI starts streaming (~1700 ms TTFT).
- For real-time voice, Groq remains the right call. Switching to OpenAI adds ~1.3 s to end-to-end TTFA.

**Voice-pipeline TTFA budget (user speech end → first audio):** STT (~1.5 s on a typical utterance) + LLM TTFT (~0.4 s on Groq) + sentence buffer (~0.3 s) + TTS first-audio (~0.6–1.3 s) ≈ **2.5–3.5 s**. Same query through OpenAI lands at ~3.8–4.8 s.

**RAG vs RAG_VOICE prompt:** With the full RAG prompt (24,260 chars / ~8k tokens), TTFT on Groq median was 1086 ms (range 794–1311 ms). Switching to RAG_VOICE (916 chars) cut TTFT 61% and removed `<PAGE_ID>` placeholder emission entirely (RAG_VOICE explicitly forbids them — important because `_strip_markdown` does not strip those tags, so they would otherwise be spoken by TTS).

**Reproducibility:** All three models refused to fabricate ("ما عندي معلومات…") since RAG_VOICE instructs grounding-only and no retrieved context is wired into the prompt yet. Quality comparison requires the RAG context plumbed in — only latency was meaningfully comparable in this run.

### STT and TTS switched to Nusuk
`CUSTOM_STT_URL` and `CUSTOM_STT_PROVIDER` updated to point at `https://dev.nusukai.com` using the `nusuk` provider. STT now calls `/transcribe` (multipart WAV, 16 kHz) instead of the local ASR container at port 8102. TTS now calls `/synthesize` (JSON `{text}`) instead of the local TTS wrapper at port 8000. Both adapters now accept and use the shared `NusukTokenManager` from `prewarm()` — previously the token manager was only passed to the LLM adapter. Auth headers are fetched dynamically via `_auth_headers()` in both `CustomSTTAdapter` and `CustomTTS`.

### Added `mode: "voice"` to Nusuk LLM payload
`_run_nusuk()` now includes `"mode": "voice"` in the POST body. Nusuk uses this to return shorter, spoken-style responses rather than detailed text answers.

### Fixed `nusukAuth.ts` missing `user_id`
The Nusuk `/auth/token` call in `demo/lib/nusukAuth.ts` was missing the required `user_id` field. Added `user_id: clientId` to the request body.

---

## 2026-05-03

### Removed Langfuse — Prometheus + Grafana only
Removed `langfuse>=2,<4` from `agent/requirements.txt`, deleted `agent/observability.py`, removed `LangfuseSettings` from `agent/config.py`, dropped all `import observability` / `start_span` / `start_generation` calls from `custom_stt.py`, `custom_llm.py`, `custom_tts.py`, and `agent.py`. Removed `LANGFUSE_*` vars from `.env.example` and `.env`. Removed `observability/langfuse/` stack. Prometheus + Grafana remain under `--profile observability`. Updated all docs.

---

## 2026-04-22 (observability stack)

### Added Prometheus + Grafana compose services
New `prometheus` and `grafana` services in [docker-compose.yml](../docker-compose.yml) under profile `observability` (`docker compose --profile observability up -d`). Prometheus scrape config at [observability/prometheus.yml](../observability/prometheus.yml) covers `agent:9090` (existing histograms) and `livekit-server:6789` (new — enabled via `prometheus_port: 6789` in [livekit-server/livekit.yaml](../livekit-server/livekit.yaml)). Grafana auto-provisions a Prometheus datasource and a starter dashboard **S2S / S2S Agent** from [observability/grafana/provisioning/](../observability/grafana/provisioning/) — panels: active sessions, STT/LLM/TTS p50/p95/p99 latency, error rates. Default host ports: Grafana 3001, Prometheus 9091 (both overridable).

### Added Langfuse trace instrumentation to plugins
New module [agent/observability.py](../agent/observability.py) — per-worker Langfuse client (`init`), per-session contextvar (`set_session`), and `start_span` / `start_generation` helpers returning live spans or `_NoOpSpan`s when disabled. [agent/plugins/custom_stt.py](../agent/plugins/custom_stt.py) wraps its HTTP call in an `stt` span, [agent/plugins/custom_llm.py](../agent/plugins/custom_llm.py) wraps both openai and nusuk stream paths in an `llm-chat` generation (with `ttft_s` / `duration_s` metadata), and [agent/plugins/custom_tts.py](../agent/plugins/custom_tts.py) wraps its HTTP call in a `tts` span. All three pass `session_id = LiveKit room name` and `user_id = participant identity` via `update_trace` so Langfuse's Sessions view groups a whole call into a waterfall. `LangfuseSettings` added to [agent/config.py](../agent/config.py); new env vars `LANGFUSE_ENABLED`, `LANGFUSE_HOST`, `LANGFUSE_PUBLIC_KEY`, `LANGFUSE_SECRET_KEY`, `LANGFUSE_FLUSH_AT`, `LANGFUSE_FLUSH_INTERVAL`.

### Self-hosted Langfuse v3 stack
New [observability/langfuse/docker-compose.yml](../observability/langfuse/docker-compose.yml) — separate compose project for isolation, services: `langfuse-web`, `langfuse-worker`, `postgres`, `clickhouse`, `redis`, `minio`. Port remaps from upstream defaults to avoid conflicts: web 3000 → 3100 (demo-frontend owns 3000), MinIO API 9000 → 9190, MinIO console 9001 → 9191 (Prometheus owns 9091), Postgres → 5532, Redis → 6389, ClickHouse native → 9500. Secrets and `LANGFUSE_INIT_*` bootstrap vars live in the project-local `.env` (gitignored). Agent reaches Langfuse via `host.docker.internal:3100` (same pattern as ASR/TTS).

### Fixed `prewarm()` never running
Reverted `prewarm()` from `async def` back to sync. The livekit-agents 1.5.x SDK invokes `setup_fnc` from a sync context — an `async def prewarm` produced a coroutine that was never awaited, so metrics.start_server, VAD load, and (previously) Nusuk token prefetch were silently dropped. Sync prewarm now actually runs. JWT prefetch wrapped in `asyncio.run(token_manager.get_token())` to preserve the prewarm-time fetch without needing an async function. This resolves a previously-silent bug introduced in the 2026-04-20 prewarm change.

### Narrowed LiveKit UDP port range back to 50000–50100
Reverted the 2026-04-20 widening to 60000. The 10k-port Docker bind races against ephemeral UDP sockets on busy hosts and fails with `address already in use`. The running container since 2026-04-15 had been on the narrow 50000–50100 range and worked fine, so the narrower range was restored to unblock restarts. For >50 concurrent participants in production, switch `livekit-server` to `network_mode: host` (no Docker port proxy) rather than widening the mapped range.

### Added `COPY` for metrics.py and observability.py in agent Dockerfile
[agent/Dockerfile](../agent/Dockerfile) was missing `COPY metrics.py` (since 2026-04-20) — the build-time `download-files` step failed with `ModuleNotFoundError: No module named 'metrics'`. Runtime worked only because `docker-compose.yml` volume-mounts `./agent:/app` at runtime, masking the incomplete image. Now both `metrics.py` and the new `observability.py` are explicitly copied.

### New doc
[docs/observability.md](observability.md) — full setup + ports reference for both the Prometheus/Grafana and Langfuse stacks.

---

## 2026-04-20 (audio pipeline cleanup)

### Agent input sample rate lowered from 24 kHz to 16 kHz
`AudioInputOptions.sample_rate` in [agent.py:93](../agent/agent.py#L93) changed from 24000 → 16000. LiveKit's server now delivers 16 kHz audio directly — matching Silero VAD's native rate and the ASR target rate. The `rtc.AudioResampler` call in `custom_stt.py` becomes a no-op (guarded by `if sample_rate != target_sample_rate`) and is kept as a safety net. Saves one resample per turn and avoids mild filter ringing from the 48→24→16 chain. TTS output rate is unchanged (24 kHz, independent stream).

---

## 2026-04-20 (concurrency + observability)

### Added load function to AgentServer
`server.load_threshold = 0.8` and `server.load_fnc = lambda s: min(len(s.active_jobs) / _MAX_JOBS_PER_WORKER, 1.0)`. Workers stop accepting new rooms above 80% of their job cap. Cap defaults to 10, overridable via `AGENT_MAX_JOBS_PER_WORKER`. Previously there was no cap — workers would accept unlimited jobs until OOM.

### Prefetch Nusuk token in prewarm
`prewarm()` is now `async`. When `CUSTOM_LLM_PROVIDER=nusuk` with `client_id/secret` set, it pre-fetches the JWT into `proc.userdata["nusuk_token_manager"]`. `entrypoint` passes this to `CustomLLM` via the new `token_manager=` parameter. The token manager is shared across all sessions on the same worker process — first room no longer pays an auth roundtrip before its first LLM call. `CustomLLM.__init__` falls back to creating its own session-scoped manager if no pre-warmed one is available.

### Added Prometheus metrics
New `agent/metrics.py` with counters, gauges, and histograms for: active sessions, STT duration/errors, LLM TTFT / total duration / errors (labelled by provider), TTS duration/errors. Metrics server starts in `prewarm()` on `AGENT_METRICS_PORT` (default 9090). Agent container now exposes that port via docker-compose. Access at `http://localhost:9090/metrics`. Note: for multi-worker-process containers, configure `PROMETHEUS_MULTIPROC_DIR` for cross-process aggregation.

### Widened LiveKit UDP port range
`livekit.yaml` `port_range_end` and docker-compose UDP mapping changed from 50100 to 60000 (10k ports → ~2500 concurrent participant slots instead of ~50).

---

## 2026-04-20 (docs)

### Added `docs/` folder
Created long-term institutional memory for the agent system:
- `docs/overview.md` — system summary, demos, services, design decisions
- `docs/architecture.md` — ASCII component diagram, data flow, machine split
- `docs/agents.md` — startup sequence, session parameters, Nusuk behavior
- `docs/livekit.md` — LiveKit SDK patterns, custom adapter contracts
- `docs/functions.md` — internal function reference for all Python modules
- `docs/workflows.md` — end-to-end execution paths for all major flows
- `docs/troubleshooting.md` — known issues, fixes, and debugging steps

### Added `CUSTOM_LLM_QUERY_PREFIX` support
Nusuk ignores `system_prompt`. A bilingual query prefix is now prepended to every user query to control response style (short sentences, proper punctuation, no markdown). Set via `CUSTOM_LLM_QUERY_PREFIX` env var. Wired in both the Python agent and the PTT demo frontend.

### Fixed sentence buffering not firing
Root cause: Nusuk was returning 150+ word responses with no punctuation, so `AgentSession`'s sentence boundary detection never triggered. Fix: query prefix instructs Nusuk to use short sentences ending with `.` or `،`.

### Added markdown stripping to TTS layer
`_strip_markdown()` in `custom_tts.py` removes `**bold**`, `*italic*`, `> blockquotes`, `[4]` citation markers, and `\n\n` paragraph breaks before posting to the TTS service. Prevents the TTS from speaking formatting symbols.

### Added markdown stripping to PTT TTS route
`stripMarkdown()` added to `demo/app/api/ptt/tts/route.ts` for parity with the LiveKit agent behavior.

### Added `NUSUK_QUERY_PREFIX` to PTT chat route
PTT chat route now reads `NUSUK_QUERY_PREFIX` env var and prepends it to user queries, matching agent behavior.

### Removed VAD toggle from token route
Deleted all `turnDetection` code from `demo/app/api/token/route.ts` (variable declaration, body parsing, query string parsing, `roomMetadata`/`roomConfig.metadata` assignment). Turn detection is always on; the toggle was never needed. `MultilingualModel` is used when installed; VAD-only fallback otherwise.

### Python code cleanup (all plugin files)
- Extracted shared `_iter_sse()` SSE parser used by both `_run_openai` and `_run_nusuk`
- Extracted `_extract_openai_delta()` helper
- Replaced `while True + tried_refresh` retry with `for attempt in range(2):`
- Normalized `_provider_key` in `__init__` for both LLM and STT adapters
- Changed `conn_options` parameter in STT to `conn_options: Any = None  # noqa: ARG002` (must not be renamed — SDK uses it as keyword arg)
- Replaced STT `or`-chain text extraction with explicit `for key in (...)` loop
- Hardened `nusuk_auth.py`: `assert` → explicit check, `except Exception` → specific types, `3600.0` → `_DEFAULT_TOKEN_TTL`, JWT split length guard
- WAV detection: `if settings.audio_format == "wav" or audio_bytes[:4] == b"RIFF"` → `if audio_bytes[:4] == b"RIFF"` (magic bytes more robust)
- `_tts_url` wrapper branch dead code removed
- Added inline comments to all `AgentSession` and `RoomOptions` parameters

### Added `query_prefix` field to `LLMSettings`
New `CUSTOM_LLM_QUERY_PREFIX` env var. Stored in `LLMSettings.query_prefix`, prepended to every user query in `_run_nusuk()`.

### Fixed eval comparison to be fair
`eval/compare.py` was comparing Groq LLM + `local_api` TTS (direct mode) against Nusuk LLM + `wrapper` TTS (LiveKit mode). Added Nusuk provider support and `wrapper` TTS support to `direct_llm()` and `direct_tts()` so both modes use the same providers. Also added `eval/requirements.txt`.

---

## 2026-04-18 (approx)

### Built push-to-talk (PTT) demo
New `/ptt` route in the Next.js demo with hold-to-talk button, sequential ASR → Nusuk chat → TTS pipeline, status chips per stage, and server-side Nusuk auth via `demo/lib/nusukAuth.ts`.

New API proxy routes:
- `demo/app/api/ptt/transcribe/route.ts` — proxy to ASR service
- `demo/app/api/ptt/chat/route.ts` — proxy to Nusuk `/chat` (non-streaming, server-side auth)
- `demo/app/api/ptt/tts/route.ts` — proxy to TTS wrapper service

### Fixed LiveKit TTS adapter for F5-TTS wrapper
Nusuk's TTS wrapper (`provider=wrapper`) expects `POST /` with `{"text": "..."}` — no auth, no path suffix. Added `wrapper` provider to `_tts_url()` and `_request_payload()`.

### Wired Nusuk automatic auth in Python agent
`NusukTokenManager` created in `CustomLLM.__init__` when `CUSTOM_LLM_CLIENT_ID` + `CUSTOM_LLM_CLIENT_SECRET` are set. Tokens refreshed automatically using JWT `exp` claim. On 401, token invalidated and one retry issued.

### Fixed agent to use Nusuk for LLM
Updated `.env`: `CUSTOM_LLM_PROVIDER=nusuk`, `CUSTOM_LLM_URL=https://dev.nusukai.com`. STT URL updated to `http://host.docker.internal:8102`.

### Added error resilience to STT and TTS adapters
Both adapters now catch `httpx.HTTPError`, log the error, and return gracefully (empty transcript / empty audio) so the session survives service failures.

### Added LiveKit healthcheck
`livekit-server` service in `docker-compose.yml` has a `curl` healthcheck. Agent and token-server `depends_on` with `condition: service_healthy` so they don't register before the server is ready.

### Rewrote agent.py
- Added `_AGENT_PARTICIPANT_KIND = 4` constant
- Added `_aclose_providers()` helper (was duplicated)
- Always-on turn detection (removed `use_turn_detector` toggle logic — `MultilingualModel` when installed, VAD fallback otherwise)
- Added inline comments to all `AgentSession` and `RoomOptions` parameters
- Added explicit EOS mode (`AGENT_EXPLICIT_EOS_MODE=true`) for eval
- Added stage logging: `stage=session_start`, `stage=session_ready`

### Added production deployment docs
README updated with:
- CPU/GPU machine split guidance
- LiveKit public IP requirement (most common production failure)
- VAD: CPU-only, no GPU benefit
- Horizontal scaling table (agent replicas, ASR replicas, TTS replicas, LiveKit server)

---

## Design decisions on record

### Nusuk `system_prompt` is ignored
Nusuk does not honor the `system_prompt` field. Response style is controlled by prepending a query prefix to every user message. This is a workaround, not a feature. If Nusuk adds system prompt support, remove `CUSTOM_LLM_QUERY_PREFIX` and use `AGENT_SYSTEM_PROMPT` directly.

### Room I/O defaults are hard-coded
Audio input (16 kHz mono, 50 ms frames, pre-connect audio) is hard-coded in `agent.py` rather than exposed as env vars. These values are stable and don't need per-deployment tuning. Changing them requires a code edit and image rebuild.

### VAD is always on
Silero VAD is preloaded in `prewarm()` and passed to both `stt.StreamAdapter` and `AgentSession`. There is no env var to disable it — disabling VAD would break the streaming STT interface the SDK expects. Turn *detection* (when to commit a turn) is separate from VAD (when speech is present).

### `MultilingualModel` does not support Arabic
`MultilingualModel` (LiveKit `turn_detector` plugin) improves turn detection for supported languages but falls back to VAD-only for Arabic. The agent logs a warning but continues. VAD-only fallback adds 0.5–3 s overhead depending on silence length.

### Session history is in memory only
`AgentSession` keeps conversation history in memory for the lifetime of the room. It is cleared when the room ends. No persistence layer is included. Add one only if cross-session history or analytics are needed.

### `conn_options` parameter name is load-bearing
The LiveKit SDK calls `_recognize_impl(buffer, conn_options=...)` as a keyword argument. The parameter must be named exactly `conn_options` even though it is unused. Renaming it to `_conn_options` or deleting it causes a `TypeError` at runtime that produces silent failures (empty transcripts) without a visible traceback in some log configurations.
