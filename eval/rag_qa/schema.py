from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass
class RetrievedHit:
    rank: int
    id: str
    collection_type: str
    embedding_score: float | None
    reranked_score: float | None
    text: str


@dataclass
class Alignment:
    sentence_count: int
    exceeds_sentence_limit: bool
    has_markdown: bool
    has_url: bool
    has_service_id: bool
    language_match: bool
    grounded_refusal_when_empty: bool | None
    all_align_rules_passed: bool


@dataclass
class Timings:
    retrieve_ms: float = 0.0      # embed + milvus + rerank combined
    llm_ms: float = 0.0
    ttft_ms: float = 0.0          # LLM first token
    pipeline_ttft_ms: float = 0.0  # retrieve + llm ttft (user-perceived)
    total_ms: float = 0.0


@dataclass
class ResponseRecord:
    id: str
    language: str
    category: str
    domain: str
    persona: str
    trick_type: str
    question: str
    ideal_answer: str
    guardrail_note: str
    answer: str
    retrieved: list[RetrievedHit] = field(default_factory=list)
    alignment: Alignment | None = None
    timings_ms: Timings = field(default_factory=Timings)
    model: str = ""
    system_prompt_hash: str = ""
    run_id: str = ""
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
