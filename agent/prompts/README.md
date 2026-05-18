# Voice prompts (durable)

This directory holds the **durable** voice-prompt artifact: the JSON sections,
the assembler, and the version label. It is intentionally **outside**
`agent/plugins/rag/` so it survives the eventual deletion of that temp subtree.

## Files

| File | Purpose |
|---|---|
| `prompts.json` | Source-of-truth voice-prompt sections (`RAG_VOICE_SECTIONS`) plus a top-level `prompt_version` string. Edit this to change agent behavior. |
| `voice_prompt.py` | Stdlib-only assembler. Reads `prompts.json`, prepends the absolute language rule, concatenates sections in `_SECTION_ORDER`, exposes `RAG_VOICE_PROMPT`, `PROMPT_HASH`, `PROMPT_VERSION`. |

## Versioning strategy (R-006)

Three layers:

1. **Git is the source of truth.** Every edit to `prompts.json` is a commit.
2. **`PROMPT_HASH`** (first 16 hex chars of sha256) is the runtime fingerprint.
   Logged per-turn by `custom_llm._run_nusuk_rag`, so any production response
   can be attributed to a specific prompt state.
3. **`prompt_version`** is the human-readable label inside `prompts.json`.
   Bump on shippable changes (e.g. `v4-2026-05-12` → `v5-2026-05-XX`).
   Logged alongside the hash for grep-ability.

When you change `prompts.json`, run the golden-set regression check before
merging — see [`eval/rag_qa/golden/README.md`](../../eval/rag_qa/golden/README.md).

## Future migration

The agent's RAG retrieval (Milvus + embedding + reranker, currently in
`agent/plugins/rag/`) will eventually move behind a `rag-nusuk-ai` endpoint
(`/voice/rag` or similar). When that happens:

- `agent/plugins/rag/` will be deleted in one PR.
- `agent/prompts/` (this directory) stays. The agent continues to use
  `RAG_VOICE_PROMPT` to seed its initial `Agent(instructions=...)`, and the
  fused per-turn system message is built either here or returned by
  rag-nusuk-ai. Decision deferred to the migration spec.
- If rag-nusuk-ai becomes the canonical source of prompts at that point, this
  directory either mirrors it (via a sync script) or is replaced by an
  HTTP-fetched assembler. Until then this is authoritative.
