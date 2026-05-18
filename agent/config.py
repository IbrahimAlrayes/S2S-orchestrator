from __future__ import annotations

import logging
import os
import sys
import tempfile
from pathlib import Path

from pydantic import AliasChoices, Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

logger = logging.getLogger("nusuk-agent.config")

# Resolved relative to this file so the default works inside Docker (/app/...)
# and outside (any CWD that has agent/ in scope). Kept as a string for Pydantic
# field default compatibility.
_AGENT_DIR = Path(__file__).resolve().parent
DEFAULT_SYSTEM_PROMPT_FILE = str(_AGENT_DIR / "system_prompt_rag.txt")


def _regenerate_prompt_file(target: Path) -> str | None:
    """Rebuild RAG_VOICE_PROMPT from agent/prompts/prompts.json and atomically write to target.

    Returns the prompt string if regeneration succeeded (whether or not the
    write to disk succeeded — in-memory still works), else None when the RAG
    voice-prompt source is unavailable.
    """
    try:
        from prompts.voice_prompt import RAG_VOICE_PROMPT
    except Exception as exc:
        logger.warning(
            "system_prompt_file=%s missing and voice-prompt source "
            "unavailable (%s); restore the file or check agent/prompts/",
            target, exc,
        )
        return None

    tmp_path: str | None = None
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=str(target.parent),
            prefix=f".{target.name}.", suffix=".tmp", delete=False,
        ) as tmp:
            tmp.write(RAG_VOICE_PROMPT)
            tmp_path = tmp.name
        os.replace(tmp_path, target)
        os.chmod(target, 0o644)
        logger.info(
            "regenerated %s from RAG_VOICE_PROMPT (%d chars)",
            target, len(RAG_VOICE_PROMPT),
        )
    except OSError as exc:
        if tmp_path is not None:
            Path(tmp_path).unlink(missing_ok=True)
        logger.warning(
            "regenerated RAG_VOICE_PROMPT (%d chars) but failed to persist %s: %s; "
            "using in-memory value (regen will retry on next process start)",
            len(RAG_VOICE_PROMPT), target, exc,
        )
    return RAG_VOICE_PROMPT


class STTSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="CUSTOM_STT_", extra="ignore")

    url: str = Field(..., description="External transcription endpoint")
    provider: str = Field(default="local_api", description="local_api, openai, or nusuk")
    model: str = Field(default="placeholder", description="ASR model name when the provider uses one")
    access_token: str | None = Field(default=None, description="Bearer token for the STT API")
    language: str = Field(default="ar", description="Language hint")
    timeout_seconds: float = Field(default=30.0, ge=1)
    target_sample_rate: int = Field(default=16000, ge=8000)


class LLMSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="CUSTOM_LLM_", extra="ignore")

    url: str = Field(..., description="External LLM base URL or chat endpoint")
    provider: str = Field(default="openai", description="openai or nusuk")
    model: str = Field(default="qwen/qwen3-32b", description="LLM model name when the provider uses one")
    access_token: str | None = Field(
        default=None,
        description="Bearer token for the LLM API",
        validation_alias=AliasChoices("CUSTOM_LLM_ACCESS_TOKEN", "GROQ_API_KEY", "GROQ"),
    )
    client_id: str | None = Field(
        default=None,
        description="OAuth-style client_id for providers that mint tokens on demand (e.g. Nusuk)",
    )
    client_secret: str | None = Field(
        default=None,
        description="OAuth-style client_secret paired with client_id",
    )
    auth_user_id: str | None = Field(
        default=None,
        description="user_id passed in the Nusuk /auth/token body. Defaults to client_id when unset.",
    )
    language: str = Field(default="ar", description="Language hint for the LLM service")
    query_prefix: str | None = Field(
        default=None,
        description="Text prepended to every user query (e.g. response-style instructions for providers that ignore system prompts)",
    )
    include_metadata: bool = Field(default=True, description="Request metadata when the provider supports it")
    tool: str = Field(default="Knowledge", description="Nusuk tool name")
    temperature: float = Field(default=0.2, ge=0.0, le=2.0)
    max_tokens: int = Field(default=96, ge=1)
    reasoning_effort: str | None = Field(
        default=None,
        description="Groq gpt-oss reasoning_effort: low/medium/high. Unset = provider default.",
    )
    timeout_seconds: float = Field(default=60.0, ge=1)
    # TEMP: only used by the `nusuk_rag` provider. Remove with that provider.
    rag_top_k: int = Field(default=12, ge=1, le=50)


class TTSSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="CUSTOM_TTS_", extra="ignore")

    provider: str = Field(default="local_api", description="local_api or generic")
    url: str = Field(..., description="External TTS endpoint")
    access_token: str | None = Field(default=None, description="Bearer token for the TTS API")
    model: str = Field(..., description="TTS model name")
    voice: str = Field(default="default", description="Requested voice")
    sample_rate: int = Field(default=24000, ge=8000)
    num_channels: int = Field(default=1, ge=1)
    audio_format: str = Field(default="wav", description="wav or pcm")
    timeout_seconds: float = Field(default=60.0, ge=1)


class AgentSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="AGENT_", extra="ignore")

    name: str = Field(default="nusuk-agent")
    system_prompt: str = Field(
        default="أجب بالعربية في أقل من 40 كلمة، وحاول الإجابة مباشرة عن سؤال المستخدم."
    )
    system_prompt_file: str | None = Field(
        default=DEFAULT_SYSTEM_PROMPT_FILE,
        description=(
            "Path to a file whose contents replace system_prompt at startup. "
            "Defaults to <agent_dir>/system_prompt_rag.txt — self-healing via "
            "_load_prompt_file when missing (regenerates from "
            "prompts.voice_prompt.RAG_VOICE_PROMPT). Override only when "
            "pointing at a non-standard layout."
        ),
    )
    explicit_eos_mode: bool = Field(default=False)
    explicit_eos_topic: str = Field(default="eval.eos")
    vad_activation_threshold: float = Field(default=0.5, ge=0.0, le=1.0)
    allow_interruptions: bool = Field(default=True)
    discard_audio_if_uninterruptible: bool = Field(default=True)
    min_interruption_duration: float = Field(default=0.5, ge=0.0)
    min_interruption_words: int = Field(default=1, ge=0)
    min_endpointing_delay: float = Field(default=0.3, ge=0.0)
    max_endpointing_delay: float = Field(default=2.0, ge=0.0)
    false_interruption_timeout: float | None = Field(default=2.0, ge=0.0)
    resume_false_interruption: bool = Field(default=True)
    min_consecutive_speech_delay: float = Field(default=0.0, ge=0.0)
    use_tts_aligned_transcript: bool = Field(default=False)
    participant_identity: str | None = Field(default=None)
    close_on_disconnect: bool = Field(default=True)
    delete_room_on_close: bool = Field(default=False)
    # Self-hosted noise cancellation via DeepFilterNet3 (Apache-2.0). Off by
    # default — enabling pulls PyTorch + DeepFilterNet into the agent image
    # (~600 MB) and adds ~12 ms latency per audio frame. See
    # agent/plugins/denoiser.py.
    noise_cancellation: bool = Field(default=False)

    @model_validator(mode="after")
    def _load_prompt_file(self) -> "AgentSettings":
        if not self.system_prompt_file:
            return self

        path = Path(self.system_prompt_file)
        if path.exists():
            self.system_prompt = path.read_text(encoding="utf-8")
            return self

        # File missing — regenerate from in-repo source (agent/prompts/prompts.json)
        # so a fresh checkout or `git clean -fdx` doesn't break agent worker startup.
        logger.warning("system_prompt_file=%s missing; attempting regeneration", path)
        regenerated = _regenerate_prompt_file(path)
        if regenerated is not None:
            self.system_prompt = regenerated
            return self

        # Print a stderr banner first so the actionable message survives any
        # later wrapping inside LiveKit IPC's DuplexClosed traceback. ValueError
        # (not FileNotFoundError) is the pydantic-idiomatic shape — it's
        # surfaced as a clean ValidationError with the offending field name.
        banner = (
            "\n" + "=" * 72 + "\n"
            "FATAL: agent system prompt unavailable\n"
            + "=" * 72 + "\n"
            f"  system_prompt_file = {path}\n"
            f"  File missing AND regeneration from agent/prompts/voice_prompt.py failed.\n"
            f"  Fix one of:\n"
            f"    1. Restore the file at {path}.\n"
            f"    2. Ensure agent/prompts/prompts.json is present in the image.\n"
            f"    3. Unset AGENT_SYSTEM_PROMPT_FILE to use the inline AGENT_SYSTEM_PROMPT.\n"
            + "=" * 72 + "\n"
        )
        sys.stderr.write(banner)
        sys.stderr.flush()
        raise ValueError(
            f"system_prompt_file={path} does not exist and agent/prompts/voice_prompt.py "
            f"fallback could not be loaded — see banner above for fix options."
        )
