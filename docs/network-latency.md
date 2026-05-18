# Network latency — empirical measurements

> **Status:** ground-truth measurements, captured 2026-05-17.
> **Why this exists:** [`in-cluster-migration.md`](in-cluster-migration.md) argues the agent should move into me-central2. This doc shows the real numbers behind that claim — per-component, measured from both sides of the move — so anyone re-evaluating the cost/benefit later has the raw data, not a verbal summary.

Companion to [`docs/changelog.md`](changelog.md) (2026-05-17 entries) and [`eval/network_breakdown.py`](../eval/network_breakdown.py).

---

## 1. TL;DR

| Setup | Mouth-to-ear (p50) | Pipeline TTFT (retrieve + LLM first token) |
|---|---:|---:|
| Today — agent on a Mac in Riyadh (Zain) | **~3.2 s** | 1370 ms |
| Agent moved into me-central2 (in-cluster) | **~1.7 s** | ~445 ms |
| In-cluster + 200 ms endpointing + streaming TTS + Saudi LLM | **~0.8 s** | ~250 ms |

Two surprises worth knowing before you read the rest:
- **Groq LLM is slower from me-central2 than from a Riyadh consumer ISP** — 175 ms TTFB from Mac vs 377 ms from cluster. The Cloudflare anycast that serves Zain Riyadh lands you on a *better* path to Groq than GCP me-central2 egress does.
- **TTS `curl time_starttransfer` is not "time to first audio".** The FastAPI `StreamingResponse` flushes HTTP headers ~10 ms after the request, but the first audio byte doesn't ship until TTS span 0 finishes computing (~550 ms server-side for a 51-char Arabic sentence). Per-R-019 design. Always measure via byte-level chunk timing, not `curl -w`.

---

## 2. Setup

**Client A (current production-shaped):** Mac in Riyadh on Zain (`51.39.229.19`, AS43766 STC mobile). Docker container with `host.docker.internal` → Mac.

**Client B (in-cluster target):** Pod `nlp-rag-app-8647cc84db-ds8h5` in namespace `nlp-rag`, GKE cluster `gke_hajj-umrah-nsk-dev_me-central2_nsk` (Dammam region).

**Trial protocol:** 5 warm trials per measurement, keep-alive reused. Bearer token obtained once via `POST /auth/token` with `{client_id, client_secret, user_id}`. Audio: `eval/testdata/chunk_0005.wav` (4.1 s Arabic, 128 KB). TTS text: `"السعودية بلد عربي. عاصمتها الرياض. تقع في غرب آسيا."` (51 chars).

**Endpoints exercised:**

| Service | External URL | In-cluster ClusterIP |
|---|---|---|
| Auth | `https://dev.nusukai.com/auth/token` | `http://nlp-rag-app:80/auth/token` |
| STT | `https://dev.nusukai.com/transcribe` | `http://nlp-rag-app:80/transcribe` |
| TTS | `https://dev.nusukai.com/synthesize` | `http://nlp-rag-app:80/synthesize` |
| Embedding | `https://embed.llmtests.org/v1/embeddings` | `http://embedding:8082/v1/embeddings` |
| Reranker | `https://ranker.llmtests.org/score` | `http://reranker:8002/score` |
| Milvus | (port-forward `kubectl port-forward -n nlp-rag svc/milvus 19530:19530`) | `milvus:19530` |
| LLM | `https://api.groq.com/openai/v1/chat/completions` | same (Groq is external in both worlds) |

---

## 3. Raw TCP RTT (10 samples each)

| Target | min | **p50** | mean | max |
|---|---:|---:|---:|---:|
| **From Mac (Riyadh)** | | | | |
| `dev.nusukai.com:443` | 105 | **111** | 169 | 404 |
| `api.groq.com:443` | 27 | **30.5** | 34 | 55 |
| `embed.llmtests.org:443` | (Cloudflare edge) | ~115 | | |
| `ranker.llmtests.org:443` | (Cloudflare edge) | ~130 | | |
| **From pod (me-central2)** | | | | |
| `milvus:19530` | 1.1 | **1.4** | 1.8 | 6.1 |
| `embedding:8082` | 0.5 | **0.6** | 0.9 | 3.7 |
| `reranker:8002` | 0.4 | **0.5** | 0.7 | 2.2 |
| `nlp-rag-app:80` | 0.3 | **0.6** | 1.0 | 2.6 |
| `dev.nusukai.com:443` | 1.5 | **2.1** | 21.2 | 190.9 |
| `embed.llmtests.org:443` | 70.6 | **73.4** | 114.9 | 484.5 |
| `ranker.llmtests.org:443` | 69.9 | **71.4** | 81.8 | 165.2 |
| `api.groq.com:443` | 69.9 | **73.4** | 75.5 | 95.6 |

The `dev.nusukai.com:443` p50 = 2.1 ms from the pod means: when *the cluster itself* hits the public hostname, the request stays internal — the GCLB recognises in-region traffic and short-circuits. From a Saudi consumer ISP the same hostname is 111 ms because global anycast steers to a non-Saudi PoP.

---

## 4. /transcribe — STT, 4.1 s Arabic, 128 KB upload

| Source | Wall (warm p50) | Server compute (`processing_time_seconds`) | Network share |
|---|---:|---:|---:|
| **Mac → dev.nusukai.com** (eval-measured) | 489 ms | 60 ms | **87.8 %** |
| **Pod → nlp-rag-app:80** (HTTP, internal) | **181 ms** | 58 ms | 32 % |
| **Pod → dev.nusukai.com** (HTTPS, external from pod) | 181 ms | 59 ms | 32 % |

Real five-trial sequence (pod, internal):
```
1: total=0.222s ttfb=0.222s
2: total=0.156s ttfb=0.156s
3: total=0.181s ttfb=0.181s
4: total=0.176s ttfb=0.176s
5: total=0.184s ttfb=0.184s
```

The ~120 ms gap between server compute (60 ms) and wall (181 ms) is **FastAPI → Triton proxy overhead**. That's the floor for `/transcribe` until/unless the agent talks gRPC to Triton directly (Path B in [`in-cluster-migration.md`](in-cluster-migration.md)).

**Internal ClusterIP vs external from inside cluster:** identical for `/transcribe` because the public hostname routes internally too (see §3 RTT note).

---

## 5. /synthesize — TTS, 51-char Arabic, 175 KB WAV response

This is where naive `curl -w "%{time_starttransfer}"` lies. The server is a FastAPI `StreamingResponse`; it sends HTTP headers immediately (committing to a stream), then per-R-019 awaits TTS **span 0** before flushing the first body byte. Two distinct numbers matter:

- `t_headers_ms` = time-to-HTTP-headers (`curl`'s `time_starttransfer` measures this for streaming responses)
- **`t_first_body_ms` = time-to-first-audio-byte** ← this is what voice UX cares about

Byte-level chunk timing, **5 warm trials each, p50**:

| Source | HTTP headers | **First audio byte (TTFA)** | Done (full body) |
|---|---:|---:|---:|
| Mac → dev.nusukai.com | 205–534 ms | **~852 ms** | 1077–1678 ms |
| Pod → nlp-rag-app:80 (HTTP) | 11–23 ms | **~556 ms** | 877–883 ms |
| Pod → dev.nusukai.com (HTTPS) | 42–66 ms | ~583 ms | 906–927 ms |

**TTS span-0 compute time for this text ≈ 550 ms** (subtract internal-cluster network from the pod TTFA: 556 − 2 RTT − HTTP overhead ≈ 550 ms). That's the model time; nothing about co-location can shrink it. The 295 ms TTFA win from in-cluster vs Mac (852 → 556) is **all network**, not compute.

**Earlier reporting in this repo's `network_breakdown.py` reported "TTFA = 203 ms warm" for the Mac case** — that was `time_starttransfer` from aiohttp, i.e., time to HTTP headers, not first audio. The true Mac TTFA is ~850 ms. If you re-quote the old number, correct it.

---

## 6. Embedding — POST 2 texts, normalize=true

Same model, same response payload size in both cases (43,658 bytes vs 43,643 bytes — diff is timestamp), same response format. The "external" host is the same in-cluster `embedding` service behind a Cloudflare proxy.

| Source | Wall (warm p50) |
|---|---:|
| Mac (eval-logged) | 280 ms |
| Pod → `embedding:8082` (HTTP, internal) | **15 ms** |
| Pod → `embed.llmtests.org` (HTTPS, through Cloudflare) | 437 ms |

**18.7× speedup measured.** Cloudflare's edge is fast (~75 ms connect from pod), but the path Cloudflare → origin is the slow leg — and the origin is right next door in the cluster.

---

## 7. Reranker — POST 10 docs

| Source | Wall (warm p50) |
|---|---:|
| Mac (eval-logged) | 390 ms |
| Pod → `reranker:8002` (HTTP, internal) | **21 ms** |
| Pod → `ranker.llmtests.org` (HTTPS) | 439 ms |

**18.6× speedup measured.** Same diagnosis as embedding — Cloudflare edge → origin round-trip dominates.

---

## 8. Milvus search

| Source | TCP RTT | Search latency (whole call, per eval logs) |
|---|---:|---:|
| Mac via `kubectl port-forward` | ~75 ms (most is tunnel overhead) | ~75 ms |
| Pod, ClusterIP `milvus:19530` | 1.4 ms | ~5–10 ms (compute + ClusterIP RTT) |

The 75 ms from Mac was dominated by the kubectl WS tunnel hop to the GKE API server (which then proxies to the in-cluster Service); the actual Milvus hybrid-search compute is single-digit ms.

---

## 9. Groq LLM (api.groq.com) — the counter-intuitive one

Groq is in the US and Cloudflare-fronted. Distance to a Cloudflare PoP depends on your network's BGP, not your geography.

| Source | TCP RTT | TTFB |
|---|---:|---:|
| Mac (Riyadh, Zain) | 30 ms | **175 ms** |
| Pod (me-central2 egress) | 73 ms | **377 ms** |

**Groq is ~200 ms slower from the cluster than from your Riyadh consumer ISP.** Why: Zain has a Cloudflare PoP route that lands close to Groq's origin; me-central2 GCP egress hits a different (worse for Groq) PoP. This is a *negative* delta in the migration math — Groq TTFT in-cluster will be ~400 ms p50, not the 175 ms you see locally.

If LLM TTFT ever becomes the floor (after the embed/rerank/STT/TTS wins land), the answer is **a Saudi-hosted LLM**, not a different network config.

---

## 10. Composed end-to-end (mouth-to-ear)

```
                                  Mac (Riyadh)         In-cluster (me-central2)
                                  ────────────         ───────────────────────
VAD endpointing (silence wait)       500 ms                 500 ms     (config)
STT /transcribe wall                 489 ms                 181 ms     ← §4
RAG retrieve (embed+milvus+rerank)   768 ms                  43 ms     ← §§6,7,8 (15+7+21)
LLM TTFT (Groq)                      559 ms                 400 ms     ← §9
TTS time-to-first-audio              852 ms                 556 ms     ← §5 (byte-level)
                                  ────────────         ───────────────────────
TOTAL (mouth-to-ear)                3168 ms                1680 ms
```

In-cluster is **~47 % faster** end-to-end. RAG retrieve goes from a major cost to negligible (18×). The remaining time is split roughly equally between three things: VAD endpointing, TTS span-0 compute, and the LLM TTFT.

---

## 11. Where the floor is

After in-cluster, the budget is dominated by four roughly equal components. To beat ~1.7 s, you have to attack at least one:

| Component | In-cluster cost | Lever | Estimated save |
|---|---:|---|---:|
| VAD endpointing | 500 ms | Turn-detection model (replace silence-wait with end-of-utterance prediction) | −300 ms |
| TTS span-0 compute | 550 ms | Smaller / streaming TTS that emits before the full first sentence is rendered | −250 ms |
| LLM TTFT | 400 ms | Saudi-hosted LLM (replace Groq); requires model swap | −200 ms |
| STT proxy overhead | 120 ms (above 60 ms compute) | Triton gRPC directly, bypass FastAPI hop | −80 ms |
| RAG retrieve | 43 ms | (already optimal at this scale) | — |

Stacked best-case: ~1.7 s → ~0.8 s. Below 0.7 s is into hardware/model territory.

---

## 12. What is NOT a fix

These were considered and ruled out by the data above:

- **Better wifi.** Wifi adds 3 ms (traceroute showed `oppowifi.com:192.168.0.1` at 3.135 ms). Not relevant.
- **Faster ISP plan.** Bandwidth isn't the bottleneck — 128 KB STT upload is ~10 ms on any reasonable link. RTT is.
- **CDN in front of `/transcribe`.** POST with unique body, non-cacheable. Edge can't help.
- **More cores on the client.** The client doesn't compute anything heavy.
- **HTTPS → HTTP for in-cluster.** Already done (ClusterIP services are plaintext intracluster).

---

## 13. How to re-run

**Pod TCP probe:**
```bash
POD=nlp-rag-app-8647cc84db-ds8h5
kubectl exec -n nlp-rag $POD -- python3 -c "..."  # see §3 script in conversation history
```

**Real /transcribe and /synthesize timings from inside the pod** (need a valid bearer; the test client is `{"client_id":"test@elm.sa","client_secret":"112233","user_id":"user-678"}`):

```bash
kubectl cp -n nlp-rag eval/testdata/chunk_0005.wav $POD:/tmp/chunk_0005.wav
kubectl exec -n nlp-rag $POD -- bash -c '<token; curl ... /transcribe>'
```

**Byte-level TTS chunk timing** (correct way to measure TTFA) — use `http.client` with `resp.read(4096)` and stamp each chunk arrival. `curl -w "%{time_starttransfer}"` is wrong for streaming WAV responses.

**Mac-side Nusuk endpoint probes** are also packaged in [`eval/network_breakdown.py`](../eval/network_breakdown.py) — but note that script reports "TTFA" via `aiohttp` `time_starttransfer`, which is HTTP-header time. To measure true TTFA, use the byte-level approach from §5.

---

## 14. Sources of variance / caveats

- **Single-day snapshot (2026-05-17).** Network state changes; re-run before quoting these numbers to anyone who matters.
- **5 trials per measurement** — small sample, noticeable variance on TTS in particular (one trial of 1678 ms vs four trials of ~1100 ms).
- **Bench audio is one fixed 4.1 s WAV.** Longer audio = more upload bytes + more STT compute; STT compute scales with audio length. Numbers here are for ~4 s utterances.
- **Bench TTS text is one fixed 51-char Arabic.** TTS compute scales roughly with character count. A 200-char reply would have a longer span-0 (probably ~1.5 s server-side), pushing TTFA proportionally.
- **Test client (`test@elm.sa`)** may have different rate-limiting / priority than a real production client.
- **Cluster load varies.** The `nlp-rag-app` pod had 5 replicas at measurement time. Numbers under sustained load may differ.

---

## 15. Related docs

- [`docs/in-cluster-migration.md`](in-cluster-migration.md) — the migration plan these numbers justify
- [`docs/changelog.md`](changelog.md) — 2026-05-12 LiveKit UDP-range fix; 2026-05-17 prompt versioning + risk closures
- [`../S2S-roadmap/docs/06-risks-issues.md`](../../S2S-roadmap/docs/06-risks-issues.md) — R-019 (TTS span-0 pre-validation) explains the "header vs first audio" behavior; R-023 (streaming WAV with placeholder sizes) explains the early-flushed header
- [`../S2S-roadmap/docs/evaluation/s2s-eval-plan.md`](../../S2S-roadmap/docs/evaluation/s2s-eval-plan.md) — broader S2S eval plan
- [`eval/network_breakdown.py`](../eval/network_breakdown.py) — Mac-side probe script
- [`eval/rag_qa/run.py`](../eval/rag_qa/run.py) — end-to-end eval runner with per-stage timings
