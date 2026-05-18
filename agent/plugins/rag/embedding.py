# Ported from rag-nusuk-ai/llms/embedding.py.
# Only the `from core.setting import settings` import path was changed.

import asyncio
import logging
from typing import Any, Dict, List, Optional

import httpx

from .settings_shim import settings

logger = logging.getLogger(__name__)

# Global connection pool for embedding service - reused across all requests
_embedding_client: Optional[httpx.AsyncClient] = None
_client_lock = asyncio.Lock()


async def get_embedding_client() -> httpx.AsyncClient:
    """Get or create a shared httpx client with connection pooling."""
    global _embedding_client
    if _embedding_client is None or _embedding_client.is_closed:
        async with _client_lock:
            if _embedding_client is None or _embedding_client.is_closed:
                timeout = httpx.Timeout(connect=2.0, read=6.0, write=3.0, pool=2.0)
                limits = httpx.Limits(
                    max_connections=1500,
                    max_keepalive_connections=500,
                    keepalive_expiry=10.0,
                )
                _embedding_client = httpx.AsyncClient(
                    timeout=timeout,
                    limits=limits,
                    http2=True,
                    follow_redirects=False,
                )
                logger.info("Created embedding service connection pool")
    return _embedding_client


async def close_embedding_client():
    """Close the shared client (call on app shutdown)."""
    global _embedding_client
    if _embedding_client is not None:
        await _embedding_client.aclose()
        _embedding_client = None
        logger.info("Closed embedding service connection pool")


async def get_text_embeddings(
    texts: List[str],
    timeout: float = 6.0,
    client: Optional[httpx.AsyncClient] = None,
    retries: int = 2,
) -> List[List[float]]:
    """
    Async HTTP client with connection pooling and fast timeouts.
    Uses shared connection pool for better performance under load.
    """
    if not texts:
        return []

    payload: Dict[str, Any] = {"input": texts, "normalize": True}
    headers = {"Content-Type": "application/json"}

    if client is None:
        client = await get_embedding_client()
        close_client = False
    else:
        close_client = False

    try:
        attempt = 0
        while True:
            try:
                resp = await client.post(settings.EMBEDDING_SERVICE_URL, json=payload, headers=headers)
                resp.raise_for_status()
                data = resp.json()

                embeddings = None
                if isinstance(data, dict):
                    if "embeddings" in data:
                        embeddings = data["embeddings"]
                    elif isinstance(data.get("data"), list):
                        try:
                            embeddings = [item["embedding"] for item in data["data"]]
                        except (TypeError, KeyError):
                            raise ValueError("Invalid response format: each item in 'data' must have 'embedding' list")

                if not isinstance(embeddings, list) or (embeddings and not isinstance(embeddings[0], list)):
                    raise ValueError("Invalid response format: 'embeddings' must be a list of lists")

                return embeddings
            except (httpx.TimeoutException, httpx.TransportError) as e:
                attempt += 1
                logger.warning("Embedding request attempt %s failed: %s", attempt, e)
                if attempt > retries:
                    return []
                await asyncio.sleep(min(1.0 * attempt, 2.0))

    except ValueError as e:
        logger.error("[ERROR] Invalid response data: %s", e)
        return []
    finally:
        if close_client:
            await client.aclose()
