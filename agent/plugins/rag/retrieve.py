# High-level retrieval orchestrator. One call per agent turn:
#   query -> embed -> Milvus hybrid search -> rerank -> top_k by rerank score
#
# A single VectorClient and a single GenerativeRerankerModelAsync are held at
# module scope and shared across all turns inside a worker process. Connect
# happens lazily on first call.

from __future__ import annotations

import asyncio
import logging
import socket
import time
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

from .milvus_client import VectorClient
from .preprocessor_nusuk import get_vectorization_text
from .rerank import GenerativeRerankerModelAsync
from .settings_shim import settings

logger = logging.getLogger(__name__)

_client: Optional[VectorClient] = None
_reranker: Optional[GenerativeRerankerModelAsync] = None
_init_lock = asyncio.Lock()


def _milvus_host_port() -> tuple[str, int]:
    """Extract (host, port) from settings. MILVUS_HOST may be a bare hostname
    or include a scheme like `http://`."""
    host = settings.MILVUS_HOST or ""
    if "://" in host:
        host = urlparse(host).hostname or host.split("://", 1)[1]
    return host, int(settings.MILVUS_PORT or "19530")


async def _milvus_reachable(timeout_s: float = 0.5) -> bool:
    """Cheap TCP-only probe: returns True if Milvus's port is accepting
    connections, False otherwise. Used to short-circuit before invoking
    pymilvus's MilvusClient (whose own connect has a 5-10 s TCP timeout
    that blocks the agent's hot path)."""
    host, port = _milvus_host_port()
    if not host:
        return False

    loop = asyncio.get_running_loop()

    def _probe() -> bool:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(timeout_s)
        try:
            sock.connect((host, port))
            return True
        except OSError:
            return False
        finally:
            sock.close()

    try:
        return await asyncio.wait_for(loop.run_in_executor(None, _probe), timeout=timeout_s + 0.2)
    except (asyncio.TimeoutError, Exception):
        return False


async def _ensure_initialized() -> None:
    global _client, _reranker
    if _client is not None and _reranker is not None:
        return

    # Pre-flight TCP probe so we never block on pymilvus's multi-second TCP
    # timeout when the port-forward (or the cluster) is unreachable.
    if not await _milvus_reachable():
        raise RuntimeError("milvus_unreachable")

    async with _init_lock:
        if _client is not None and _reranker is not None:
            return
        client = VectorClient()
        # Short retry budget — if Milvus IS reachable at the TCP level but
        # the pymilvus handshake fails (auth, collection missing, etc.), fail
        # fast so the agent falls back to plain Groq.
        await client.connect(max_retries=2, base_delay=1.0)
        reranker = GenerativeRerankerModelAsync()
        _client = client
        _reranker = reranker
        logger.info(
            "rag_initialized uri=%s collection=%s",
            client.uri,
            client.collection_name,
        )


async def retrieve(query: str, top_k: int = 12) -> List[Dict[str, Any]]:
    """One-shot RAG retrieval: query -> ranked context.

    On failure (Milvus down, embed timeout, etc.) logs a warning and returns
    an empty list. The caller is expected to handle the empty case (e.g. the
    CustomLLM provider falls back to non-RAG generation).
    """
    if not query or not query.strip():
        return []

    try:
        await _ensure_initialized()
        assert _client is not None and _reranker is not None
    except RuntimeError as exc:
        # Expected: port-forward down, Milvus unreachable. One-line log so it
        # doesn't spam the agent log on every turn.
        logger.warning("rag_disabled reason=%s — falling back to plain LLM", exc)
        return []
    except Exception:
        logger.warning("rag_init_failed", exc_info=True)
        return []

    t0 = time.perf_counter()
    try:
        # 1. Hybrid search (embedding is computed inside .search()).
        hits = await _client.search(data=query)
    except Exception:
        logger.warning("rag_search_failed", exc_info=True)
        return []

    if not hits:
        logger.info("rag_search_empty query=%r", query[:80])
        return []

    embed_ms = _client._last_embedding_ms
    milvus_ms = _client._last_milvus_ms

    # 2. Rerank.
    try:
        docs = [get_vectorization_text(r, r.get("collection_type", "")) for r in hits]
        t1 = time.perf_counter()
        scores = await _reranker.get_rerank_scores(query=query, documents=docs)
        rerank_ms = (time.perf_counter() - t1) * 1000
        for s in scores:
            idx = s["index"]
            if 0 <= idx < len(hits):
                hits[idx]["reranked_score"] = s["reranked_score"]
        # Sort by rerank score where available, falling back to embedding score.
        hits.sort(
            key=lambda r: r.get("reranked_score", r.get("embedding_score", 0)) or 0,
            reverse=True,
        )
    except Exception:
        logger.warning("rag_rerank_failed_keeping_embedding_order", exc_info=True)
        rerank_ms = 0.0

    total_ms = (time.perf_counter() - t0) * 1000
    top = hits[:top_k]
    logger.info(
        "rag_done hits=%d top_k=%d embed_ms=%.1f milvus_ms=%.1f rerank_ms=%.1f total_ms=%.1f",
        len(hits),
        len(top),
        embed_ms,
        milvus_ms,
        rerank_ms,
        total_ms,
    )
    return top


async def close() -> None:
    """Tear down module-scope state. Call on worker shutdown."""
    global _client, _reranker
    if _reranker is not None:
        try:
            await _reranker.aclose()
        except Exception:
            logger.warning("rag_close_reranker_failed", exc_info=True)
        _reranker = None
    if _client is not None:
        try:
            await _client.disconnect()
        except Exception:
            logger.warning("rag_close_client_failed", exc_info=True)
        _client = None


def format_context_block(hits: List[Dict[str, Any]]) -> str:
    """Build the citation-numbered context block injected into the system prompt.

    Each hit becomes a `[N]` block followed by the vectorization text. The TTS
    side strips `[...]` patterns, so the numbers don't leak into spoken audio.
    """
    if not hits:
        return ""
    parts = []
    for i, hit in enumerate(hits, 1):
        text = get_vectorization_text(hit, hit.get("collection_type", ""))
        if not text:
            continue
        parts.append(f"[{i}] {text}")
    return "\n\n".join(parts)
