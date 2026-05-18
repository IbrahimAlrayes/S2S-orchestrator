# Ported from rag-nusuk-ai/llms/rerank.py.
# Only the `from utils.config_loader import load_config` import was changed
# to use the local module path.

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any, Dict, List, Optional, Tuple

import httpx
from dotenv import load_dotenv

from .config_loader import load_config

load_dotenv()

logger = logging.getLogger(__name__)

URL = os.getenv("RERANK_SERVICE_URL")


def _make_timeout(total: float) -> httpx.Timeout:
    return httpx.Timeout(
        connect=min(3.0, total / 2.0),
        read=total,
        write=min(5.0, total),
        pool=5.0,
    )


class RerankerModelAsync:
    """Connects to the reranker service and scores documents against a query."""

    def __init__(
        self,
        *,
        url: str = URL,
        client: Optional[httpx.AsyncClient] = None,
    ) -> None:
        model_cfg = load_config("models.json")["RERANKER"]
        search_cfg = load_config("milvus.json")["SEARCH_CONFIG"]
        # Env override lets dev (`.org` serves `elm_rerank_32k_0.6b`) and prod
        # (in-cluster vLLM serves `elm_reranker_v1`) share the same image.
        self.model_name: str = os.getenv("RERANK_MODEL") or model_cfg["MODEL_NAME"]
        self.top_k_to_rerank: int = search_cfg.get("TOP_K_TO_RERANK", 10)

        self._url = url
        self._timeout = model_cfg.get("TIMEOUT", 45.0)
        self._retries = model_cfg.get("MAX_RETRIES", 5)

        self._client = client
        self._owns_client = client is None
        if self._owns_client:
            self._client = httpx.AsyncClient(
                timeout=_make_timeout(self._timeout),
                follow_redirects=False,
                http2=True,
            )

    async def aclose(self) -> None:
        if self._owns_client and self._client is not None:
            await self._client.aclose()

    async def get_rerank_scores(self, query: str, documents: List[str]) -> List[dict]:
        if not documents or not query:
            raise ValueError("No Documents or Query passed to be reranked")

        payload: Dict[str, Any] = {
            "model": self.model_name,
            "text_1": query,
            "text_2": documents[: self.top_k_to_rerank],
        }
        headers = {"Content-Type": "application/json"}

        attempt = 0
        while True:
            try:
                resp = await self._client.post(self._url, json=payload, headers=headers)
                resp.raise_for_status()
                data = resp.json()

                results = data.get("data")
                if not isinstance(results, list):
                    raise ValueError("Invalid response: 'data' must be a list")
                if results and not isinstance(results[0], dict):
                    raise ValueError("Invalid response: 'data' items must be objects")

                return [
                    {
                        "index": item["index"],
                        "object": item["object"],
                        "reranked_score": item["score"],
                    }
                    for item in results
                ]

            except (httpx.TimeoutException, httpx.TransportError) as e:
                attempt += 1
                logger.warning("Rerank attempt %s failed: %s", attempt, e)
                if attempt > self._retries:
                    raise ValueError("Max retries reached for reranking")
                await asyncio.sleep(min(1.0 * attempt, 2.0))

            except ValueError as e:
                raise ValueError(f"Reranker returned invalid response: {e}.")


class GenerativeRerankerModelAsync(RerankerModelAsync):
    """Async generative reranker that wraps query+docs with prompt templates."""

    def __init__(
        self,
        *,
        url: str = URL,
        client: Optional[httpx.AsyncClient] = None,
    ) -> None:
        super().__init__(url=url, client=client)

        prompts_cfg = load_config("prompts.json")["RERANKER"]["GENERATIVE"]

        self._prefix: Optional[str] = prompts_cfg.get("prefix_template")
        self._suffix: Optional[str] = prompts_cfg.get("suffix_template")
        self._instruction: Optional[str] = prompts_cfg.get("instruction")
        self._query_template = prompts_cfg.get("query_template")
        self._document_template = prompts_cfg.get("document_template")

        if not self._query_template or not self._document_template:
            raise ValueError(
                "Query and Document prompt templates must exist to run generative reranker"
            )

    def _process_input(self, query: str, documents: List[str]) -> Tuple[str, List[str]]:
        # Process query
        if self._query_template:
            if self._prefix and self._instruction:
                processed_query = self._query_template.format(
                    prefix=self._prefix, query=query, instruction=self._instruction
                )
            elif self._prefix:
                processed_query = self._query_template.format(
                    prefix=self._prefix, query=query
                )
            elif self._instruction:
                processed_query = self._query_template.format(
                    query=query, instruction=self._instruction
                )
            else:
                processed_query = self._query_template.format(query=query)
        else:
            processed_query = query

        if self._document_template:
            if self._suffix:
                processed_docs = [
                    self._document_template.format(doc=doc, suffix=self._suffix)
                    for doc in documents
                ]
            else:
                processed_docs = [
                    self._document_template.format(doc=doc) for doc in documents
                ]
        else:
            processed_docs = documents

        return processed_query, processed_docs

    async def get_rerank_scores(
        self, query: str, documents: List[str]
    ) -> List[Dict[str, Any]]:
        processed_query, processed_documents = self._process_input(query, documents)
        return await super().get_rerank_scores(processed_query, processed_documents)
