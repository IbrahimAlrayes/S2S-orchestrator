# Troubleshooting

## LiveKit Public IP

**Symptom**: WebSocket signaling connects (browser shows "connected"), but no audio flows. The agent joins the room but the user never hears the greeting, and user speech never reaches the agent.

**Root cause**: WebRTC requires LiveKit to advertise its public IP to browsers so they can send UDP media packets. If the server is behind NAT without this config, ICE negotiation silently fails.

**Fix** — in `livekit-server/livekit.yaml`:

```yaml
rtc:
  use_external_ip: true   # auto-detect from cloud instance metadata (EC2, GCP, Azure)
```

Or hardcode:

```yaml
rtc:
  node_ip: <your-public-ip>
```

Also open inbound UDP `50000–50100` in your firewall/security group.

---

## `conn_options` Keyword Argument Error

**Symptom**: `TypeError: _recognize_impl() got an unexpected keyword argument 'conn_options'`

**Root cause**: The LiveKit SDK calls `_recognize_impl(buffer, conn_options=...)` as a keyword argument. Renaming or deleting the parameter in the override breaks the call.

**Fix** — the parameter must stay named `conn_options`:

```python
async def _recognize_impl(self, buffer, *, language=None, conn_options=None):
    ...
```

Adding a `# noqa: ARG002` comment suppresses linters that flag it as unused.

---

## STT Returns Empty Transcript

**Symptom**: In eval, `speech_frames > 0` but `TTFA = n/a` and the transcript is blank. In normal mode, the agent stays silent after user speech.

**Possible causes**:

1. **Wrong STT URL**: Check `CUSTOM_STT_URL` points to the actual ASR service. The Nusuk provider appends `/transcribe`; `local_api` appends `/api/transcribe/`. Verify with `curl`.
2. **Auth failure**: The ASR service returns 401 or 403 — check `CUSTOM_STT_ACCESS_TOKEN`.
3. **Wrong audio format**: The ASR service expects 16 kHz mono WAV. Verify `CUSTOM_STT_TARGET_SAMPLE_RATE=16000`.
4. **`conn_options` rename**: See above — if the STT method is never called, `speech_frames` accumulates but transcription never fires.

---

## TTS Takes 7+ Seconds

**Symptom**: TTS wall time in eval is 7–8 s; a direct `curl` to the TTS endpoint takes 1–2 s.

**Root cause**: The LLM returned a very long response (150+ words with markdown) even though `AGENT_MAX_TOKENS` is set. Nusuk ignores `max_tokens`. The TTS is slow because it is synthesizing far more text than expected.

**Fix**:

1. Set `CUSTOM_LLM_QUERY_PREFIX` to instruct Nusuk to use short sentences:
   ```
   أجب بجمل قصيرة وواضحة مع علامات الترقيم. Answer in short clear sentences with punctuation.
   ```
2. The TTS adapter also strips markdown before synthesis (`_strip_markdown()`) — this alone does not reduce length but prevents the TTS from speaking formatting symbols.

---

## TTS Speaks Markdown Literally

**Symptom**: The agent says "asterisk asterisk bold asterisk asterisk" or "bracket one bracket".

**Root cause**: The LLM (especially Nusuk) returns markdown in responses. The TTS adapter did not strip it.

**Fix**: `_strip_markdown()` is already called in `CustomTTSChunkedStream._run()` before posting text to the TTS service. If this is happening, check that you are running the latest image after the fix was applied (`docker compose up --build`).

Patterns stripped:
- `**bold**` / `*italic*` → inner text
- `> blockquote` → removed
- `[4]` citation markers → removed
- `\n\n` paragraph breaks → space

---

## Sentence Buffering Not Firing

**Symptom**: AgentSession waits for the full LLM response before starting TTS (high TTFA despite streaming LLM).

**Root cause**: The LLM is not emitting sentence-boundary punctuation. AgentSession triggers TTS per sentence on `.`, `،`, `؟`, `!`, or newline. If the LLM returns a single long sentence with no punctuation, buffering fires only at the end.

**Fix**: Set `CUSTOM_LLM_QUERY_PREFIX` to include explicit punctuation instructions. For Nusuk, also verify the prefix is being prepended — check `llm_start ... query_len=...` in agent logs to confirm the prefix is included.

---

## Agent Does Not Respond to Arabic

**Symptom**: User speaks Arabic; the agent never replies or gives a generic response.

**Possible causes**:

1. **Turn detection not triggering**: `MultilingualModel` does not support Arabic (`ar`). The agent falls back to VAD-only silence detection. Increase `AGENT_MAX_ENDPOINTING_DELAY` if responses are cut off early.
2. **STT language hint wrong**: Verify `CUSTOM_STT_LANGUAGE=ar`.
3. **LLM language hint wrong**: Verify `CUSTOM_LLM_LANGUAGE=ar`.
4. **Query prefix in wrong language**: The prefix should be in Arabic (or bilingual) so Nusuk responds in Arabic.

---

## Docker Containers Cannot Reach Host Services

**Symptom**: ASR or TTS errors like `Connection refused` when services are running locally (not in Docker).

**Root cause**: Containers cannot use `localhost` to reach host services.

**Fix**: Use `host.docker.internal` as the hostname:
```env
CUSTOM_STT_URL=http://host.docker.internal:8102
CUSTOM_TTS_URL=http://host.docker.internal:8000
```

Also add to the agent service in `docker-compose.yml`:
```yaml
extra_hosts:
  - "host.docker.internal:host-gateway"
```

---

## Agent Worker Fails to Register

**Symptom**: Agent container starts but logs show it cannot connect to LiveKit, or rooms are created but no agent joins.

**Possible causes**:

1. **LiveKit not ready**: Agent `depends_on` LiveKit with `condition: service_healthy`. If the healthcheck fails, the agent won't start.
2. **Wrong `LIVEKIT_URL`**: The internal URL used by containers should be `ws://livekit-server:7880` (service name, not localhost).
3. **API key mismatch**: `LIVEKIT_API_KEY` / `LIVEKIT_API_SECRET` in `.env` must match `livekit-server/livekit.yaml`.

---

## Nusuk 401 on Every Call

**Symptom**: Logs show `llm_nusuk_401_invalidating_token` on every turn; responses never arrive.

**Possible causes**:

1. **Wrong `CUSTOM_LLM_CLIENT_ID` / `CUSTOM_LLM_CLIENT_SECRET`**: Check credentials match the Nusuk account.
2. **Auth endpoint unreachable**: The agent POSTs to `{CUSTOM_LLM_URL}/auth/token` — verify the URL is reachable from inside the container.
3. **Static `CUSTOM_LLM_ACCESS_TOKEN` expired**: If `client_id/client_secret` are not set, the agent uses `access_token` as a static Bearer. Static tokens expire; switch to `client_id` + `client_secret` for automatic refresh.

---

## Eval Shows `TTFA = n/a` for LiveKit Mode

**Symptom**: `eval/compare.py` reports `TTFA=n/a` and `speech_frames=0` for the LiveKit path.

**Root cause**: The eval script connected to the room but no audio track from the agent was received, or the audio source published zero frames.

**Steps**:

1. Check agent container logs for tracebacks.
2. Verify the `conn_options` parameter name is unchanged in `custom_stt.py._recognize_impl`.
3. Confirm `CUSTOM_STT_URL` and `CUSTOM_TTS_URL` are reachable.
4. Run with `--livekit-turn-mode explicit_eos` to bypass VAD and test the raw pipeline directly.

---

## High Latency on Consecutive Turns

**Symptom**: First turn is fast; subsequent turns take noticeably longer.

**Possible causes**:

1. **VAD endpointing delay compounding**: Check `AGENT_MIN_ENDPOINTING_DELAY` and `AGENT_MAX_ENDPOINTING_DELAY`. The defaults (0.5 s / 5.0 s) add silence-detection overhead.
2. **LLM session context growing**: Nusuk's `/chat/stream` uses `session_id` to track conversation history on its side. Longer history may increase LLM response time.
3. **httpx connection not reused**: STT, LLM, TTS, and the Nusuk token manager all share **one** `httpx.AsyncClient(http2=True)` built in `prewarm()` and stored in `proc.userdata["http_client"]`. The client lives for the worker process lifetime, not the session — no TCP/TLS re-handshake between turns or between sessions on the same worker. If you see fresh handshake time, check that `entrypoint` actually picked it up via `ctx.proc.userdata.get("http_client")` and passed it to each adapter constructor (otherwise each adapter falls back to creating its own and `_owns_client=True`).

---

## Push-to-Talk API 500 Errors

**Symptom**: Browser shows `Thinking…` state indefinitely; server logs show 500 from `/api/ptt/chat`.

**Possible causes**:

1. **Nusuk credentials missing**: `NUSUK_CLIENT_ID` / `NUSUK_CLIENT_SECRET` not set in the demo container.
2. **Module-scope token cache stale**: The Next.js `nusukAuth.ts` caches the token in module scope. If the module is hot-reloaded in dev mode, the cached token may be lost without re-fetch.
3. **NUSUK_URL not reachable from frontend container**: Add `extra_hosts: ["host.docker.internal:host-gateway"]` to the demo service if Nusuk is on localhost.
