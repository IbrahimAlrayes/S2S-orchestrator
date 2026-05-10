# Workflows — End-to-End Execution Paths

## 1. LiveKit Realtime Turn (Normal Mode)

This is the main production path. One turn = one user utterance + one agent reply.

```
User speaks into microphone
    │
    │  WebRTC audio frames (16 kHz mono, 50 ms)
    ▼
AgentSession (VAD listener)
    │  VAD (Silero) detects speech start, begins buffering frames
    │  VAD detects speech end → flushes frame buffer
    ▼
stt.StreamAdapter._recognize_impl(buffer)
    │  → frames_to_wav_bytes(): merge frames → WAV (resample guard is no-op, input already 16 kHz)
    │  → POST https://dev.nusukai.com/transcribe (multipart, Bearer JWT from NusukTokenManager)
    │  ← {transcription_text, language}
    ▼
AgentSession receives final transcript
    │  Turn detection: MultilingualModel predicts utterance complete
    │  (or silence timer fires if MultilingualModel not installed)
    ▼
CustomLLMStream._run_openai()  (current — Groq backend)
    │  → POST https://api.groq.com/openai/v1/chat/completions
    │     model=openai/gpt-oss-120b, reasoning_effort=low, max_completion_tokens=768
    │  ← SSE stream of {choices: [{delta: {content: "token"}}]} events
    │     reasoning fields ignored; legacy <think> blocks stripped via ReasoningStreamFilter
    │
    │  AgentSession sentence buffering:
    │  accumulates tokens until sentence boundary (. ، ؟ ! \n)
    │  → fires TTS for sentence 1 while LLM still streams sentence 2
    │  → preemptive_tts=True also fires speculative TTS during endpointing window
    │
    ▼
CustomTTSChunkedStream._run()
    │  → _strip_markdown() on the sentence text
    │  → httpx.stream("POST", https://dev.nusukai.com/synthesize, json={"text": "..."})
    │     (Bearer JWT from shared NusukTokenManager)
    │  ← WAV bytes (24 kHz, chunked transfer)
    │  → _parse_wav_header() walks RIFF/fmt /data on the prefix buffer
    │  → output_emitter.initialize(...) + output_emitter.push(pcm) per chunk as it arrives
    ▼
AgentSession publishes PCM frames to LiveKit room
    │
    │  WebRTC audio frames (TTS sample rate)
    ▼
Browser plays audio
```

### Key latency points
- **TTFA (Time to First Audio)** — from end of user speech to first TTS audio playing
- Sentence buffering means TTS for sentence 1 starts before LLM finishes the full response
- The LLM must emit proper punctuation (`.`, `،`, `؟`, `!`, newline) for sentence buffering to fire
- The system prompt (loaded from `AGENT_SYSTEM_PROMPT_FILE`, currently RAG_VOICE) instructs short sentences with punctuation
- TTS streams PCM as bytes arrive — first audio reaches the room before the full WAV body is received
- Primary remaining latency is **STT WAN round-trip** (~1 s of ~1.5 s STT wall time) — Nusuk hosted on Google Cloud, Belgium

---

## 2. LiveKit Explicit EOS Mode (Eval/Testing)

Activated by `AGENT_EXPLICIT_EOS_MODE=true`. Used by `eval/compare.py --livekit-turn-mode explicit_eos`.

```
Agent joins room
    │  publishes local audio track, subscribes to remote audio
    │  listens for data messages on AGENT_EXPLICIT_EOS_TOPIC
    ▼
All user audio frames buffered in memory (no VAD)
    │
    │  eval script sends data message: topic=eval.eos, payload="__EOS__"
    ▼
_handle_explicit_eos() fires
    │  → drain buffered frames → frames_to_wav_bytes()
    │  → stt_adapter.transcribe_frames()   (direct, not via StreamAdapter)
    │  → _collect_llm_reply()              (direct stream drain)
    │  → _publish_tts_reply()              (push frames to audio_source)
    ▼
Agent plays back response audio
    │
    │  eval script measures time from EOS signal to first audio frame
    ▼
disconnected event → cleanup
```

### Differences from Normal Mode
- No VAD, no `AgentSession`, no sentence buffering
- STT, LLM, TTS are called sequentially (not pipelined)
- No interruption handling
- Useful for measuring raw pipeline latency without turn-detection overhead

---

## 3. Push-to-Talk (PTT) Demo

Browser-only pipeline, no WebRTC, no LiveKit agent. Runs through Next.js API proxies.

```
User holds button → MediaRecorder captures audio (webm/opus)
    │
    │  On release: browser POSTs blob to /api/ptt/transcribe
    ▼
demo/app/api/ptt/transcribe/route.ts
    │  → forwards multipart to ASR_URL/api/transcribe/ with Bearer ASR_TOKEN
    │  ← {transcription_text, language}
    ▼
Browser shows transcript
    │
    │  POSTs {query, session_id} to /api/ptt/chat
    ▼
demo/app/api/ptt/chat/route.ts
    │  → prepends NUSUK_QUERY_PREFIX to query (if set)
    │  → gets cached Nusuk JWT from nusukAuth.ts (refreshes if near-expiry)
    │  → POSTs to NUSUK_URL/chat (non-streaming) with Bearer JWT
    │  ← {response, session_id}
    ▼
Browser shows response text
    │
    │  POSTs {text} to /api/ptt/tts
    ▼
demo/app/api/ptt/tts/route.ts
    │  → stripMarkdown() on text
    │  → POSTs to TTS_URL/ body {"text": "..."}
    │  ← WAV stream
    ▼
Browser plays audio via Audio(URL.createObjectURL(blob))
```

### PTT vs LiveKit comparison

| Aspect | PTT | LiveKit |
|---|---|---|
| Turn control | Manual (hold-to-release) | Automatic (VAD + turn detection) |
| LLM | Non-streaming `/chat` | Streaming `/chat/stream` (SSE) |
| TTS trigger | After full LLM response | Per sentence while LLM streams |
| Session continuity | `session_id` passed per-call | Managed by `AgentSession` |
| Interruption | Not possible | Supported |
| Latency | Higher (sequential) | Lower (pipelined) |
| Debugging | Easy (each stage visible) | Harder (all in one session) |

---

## 4. Token Generation (Client → LiveKit)

```
Frontend calls GET /token?room=<room>&identity=<identity>
    │
    ▼
token-server/server.py
    │  → generates room name (or uses provided)
    │  → generates identity (or uses provided)
    │  → builds RoomConfiguration with agent dispatch entry
    │     cfg.agents.add().agent_name = AGENT_NAME
    │  → mints JWT:
    │     AccessToken(api_key, api_secret)
    │       .with_identity(identity)
    │       .with_grants(VideoGrants(room_join, can_publish, can_subscribe))
    │       .with_room_config(cfg)
    │       .to_jwt()
    │  ← {token, url, room, identity}
    ▼
Frontend connects to LiveKit server with token
    │  LiveKit server creates/joins room
    │  LiveKit dispatches job to agent worker pool (due to RoomConfiguration)
    ▼
Agent worker calls entrypoint(ctx)
```

---

## 5. Nusuk Authentication Flow

Used both in the Python agent (`NusukTokenManager`) and the Next.js demo (`nusukAuth.ts`).

```
First call to get_token() (or after invalidate())
    │
    ▼
POST {base_url}/auth/token
    body: {client_id, client_secret}
    │
    ← {access_token, token_type}
    │
    ▼
Decode JWT payload (base64url, no signature check)
    → extract exp claim → store as expires_at
    fallback: now + 3600 s if decode fails
    │
    ▼
Cache token until (expires_at - 60 s)
    │
On 401 from /chat/stream:
    → invalidate() → retry once with fresh token
```

---

## 6. Worker Process Lifecycle

```
Docker container starts (or replica launched)
    │
    ▼
AgentServer registers worker with LiveKit server
    │  → WebSocket connection to LiveKit for job dispatch
    ▼
prewarm() called once (sync)
    │  → metrics.start_server(AGENT_METRICS_PORT) — registers MultiProcessCollector
    │     so all forked workers' samples aggregate via PROMETHEUS_MULTIPROC_DIR
    │  → silero.VAD.load(activation_threshold=...) → proc.userdata["vad"]
    │  → httpx.AsyncClient(http2=True, max_keepalive=20, keepalive_expiry=120s)
    │     → proc.userdata["http_client"]  (shared by STT, LLM, TTS, Nusuk auth)
    │  → (if any of STT/LLM/TTS provider == "nusuk" + client_id/secret)
    │     NusukTokenManager + asyncio.run(JWT prefetch)
    │     → proc.userdata["nusuk_token_manager"]
    ▼
Worker idle — waiting for job assignments
    │
    │  New room created → LiveKit dispatches job
    ▼
entrypoint(ctx) called in isolated async task
    │  One process can handle multiple sessions concurrently
    │  VAD instance is shared across all sessions in the same process
    │
    │  Session ends (user disconnects)
    ▼
finally block: aclose() all HTTP clients
    │
    ▼
Worker returns to idle pool
```

---

## 7. Agent Startup Sequence (within entrypoint)

```
1. await ctx.connect()
      → agent joins LiveKit room as kind=4 (agent participant)
      → remote participants already present are visible

2. Build adapters (all three reuse the worker-shared httpx client + token manager from proc.userdata)
      → CustomSTTAdapter(stt_settings, token_manager=..., client=shared_client)
      → CustomLLM(llm_settings, ..., token_manager=..., client=shared_client)
      → CustomTTS(tts_settings, token_manager=..., client=shared_client)

3. (If explicit_eos_mode) → _run_explicit_eos_mode() → return

4. streaming_stt = stt.StreamAdapter(stt=stt_adapter, vad=proc.userdata["vad"])
      → wraps non-streaming STT with VAD-based audio segmentation

5. turn_detection = MultilingualModel() if installed else None

6. session = AgentSession(stt, llm, tts, vad, turn_detection, ...)
      → wires all pipeline components together

7. Attach event handlers (user_input_transcribed, conversation_item_added)

8. session.start(room, agent, room_options)
      → attaches session to room I/O
      → agent sends greeting via TTS

9. await disconnected.wait()
      → hold until user leaves room

10. finally:
    → metrics.record_turn_metrics(session.history)
       (walks ChatMessage.metrics → 5 agent_turn_* histograms)
    → streaming_stt.aclose()
    → _aclose_providers(...)  (only adapters with _owns_client=True actually close)
```
