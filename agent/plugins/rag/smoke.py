# Smoke test for the temp RAG adapter. Run inside the agent container:
#   docker compose exec agent python -m plugins.rag.smoke
#
# Requires:
#   - Milvus reachable at $MILVUS_HOST:$MILVUS_PORT
#     (locally: `kubectl port-forward -n nlp-rag svc/milvus 19530:19530`
#      from the host, and MILVUS_HOST=http://host.docker.internal in .env)
#   - $EMBEDDING_SERVICE_URL and $RERANK_SERVICE_URL reachable (public)

from __future__ import annotations

import asyncio
import sys

from .milvus_client import VectorClient
from .preprocessor_nusuk import get_vectorization_text
from .rerank import GenerativeRerankerModelAsync

QUERY_EN = "how do I book umrah"
QUERY_AR = "كيف أحجز عمرة؟"


async def _run_one(query: str) -> bool:
    print(f"\n=== query: {query!r} ===", flush=True)

    client = VectorClient()
    await client.connect()
    print(f"  connected uri={client.uri} collection={client.collection_name}", flush=True)

    results = await client.search(data=query)
    if not results:
        print("  FAIL: 0 hits from Milvus", file=sys.stderr)
        await client.disconnect()
        return False
    print(
        f"  milvus: {len(results)} hits  embed_ms={client._last_embedding_ms:.1f}  search_ms={client._last_milvus_ms:.1f}",
        flush=True,
    )
    print(f"  first hit fields: {sorted(results[0].keys())[:8]}", flush=True)

    reranker = GenerativeRerankerModelAsync()
    docs = [get_vectorization_text(r, r.get("collection_type", "")) for r in results]
    scores = await reranker.get_rerank_scores(query=query, documents=docs)
    if not scores:
        print("  FAIL: reranker returned no scores", file=sys.stderr)
        await reranker.aclose()
        await client.disconnect()
        return False

    max_score = max(s["reranked_score"] for s in scores)
    if max_score <= 0:
        print(
            f"  WARN: max rerank score is {max_score:.4f} — check prompt templates",
            file=sys.stderr,
        )
    print(f"  reranker: scored {len(scores)} docs  max_score={max_score:.4f}", flush=True)

    # Show the top-3 by rerank score.
    by_score = sorted(scores, key=lambda s: s["reranked_score"], reverse=True)
    for s in by_score[:3]:
        hit = results[s["index"]]
        ct = hit.get("collection_type")
        snippet = (
            (hit.get(f"{ct}_question_ar") or hit.get(f"title_ar") or hit.get(f"title_en") or "")
            if ct
            else ""
        )[:60]
        print(
            f"    rank: score={s['reranked_score']:.4f}  type={ct}  id={hit.get('id')}  text={snippet!r}",
            flush=True,
        )

    await reranker.aclose()
    await client.disconnect()
    return True


async def main() -> int:
    ok_en = await _run_one(QUERY_EN)
    ok_ar = await _run_one(QUERY_AR)
    if ok_en and ok_ar:
        print("\nOK", flush=True)
        return 0
    print("\nFAIL", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
