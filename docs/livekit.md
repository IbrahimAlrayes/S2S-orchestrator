# LiveKit — SDK Patterns and Implementation Notes

## Version

`livekit-agents` Python SDK. The agent uses `AgentServer`, `AgentSession`, `Agent`, `JobContext`, `cli`, `llm`, `stt`, `tts`, `room_io`.

## How LiveKit Dispatches Jobs

1. A browser client connects to the LiveKit server with a JWT that includes a `RoomConfiguration` with an `agents` dispatch entry naming the agent.
2. The LiveKit server creates the room and dispatches a job to the registered worker pool.
3. A worker process is assigned the job and calls the `@server.rtc_session` decorated function.
4. The agent calls `ctx.connect()` to join the room as a participant.

The agent is **not** a participant kind `0` (standard) — it is kind `4` (`_AGENT_PARTICIPANT_KIND = 4`). This constant is used to filter out agent audio tracks from user audio listeners.

## `AgentServer` and `server.setup_fnc`

```python
server = AgentServer()
server.setup_fnc = prewarm   # called once per worker process before any jobs
```

`prewarm` is invoked synchronously by the SDK (not awaited), so it must be `def` not `async def`. It starts the Prometheus metrics server, loads Silero VAD into `proc.userdata["vad"]`, and (for the nusuk provider) pre-fetches a JWT via `asyncio.run(...)`. Everything in `proc.userdata` is shared across all sessions handled by that worker.

## `AgentSession`

The central orchestrator. Wires together STT, LLM, TTS, VAD, and turn detection into a pipeline.

```python
session = AgentSession(
    stt=streaming_stt,       # livekit.agents.stt.StreamAdapter wrapping CustomSTTAdapter
    llm=llm_provider,        # CustomLLM instance
    tts=tts_provider,        # CustomTTS instance
    vad=ctx.proc.userdata["vad"],
    turn_detection=turn_detection,   # MultilingualModel or None
    ...
)
await session.start(room=ctx.room, agent=agent, room_options=...)
```

### Sentence Buffering (LLM → TTS)

When the LLM streams tokens, `AgentSession` accumulates them into a buffer. When a sentence boundary is detected, it flushes the buffer to TTS immediately. This means TTS starts for sentence 1 while the LLM is still generating sentence 2. This is the primary LiveKit latency advantage over non-streaming PTT.

Sentence boundaries detected: `.`, `،`, `؟`, `!`, newline. **The LLM must emit proper punctuation for this to work.**

### Pre-emptive TTS

`turn_handling={"preemptive_generation": {"preemptive_tts": True}}` — the SDK already runs the LLM speculatively during the endpointing-delay window (default behaviour); flipping `preemptive_tts` makes TTS also fire as soon as sentence 1 of the speculative LLM stream is ready. If the user keeps talking (false turn-end), the in-flight LLM+TTS calls are cancelled. Capped at `max_retries=3` per turn. Mitigates VAD endpointing delay on confidently-detected short turns.

### Turn Detection

`AgentSession` integrates `MultilingualModel` for semantic end-of-turn detection. If not installed or if the language is unsupported (e.g., Arabic), it falls back to VAD-based silence detection using `min_endpointing_delay` and `max_endpointing_delay`.

VAD overhead on post-speech turns (measured): +0.5s to +3s depending on audio content and silence length — partially offset by pre-emptive TTS.

## `stt.StreamAdapter`

Wraps a non-streaming STT (like `CustomSTTAdapter`) to present a streaming interface to `AgentSession`. VAD segments the audio stream; when VAD detects end-of-speech, it flushes the buffered frames to `_recognize_impl`.

```python
streaming_stt = stt.StreamAdapter(stt=stt_adapter, vad=ctx.proc.userdata["vad"])
```

**Important:** `_recognize_impl` must accept `conn_options` as a keyword argument (even if unused). Renaming it breaks the SDK call:
```python
# CORRECT
async def _recognize_impl(self, buffer, *, language=None, conn_options=None):
    ...
```

## `room_io.RoomOptions`

Controls how the agent's I/O is wired to the room. Key options used here:

```python
room_io.RoomOptions(
    text_input=False,                        # voice-only, no text channel
    audio_input=room_io.AudioInputOptions(
        sample_rate=16000,                   # native rate for Silero VAD + ASR; no agent-side resample
        num_channels=1,
        frame_size_ms=50,
        pre_connect_audio=True,              # buffer audio before session is fully ready
        pre_connect_audio_timeout=3.0,
    ),
    audio_output=room_io.AudioOutputOptions(...),
    text_output=room_io.TextOutputOptions(
        sync_transcription=False,            # don't gate text output on audio timing
    ),
    close_on_disconnect=True,
    delete_room_on_close=False,
)
```

## Writing a Custom LLM

Must extend `llm.LLM` and return an `llm.LLMStream` subclass from `chat()`.

The stream must implement `_run()`, which sends `llm.ChatChunk` events to `self._event_ch`:

```python
self._event_ch.send_nowait(
    llm.ChatChunk(
        id=request_id,
        delta=llm.ChoiceDelta(role="assistant", content=token_text),
    )
)
```

Unused parameters in `chat()` should be deleted:
```python
del parallel_tool_calls, tool_choice, extra_kwargs
```

## Writing a Custom STT

Must extend `stt.STT` with `streaming=False` capabilities.

```python
super().__init__(capabilities=stt.STTCapabilities(streaming=False, interim_results=False, diarization=False))
```

Must implement `_recognize_impl(buffer, *, language=None, conn_options=None)` — note: `conn_options` must keep its original name (SDK passes it as a keyword argument).

Return a `stt.SpeechEvent` with `type=stt.SpeechEventType.FINAL_TRANSCRIPT`.

## Writing a Custom TTS

Must extend `tts.TTS` with `streaming=False`.

Return a `tts.ChunkedStream` subclass from `synthesize()`. The stream implements `_run(output_emitter: AudioEmitter)`:

```python
output_emitter.initialize(
    request_id=...,
    sample_rate=...,
    num_channels=...,
    mime_type="audio/pcm",
    frame_size_ms=20,
)
output_emitter.push(pcm_bytes)
```

On HTTP error, initialize the emitter with empty push (don't raise — the session survives).

## Participant Kind Constants

LiveKit uses integer participant kinds internally:

| Kind | Value | Meaning |
|---|---|---|
| Standard | 0 | Regular participant |
| Ingress | 1 | Ingress participant |
| Egress | 2 | Egress participant |
| SIP | 3 | SIP participant |
| Agent | 4 | Agent worker |

`_AGENT_PARTICIPANT_KIND = 4` is used in track subscription handlers to skip agent audio.

## Token Generation

Tokens are JWTs signed with `LIVEKIT_API_KEY` / `LIVEKIT_API_SECRET`. The `RoomConfiguration` embedded in the token tells LiveKit which agent to dispatch.

```python
cfg = RoomConfiguration()
dispatch = cfg.agents.add()
dispatch.agent_name = "nusuk-agent"

token = (
    AccessToken(api_key, api_secret)
    .with_identity(identity)
    .with_grants(VideoGrants(room_join=True, room=room, can_publish=True, can_subscribe=True))
    .with_room_config(cfg)
    .to_jwt()
)
```

The Next.js demo token route (`/api/token`) does the same thing with the TypeScript SDK.

## SDK Features We Already Use

| Feature | Where | Why |
|---|---|---|
| `AgentServer.setup_fnc = prewarm` | [agent/agent.py](../agent/agent.py) | One-shot per-worker init: VAD load, JWT prefetch, shared httpx client, multiproc Prom dir |
| `proc.userdata` for cross-session state | prewarm + entrypoint | Holds VAD instance, shared `httpx.AsyncClient`, `NusukTokenManager` — sessions on the same worker reuse them |
| `stt.StreamAdapter(stt=..., vad=...)` | entrypoint | Wraps non-streaming STT with VAD-segmented streaming interface |
| Sentence-buffered LLM → TTS | AgentSession default | TTS for sentence 1 fires before LLM finishes streaming sentence 2 |
| Pre-emptive generation (LLM + TTS) | `turn_handling.preemptive_generation` in entrypoint | LLM kicks off before turn-end is fully confirmed (SDK default `enabled=True`); TTS also fires speculatively on sentence 1 of the speculative LLM stream (`preemptive_tts=True`, we set it; default is `False`) |
| Dynamic endpointing | `turn_handling.endpointing.mode="dynamic"` in entrypoint | EMA-smoothed (`alpha=0.9`) per-session pause statistics adapt the wait inside `[min_delay, max_delay]`. Zero added latency. First 2–3 turns still behave like fixed because the EMA hasn't seeded |
| VAD interruption + word floor | `turn_handling.interruption.mode="vad"` + `min_words=1` | Kept on VAD because adaptive mode (`mode="adaptive"`) calls LiveKit's hosted inference service; on self-hosted it silently falls back to VAD anyway. `min_words=1` filters coughs / mic bumps |
| Self-hosted noise cancellation (optional) | [`agent/plugins/denoiser.py::DeepFilterDenoiser`](../agent/plugins/denoiser.py) wired via `room_io.AudioInputOptions.noise_cancellation` when `AGENT_NOISE_CANCELLATION=true` | DeepFilterNet3 (Apache-2.0); ~10 ms algorithmic latency, ~2 ms compute per 10 ms frame on server CPU. Pulls torch (~600 MB image bloat) so default off |
| `record_turn_metrics(session.history)` (per-turn metrics) | [agent/metrics.py](../agent/metrics.py) + entrypoint `finally` | Walks `ChatMessage.metrics` into 5 multiproc-safe Prometheus histograms (e2e, llm_ttft, tts_ttfb, transcription_delay, end_of_turn_delay) |
| Streaming TTS via `httpx.stream` | [agent/plugins/custom_tts.py](../agent/plugins/custom_tts.py) | PCM pushed to `output_emitter` chunk-by-chunk as bytes arrive instead of awaiting full WAV body — ~360 ms TTFA savings per sentence |
| `MultilingualModel` turn detector | entrypoint (optional dep) | Semantic end-of-turn; falls back to VAD when language is unsupported (e.g. `ar`) |
| Prometheus multiprocess metrics | [agent/metrics.py](../agent/metrics.py) | `MultiProcessCollector` aggregates samples from all forked workers — see `docs/observability.md` |
| Hard-coded 16 kHz mono input | `_build_room_options` | Matches Silero VAD native rate + Nusuk ASR target → no resample on hot path |

## SDK Features Worth Adopting (Not Yet Used)

Surveyed `/usr/local/lib/python3.11/site-packages/livekit/agents/` 2026-05-07. Top candidates ranked by ROI for our voice pipeline.

### High ROI (small effort)

| Feature | Source | Benefit | Effort | Caveat |
|---|---|---|---|---|
| **`utils.connection_pool.ConnectionPool`** | `utils/connection_pool.py` | Generic pool primitive (not just HTTP) with `prewarm`, `max_session_duration`, background recycling | S | Useful if we ever switch STT/TTS to WebSocket — pool socket sessions across turns |

### Medium ROI

| Feature | Source | Benefit | Effort | Caveat |
|---|---|---|---|---|
| **`tts.FallbackAdapter([primary, backup])`** | `tts/fallback_adapter.py` | Auto-failover with non-blocking recovery probes; emits `tts_availability_changed` events | M | Need a real backup TTS (currently we only have Nusuk); set `availability_check_interval` to avoid hammering primary on outages |
| **`stt.FallbackAdapter`** | `stt/fallback_adapter.py` | Same idea for STT — primary Nusuk + backup if/when an alternative ASR is integrated | M | Same: need a real backup |
| **OTel trace export** | `telemetry/trace_types.py` + `telemetry/utils.py` | Distributed traces for STT/LLM/TTS spans → Jaeger/Datadog correlation with Prom histograms | M | Requires OTLP collector; adds env vars |
| **`tts.SentenceStreamPacer`** | `tts/stream_pacer.py` | Buffers tokens and flushes based on remaining playback (`min_remaining_audio`, `max_text_length`) | S | Refines our sentence buffering — useful if we move to single-call full-reply TTS |

### Worth Knowing

| Feature | Source | When it matters |
|---|---|---|
| **Adaptive interruption detector** | `inference/interruption.py` | ML-based overlap classifier — replaces VAD-only interrupt detection. Set `turn_handling.interruption.mode="adaptive"`. Reduces false interrupts when user makes backchannel sounds ("uh-huh", "نعم") |
| **AMD (answering machine detection)** | `inference/` | If you ever do outbound calling — classifies callee as human/silence/machine in first 5 s, lets you skip LLM if a machine picks up |
| **`AgentSession.session_close_transcript_timeout`** | constructor | Wait up to N s after session ends to capture trailing transcript for logging. Useful for post-call analytics |
| **Worker `load_fnc`** | `worker.py` | Already used (`server.load_fnc = ...`); LiveKit honours `load_threshold=0.8` to stop dispatching new jobs |
| **`evals/` framework** | `evals/` | Built-in eval scaffolding — could replace our `eval/compare.py` if we standardize |

### Top 3 to Do Next

Pre-emptive TTS, per-turn metrics, dynamic endpointing, VAD interruption with `min_words=1`, and the DeepFilterNet noise-cancellation hook have shipped — see "SDK Features We Already Use". Remaining priorities:

1. **STT/TTS FallbackAdapter** — only worth it once we have a second backend (e.g. fallback to a local Whisper or Edge-TTS). Architecturally important for production uptime.
2. ~~**Adaptive interruption detector**~~ — deferred. Requires LiveKit's hosted inference service; on self-hosted it silently falls back to VAD. Revisit if/when we move to LiveKit Cloud.
3. **`utils.connection_pool.ConnectionPool`** — relevant once any backend moves to WebSocket; not needed while everything is HTTP/2 over the shared httpx client.

## Room Events Used

```python
ctx.room.on("disconnected")   # set Event to unblock entrypoint
ctx.room.on("track_subscribed")  # detect user audio track (explicit EOS mode)
ctx.room.on("data_received")     # detect __EOS__ signal (explicit EOS mode)
```

```python
session.on("user_input_transcribed")   # log final transcript
session.on("conversation_item_added")  # log assistant reply
```

## Public IP Requirement (Production)

WebRTC requires LiveKit to know its public IP. In `livekit-server/livekit.yaml`:

```yaml
rtc:
  use_external_ip: true   # auto-detect from cloud instance metadata
  # or:
  node_ip: <your-public-ip>
```

Without this, signaling works but no audio flows. See [troubleshooting.md](troubleshooting.md#livekit-public-ip).
