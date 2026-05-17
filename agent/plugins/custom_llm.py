from __future__ import annotations

import json
import logging
import re
import time
import uuid
from collections.abc import AsyncGenerator

import httpx
from livekit.agents import DEFAULT_API_CONNECT_OPTIONS, APIConnectOptions, NOT_GIVEN, llm

import metrics
from config import AgentSettings, LLMSettings
from plugins.nusuk_auth import NusukAuthError, NusukTokenManager

logger = logging.getLogger("nusuk-agent.llm")


class CustomLLM(llm.LLM):
    """LiveKit-native LLM provider backed by Groq/OpenAI-style or Nusuk APIs."""

    def __init__(
        self,
        settings: LLMSettings,
        agent_settings: AgentSettings,
        *,
        session_id: str,
        user_id: str | None = None,
        token_manager: NusukTokenManager | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        super().__init__()
        self.settings = settings
        self.agent_settings = agent_settings
        self.session_id = session_id
        self.user_id = user_id
        self._provider_key = settings.provider.strip().lower()
        if client is not None:
            self._client = client
            self._owns_client = False
        else:
            self._client = httpx.AsyncClient(timeout=settings.timeout_seconds, http2=True)
            self._owns_client = True

        # Prefer a pre-warmed token manager passed from prewarm() — avoids one
        # extra auth RTT on the first turn of each room.
        if token_manager is not None:
            self.token_manager: NusukTokenManager | None = token_manager
        elif self._provider_key == "nusuk" and settings.client_id and settings.client_secret:
            self.token_manager = NusukTokenManager(
                base_url=settings.url,
                client_id=settings.client_id,
                client_secret=settings.client_secret,
                user_id=settings.auth_user_id,
                client=self._client,
            )
        else:
            self.token_manager = None

    @property
    def model(self) -> str:
        return self.settings.model

    @property
    def provider(self) -> str:
        return self.settings.provider

    def chat(
        self,
        *,
        chat_ctx: llm.ChatContext,
        tools: list[llm.Tool] | None = None,
        conn_options: APIConnectOptions = DEFAULT_API_CONNECT_OPTIONS,
        parallel_tool_calls=NOT_GIVEN,
        tool_choice=NOT_GIVEN,
        extra_kwargs=NOT_GIVEN,
    ) -> llm.LLMStream:
        del parallel_tool_calls, tool_choice, extra_kwargs
        return CustomLLMStream(
            self,
            chat_ctx=chat_ctx,
            tools=tools or [],
            conn_options=conn_options,
        )

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()


class CustomLLMStream(llm.LLMStream):
    def __init__(
        self,
        llm_provider: CustomLLM,
        *,
        chat_ctx: llm.ChatContext,
        tools: list[llm.Tool],
        conn_options: APIConnectOptions,
    ) -> None:
        super().__init__(
            llm=llm_provider,
            chat_ctx=chat_ctx,
            tools=tools,
            conn_options=conn_options,
        )
        self._provider = llm_provider

    async def _run(self) -> None:
        if self._provider._provider_key == "nusuk":
            await self._run_nusuk()
            return
        if self._provider._provider_key == "nusuk_rag":
            await self._run_nusuk_rag()
            return
        await self._run_openai()

    async def _run_openai(self, messages: list[dict] | None = None) -> None:
        # `messages=` lets `_run_nusuk_rag` reuse this streaming loop after
        # fusing RAG context into the system prompt. Default behavior is
        # unchanged when called with no args.
        if messages is None:
            messages, _ = self.chat_ctx.to_provider_format("openai")
            if not any(message.get("role") == "system" for message in messages):
                messages.insert(
                    0,
                    {
                        "role": "system",
                        "content": self._provider.agent_settings.system_prompt,
                    },
                )

        payload = {
            "model": self._provider.settings.model,
            "messages": messages,
            "stream": True,
            "temperature": self._provider.settings.temperature,
            "max_tokens": self._provider.settings.max_tokens,
        }
        if self._provider.settings.reasoning_effort:
            payload["reasoning_effort"] = self._provider.settings.reasoning_effort

        request_id = str(uuid.uuid4())
        reasoning_filter = ReasoningStreamFilter()
        provider_name = self._provider.settings.provider

        logger.info("llm_start provider=%s", provider_name)
        t0 = time.monotonic()
        first_token = True
        try:
            async with self._provider._client.stream(
                "POST",
                _openai_chat_url(self._provider.settings.url),
                json=payload,
                headers=_bearer_headers(self._provider.settings),
            ) as response:
                response.raise_for_status()
                async for event in _iter_sse(response):
                    request_id = event.get("id") or request_id
                    delta = _extract_openai_delta(event)
                    if not delta:
                        continue
                    filtered = reasoning_filter.push(delta)
                    if not filtered:
                        continue
                    if first_token:
                        metrics.LLM_TTFT.observe(time.monotonic() - t0)
                        first_token = False
                    self._event_ch.send_nowait(
                        llm.ChatChunk(
                            id=request_id,
                            delta=llm.ChoiceDelta(role="assistant", content=filtered),
                        )
                    )
        except Exception as exc:
            metrics.LLM_ERRORS.labels(provider=provider_name).inc()
            raise
        duration_s = time.monotonic() - t0
        metrics.LLM_DURATION.observe(duration_s)
        logger.info("llm_done provider=%s duration_s=%.3f", provider_name, duration_s)

    async def _run_nusuk(self) -> None:
        query = _latest_user_message(self.chat_ctx)
        if not query:
            return

        prefix = self._provider.settings.query_prefix
        if prefix:
            query = f"{prefix.strip()} {query}"

        payload = {
            "query": query,
            "session_id": self._provider.session_id,
            "language": self._provider.settings.language,
            "mode": "voice",
            "include_metadata": self._provider.settings.include_metadata,
            "tool": self._provider.settings.tool,
        }
        if self._provider.user_id:
            payload["user_id"] = self._provider.user_id

        request_id = str(uuid.uuid4())
        provider_name = self._provider.settings.provider

        logger.info(
            "llm_start provider=%s session_id=%s query_len=%d",
            provider_name,
            self._provider.session_id,
            len(query),
        )

        t0 = time.monotonic()
        first_token = True
        for attempt in range(2):
            headers = await self._nusuk_headers()
            try:
                async with self._provider._client.stream(
                    "POST",
                    _nusuk_stream_url(self._provider.settings.url),
                    json=payload,
                    headers=headers,
                ) as response:
                    if response.status_code == 401 and attempt == 0 and self._provider.token_manager:
                        await response.aread()
                        logger.warning("llm_nusuk_401_invalidating_token")
                        await self._provider.token_manager.invalidate()
                        continue
                    response.raise_for_status()
                    async for event in _iter_sse(response):
                        delta = event.get("delta")
                        if not isinstance(delta, str) or not delta:
                            continue
                        if first_token:
                            metrics.LLM_TTFT.observe(time.monotonic() - t0)
                            first_token = False
                        self._event_ch.send_nowait(
                            llm.ChatChunk(
                                id=request_id,
                                delta=llm.ChoiceDelta(role="assistant", content=delta),
                            )
                        )
                break
            except NusukAuthError:
                metrics.LLM_ERRORS.labels(provider=provider_name).inc()
                logger.exception("llm_nusuk_auth_failed")
                raise
            except Exception:
                metrics.LLM_ERRORS.labels(provider=provider_name).inc()
                raise
        duration_s = time.monotonic() - t0
        metrics.LLM_DURATION.observe(duration_s)
        logger.info("llm_done provider=%s duration_s=%.3f", provider_name, duration_s)

    async def _run_nusuk_rag(self) -> None:
        # TEMP provider — see agent/plugins/rag/README.md for removal steps.
        # Path: pull the last user message, run local RAG (Milvus + reranker),
        # fuse retrieved context into the system prompt Nusuk-style, then
        # delegate to the existing OpenAI streaming loop (Groq backend).
        from plugins.rag import retrieve as rag_retrieve
        from prompts.voice_prompt import PROMPT_HASH, PROMPT_VERSION, RAG_VOICE_PROMPT

        query = _latest_user_message(self.chat_ctx)
        if not query:
            await self._run_openai()
            return

        top_k = getattr(self._provider.settings, "rag_top_k", 12)
        try:
            hits = await rag_retrieve.retrieve(query, top_k=top_k)
        except Exception:
            logger.warning("nusuk_rag_retrieve_failed_falling_back_to_groq", exc_info=True)
            hits = []

        # Build messages with the voice prompt + appended context.
        messages, _ = self.chat_ctx.to_provider_format("openai")
        base_prompt = RAG_VOICE_PROMPT
        if hits:
            context_block = rag_retrieve.format_context_block(hits)
            fused_system = (
                f"{base_prompt}\n\n"
                f"استخدم السياق التالي للإجابة عن سؤال المستخدم. السياق مرقَّم؛ "
                f"لا تذكر أرقام المراجع في إجابتك.\n\n{context_block}"
            )
        else:
            fused_system = base_prompt

        lang = _query_language(query)
        fused_system = fused_system + f"\n\nThe user's message is in {lang}. You MUST respond in {lang} only."

        # Replace any existing system message (or insert one) with the fused version.
        non_system = [m for m in messages if m.get("role") != "system"]
        # Apply query_prefix to the last user message (format hint for LLM, not retrieval).
        prefix = self._provider.settings.query_prefix
        if prefix:
            for i in range(len(non_system) - 1, -1, -1):
                if non_system[i].get("role") == "user":
                    content = _message_text(non_system[i].get("content", ""))
                    non_system[i] = {**non_system[i], "content": f"{prefix.strip()} {content}"}
                    break
        messages = [{"role": "system", "content": fused_system}, *non_system]

        logger.info(
            "llm_start provider=nusuk_rag prompt_version=%s prompt_hash=%s "
            "rag_hits=%d query_len=%d query_lang=%s",
            PROMPT_VERSION,
            PROMPT_HASH,
            len(hits),
            len(query),
            lang,
        )
        await self._run_openai(messages=messages)

    async def _nusuk_headers(self) -> dict[str, str]:
        if self._provider.token_manager is not None:
            token = await self._provider.token_manager.get_token()
            return {"Authorization": f"Bearer {token}"}
        return _bearer_headers(self._provider.settings)


async def _iter_sse(response: httpx.Response) -> AsyncGenerator[dict, None]:
    """Yield parsed JSON events from an SSE response, skipping non-data lines and [DONE]."""
    async for line in response.aiter_lines():
        if not line or not line.startswith("data:"):
            continue
        data = line.removeprefix("data:").strip()
        if data == "[DONE]":
            return
        try:
            yield json.loads(data)
        except json.JSONDecodeError:
            logger.warning("sse_bad_chunk data=%s", data[:120])


def _extract_openai_delta(event: dict) -> str | None:
    """Extract the text delta from an OpenAI-style SSE chunk."""
    return event.get("choices", [{}])[0].get("delta", {}).get("content")


def _openai_chat_url(url: str) -> str:
    if url.rstrip("/").endswith("/chat/completions"):
        return url
    return url.rstrip("/") + "/chat/completions"


def _nusuk_stream_url(url: str) -> str:
    normalized = url.rstrip("/")
    if normalized.endswith("/chat/stream"):
        return normalized
    if normalized.endswith("/chat"):
        return normalized + "/stream"
    return normalized + "/chat/stream"


def _bearer_headers(settings: LLMSettings) -> dict[str, str]:
    if not settings.access_token:
        return {}
    return {"Authorization": f"Bearer {settings.access_token}"}


def _latest_user_message(chat_ctx: llm.ChatContext) -> str:
    messages, _ = chat_ctx.to_provider_format("openai")
    for message in reversed(messages):
        if message.get("role") != "user":
            continue
        text = _message_text(message.get("content"))
        if text:
            return text
    return ""


def _message_text(content: object) -> str:
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, str) and item.strip():
                parts.append(item.strip())
            elif isinstance(item, dict) and isinstance(item.get("text"), str):
                text = item["text"].strip()
                if text:
                    parts.append(text)
        return " ".join(parts)
    return ""


class ReasoningStreamFilter:
    def __init__(self) -> None:
        self._raw = ""
        self._visible_len = 0

    def push(self, chunk: str) -> str:
        self._raw += chunk
        visible = _visible_text(self._raw)
        if len(visible) <= self._visible_len:
            return ""
        delta = visible[self._visible_len :]
        self._visible_len = len(visible)
        return delta


_ARABIC_RE = re.compile(r"[؀-ۿ]")


def _query_language(text: str) -> str:
    return "Arabic" if _ARABIC_RE.search(text) else "English"


def _visible_text(text: str) -> str:
    cleaned = text
    while True:
        start = cleaned.find("<think>")
        if start == -1:
            break
        end = cleaned.find("</think>", start + len("<think>"))
        if end == -1:
            cleaned = cleaned[:start]
            break
        cleaned = cleaned[:start] + cleaned[end + len("</think>") :]

    for suffix in ("<think>", "<think", "<thin", "<thi", "<th", "<t", "<"):
        if cleaned.endswith(suffix):
            cleaned = cleaned[: -len(suffix)]
            break

    return cleaned
