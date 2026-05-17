# TODO

## Done

- [x] Validate the implementation plan against current LiveKit docs
- [x] Scaffold the new repo structure and base config
- [x] Add Docker Compose stack with LiveKit, Redis, agent, and token server
- [x] Implement token generation service
- [x] Implement agent worker skeleton with VAD prewarm and session hooks
- [x] Add first-pass external STT, LLM, and TTS adapter modules
- [x] Make LiveKit room I/O options explicit in the agent
- [x] Surface interruption and turn-handling controls in config
- [x] Fix LiveKit demo (STT/TTS/LLM URLs, Nusuk auth, wrapper TTS provider)
- [x] Build push-to-talk demo (PTT page, ASR/chat/TTS proxies, Nusuk JWT server-side)
- [x] Remove VAD per-call toggle (turn detection always on)
- [x] Clean up and comment all Python agent code
- [x] Strip markdown from TTS input (prevents **bold**, [4] refs being spoken)
- [x] Tune system prompt for proper Arabic punctuation (enables LiveKit sentence buffering)
- [x] Add eval comparison script (direct vs LiveKit, Nusuk + wrapper TTS)
- [x] Document production deployment in README (CPU/GPU split, public IP gotcha, VAD on CPU)

## Pending

### Latency
- [~] Run full 20-file eval benchmark (direct vs LiveKit) and record results in EXPERIMENT_LOG.md
  - [x] Direct mode — recorded under "2026-05-10 — Direct pipeline baseline" via `eval/quick_speed.py`
  - [ ] LiveKit mode — `eval/compare.py` still hardcoded for `local_api` flavor, needs Nusuk-JWT support before re-running
- [ ] **In-cluster agent migration** — agent ships as part of Nusuk's voice API. Customer flow: `POST dev.nusukai.com/voice/session` → returns LiveKit Cloud URL + token → customer connects to LiveKit Cloud edge → agent (in nsk cluster) is dispatched. Predicted STT warm wall: 568 ms → ~110 ms. See `docs/in-cluster-migration.md`.
- [ ] **LiveKit Cloud project setup** — create Nusuk project, capture URL + API key/secret, distribute to both `s2s-agent/agent-secret` and `nlp-rag/nlp-rag-app-secret`. See `docs/in-cluster-migration.md` §10.
- [ ] **nlp-rag-app `/voice/session` endpoint** — coordinate with the nlp-rag-app team. Endpoint mints a LiveKit participant token + creates a `RoomConfiguration` with `agents: [{ agentName: "nusuk-agent" }]`. Uses `livekit-api` Python SDK. Auth via existing Nusuk JWT.
- [ ] **Remove temp local-RAG provider** (`agent/plugins/rag/`, `CustomLLM._run_nusuk_rag`, `LLMSettings.rag_top_k`, `pymilvus` dep, RAG env vars) once Nusuk `/chat/stream` is fixed upstream. See `agent/plugins/rag/README.md` for the removal checklist.
- [ ] Confirm Nusuk sentence boundaries trigger LiveKit sentence buffering correctly
- [ ] Measure TTFA improvement after system prompt + markdown strip fix

### Quality
- [ ] Confirm the exact ASR endpoint contract and audio format requirements
- [ ] Confirm the exact TTS endpoint contract and streaming capabilities
- [ ] Confirm Nusuk honors the system prompt (currently returns 150+ word responses despite 50-word limit)

### Production
- [ ] Add external transcript/session persistence only if product requirements need it
- [ ] Add integration tests and structured observability
- [ ] Test CPU/GPU split deployment on real machines
- [ ] Configure LiveKit `use_external_ip: true` in livekit.yaml before cloud deploy
