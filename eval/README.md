# Evaluation

Tools for benchmarking the S2S pipeline latency, both in isolation (direct API calls) and end-to-end through LiveKit (full stack).

## Scripts

| Script | Purpose |
|--------|---------|
| `quick_speed.py` | Per-stage latency against the live Nusuk + Groq endpoints (TTFT / TTFA via streamed responses) |
| `run_pipeline_eval.py` | Direct STT → LLM → TTS API calls; saves audio artifacts (assumes `local_api` provider — does not speak Nusuk JWT) |
| `compare.py` | Side-by-side latency comparison: direct vs. LiveKit (same `local_api` assumption as above) |

## Test data

```
testdata/
├── chunk_0000.wav  (11.36s)
├── chunk_0001.wav  (11.36s)
├── chunk_0002.wav   (3.28s)
...
└── chunk_0019.wav  (12.45s)
```

Both scripts accept **any WAV format** — float32, float64, int32, int8, and int16 PCM are all handled automatically via an in-memory `normalize_wav()` conversion using numpy. No manual pre-conversion needed.

The constraint is a Python tooling limitation, not a LiveKit server limitation:
- `wave` module only reads int16 PCM (format 1)
- `livekit.rtc.AudioFrame` requires int16 data (`sizeof(int16)` hardcoded)
- LiveKit server itself receives Opus-encoded WebRTC audio and doesn't care about WAV format

---

## quick_speed.py

Per-stage latency against the production endpoints (Nusuk JWT + Groq) without booting the agent or LiveKit. Streams the LLM and TTS responses to capture TTFT and TTFA — the metrics that drive perceived responsiveness.

Reads `.env` from the repo root for credentials and URLs. Loads `agent/system_prompt_rag.txt` if present, otherwise falls back to `AGENT_SYSTEM_PROMPT`. Honours `CUSTOM_LLM_QUERY_PREFIX`, `CUSTOM_LLM_TEMPERATURE`, `CUSTOM_LLM_MAX_TOKENS`, and `CUSTOM_LLM_REASONING_EFFORT` so the call shape matches what the agent sends.

Requires a venv with `aiohttp`, `numpy`, `certifi` installed (the repo-level `.venv` is set up by `python3 -m venv .venv && .venv/bin/pip install -r eval/requirements.txt && .venv/bin/pip install certifi`).

```bash
# Single file
.venv/bin/python eval/quick_speed.py eval/testdata/chunk_0005.wav

# Full 20-file suite
.venv/bin/python eval/quick_speed.py eval/testdata/chunk_*.wav
```

Output: one JSON line per file plus a summary block with `min / p50 / mean / max` for `stt_s`, `llm_ttft_s`, `llm_total_s`, `tts_ttfa_s`, `tts_total_s`, `e2e_first_audio_s`, and `e2e_total_s`.

The first call typically pays a TLS handshake penalty (~`0.5s` extra on STT) that the keepalive pool absorbs for subsequent calls. The agent's prewarm prefetch already heats this connection in production.

See `EXPERIMENT_LOG.md` for the most recent baseline numbers.

---

## run_pipeline_eval.py

Runs STT → LLM → TTS as direct HTTP calls and saves all artifacts. Does not require LiveKit or the agent to be running.

```bash
python3 eval/run_pipeline_eval.py eval/testdata/chunk_0005.wav

# Multiple files
python3 eval/run_pipeline_eval.py eval/testdata/*.wav
```

Each run writes to `eval/runs/<timestamp>/`:

```
runs/20260401-160111/
└── chunk_0005/
    ├── input.wav
    ├── output.wav
    ├── llm_response.txt
    └── result.json
```

`result.json` contains per-stage timing, RTF, and the LLM reply text.

---

## compare.py

Measures and compares latency across two modes.

### Modes

| Mode | What runs | LiveKit required? |
|------|-----------|-------------------|
| `direct` | STT + LLM + TTS via HTTP | No |
| `livekit` | Audio through LiveKit room → agent → audio back | Yes |
| `both` | Both modes, prints overhead diff | Yes |

### Usage

```bash
# Both modes (default)
python3 eval/compare.py eval/testdata/chunk_0011.wav

# Direct only
python3 eval/compare.py --mode direct eval/testdata/*.wav

# LiveKit only, 3 repeated runs
python3 eval/compare.py --mode livekit --runs 3 eval/testdata/chunk_0005.wav

# Full batch comparison
python3 eval/compare.py --mode both eval/testdata/*.wav
```

### What it measures

**Direct mode**

| Metric | Definition |
|--------|-----------|
| `stt_wall_s` | Wall time for STT API call |
| `llm_ttft_s` | Time to first visible LLM token (skips `<think>` blocks) |
| `llm_total_s` | Full LLM streaming time |
| `tts_wall_s` | Wall time for TTS API call |
| `e2e_approx_s` | `stt + llm_ttft + tts` — approximates latency from end-of-speech to first audio |

**LiveKit mode**

| Metric | Definition |
|--------|-----------|
| `room_connect_s` | WebSocket connect to LiveKit server |
| `agent_join_delay_s` | Time from connect until agent joins the room |
| `ttfa_from_end_s` | End-of-speech → first agent audio frame (can be negative if VAD triggers mid-file) |
| `ttfa_from_start_s` | Start-of-speech → first agent audio frame (always ≥ 0) |
| `agent_audio_duration_s` | Duration of agent's speech response |
| `total_wall_s` | Connect → agent audio complete |

**Overhead**

```
overhead = ttfa_from_start_s - input_duration_s - direct_e2e_s
```

This isolates the cost added by WebRTC transport, VAD endpointing, and agent coordination on top of the raw API latency.

### Sample output

```
────────────────────────────────────────────────────────────
  Audio: chunk_0011.wav
────────────────────────────────────────────────────────────
  DIRECT (no LiveKit)
    STT wall:         0.095s   (backend  0.060s)
    LLM TTFT:         0.507s   (total  0.621s)
    TTS wall:         0.310s   (output  5.824s)
    ─────────────────────────────
    E2E (approx):     0.901s   [STT + LLM_TTFT + TTS]

  LIVEKIT (full stack)
    Room connect:     0.621s
    Agent join:       1.312s
    TTFA (from end):  1.265s   [end-of-speech → first agent audio]
    TTFA (from start): 2.618s  [includes 1.23s audio duration]
    Agent audio:      6.350s
    Total wall:      12.981s

  OVERHEAD (TTFA_start − audio_dur − Direct E2E):   0.489s
```

---

## Latency analysis

### Full benchmark (20 Arabic audio files, local stack)

#### Direct pipeline breakdown

| Stage | Avg | Min | Max | Stdev | Share of E2E |
|-------|-----|-----|-----|-------|--------------|
| STT | 0.117s | 0.066s | 0.158s | 0.024s | 14% |
| **LLM TTFT** | **0.518s** | **0.209s** | **1.436s** | **0.314s** | **51%** |
| TTS | 0.319s | 0.237s | 0.426s | 0.044s | 37% |
| **E2E total** | **0.944s** | **0.556s** | **1.884s** | **0.321s** | — |

STT and TTS are consistent (low stdev). **LLM TTFT is the dominant bottleneck in direct mode** — it varies 7× between best and worst case and accounts for 51% of end-to-end latency on average.

#### LiveKit overhead breakdown (15 files where VAD triggered after speech ended)

| Metric | Avg | Min | Max | Stdev |
|--------|-----|-----|-----|-------|
| TTFA (from end) | 2.116s | 0.526s | 3.455s | 1.062s |
| VAD delay (TTFA_end − Direct E2E) | 1.195s | −0.57s | 2.808s | 1.198s |
| Total overhead (TTFA_start − dur − Direct E2E) | 1.868s | −0.19s | 3.531s | 1.227s |

**VAD endpointing delay is the dominant LiveKit overhead** — it adds 0.5–2.8s beyond the raw pipeline latency. WebRTC transport and agent join cost are negligible by comparison.

5 of 20 files had `TTFA_end < 0` (agent responded before we finished publishing the audio). These are all long clips (>8s) where the audio contained a natural internal silence — VAD correctly triggered mid-utterance. This is expected behavior, not an error.

#### Per-file results

| File | Dur | Direct E2E | TTFA(end) | TTFA(start) | Overhead |
|------|-----|-----------|-----------|-------------|---------|
| chunk_0000 | 11.4s | 0.687s | +3.063s | 15.255s | +3.205s |
| chunk_0001 | 11.4s | 0.782s | +2.451s | 14.664s | +2.519s |
| chunk_0002 | 3.3s | 0.836s | +3.311s | 6.851s | +2.739s |
| chunk_0003 | 16.5s | 0.834s | +0.526s | 18.279s | +0.991s |
| chunk_0004 | 16.5s | 0.720s | +2.977s | 20.705s | +3.531s |
| chunk_0005 | 4.1s | 0.584s | +1.417s | 5.796s | +1.104s |
| chunk_0006 | 6.3s | 0.647s | +3.455s | 10.246s | +3.315s |
| chunk_0007 | 5.2s | 0.556s | +1.288s | 6.853s | +1.101s |
| chunk_0008 | 8.5s | 0.739s | **−4.466s** | 4.537s | — (mid-file VAD) |
| chunk_0009 | 4.4s | 0.982s | +3.438s | 8.174s | +2.828s |
| chunk_0010 | 2.9s | 1.884s | +1.447s | 4.618s | −0.190s |
| chunk_0011 | 1.2s | 0.901s | +1.265s | 2.618s | +0.489s |
| chunk_0012 | 10.9s | 0.686s | **−1.084s** | 10.544s | — (mid-file VAD) |
| chunk_0013 | 10.9s | 1.183s | **−3.885s** | 7.766s | — (mid-file VAD) |
| chunk_0014 | 13.4s | 0.711s | +2.484s | 16.775s | +2.676s |
| chunk_0015 | 19.4s | 1.153s | +0.641s | 21.164s | +0.629s |
| chunk_0016 | 19.4s | 1.206s | **−1.142s** | 19.544s | — (mid-file VAD) |
| chunk_0017 | 16.0s | 1.245s | **−3.632s** | 13.430s | — (mid-file VAD) |
| chunk_0018 | 12.5s | 1.331s | +0.765s | 14.035s | +0.252s |
| chunk_0019 | 12.5s | 1.210s | +3.210s | 16.499s | +2.837s |

### Agent-side timeline (from `docker compose logs agent`)

For a 1.23s audio clip (`chunk_0011.wav`):

```
+0.00s  Job dispatch received
+0.32s  Stream attached — reading user audio
+1.55s  Audio publishing ends  (0.32 + 1.23s)
+2.02s  STT request starts     ← +0.47s VAD endpointing delay
+2.12s  STT done               (0.09s)
+2.62s  TTS starts             (0.49s LLM TTFT)
+2.85s  TTS done               (0.23s)
~2.90s  First audio frame received by client
+2.85s  AEC warmup begins      (3.0s timer)
+5.85s  AEC warmup expires
```

**TTFA from end = VAD delay (0.47s) + STT (0.09s) + LLM TTFT (0.49s) + TTS (0.23s) = 1.28s**

The STT, LLM, and TTS latencies closely match the direct mode — they are not the bottleneck.

### Root causes of LiveKit overhead

#### 1. VAD endpointing delay (dominant, 0.5–5s)

The Silero VAD must observe silence for `min_endpointing_delay` before triggering STT. For audio that ends without a clean sentence boundary, the agent waits up to `max_endpointing_delay`.

```python
# agent/config.py
min_endpointing_delay: float = Field(default=0.5)   # silence wait before STT
max_endpointing_delay: float = Field(default=5.0)   # fallback for ambiguous endings
false_interruption_timeout: float | None = Field(default=2.0)
```

This is the primary variable. Short, clean utterances: ~0.5s delay. Longer or trailing utterances: up to 3–5s.

#### 2. AEC warmup (3.0s, first response per session)

After the agent sends its first TTS response, an Acoustic Echo Cancellation warmup runs for 3 seconds. During this window, user interruptions are ignored. This does not delay TTFA for the first turn but makes rapid back-and-forth conversation unresponsive.

```
aec warmup active, disabling interruptions for 3.00s
```

#### 3. Agent join delay (0.3–1.5s, per room)

The agent is dispatched fresh per room. The fork + init takes ~0.14s, but the full join delay observed was 0.3–1.5s depending on server load.

#### 4. WebRTC transport (negligible, <50ms)

On a local stack, WebRTC encoding/decoding and network latency are below measurement noise.

---

## Mitigation strategies

### ① Enable Turn Detector — highest impact

Replaces VAD-based silence detection with a semantic turn detection model. The model predicts when the user has actually finished their turn, reducing endpointing delay from 0.5–5s to ~0.1–0.3s.

The `MultilingualModel` is already imported and conditionally loaded in `agent/agent.py`.

Enable via `.env`:

```
AGENT_USE_TURN_DETECTOR=true
```

Expected improvement: 0.3–2.5s reduction in TTFA depending on audio content.

### ② Reduce endpointing delays

Lower the minimum silence window and the fallback maximum:

```
AGENT_MIN_ENDPOINTING_DELAY=0.2
AGENT_MAX_ENDPOINTING_DELAY=2.0
AGENT_FALSE_INTERRUPTION_TIMEOUT=1.0
```

Trade-off: lower values increase the chance of the agent cutting the user off mid-sentence.

### ③ Migrate to TurnHandlingOptions API

The current config fields (`min_endpointing_delay`, etc.) are deprecated in agents v1.5+. Migrating removes the warning and unlocks future improvements:

```python
from livekit.agents.voice import TurnHandlingOptions

session = AgentSession(
    stt=streaming_stt,
    llm=llm_provider,
    tts=tts_provider,
    vad=ctx.proc.userdata["vad"],
    turn_handling=TurnHandlingOptions(
        min_endpointing_delay=0.2,
        max_endpointing_delay=2.0,
    ),
    allow_interruptions=agent_settings.allow_interruptions,
)
```

### ④ Streaming STT

The current STT adapter buffers the entire utterance before sending. A streaming STT adapter could forward audio chunks as they arrive, allowing the LLM to start generating before transcription is complete. This would reduce TTFA by `stt_wall_s` (currently ~0.09–0.17s).

### Summary of expected gains

| Mitigation | Expected TTFA reduction | Risk |
|-----------|------------------------|------|
| Turn Detector | 0.3–2.5s | Slight increase in false positives |
| Reduce min_endpointing_delay 0.5→0.2 | 0.3s | May cut off slow speakers |
| Reduce max_endpointing_delay 5.0→2.0 | 0–3s on long pauses | May cut off long pauses |
| Streaming STT | ~0.1s | Adapter rewrite required |

---

## Requirements

```bash
pip install livekit livekit-api aiohttp
```

LiveKit server and agent must be running for `--mode livekit`:

```bash
docker compose up
```
