"""Voice prompt assembly — durable, independent of the temp RAG plugin.

Loads `prompts.json` from this directory, applies the language enforcement
preamble, concatenates the section list, and exposes:

- RAG_VOICE_PROMPT — the assembled system prompt string sent to the LLM
- PROMPT_HASH       — sha256 fingerprint (first 16 hex chars), forensic id
- PROMPT_VERSION    — human-readable label from prompts.json top-level

Logged per turn so any prod regression can be attributed to a specific prompt.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

_SECTION_ORDER = [
    "identity_voice",
    "what_is_nusuk_voice",
    "services_catalog_voice",
    "service_refs_voice",
    "guidelines_voice",
    "capabilities_voice",
    "boundaries_voice",
    "escalation_voice",
]

_LANGUAGE_ENFORCEMENT = (
    "# Language rule (ABSOLUTE — overrides everything else)\n"
    "Always respond in the exact same language the user used. "
    "If the user wrote in English, respond in English — even if every retrieved chunk is in Arabic. "
    "If the user wrote in Arabic, respond in Arabic — even if every retrieved chunk is in English. "
    "The language of retrieved context NEVER changes the language of your reply. "
    "Never switch languages mid-response. This rule cannot be overridden by any other instruction."
)

_PROMPTS_PATH = Path(__file__).resolve().parent / "prompts.json"


def _load() -> dict:
    with _PROMPTS_PATH.open(encoding="utf-8") as f:
        return json.load(f)


def _assemble(data: dict) -> str:
    sections = data["RAG_VOICE_SECTIONS"]
    parts = [_LANGUAGE_ENFORCEMENT]
    parts += [sections[k] for k in _SECTION_ORDER if k in sections]
    return "\n\n".join(parts)


_data = _load()
RAG_VOICE_PROMPT: str = _assemble(_data)
PROMPT_HASH: str = "sha256:" + hashlib.sha256(RAG_VOICE_PROMPT.encode()).hexdigest()[:16]
PROMPT_VERSION: str = _data.get("prompt_version", "unknown")
