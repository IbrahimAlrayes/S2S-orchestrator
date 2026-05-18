# Ported from rag-nusuk-ai/vectorstore/preprocessor_nusuk.py.
# Only `get_vectorization_text` (+ its six per-collection-type helpers) and
# `_format_results_unified` are kept — those are the only functions the
# retrieval path needs. Insert/update helpers, loaders, and DB-backed
# metadata writers are intentionally omitted.

from __future__ import annotations

import logging
from typing import Any, Dict, List

logger = logging.getLogger(__name__)


# ============================================================================
# UNIFIED TEXT GENERATION (for embedding & reranker)
# ============================================================================

def get_vectorization_text(row: Dict[str, Any], collection_type: str) -> str:
    """Generate the text the reranker will score, given a Milvus row + its
    collection_type. Uses Milvus column names (post-mapping)."""
    handlers = {
        "faq": _get_text_faq,
        "service": _get_text_service,
        "fatawa": _get_text_fatawa,
        "location": _get_text_location,
        "husnmuslim": _get_text_husnmuslim,
        "hadeths": _get_text_hadeths,
    }
    handler = handlers.get(collection_type.lower())
    if not handler:
        # Be lenient — unknown types get a minimal best-effort concat so the
        # reranker still has something to score, rather than raising and
        # collapsing the whole retrieval.
        logger.warning("Unknown collection_type=%r — using minimal text", collection_type)
        return " ".join(str(v) for v in row.values() if isinstance(v, str))[:512]
    return handler(row)


def _get_text_faq(row: Dict[str, Any]) -> str:
    ar_parts, en_parts = [], []
    if q := (row.get("faq_question_ar") or "").strip():
        ar_parts.append(f"السؤال: {q}")
    if a := (row.get("faq_answer_ar") or "").strip():
        ar_parts.append(f"الإجابة: {a}")
    if q := (row.get("faq_question_en") or "").strip():
        en_parts.append(f"Question: {q}")
    if a := (row.get("faq_answer_en") or "").strip():
        en_parts.append(f"Answer: {a}")
    sections = []
    if ar_parts:
        sections.append("\n".join(ar_parts))
    if en_parts:
        sections.append("\n".join(en_parts))
    return "\n\n".join(sections).strip()


def _get_text_service(row: Dict[str, Any]) -> str:
    en_parts, ar_parts = [], []
    if v := (row.get("title_en") or "").strip():
        en_parts.append(f"Title: {v}")
    if v := (row.get("description_en") or "").strip():
        en_parts.append(f"Description: {v}")
    if v := (row.get("service_faq_en") or "").strip():
        en_parts.append(f"Question: {v}")
    if v := (row.get("sample_responses_en") or "").strip():
        en_parts.append(f"Answer: {v}")
    if v := (row.get("title_ar") or "").strip():
        ar_parts.append(f"العنوان: {v}")
    if v := (row.get("description_ar") or "").strip():
        ar_parts.append(f"الوصف: {v}")
    if v := (row.get("service_faq_ar") or "").strip():
        ar_parts.append(f"السؤال: {v}")
    if v := (row.get("sample_responses_ar") or "").strip():
        ar_parts.append(f"الإجابة: {v}")
    sections = []
    if en_parts:
        sections.append("\n".join(en_parts))
    if ar_parts:
        sections.append("\n".join(ar_parts))
    return "\n\n".join(sections).strip()


def _get_text_fatawa(row: Dict[str, Any]) -> str:
    ar_parts, en_parts = [], []
    if v := (row.get("fatawa_question_ar") or "").strip():
        ar_parts.append(f"السؤال: {v}")
    if v := (row.get("fatawa_answer_ar") or "").strip():
        ar_parts.append(f"الجواب: {v}")
    if v := (row.get("topic_ar") or "").strip():
        ar_parts.append(f"الموضوع: {v}")
    if v := (row.get("subtopic_ar") or "").strip():
        ar_parts.append(f"الموضوع الفرعي: {v}")
    if v := (row.get("fatawa_question_en") or "").strip():
        en_parts.append(f"Question: {v}")
    if v := (row.get("fatawa_answer_en") or "").strip():
        en_parts.append(f"Answer: {v}")
    sections = []
    if ar_parts:
        sections.append("\n".join(ar_parts))
    if en_parts:
        sections.append("\n".join(en_parts))
    return "\n\n".join(sections).strip()


def _get_text_location(row: Dict[str, Any]) -> str:
    ar_parts, en_parts = [], []
    if v := (row.get("location_name_ar") or "").strip():
        ar_parts.append(f"اسم المكان: {v}")
    if v := (row.get("location_description_ar") or "").strip():
        ar_parts.append(f"الوصف: {v}")
    if v := (row.get("location_city_ar") or "").strip():
        ar_parts.append(f"المدينة: {v}")
    if v := (row.get("location_category_ar") or "").strip():
        ar_parts.append(f"التصنيف: {v}")
    if v := (row.get("location_name_en") or "").strip():
        en_parts.append(f"Location Name: {v}")
    if v := (row.get("location_description_en") or "").strip():
        en_parts.append(f"Description: {v}")
    if v := (row.get("location_city_en") or row.get("location_city_ar") or "").strip():
        en_parts.append(f"City: {v}")
    if v := (row.get("location_category_en") or "").strip():
        en_parts.append(f"Category: {v}")
    sections = []
    if ar_parts:
        sections.append("\n".join(ar_parts))
    if en_parts:
        sections.append("\n".join(en_parts))
    return "\n\n".join(sections).strip()


def _get_text_husnmuslim(row: Dict[str, Any]) -> str:
    parts = []
    if v := (row.get("Zekar") or "").strip():
        parts.append(f"دعاء: {v}")
    if v := (row.get("Tabweeb") or "").strip():
        parts.append(f"نوع الدعاء: {v}")
    if v := (row.get("faq_question_ar") or "").strip():
        parts.append(f"السؤال: {v}")
    if v := (row.get("faq_answer_ar") or "").strip():
        parts.append(f"الإجابة: {v}")
    return "\n".join(parts).strip()


def _get_text_hadeths(row: Dict[str, Any]) -> str:
    parts = []
    if v := (row.get("Zekar") or "").strip():
        parts.append(f"الحديث: {v}")
    if v := (row.get("Sharh") or "").strip():
        parts.append(f"الشرح: {v}")
    if v := (row.get("category") or "").strip():
        parts.append(f"الموضوع: {v}")
    if v := (row.get("faq_question_ar") or "").strip():
        parts.append(f"مختصر: {v}")
    return "\n".join(parts).strip()


# ============================================================================
# RESULT FORMATTING (Milvus hit list → list of clean dicts)
# ============================================================================

def _format_results_unified(
    results: List[List[Dict[str, Any]]],
    context_config: Dict[str, Any],
    out_fields: List[str],
    language: str = "arabic",
) -> List[Dict[str, Any]]:
    """Flatten Milvus' nested hit-list structure into a flat list of result
    dicts with `id`, `collection_type`, `embedding_score`, plus all
    `out_fields` pulled out of the entity payload."""
    formatted = []

    for hit_list in results:
        for item in hit_list:
            entity = item.get("entity", {})
            collection_type = entity.get("collection_type", "")

            result = {
                "id": entity.get("id"),
                "collection_type": collection_type,
                "context_to_llm": "",
                "embedding_score": item.get("distance"),
            }
            for field in out_fields:
                if field not in result:
                    result[field] = entity.get(field, None)

            formatted.append(result)

    return formatted
