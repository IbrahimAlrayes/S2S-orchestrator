# Experiment Log

This file is the running markdown log for latency and quality experiments in `S2S-orchestrator`.

Future updates should append a new dated section instead of creating standalone notes.

## 2026-04-12 — VAD vs No-VAD Conversational Latency

### Question

Does enabling VAD improve or worsen conversational latency compared with a no-VAD baseline under identical audio conditions?

### Baseline and Method

- **No-VAD baseline**
  Direct pipeline timing from `eval/compare.py`:
  `STT wall + LLM TTFT + TTS wall`
- **VAD-enabled path**
  Full LiveKit pipeline timing from the same comparison harness:
  `end-of-speech -> first agent audio`
- **Primary metrics**
  - end-of-speech detection time
  - time to first token
  - total response time
- **Data source**
  Existing paired `direct` + `livekit` comparison runs in `eval/runs/compare-*`

### Dataset Summary

- Paired runs analyzed: `39`
- Post-speech runs: `23`
- Mid-utterance VAD-trigger runs: `16`

Post-speech means the user finished speaking and the agent replied after the end of the utterance.

Mid-utterance means the audio had an internal silence long enough for VAD to trigger before the whole file finished.

### Main Result

For normal **post-speech** turns, enabling VAD currently **worsens latency**.

Average post-speech results:

| Metric | No-VAD Baseline | VAD / LiveKit | Delta |
|---|---:|---:|---:|
| Time to first token | `0.598s` | `2.592s` | `+1.994s` |
| Time to first audio | `0.915s` | `2.909s` | `+1.994s` |
| Response complete | `7.817s` | `8.887s` | `+1.070s` |

Interpretation:

- VAD adds about **2.0 seconds** before the pipeline effectively starts in the common post-speech case.
- Most of that delay is endpointing / turn-finalization delay, not model inference.
- Once the final turn is committed, STT/LLM/TTS remain relatively fast.

### Important Nuance

VAD is not always harmful.

For the `16` **mid-utterance** runs:

- Average VAD-added detection time: `-1.960s`

Interpretation:

- On long audio with natural internal pauses, VAD can trigger early and reduce latency.
- This is beneficial for genuinely conversational speech with pauses.
- It is harmful when the utterance ends cleanly but VAD waits too long to commit the turn.

### Aggregate Results

#### All paired runs (`39`)

| Metric | Avg | Median | Min | Max |
|---|---:|---:|---:|---:|
| No-VAD first token | `0.640s` | `0.567s` | `0.291s` | `1.565s` |
| No-VAD first audio | `0.947s` | `0.837s` | `0.556s` | `1.884s` |
| No-VAD response complete | `7.607s` | `7.560s` | `4.216s` | `11.843s` |
| VAD first audio from end | `1.319s` | `1.417s` | `-4.466s` | `11.463s` |
| VAD-added detection time | `0.372s` | `0.662s` | `-5.205s` | `10.874s` |
| VAD-estimated first token | `1.011s` | `1.124s` | `-4.698s` | `11.188s` |
| VAD response complete | `7.651s` | `8.665s` | `-2.316s` | `13.688s` |

#### Post-speech only (`23`)

| Metric | Avg | Median | Min | Max |
|---|---:|---:|---:|---:|
| No-VAD first token | `0.598s` | `0.550s` | `0.291s` | `1.235s` |
| No-VAD first audio | `0.915s` | `0.837s` | `0.556s` | `1.513s` |
| No-VAD response complete | `7.817s` | `7.674s` | `4.481s` | `11.843s` |
| VAD first audio from end | `2.909s` | `2.588s` | `1.083s` | `11.463s` |
| VAD-added detection time | `1.994s` | `1.773s` | `0.245s` | `10.874s` |
| VAD-estimated first token | `2.592s` | `2.230s` | `0.786s` | `11.188s` |
| VAD response complete | `8.887s` | `8.968s` | `1.863s` | `13.551s` |

#### Mid-utterance only (`16`)

| Metric | Avg | Median | Min | Max |
|---|---:|---:|---:|---:|
| No-VAD first token | `0.700s` | `0.614s` | `0.325s` | `1.565s` |
| No-VAD first audio | `0.993s` | `0.923s` | `0.565s` | `1.884s` |
| No-VAD response complete | `7.304s` | `7.508s` | `4.216s` | `10.314s` |
| VAD first audio from end | `-0.967s` | `-0.831s` | `-4.466s` | `1.447s` |
| VAD-added detection time | `-1.960s` | `-1.518s` | `-5.205s` | `-0.146s` |
| VAD-estimated first token | `-1.261s` | `-1.100s` | `-4.698s` | `1.128s` |
| VAD response complete | `5.875s` | `5.572s` | `-2.316s` | `13.688s` |

### Representative Examples

#### Best post-speech cases

| Audio | No-VAD first audio | VAD first audio from end | Added detection time |
|---|---:|---:|---:|
| `chunk_0011.wav` | `0.837s` | `1.083s` | `+0.246s` |
| `chunk_0011.wav` | `0.901s` | `1.265s` | `+0.364s` |
| `chunk_0014.wav` | `1.246s` | `1.908s` | `+0.662s` |

#### Worst post-speech cases

| Audio | No-VAD first audio | VAD first audio from end | Added detection time |
|---|---:|---:|---:|
| `chunk_0011.wav` | `0.589s` | `11.463s` | `+10.874s` |
| `chunk_0010.wav` | `1.067s` | `4.106s` | `+3.039s` |
| `chunk_0005.wav` | `0.670s` | `3.575s` | `+2.905s` |

#### Strong mid-utterance wins

| Audio | No-VAD first audio | VAD first audio from end | Added detection time |
|---|---:|---:|---:|
| `chunk_0008.wav` | `0.739s` | `-4.466s` | `-5.205s` |
| `chunk_0013.wav` | `1.183s` | `-3.885s` | `-5.068s` |
| `chunk_0017.wav` | `1.341s` | `-3.708s` | `-5.049s` |

### Takeaway

Current behavior:

- **Post-speech turns**: VAD is mostly hurting latency.
- **Long utterances with internal pauses**: VAD can help by committing early.

So the optimization target is not “remove VAD entirely,” but:

1. Reduce endpointing delay for normal post-speech turns.
2. Preserve early commit behavior for genuine mid-utterance pauses.

### Recommended Next Steps

1. Tune `min_endpointing_delay` and `max_endpointing_delay`.
2. Re-run this comparison after each tuning change.
3. Evaluate semantic turn detection as a replacement for pure silence-based endpointing.
4. Keep logging future experiments in this file.

### Notes

- A fresh rerun on `2026-04-12` using `eval/compare.py` produced incomplete LiveKit capture artifacts for some files, so this write-up relies on the existing successful paired comparison runs already saved under `eval/runs/compare-*`.
- The reusable analyzer used for this summary is [analyze_vad_impact.py](/home/elm/Projects/S2S-orchestrator/eval/analyze_vad_impact.py).

## 2026-04-15 — VAD toggle exposed per call (superseded)

### Change

This experiment introduced a per-call `turn_detection=on|off` toggle carried through LiveKit room metadata:

- `demo/components/app/app.tsx` rebuilt the token source with `/api/token?turn_detection=on|off`.
- `demo/app/api/token/route.ts` wrote `{"turn_detection": "on"|"off"}` into `RoomConfiguration.metadata`.
- `agent/agent.py` parsed `ctx.room.metadata` and switched between the env defaults and a short-fuse preset.

### Why

The prior finding (2026-04-12) showed VAD + MultilingualModel adds ~2s on post-speech turns. Rather than ship a single global preset, this let the operator compare both behaviours live during a walkthrough.

This was later removed from the checked-in demo code. The current repo state keeps turn detection always on when the dependency is available, so treat this section as experiment history rather than current behavior.

### TODO

- Capture round-trip latency on `on` vs `off` during the next live run and append numbers here.
- Revisit the short-fuse preset values once the semantic turn-detection experiment lands.

## 2026-05-10 — Direct pipeline baseline on current Groq + Nusuk config

### Question

What is the per-stage and end-to-end latency of the direct pipeline (no LiveKit, no VAD, no agent process) against the production endpoints we use today: Nusuk for STT/TTS and Groq for LLM?

### Method

- New script `eval/quick_speed.py` — replaces `eval/run_pipeline_eval.py` for this run because the older script is hardcoded for the `local_api` flavor (`/api/transcribe/`, static bearer) and doesn't speak Nusuk's JWT or the Groq OpenAI-compat path.
- Hits the same URLs and auth the agent uses: Nusuk JWT (`POST /transcribe`, `POST /synthesize`) and Groq Bearer (`POST /chat/completions` with `stream=true`, `reasoning_effort=low`, system prompt loaded from `agent/system_prompt_rag.txt`, query prefix from `CUSTOM_LLM_QUERY_PREFIX`).
- Streams the LLM and TTS responses to capture TTFT and TTFA — the metrics that matter for perceived responsiveness.
- Run from the Mac host (Saudi Arabia → Nusuk dev in Belgium, ~117 ms RTT).

### Dataset

20 Arabic clips from `eval/testdata/chunk_0000.wav` … `chunk_0019.wav`. Audio durations 1.23s – 19.38s.

### Aggregate per-stage results

| Stage | min | p50 | mean | max |
|---|---:|---:|---:|---:|
| STT (full WAV → transcript) | `0.367s` | `0.546s` | `0.583s` | `1.047s` |
| LLM TTFT (Groq, `gpt-oss-120b`) | `0.196s` | `0.231s` | `0.250s` | `0.375s` |
| LLM full reply | `0.264s` | `0.308s` | `0.324s` | `0.457s` |
| TTS TTFA (first PCM chunk) | `0.194s` | `0.206s` | `0.209s` | `0.238s` |
| TTS full audio body | `0.961s` | `1.097s` | `1.142s` | `1.404s` |
| **E2E first audio** (STT + LLM-TTFT + TTS-TTFA) | `0.814s` | `0.990s` | `1.043s` | `1.546s` |
| E2E total (STT + LLM-total + TTS-total) | `1.669s` | `1.985s` | `2.050s` | `2.807s` |

### Observations

- **Cold-start STT**: `chunk_0000.wav` paid `1.047s` on STT vs ~`0.55s` for every subsequent clip on the same connection. The first call eats the TLS handshake + initial connection setup; httpx keepalive amortizes the rest. The agent's prewarm prefetch already heats this connection, so a real LiveKit session shouldn't pay it on turn 1.
- **Streaming TTS pays off cleanly**: TTFA is ~`210 ms` regardless of reply length (TTS body ranges 285–525 KB, full audio takes ~1.1s). The agent pipes chunks straight to LiveKit as they arrive — it does not wait for the full body.
- **LLM is the cheapest stage**: Groq + `reasoning_effort=low` keeps TTFT under `0.25s` p50. The `<think>` reasoning trace is collapsed and never reaches TTS.
- **Versus handoff expectations** (handoff cited TTFA `2.5–3.5s`, STT `~1.5s`): direct-pipeline numbers are well below those, because the handoff numbers reflect *in-session* TTFA, which adds VAD endpointing (`0.5–1s`) + LiveKit signaling/jitter buffer (`~0.3s`) on top of these direct measurements.

### Takeaway

The HTTP pipeline (STT + LLM + TTS) is not the bottleneck for in-session TTFA. The dominant non-network cost remains VAD endpointing. To attack perceived latency, the priority is endpointing tuning (revisit `min_endpointing_delay`, semantic turn detection) — not STT/LLM/TTS optimization.

### Caveats

- Direct HTTP, no agent process — bypasses VAD, STT streaming wrapper, sentence buffering, preemptive TTS, and the LiveKit SFU.
- Mac host with home internet; results from a server in EU/ME may differ.
- Single sequential run, no warmup pass discarded — the first-call STT outlier is real but biases the means slightly upward.

### Next

- Pair this baseline with a fresh LiveKit-mode run via `eval/compare.py` once `compare.py` is updated to also speak Nusuk JWT (it currently has the same `local_api` assumption as `run_pipeline_eval.py`).
- Re-run after each endpointing-tuning change and append a new dated section here.
