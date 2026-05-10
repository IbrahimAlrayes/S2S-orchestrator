# Functions — Internal Reference

## `agent/metrics.py`

Module-level Prometheus metric objects imported by the plugin files. Call `metrics.start_server(port)` once per worker process (done in `prewarm()`). Five inline `agent_*` metrics record per-HTTP-request stage timings; five `agent_turn_*` histograms record per-turn metrics walked from `ChatMessage.metrics` at session end.

### `start_server(port)`
Starts the Prometheus HTTP exporter. When `PROMETHEUS_MULTIPROC_DIR` is set (production), creates the dir, builds a `CollectorRegistry`, registers a `MultiProcessCollector(registry)` on it, and serves that registry — every forked worker writes counter / histogram / gauge values to shared memory-mapped files in the multiproc dir, and the bound HTTP server aggregates them on scrape. When the env is unset, falls back to the in-process default registry. Wraps the bind in `try/except OSError` so workers that lose the port-9090 race silently skip — with multiproc enabled the bound worker still aggregates everyone's samples.

### `record_turn_metrics(history) → None`
Called once at session end from the `entrypoint` `finally` block. Walks `session.history.messages()` and reads each `ChatMessage.metrics` (a `MetricsReport` TypedDict the SDK populates as turns happen). User messages carry `transcription_delay` and `end_of_turn_delay`; assistant messages carry `e2e_latency`, `llm_node_ttft`, `tts_node_ttfb`. Each present field is observed into its corresponding `agent_turn_*` histogram. Returns silently if `history.messages()` raises (e.g., session torn down before any turns completed).

---

## `agent/agent.py`

### `prewarm(proc)`
Called once per worker process before any jobs are dispatched. Must be sync — the livekit-agents SDK invokes it from a sync context; an `async def` coroutine would be returned un-awaited and silently dropped. Steps in order:
1. Starts the Prometheus HTTP server on `AGENT_METRICS_PORT` (default 9090) via `metrics.start_server()` (with `MultiProcessCollector` registry when `PROMETHEUS_MULTIPROC_DIR` is set).
2. Loads Silero VAD into `proc.userdata["vad"]` — shared by all sessions in this worker.
3. Builds one `httpx.AsyncClient(http2=True, max_keepalive_connections=20, keepalive_expiry=120s)` and stores it in `proc.userdata["http_client"]`. Reused by STT, LLM, TTS, and the Nusuk token manager so the worker keeps a single warm TCP+TLS connection to `dev.nusukai.com` instead of opening three.
4. If **any** of `CUSTOM_STT_PROVIDER` / `CUSTOM_LLM_PROVIDER` / `CUSTOM_TTS_PROVIDER` is `nusuk` and `client_id` + `client_secret` are set: creates a `NusukTokenManager` (using the shared `http_client`), pre-fetches the JWT via `asyncio.run(token_manager.get_token())`, and stores it in `proc.userdata["nusuk_token_manager"]`. All sessions on this worker share the token manager, so the first turn of every room skips the auth roundtrip. Gating on **any** (not just LLM) was added when LLM moved to Groq while STT/TTS stayed on Nusuk — gating only on LLM left STT/TTS unauthenticated.

### `_build_room_options(agent_settings, tts_settings) → room_io.RoomOptions`
Constructs the `RoomOptions` passed to `session.start()`. Hard-codes audio input to 16 kHz mono 50 ms frames with pre-connect audio enabled (3 s timeout). 16 kHz matches Silero VAD's native rate and the ASR target rate — the `rtc.AudioResampler` in `custom_stt.py` becomes a no-op on the hot path. Audio output sample rate and channels are taken from `tts_settings` so they match what the TTS adapter produces. Text input is disabled (voice-only).

### `_extract_text(value) → str`
Normalizes an LLM content value into a plain string. Handles three forms:
- `str` — returned as-is
- `list[str | dict]` — each element is extracted (dict must have `"text"` key), joined with spaces
- anything else — returns `""`

Used in the `conversation_item_added` event handler to log assistant replies.

### `_resolve_user_identity(ctx, agent_settings) → str | None`
Returns the identity to associate with the current session. Prefers `agent_settings.participant_identity` if set; otherwise picks the first remote participant's identity. Returns `None` if the room has no remote participants yet (agent joined before the user).

### `_aclose_providers(stt_adapter, llm_provider, tts_provider)`
Closes all three `httpx.AsyncClient` instances in order. Called in a `finally` block in both `entrypoint` and `_run_explicit_eos_mode` so cleanup runs even if the session errors out.

### `_collect_llm_reply(llm_provider, user_text) → str`
Used only in explicit EOS mode. Builds a one-message `ChatContext`, calls `llm_provider.chat()`, collects all `ChatChunk.delta.content` pieces, and returns the joined string. Not used in normal mode (AgentSession handles the LLM loop there).

### `_publish_tts_reply(audio_source, tts_provider, reply_text)`
Used only in explicit EOS mode. Calls `tts_provider.synthesize(reply_text)` and streams each `SynthesizedAudio` frame to `audio_source.capture_frame()`. Not used in normal mode.

### `_run_explicit_eos_mode(ctx, *, ...)`
Alternate session loop for eval/testing. Bypasses `AgentSession` entirely:
1. Creates a `rtc.AudioSource` and publishes a local audio track.
2. Subscribes to `track_subscribed` events to buffer all remote audio frames.
3. Subscribes to `data_received` events; when a `__EOS__` message arrives on the topic, drains the buffer → STT → LLM → TTS.
4. Holds until `disconnected` fires.

### `entrypoint(ctx)`
Main room handler. Registered with `@server.rtc_session(agent_name=...)`. Per-room startup:
1. `await ctx.connect()` — join the room
2. Build `CustomSTTAdapter`, `CustomLLM`, `CustomTTS`
3. If `explicit_eos_mode`: delegate to `_run_explicit_eos_mode` and return
4. Wrap STT in `stt.StreamAdapter` (VAD-segmented streaming interface)
5. Build `NusukAgent` and `AgentSession` with full inline parameters
6. Attach `user_input_transcribed` and `conversation_item_added` log handlers
7. `await session.start(...)` — wire session to room
8. `await disconnected.wait()` — hold until user disconnects
9. `finally` — close streaming STT adapter + all three HTTP clients

---

## `agent/plugins/custom_stt.py`

### `CustomSTTAdapter._recognize_impl(buffer, *, language, conn_options)`
LiveKit SDK entry point for STT. The `conn_options` parameter name must not be changed — the SDK calls this method with `conn_options=...` as a keyword argument. Delegates to `transcribe_frames()`.

### `CustomSTTAdapter.transcribe_frames(frames) → STTResult`
Core transcription logic:
1. Merge and resample frames to `target_sample_rate` via `frames_to_wav_bytes()`
2. POST WAV to the configured URL with multipart form data
3. Parse the response JSON — tries keys `transcription_text`, `text`, `transcript`, `transcription` in order
4. Returns `STTResult(text, request_id, language)`. On HTTP failure, returns empty `STTResult` (session survives).

### `frames_to_wav_bytes(frames, *, target_sample_rate) → bytes`
Merges a list of `rtc.AudioFrame` objects using `rtc.combine_audio_frames()`, resamples if needed (using `rtc.AudioResampler` at HIGH quality), then encodes the result as a WAV file in memory. Raises `ValueError` if `frames` is empty.

### `_transcribe_url(url, provider) → str`
Normalizes the STT endpoint URL:
- `local_api`: appends `/api/transcribe/` if not already present
- `nusuk`: appends `/transcribe` if not already present
- other: returns the URL as-is

### `_request_form_data(settings, provider) → dict`
Returns form fields for the multipart POST. For `nusuk` and `local_api`, returns `{}` (no extra fields — only the `file` field). For `openai`, returns `{model, language}`.

---

## `agent/plugins/custom_llm.py`

### `CustomLLM.__init__(settings, agent_settings, *, session_id, user_id, token_manager=None)`
Normalizes `_provider_key = settings.provider.strip().lower()`. Accepts an optional `token_manager` parameter — if provided (passed from `prewarm()` via `proc.userdata["nusuk_token_manager"]`), it is used directly and no new manager is created. If `token_manager` is `None` and `provider == "nusuk"` with `client_id + client_secret` set, creates a session-scoped `NusukTokenManager`. Falls back to static `settings.access_token` otherwise.

### `CustomLLM.chat(...) → CustomLLMStream`
LiveKit SDK entry point. Drops unused SDK arguments (`parallel_tool_calls`, `tool_choice`, `extra_kwargs`) before constructing the stream.

### `CustomLLMStream._run()`
Dispatches to `_run_nusuk()` or `_run_openai()` based on `_provider_key`.

### `CustomLLMStream._run_openai()`
OpenAI-compatible SSE stream. Injects a system message if the chat context doesn't have one. If `settings.reasoning_effort` is set, includes it in the payload (Groq `gpt-oss-*` accepts `low|medium|high`; non-reasoning models will reject it, so the field is opt-in). Reads only `choices[0].delta.content`, ignoring any `delta.reasoning` / `delta.reasoning_content` fields that reasoning models stream alongside — chain-of-thought never reaches TTS. Filters legacy inline `<think>` blocks via `ReasoningStreamFilter` before emitting `ChatChunk` events. Used for Groq, OpenAI, and compatible endpoints.

### `CustomLLMStream._run_nusuk()`
Nusuk SSE stream. Extracts the latest user message, prepends `query_prefix` if set, POSTs to `/chat/stream`. On a 401 response (first attempt only), calls `token_manager.invalidate()` and retries. Delta tokens arrive in `event["delta"]` — no `choices` nesting.

### `_iter_sse(response) → AsyncGenerator[dict]`
Shared SSE parser. Reads lines from the response, skips non-`data:` lines and the `[DONE]` sentinel, parses JSON, and yields each event dict. Logs a warning on malformed JSON rather than raising.

### `_extract_openai_delta(event) → str | None`
Extracts `choices[0].delta.content` from an OpenAI-style SSE chunk. Returns `None` if any nesting is absent.

### `_latest_user_message(chat_ctx) → str`
Scans the chat context in reverse for the last user message. Handles both `str` content and `list[str | dict]` content (OpenAI multi-part format). Returns `""` if none found.

### `ReasoningStreamFilter`
State machine that strips `<think>...</think>` blocks from streamed output. Used with models that emit chain-of-thought reasoning. Tracks how much visible text has been emitted so incremental deltas are correct even when a `</think>` tag spans multiple chunks.

### `_openai_chat_url(url) → str`
Appends `/chat/completions` to the base URL if not already present.

### `_nusuk_stream_url(url) → str`
Normalizes to `.../chat/stream`. Handles three input forms: already ends in `/chat/stream`, ends in `/chat`, or neither.

---

## `agent/plugins/nusuk_auth.py`

### `NusukTokenManager.get_token() → str`
Returns the cached JWT if it is still valid (more than 60 s before expiry). Otherwise acquires a lock, re-checks, and calls `_refresh()`. The double-check pattern prevents redundant concurrent refreshes.

### `NusukTokenManager.invalidate()`
Clears the cached token under the lock. Called after a 401 from the Nusuk API so the next `get_token()` forces a fresh fetch.

### `NusukTokenManager._refresh()`
POSTs `{client_id, client_secret, user_id}` to `{base_url}/auth/token`. `user_id` defaults to `client_id` when not set explicitly. Stores `access_token` and decodes the JWT expiry via `_jwt_expiry()`. Falls back to `_DEFAULT_TOKEN_TTL = 3600` s if expiry cannot be decoded. Raises `NusukAuthError` on network or HTTP errors.

### `_jwt_expiry(token) → float | None`
Decodes the JWT payload (base64url, no signature verification) and returns the `exp` claim as a Unix timestamp. Returns `None` on any decode error.

---

## `agent/plugins/custom_tts.py`

### `CustomTTS.synthesize(text, ...) → CustomTTSChunkedStream`
LiveKit SDK entry point. Constructs a `ChunkedStream` for the given text.

### `CustomTTSChunkedStream._run(output_emitter)`
Core synthesis logic:
1. Strips markdown via `_strip_markdown()` before posting
2. POSTs to the TTS URL with provider-specific payload
3. Detects WAV magic bytes (`RIFF`) and decodes via `_decode_wav()` to extract sample rate, channels, and raw PCM
4. Calls `output_emitter.initialize(...)` then `output_emitter.push(pcm_bytes)`
5. On HTTP failure: initializes the emitter with no data (empty push) so the session survives

### `_strip_markdown(text) → str`
Removes formatting that TTS would speak literally:
- `**bold**` / `*italic*` → inner text only
- `> blockquotes` → removed
- `[4]` citation markers → removed
- `\n\n` paragraph breaks → collapsed to a space

### `_decode_wav(wav_bytes) → (sample_rate, num_channels, pcm_bytes)`
Parses a WAV file using the standard `wave` module and returns the audio parameters and raw PCM data.

### `_tts_url(url, provider) → str`
Returns the final POST URL:
- `nusuk`: appends `/synthesize` if not already present
- `wrapper`: base URL as-is (POST to `/`)
- `local_api`: normalizes to `{base}/api/synthesize/`
- other: base URL as-is

### `_request_payload(settings, text, provider) → dict`
Returns the JSON body:
- `nusuk` / `wrapper`: `{"text": text}`
- `local_api`: `{"text", "output_format", "sample_rate"}`
- generic/other: `{"model", "voice", "input", "response_format"}`
