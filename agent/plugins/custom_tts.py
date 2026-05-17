from __future__ import annotations

import io
import logging
import struct
import time
import uuid
import wave

import httpx
from livekit.agents import DEFAULT_API_CONNECT_OPTIONS, APIConnectOptions, tts
from livekit.agents.tts.tts import AudioEmitter

import metrics
from config import TTSSettings
from plugins.nusuk_auth import NusukTokenManager

logger = logging.getLogger("nusuk-agent.tts")


class CustomTTS(tts.TTS):
    """LiveKit-native non-streaming TTS provider backed by the local HTTP API."""

    def __init__(
        self,
        settings: TTSSettings,
        token_manager: NusukTokenManager | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        super().__init__(
            capabilities=tts.TTSCapabilities(streaming=False),
            sample_rate=settings.sample_rate,
            num_channels=settings.num_channels,
        )
        self.settings = settings
        self._provider_key = settings.provider.strip().lower()
        self._token_manager = token_manager
        if client is not None:
            self._client = client
            self._owns_client = False
        else:
            self._client = httpx.AsyncClient(timeout=settings.timeout_seconds, http2=True)
            self._owns_client = True

    @property
    def model(self) -> str:
        return self.settings.model

    @property
    def provider(self) -> str:
        return self.settings.provider

    def synthesize(
        self,
        text: str,
        *,
        conn_options: APIConnectOptions = DEFAULT_API_CONNECT_OPTIONS,
    ) -> tts.ChunkedStream:
        return CustomTTSChunkedStream(
            tts_provider=self,
            input_text=text,
            conn_options=conn_options,
        )

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def _auth_headers(self) -> dict[str, str]:
        if self._token_manager is not None:
            token = await self._token_manager.get_token()
            return {"Authorization": f"Bearer {token}"}
        return _bearer_headers(self.settings)


class CustomTTSChunkedStream(tts.ChunkedStream):
    def __init__(
        self,
        *,
        tts_provider: CustomTTS,
        input_text: str,
        conn_options: APIConnectOptions,
    ) -> None:
        self._provider = tts_provider
        super().__init__(
            tts=tts_provider,
            input_text=input_text,
            conn_options=conn_options,
        )

    async def _run(self, output_emitter: AudioEmitter) -> None:
        text = _strip_markdown(self.input_text).strip()
        if not text:
            return

        logger.info(
            "tts_start provider=%s text_len=%d",
            self._provider.settings.provider,
            len(text),
        )
        t0 = time.monotonic()
        url = _tts_url(self._provider.settings.url, self._provider._provider_key)
        payload = _request_payload(
            self._provider.settings, text, self._provider._provider_key
        )
        headers = await self._provider._auth_headers()
        default_sr = self._provider.settings.sample_rate
        default_nc = self._provider.settings.num_channels

        request_id: str | None = None
        initialized = False
        first_audio_t: float | None = None
        total_audio_bytes = 0
        sample_rate = default_sr
        # Buffer prefix bytes until we know whether the response is RIFF/WAV
        # and we have enough to parse the WAV header.
        prefix_buf = bytearray()

        def _ensure_initialized(sr: int, nc: int) -> None:
            nonlocal initialized, request_id
            if initialized:
                return
            if request_id is None:
                request_id = str(uuid.uuid4())
            output_emitter.initialize(
                request_id=request_id,
                sample_rate=sr,
                num_channels=nc,
                mime_type="audio/pcm",
                frame_size_ms=20,
            )
            initialized = True

        try:
            async with self._provider._client.stream(
                "POST", url, json=payload, headers=headers
            ) as response:
                response.raise_for_status()
                request_id = response.headers.get("x-synthesis-id")
                async for chunk in response.aiter_bytes():
                    if not chunk:
                        continue
                    if not initialized:
                        prefix_buf.extend(chunk)
                        # Decide format on the first 4 bytes; once decided either
                        # parse the WAV header in-stream or emit raw PCM directly.
                        if prefix_buf[:4] != b"RIFF":
                            _ensure_initialized(default_sr, default_nc)
                            sample_rate = default_sr
                            if first_audio_t is None:
                                first_audio_t = time.monotonic() - t0
                            output_emitter.push(bytes(prefix_buf))
                            total_audio_bytes += len(prefix_buf)
                            prefix_buf.clear()
                            continue
                        parsed = _parse_wav_header(prefix_buf)
                        if parsed is None:
                            # Header still incomplete — keep buffering.
                            continue
                        sample_rate, num_channels, pcm_offset = parsed
                        _ensure_initialized(sample_rate, num_channels)
                        first_pcm = bytes(prefix_buf[pcm_offset:])
                        prefix_buf.clear()
                        if first_pcm:
                            if first_audio_t is None:
                                first_audio_t = time.monotonic() - t0
                            output_emitter.push(first_pcm)
                            total_audio_bytes += len(first_pcm)
                        continue
                    if first_audio_t is None:
                        first_audio_t = time.monotonic() - t0
                    output_emitter.push(chunk)
                    total_audio_bytes += len(chunk)
        except httpx.HTTPError as exc:
            metrics.TTS_ERRORS.inc()
            logger.error("tts_failed error=%s url=%s", exc, getattr(exc, "request", None))
            _ensure_initialized(default_sr, default_nc)
            return

        # If the response was empty or we never crossed the WAV header threshold,
        # still initialise the emitter so the session doesn't stall on a half-open
        # output stream.
        _ensure_initialized(sample_rate, default_nc)

        duration_s = time.monotonic() - t0
        metrics.TTS_DURATION.observe(duration_s)
        logger.info(
            "tts_done request_id=%s audio_bytes=%d sample_rate=%d ttfa_s=%.3f duration_s=%.3f",
            request_id,
            total_audio_bytes,
            sample_rate,
            first_audio_t if first_audio_t is not None else 0.0,
            duration_s,
        )


def _request_payload(settings: TTSSettings, text: str, provider: str) -> dict[str, object]:
    if provider in {"wrapper", "nusuk"}:
        return {"text": text}
    if provider == "local_api":
        return {
            "text": text,
            "output_format": settings.audio_format,
            "sample_rate": settings.sample_rate,
        }
    return {
        "model": settings.model,
        "voice": settings.voice,
        "input": text,
        "response_format": settings.audio_format,
    }


def _tts_url(url: str, provider: str) -> str:
    normalized = url.rstrip("/")
    if provider == "wrapper":
        return normalized
    if provider == "nusuk":
        if normalized.endswith("/synthesize"):
            return normalized
        return normalized + "/synthesize"
    if provider == "local_api":
        base = normalized.removesuffix("/api/synthesize/").removesuffix("/api/synthesize")
        return base + "/api/synthesize/"
    return normalized


def _bearer_headers(settings: TTSSettings) -> dict[str, str]:
    if not settings.access_token:
        return {}
    return {"Authorization": f"Bearer {settings.access_token}"}


def _strip_markdown(text: str) -> str:
    """Remove markdown formatting that TTS would speak literally."""
    import re
    text = re.sub(r'\*+([^*\n]+)\*+', r'\1', text)   # **bold** / *italic*
    text = re.sub(r'^\s*>+\s*', '', text, flags=re.MULTILINE)  # > blockquotes
    text = re.sub(r'\[[^\]]*\]', '', text)            # [4] / [أ] / [ref:x] citation markers
    text = re.sub(r'\n{2,}', ' ', text)               # collapse paragraph breaks
    return text.strip()


def _decode_wav(wav_bytes: bytes) -> tuple[int, int, bytes]:
    with wave.open(io.BytesIO(wav_bytes), "rb") as wav_file:
        sample_rate = wav_file.getframerate()
        num_channels = wav_file.getnchannels()
        pcm = wav_file.readframes(wav_file.getnframes())
    return sample_rate, num_channels, pcm


def _parse_wav_header(buf: bytes | bytearray) -> tuple[int, int, int] | None:
    """Streaming WAV header parser.

    Returns (sample_rate, num_channels, pcm_offset_in_buf) once the buffer
    contains the full RIFF/WAVE/fmt/data prologue. Returns None if more bytes
    are still needed. Locates `fmt ` and `data` markers explicitly so it
    tolerates extra chunks (LIST, JUNK) inserted between them.
    """
    if len(buf) < 12 or buf[:4] != b"RIFF" or buf[8:12] != b"WAVE":
        return None
    fmt = buf.find(b"fmt ")
    if fmt < 0 or len(buf) < fmt + 24:
        return None
    num_channels = struct.unpack_from("<H", buf, fmt + 10)[0]
    sample_rate = struct.unpack_from("<I", buf, fmt + 12)[0]
    data = buf.find(b"data", fmt)
    if data < 0 or len(buf) < data + 8:
        return None
    return sample_rate, num_channels, data + 8
