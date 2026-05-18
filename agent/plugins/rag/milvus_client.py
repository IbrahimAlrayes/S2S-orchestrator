# Ported from rag-nusuk-ai/vectorstore/milvus_client.py — query path only.
# All insert/update/upsert/init-collection methods are dropped because this
# adapter is read-only against an existing collection.

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Dict, List, Optional

from pymilvus import AnnSearchRequest, MilvusClient, RRFRanker

from .config_loader import load_config
from .embedding import get_text_embeddings
from .preprocessor_nusuk import _format_results_unified
from .settings_shim import settings

logger = logging.getLogger(__name__)

# Cache Milvus config at module level to avoid repeated file I/O.
_MILVUS_CONFIG_CACHE: Optional[dict] = None


def get_milvus_config(tag: str) -> dict:
    global _MILVUS_CONFIG_CACHE
    if _MILVUS_CONFIG_CACHE is None:
        _MILVUS_CONFIG_CACHE = load_config("milvus.json")
    return _MILVUS_CONFIG_CACHE[tag]


class VectorClient:
    def __init__(self):
        search_config = get_milvus_config("SEARCH_CONFIG")
        self.uri = f"{settings.MILVUS_HOST}:{settings.MILVUS_PORT}"
        self.token = settings.MILVUS_TOKEN
        self.client: Optional[MilvusClient] = None
        self.top_k = search_config["TOP_K_TO_RETRIEVE"]
        self.output_fields = search_config["OUTPUT_FIELDS"]
        self.anns_field = search_config["ANNS_FIELD"]
        self.search_parameters = search_config["SEARCH_PARAMETERS"]
        self.context_llm_fields = search_config["CONTEXT_TO_LLM_FIELDS"]
        self.collection_name = settings.MILVUS_COLLECTION
        self.hybrid_enabled = search_config.get("HYBRID_SEARCH_ENABLED", True)

        # Per-call latency telemetry (read by retrieve.py).
        self._last_embedding_ms = 0.0
        self._last_milvus_ms = 0.0

    async def connect(self, max_retries: int = 6, base_delay: float = 5.0) -> None:
        """Connect to Milvus with exponential backoff retry."""
        delay = base_delay
        for attempt in range(1, max_retries + 1):
            try:
                self.client = await asyncio.to_thread(MilvusClient, uri=self.uri, token=self.token)
                logger.info("Connected to Milvus at %s", self.uri)
                return
            except Exception as e:
                logger.error(
                    "Failed to connect to Milvus (attempt %d/%d): %s", attempt, max_retries, e
                )
                if attempt < max_retries:
                    logger.info("Retrying in %.0fs...", delay)
                    await asyncio.sleep(delay)
                    delay = min(delay * 2, 60.0)
        raise ValueError(f"Failed to connect to Milvus after {max_retries} attempts")

    async def disconnect(self) -> None:
        if self.client:
            await asyncio.to_thread(self.client.close)
            logger.info("Disconnected from Milvus")

    async def search(
        self,
        data: str,
        collection: Optional[str] = None,
        filter: Optional[str] = None,
        collection_types: Optional[List[str]] = None,
        language: str = "arabic",
        embedding: Optional[List[float]] = None,
        hybrid: Optional[bool] = None,
    ) -> List[Dict[str, Any]]:
        """Hybrid (or dense-only) search against the configured Milvus collection.

        Args:
            data: query text
            collection: collection name override (defaults to settings.MILVUS_COLLECTION)
            filter: extra filter expression
            collection_types: list of `collection_type` values to restrict to
            language: "arabic" or "english" — affects result formatting
            embedding: pre-computed embedding (skips the embedding call)
            hybrid: force hybrid on/off (defaults to config flag)

        Returns: a list of result dicts (see `_format_results_unified`).
        """
        if not self.client:
            raise RuntimeError("Milvus client is not connected")

        dense_weight = self.search_parameters.get("DENSE_WEIGHT", 0.4)
        sparse_weight = self.search_parameters.get("SPARSE_WEIGHT", 1.0)
        use_hybrid = hybrid if hybrid is not None else self.hybrid_enabled

        # Reset timing attributes.
        self._last_embedding_ms = 0.0
        self._last_milvus_ms = 0.0

        try:
            if not embedding:
                _t0 = time.perf_counter()
                embedding_data = await get_text_embeddings([data])
                self._last_embedding_ms = (time.perf_counter() - _t0) * 1000
            else:
                embedding_data = [embedding]

            # Build filter for collection types if specified.
            search_filter = filter
            if collection_types:
                collection_filter = f"collection_type in {collection_types}"
                if search_filter:
                    search_filter = f"({search_filter}) and ({collection_filter})"
                else:
                    search_filter = collection_filter

            _t1 = time.perf_counter()
            if use_hybrid:
                results = await self._execute_hybrid_search(
                    query_text=data,
                    embedding=embedding_data[0],
                    search_filter=search_filter,
                    limit=self.top_k,
                    dense_weight=dense_weight,
                    sparse_weight=sparse_weight,
                )
            else:
                results = await self._execute_dense_search(
                    embedding=embedding_data,
                    search_filter=search_filter,
                    limit=self.top_k,
                )
            self._last_milvus_ms = (time.perf_counter() - _t1) * 1000

            return _format_results_unified(
                results, self.context_llm_fields, self.output_fields, language
            )
        except Exception as e:
            logger.error("Error during search in collection %s: %s", collection, e)
            raise RuntimeError(f"Search failed: {e}")

    async def _execute_hybrid_search(
        self,
        query_text: str,
        embedding: List[float],
        search_filter: Optional[str] = None,
        limit: int = 5,
        rrf_k: int = 100,
        dense_weight: float = 0.7,
        sparse_weight: float = 0.3,
    ) -> List:
        """Execute hybrid search combining dense (HNSW) and sparse (BM25) vectors."""
        dense_request = AnnSearchRequest(
            data=[embedding],
            anns_field="vector",
            param={"ef": 64},
            limit=limit,
            expr=search_filter,
        )

        sparse_request = AnnSearchRequest(
            data=[query_text],
            anns_field="sparse_vector",
            param={"drop_ratio_search": 0.2},
            limit=limit,
            expr=search_filter,
        )

        ranker = RRFRanker(rrf_k)

        results = await asyncio.to_thread(
            self.client.hybrid_search,
            collection_name=self.collection_name,
            reqs=[dense_request, sparse_request],
            ranker=ranker,
            limit=limit,
            output_fields=self.output_fields,
        )

        logger.info("Hybrid search executed. No of results: %d", len(results))
        return results

    async def _execute_dense_search(
        self,
        embedding: List[List[float]],
        search_filter: Optional[str] = None,
        limit: int = 5,
    ) -> List:
        """Execute dense-only vector search."""
        return await asyncio.to_thread(
            self.client.search,
            collection_name=self.collection_name,
            data=embedding,
            limit=limit,
            output_fields=self.output_fields,
            anns_field=self.anns_field,
            params=self.search_parameters["DENSE_SEARCH"],
            filter=search_filter,
        )
