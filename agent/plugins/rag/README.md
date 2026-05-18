# Temp RAG adapter — ported from rag-nusuk-ai

**Status: TEMPORARY.** This entire directory exists because Nusuk's `/chat/stream` endpoint is broken upstream (`GenericRAG.stream_search() got an unexpected keyword argument 'prompt_key'`). Once that's fixed, **delete this directory** and revert the temp changes elsewhere — see "Removal" below.

> **Note (2026-05-17, R-006):** The durable voice prompt has moved out of this subtree.
> `RAG_VOICE_SECTIONS`, `prompt_version`, and `voice_prompt.py` now live in
> [`agent/prompts/`](../../prompts/) so they survive the deletion of this directory.
> Only `config/prompts.json::RERANKER` (consumed by `rerank.py`) remains here and
> dies with the subtree.

## What this is

A self-contained Milvus + reranker retrieval stack so the S2S agent can produce RAG-augmented responses without depending on `/chat/stream`. It does:

1. Embed user query (HTTPS to `embed.llmtests.org`)
2. Hybrid search Milvus (dense HNSW + sparse BM25, RRF-merged)
3. Rerank top-20 with the generative reranker (HTTPS to `ranker.llmtests.org`)
4. Return top-12 ranked context docs

The `CustomLLM._run_nusuk_rag()` provider in `agent/plugins/custom_llm.py` calls `retrieve.retrieve(query)`, fuses the result into the system prompt (Nusuk-style), and streams via the existing Groq OpenAI-compat path. Nothing about STT, TTS, VAD, or the LiveKit session changes.

## Source of truth

All Python files (except `retrieve.py`, `smoke.py`, `settings_shim.py`) are ports from `/Users/a/Documents/ELM/repos/rag-nusuk-ai`. They follow [AGENT_HANDOFF_RAG_PORT.md](../../../../rag-nusuk-ai/AGENT_HANDOFF_RAG_PORT.md) in that repo. The configs under `config/` are trimmed copies of `rag-nusuk-ai/config/*.json`.

**Do not edit ported files to add features.** Patches should be made upstream first, then re-ported here. If a fix is urgent and upstream is slow, document the divergence at the top of the modified file.

## Multilingual note (reranker)

The `RERANKER.GENERATIVE` prompt in `config/prompts.json` uses English instructions (`Judge whether the Document meets...`) with native-language content. That's a deliberate pattern — the Qwen-style generative reranker model understands Arabic queries+documents even with English instruction text. If rerank quality on Arabic content turns out to be poor, translate the `instruction` field to Arabic — the templates support either language as long as the model does.

## Env vars

```
MILVUS_HOST=http://host.docker.internal     # docker → Mac's port-forward to GKE
MILVUS_PORT=19530
MILVUS_COLLECTION=unified_nusuk_collection2
MILVUS_TOKEN=root:Milvus
EMBEDDING_SERVICE_URL=https://embed.llmtests.org/v1/embeddings
RERANK_SERVICE_URL=https://ranker.llmtests.org/score
```

Plus the agent-side switch in `.env`:

```
CUSTOM_LLM_PROVIDER=nusuk_rag
CUSTOM_LLM_RAG_TOP_K=12
```

For local development, port-forward Milvus from the GKE dev cluster:

```bash
gcloud container clusters get-credentials hajj-umrah-nsk-dev --region me-central2
kubectl port-forward -n nlp-rag svc/milvus 19530:19530
```

Embedding + reranker are public — no port-forward.

## Smoke test

After `docker compose --profile demo up -d --build agent`:

```bash
docker compose exec agent python -m plugins.rag.smoke
```

Expected: connects to Milvus, gets ≥1 hit for "how do I book umrah", reranker returns positive scores, prints per-stage timings.

## Removal (when Nusuk `/chat/stream` is restored, OR when the agent moves to a `rag-nusuk-ai /voice/rag` endpoint)

1. `rm -rf agent/plugins/rag/` — **does NOT touch `agent/prompts/`** (durable; survives this removal, R-006).
2. In `agent/plugins/custom_llm.py`: delete the `_run_nusuk_rag` method and its dispatch line in `_run()`. Keep the `from prompts.voice_prompt import ...` line — it's still needed for whatever the new path is.
3. In `agent/config.py`: delete the `LLMSettings.rag_top_k` field.
4. In `agent/requirements.txt`: drop `pymilvus`.
5. In `.env` / `.env.example`: change `CUSTOM_LLM_PROVIDER=nusuk_rag` back to `nusuk` (or whatever production uses), remove `MILVUS_*` and `EMBEDDING_SERVICE_URL` / `RERANK_SERVICE_URL`.
6. Update `docs/changelog.md` and `TODO.md` to mark this work removed.
7. **Verify** the regen path in `agent/config.py` still works — it now imports from `prompts.voice_prompt` (not `plugins.rag.voice_prompt`), so it survives the delete by design.

The diff for the removal PR should touch ~6 files and be a clean delete. The golden-set regression check at `eval/rag_qa/golden/` should continue to pass against whatever replaces this subtree — if it doesn't, fix the new path before merging the removal.
