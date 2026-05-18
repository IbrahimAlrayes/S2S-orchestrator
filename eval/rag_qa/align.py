from __future__ import annotations

import re

from .schema import Alignment

# Sentence terminators we accept for splitting: Latin . ! ?, Arabic ? ؟, CJK 。
_SENT_SPLIT = re.compile(r"[.!?؟。]+\s*")

# Markdown markers per system prompt: no bold/italic/headers/code/lists/citations.
# Citation pattern [N] is stripped by TTS but the model shouldn't emit it either.
_MD_PATTERNS = [
    re.compile(r"\*\*[^*]+\*\*"),          # **bold**
    re.compile(r"(?<!\*)\*[^*\n]+\*"),     # *italic*
    re.compile(r"(?<!_)__[^_]+__"),        # __bold__
    re.compile(r"(?:^|\n)#+\s"),           # # heading
    re.compile(r"```"),                    # code fence
    re.compile(r"`[^`\n]+`"),              # `inline code`
    re.compile(r"(?:^|\n)\s*[-*]\s"),      # - bullet / * bullet
    re.compile(r"(?:^|\n)\s*\d+\.\s"),     # 1. numbered list
    re.compile(r"\[\d+\]"),                # [1] citation marker
]

_URL_PATTERN = re.compile(r"\bhttps?://|\bwww\.", re.IGNORECASE)

# Service/FAQ/fatawa IDs as they appear in Milvus collection 4.
_SERVICE_ID_PATTERNS = [
    re.compile(r"\bservice_[a-z0-9_]+_\d+\b", re.IGNORECASE),
    re.compile(r"\bfaq_\d+\b", re.IGNORECASE),
    re.compile(r"\bfatawa_\d+\b", re.IGNORECASE),
    re.compile(r"\bhusnmuslim_\d+\b", re.IGNORECASE),
    re.compile(r"\blocation_\d+\b", re.IGNORECASE),
]

# Arabic Unicode blocks (basic + supplement + extended-A + presentation forms).
_AR_CHAR = re.compile(r"[؀-ۿݐ-ݿࢠ-ࣿﭐ-﷿ﹰ-﻿]")
_LATIN_CHAR = re.compile(r"[A-Za-z]")

# Hedging phrases we accept as "grounded refusal" when retrieval returns nothing.
_REFUSAL_HINTS_AR = [
    "لا تتوفر",
    "لا أملك",
    "لا أعرف",
    "ليس لدي",
    "لا توجد لدي",
    "غير متوفرة",
    "غير متوفر",
    "ليست لدي",
]
_REFUSAL_HINTS_EN = [
    "don't have that information",
    "do not have that information",
    "i don't have",
    "i'm not sure",
    "i am not sure",
    "i don't know",
    "no information",
    "cannot answer",
    "can't answer",
    "not available",
]


def _detect_question_language(question: str) -> str:
    ar = len(_AR_CHAR.findall(question))
    en = len(_LATIN_CHAR.findall(question))
    return "Arabic" if ar > en else "English"


def _count_sentences(text: str) -> int:
    text = text.strip()
    if not text:
        return 0
    parts = [p for p in _SENT_SPLIT.split(text) if p.strip()]
    return max(len(parts), 1)


def _has_markdown(text: str) -> bool:
    return any(p.search(text) for p in _MD_PATTERNS)


def _has_url(text: str) -> bool:
    return bool(_URL_PATTERN.search(text))


def _has_service_id(text: str) -> bool:
    return any(p.search(text) for p in _SERVICE_ID_PATTERNS)


def _language_match(question: str, answer: str) -> bool:
    q_lang = _detect_question_language(question)
    ar = len(_AR_CHAR.findall(answer))
    en = len(_LATIN_CHAR.findall(answer))
    if ar + en == 0:
        return False
    a_lang = "Arabic" if ar > en else "English"
    return q_lang == a_lang


def _grounded_refusal(question: str, answer: str) -> bool:
    lang = _detect_question_language(question)
    hints = _REFUSAL_HINTS_AR if lang == "Arabic" else _REFUSAL_HINTS_EN
    lower = answer.lower()
    return any(h.lower() in lower for h in hints)


def check_alignment(question: str, answer: str, retrieval_was_empty: bool) -> Alignment:
    sc = _count_sentences(answer)
    exceeds = sc > 3
    md = _has_markdown(answer)
    url = _has_url(answer)
    sid = _has_service_id(answer)
    lang_ok = _language_match(question, answer)
    if retrieval_was_empty:
        grounded = _grounded_refusal(question, answer)
        grounded_field: bool | None = grounded
    else:
        grounded_field = None
        grounded = True

    all_passed = (
        not exceeds
        and not md
        and not url
        and not sid
        and lang_ok
        and grounded
    )

    return Alignment(
        sentence_count=sc,
        exceeds_sentence_limit=exceeds,
        has_markdown=md,
        has_url=url,
        has_service_id=sid,
        language_match=lang_ok,
        grounded_refusal_when_empty=grounded_field,
        all_align_rules_passed=all_passed,
    )
