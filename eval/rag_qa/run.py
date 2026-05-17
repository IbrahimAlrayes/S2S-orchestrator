from __future__ import annotations

import argparse
import asyncio
import dataclasses
import hashlib
import json
import logging
import os
import re
import socket
import statistics
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx

# Make `plugins.rag` importable when this script runs from inside the agent
# container with /app/eval mounted alongside /app.
sys.path.insert(0, "/app")

from plugins.rag import retrieve as rag_retrieve  # noqa: E402
from plugins.rag.preprocessor_nusuk import get_vectorization_text  # noqa: E402

from .align import check_alignment  # noqa: E402
from .sampling import load_csv, stratified_sample, write_sample_csv  # noqa: E402
from .schema import (  # noqa: E402
    Alignment,
    ResponseRecord,
    RetrievedHit,
    Timings,
)

logger = logging.getLogger("rag_qa")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)

_PAGE_ID_NAMES: dict[int, str] = {
    20: "Companion Management",
    23: "Noble Rawdah",
    24: "Umrah Permits",
    26: "Hajj",
    28: "Certificates",
    29: "Notification Center",
    30: "Dynamic Umrah Packages",
    31: "Hotels",
    32: "SIM and eSIM",
    33: "Flights",
    34: "Profile",
    35: "Zamzam Water",
    37: "Journey",
    38: "AlSibha",
    39: "Nusuk Wallet",
    40: "Prayer Times",
    41: "AlMushaf",
    42: "Content Hub",
    47: "Haramain Railway",
}
_PAGE_ID_RE = re.compile(r"<PAGE_ID>(\d+)</PAGE_ID>")
_ARABIC_RE = re.compile(r"[؀-ۿ]")


def _query_language(text: str) -> str:
    return "Arabic" if _ARABIC_RE.search(text) else "English"


def _substitute_page_ids(text: str) -> str:
    return _PAGE_ID_RE.sub(lambda m: _PAGE_ID_NAMES.get(int(m.group(1)), m.group(0)), text)


RAG_FUSION_PREAMBLE = (
    "استخدم السياق التالي للإجابة عن سؤال المستخدم. "
    "السياق مرقَّم؛ لا تذكر أرقام المراجع في إجابتك."
)


def _read_system_prompt() -> tuple[str, str]:
    from prompts.voice_prompt import PROMPT_HASH, RAG_VOICE_PROMPT

    return RAG_VOICE_PROMPT, PROMPT_HASH


def _tcp_probe(host: str, port: int, timeout_s: float = 1.0) -> bool:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(timeout_s)
    try:
        s.connect((host, port))
        return True
    except OSError:
        return False
    finally:
        s.close()


def _milvus_host_port_from_env() -> tuple[str, int]:
    raw = os.environ.get("MILVUS_HOST", "")
    port = int(os.environ.get("MILVUS_PORT", "19530"))
    if "://" in raw:
        raw = urlparse(raw).hostname or raw.split("://", 1)[1]
    return raw, port


async def _prereq_checks(client: httpx.AsyncClient) -> None:
    host, port = _milvus_host_port_from_env()
    if not _tcp_probe(host, port):
        raise SystemExit(
            f"prereq_fail: Milvus unreachable at {host}:{port}. "
            "Run: kubectl port-forward -n nlp-rag svc/milvus 19530:19530"
        )
    rerank_url = os.environ["RERANK_SERVICE_URL"]
    embed_url = os.environ["EMBEDDING_SERVICE_URL"]
    try:
        await client.get(rerank_url.replace("/score", "/v1/models"), timeout=5.0)
    except Exception as exc:
        raise SystemExit(f"prereq_fail: reranker unreachable: {exc}")
    try:
        await client.get(embed_url.replace("/v1/embeddings", "/v1/models"), timeout=5.0)
    except Exception as exc:
        raise SystemExit(f"prereq_fail: embedding unreachable: {exc}")
    if not (os.environ.get("CUSTOM_LLM_ACCESS_TOKEN") or os.environ.get("GROQ_API_KEY")):
        raise SystemExit("prereq_fail: no CUSTOM_LLM_ACCESS_TOKEN or GROQ_API_KEY in env")


def _llm_settings() -> dict[str, Any]:
    return {
        "url": os.environ["CUSTOM_LLM_URL"].rstrip("/"),
        "model": os.environ["CUSTOM_LLM_MODEL"],
        "key": os.environ.get("CUSTOM_LLM_ACCESS_TOKEN") or os.environ["GROQ_API_KEY"],
        "temperature": float(os.environ.get("CUSTOM_LLM_TEMPERATURE", "0.2")),
        "max_tokens": int(os.environ.get("CUSTOM_LLM_MAX_TOKENS", "768")),
        "reasoning_effort": os.environ.get("CUSTOM_LLM_REASONING_EFFORT", "low"),
        "timeout": float(os.environ.get("CUSTOM_LLM_TIMEOUT_SECONDS", "60")),
    }


def _strip_think(text: str) -> str:
    """Remove <think>…</think> blocks produced by reasoning models."""
    cleaned = text
    while True:
        start = cleaned.find("<think>")
        if start == -1:
            break
        end = cleaned.find("</think>", start)
        if end == -1:
            cleaned = cleaned[:start]
            break
        cleaned = cleaned[:start] + cleaned[end + len("</think>"):]
    return cleaned.strip()


async def _call_groq(
    client: httpx.AsyncClient,
    cfg: dict[str, Any],
    messages: list[dict[str, str]],
) -> tuple[str, float, float, float]:
    """Returns (answer, llm_ms, ttft_ms, ttft_visible_ms).

    ttft_ms         — time to first token of any kind (including <think> reasoning).
    ttft_visible_ms — time to first visible token (after </think> closes).
    """
    body = {
        "model": cfg["model"],
        "messages": messages,
        "temperature": cfg["temperature"],
        "max_tokens": cfg["max_tokens"],
        "stream": True,
    }
    if cfg["reasoning_effort"]:
        body["reasoning_effort"] = cfg["reasoning_effort"]

    t0 = time.perf_counter()
    ttft_ms = 0.0
    ttft_visible_ms = 0.0
    raw_buf = ""

    async with client.stream(
        "POST",
        f"{cfg['url']}/chat/completions",
        headers={
            "Authorization": f"Bearer {cfg['key']}",
            "Content-Type": "application/json",
        },
        json=body,
        timeout=cfg["timeout"],
    ) as resp:
        resp.raise_for_status()
        async for line in resp.aiter_lines():
            if not line.startswith("data:"):
                continue
            data = line.removeprefix("data:").strip()
            if data == "[DONE]":
                break
            try:
                event = json.loads(data)
            except json.JSONDecodeError:
                continue
            delta = (event.get("choices") or [{}])[0].get("delta", {}).get("content") or ""
            if delta:
                elapsed = (time.perf_counter() - t0) * 1000
                if not ttft_ms:
                    ttft_ms = elapsed
                raw_buf += delta
                if not ttft_visible_ms and _strip_think(raw_buf).strip():
                    ttft_visible_ms = elapsed

    llm_ms = (time.perf_counter() - t0) * 1000
    answer = _strip_think(raw_buf)
    return answer, llm_ms, ttft_ms


def _build_fused_system(base_prompt: str, hits: list[dict[str, Any]]) -> str:
    if not hits:
        return base_prompt
    context_block = rag_retrieve.format_context_block(hits)
    return f"{base_prompt}\n\n{RAG_FUSION_PREAMBLE}\n\n{context_block}"


def _truncate(s: str, n: int = 160) -> str:
    s = (s or "").replace("\n", " ").strip()
    return s if len(s) <= n else s[: n - 1] + "…"


def _summarize_hits(hits: list[dict[str, Any]]) -> list[RetrievedHit]:
    out: list[RetrievedHit] = []
    for i, h in enumerate(hits, 1):
        ctype = str(h.get("collection_type") or "")
        text = get_vectorization_text(h, ctype)
        out.append(
            RetrievedHit(
                rank=i,
                id=str(h.get("id") or ""),
                collection_type=ctype,
                embedding_score=h.get("embedding_score"),
                reranked_score=h.get("reranked_score"),
                text=text,
            )
        )
    return out


async def _process_row(
    row: dict[str, str],
    *,
    base_prompt: str,
    prompt_hash: str,
    llm_cfg: dict[str, Any],
    client: httpx.AsyncClient,
    top_k: int,
    run_id: str,
    max_attempts: int = 1,
) -> ResponseRecord:
    rec = ResponseRecord(
        id=row["ID"],
        language=row["Language"],
        category=row["Category"],
        domain=row["Domain"],
        persona=row["Persona"],
        trick_type=row.get("Trick Type", ""),
        question=row["Question"],
        ideal_answer=row["Ideal Answer"],
        guardrail_note=row.get("Guardrail Note", ""),
        answer="",
        model=llm_cfg["model"],
        system_prompt_hash=prompt_hash,
        run_id=run_id,
    )

    t_total = time.perf_counter()
    try:
        t_retrieve = time.perf_counter()
        hits = await rag_retrieve.retrieve(row["Question"], top_k=top_k)
        rec.timings_ms.retrieve_ms = (time.perf_counter() - t_retrieve) * 1000
        rec.retrieved = _summarize_hits(hits)

        lang = _query_language(row["Question"])
        fused_system = (
            _build_fused_system(base_prompt, hits)
            + f"\n\nThe user's message is in {lang}. You MUST respond in {lang} only."
        )
        query_prefix = os.environ.get("CUSTOM_LLM_QUERY_PREFIX", "").strip()
        user_content = f"{query_prefix} {row['Question']}" if query_prefix else row["Question"]
        messages = [
            {"role": "system", "content": fused_system},
            {"role": "user", "content": user_content},
        ]
        for attempt in range(max_attempts):
            try:
                answer, llm_ms, ttft_ms = await _call_groq(client, llm_cfg, messages)
                break
            except Exception as exc:
                if attempt == max_attempts - 1:
                    raise
                wait = 1.0
                if isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code == 429:
                    wait = float(exc.response.headers.get("retry-after", 0)) or 5.0 * (attempt + 1)
                logger.warning(
                    "row_retry id=%s attempt=%d/%d wait=%.1fs err=%s",
                    rec.id, attempt + 1, max_attempts, wait, exc,
                )
                await asyncio.sleep(wait)
        rec.answer = _substitute_page_ids(answer)
        rec.timings_ms.llm_ms = llm_ms
        rec.timings_ms.ttft_ms = ttft_ms
        rec.timings_ms.pipeline_ttft_ms = rec.timings_ms.retrieve_ms + ttft_ms

        rec.alignment = check_alignment(
            question=row["Question"],
            answer=answer,
            retrieval_was_empty=not hits,
        )
    except Exception as exc:
        rec.error = f"{type(exc).__name__}: {exc}"
        logger.warning("row_failed id=%s err=%s", rec.id, rec.error)
    finally:
        rec.timings_ms.total_ms = (time.perf_counter() - t_total) * 1000
    return rec


def _load_done_ids(jsonl_path: Path) -> set[str]:
    if not jsonl_path.exists():
        return set()
    done: set[str] = set()
    with jsonl_path.open("r", encoding="utf-8") as f:
        for line in f:
            try:
                done.add(json.loads(line)["id"])
            except Exception:
                continue
    return done


async def _run(
    rows: list[dict[str, str]],
    run_dir: Path,
    *,
    base_prompt: str,
    prompt_hash: str,
    llm_cfg: dict[str, Any],
    top_k: int,
    concurrency: int,
    run_id: str,
    resume: bool,
    max_attempts: int = 1,
) -> list[ResponseRecord]:
    jsonl_path = run_dir / "responses.jsonl"
    done_ids = _load_done_ids(jsonl_path) if resume else set()
    todo = [r for r in rows if r["ID"] not in done_ids]
    logger.info(
        "starting rows=%d already_done=%d concurrency=%d",
        len(todo),
        len(done_ids),
        concurrency,
    )

    results: list[ResponseRecord] = []
    sem = asyncio.Semaphore(concurrency)
    write_lock = asyncio.Lock()

    async with httpx.AsyncClient(http2=True, timeout=httpx.Timeout(60.0)) as client:
        await _prereq_checks(client)

        async def worker(r: dict[str, str]) -> None:
            async with sem:
                rec = await _process_row(
                    r,
                    base_prompt=base_prompt,
                    prompt_hash=prompt_hash,
                    llm_cfg=llm_cfg,
                    client=client,
                    top_k=top_k,
                    run_id=run_id,
                    max_attempts=max_attempts,
                )
                async with write_lock:
                    with jsonl_path.open("a", encoding="utf-8") as f:
                        f.write(json.dumps(rec.to_dict(), ensure_ascii=False) + "\n")
                    results.append(rec)
                    n = len(results)
                    if n % 10 == 0 or n == len(todo):
                        logger.info("progress %d/%d", n, len(todo))

        await asyncio.gather(*(worker(r) for r in todo))

    return results


def _percentile(xs: list[float], p: float) -> float:
    if not xs:
        return 0.0
    xs2 = sorted(xs)
    k = int(round((p / 100.0) * (len(xs2) - 1)))
    return xs2[k]


def _write_summary(records: list[ResponseRecord], run_dir: Path, cfg: dict[str, Any]) -> None:
    n = len(records)
    n_err = sum(1 for r in records if r.error)
    n_empty = sum(1 for r in records if not r.retrieved and not r.error)

    def t(name: str) -> list[float]:
        return [getattr(r.timings_ms, name) for r in records if not r.error]

    retrieve = t("retrieve_ms")
    llm = t("llm_ms")
    ttft = [v for v in t("ttft_ms") if v > 0]
    pipeline_ttft = [v for v in t("pipeline_ttft_ms") if v > 0]
    total = t("total_ms")
    hits_n = [len(r.retrieved) for r in records if not r.error]

    by_cell: dict[tuple[str, str], int] = Counter(
        (r.category, r.language) for r in records
    )

    lines = [
        f"# RAG QA summary — {cfg['run_id']}\n",
        "## Config",
        "```json",
        json.dumps(cfg, ensure_ascii=False, indent=2),
        "```",
        "",
        "## Counts",
        f"- rows: {n}",
        f"- errors: {n_err}",
        f"- rows with zero hits retrieved: {n_empty}",
        "",
        "## Timings (ms, successful rows only)",
        f"| stage | p50 | p95 |",
        f"|---|---:|---:|",
        f"| retrieve (embed+rerank) | {_percentile(retrieve, 50):.0f} | {_percentile(retrieve, 95):.0f} |",
        f"| llm TTFT | {_percentile(ttft, 50):.0f} | {_percentile(ttft, 95):.0f} |" if ttft else "| llm TTFT | n/a | n/a |",
        f"| pipeline TTFT | {_percentile(pipeline_ttft, 50):.0f} | {_percentile(pipeline_ttft, 95):.0f} |" if pipeline_ttft else "| pipeline TTFT | n/a | n/a |",
        f"| llm (total) | {_percentile(llm, 50):.0f} | {_percentile(llm, 95):.0f} |",
        f"| total | {_percentile(total, 50):.0f} | {_percentile(total, 95):.0f} |",
        "",
        "## Retrieval",
        f"- avg hits returned: {statistics.mean(hits_n):.1f}" if hits_n else "- avg hits: n/a",
        "",
        "## Sample distribution (Category × Language)",
    ]
    for key in sorted(by_cell.keys()):
        lines.append(f"- {key[0]} / {key[1]}: {by_cell[key]}")

    (run_dir / "summary.md").write_text("\n".join(lines), encoding="utf-8")


def _write_align_report(records: list[ResponseRecord], run_dir: Path) -> None:
    n = len(records)
    by_rule = Counter()
    by_cell: dict[tuple[str, str], list[ResponseRecord]] = defaultdict(list)

    for r in records:
        if r.error or r.alignment is None:
            continue
        a = r.alignment
        if a.exceeds_sentence_limit: by_rule["exceeds_sentence_limit"] += 1
        if a.has_markdown:           by_rule["has_markdown"] += 1
        if a.has_url:                by_rule["has_url"] += 1
        if a.has_service_id:         by_rule["has_service_id"] += 1
        if not a.language_match:     by_rule["language_mismatch"] += 1
        if a.grounded_refusal_when_empty is False:
            by_rule["ungrounded_when_empty"] += 1
        if a.all_align_rules_passed:
            by_rule["all_passed"] += 1
        by_cell[(r.category, r.language)].append(r)

    lines = [
        "# Alignment report (rule-based)\n",
        f"Total scored: {n}",
        "",
        "## Failure counts",
        f"- exceeds_sentence_limit:   {by_rule['exceeds_sentence_limit']}",
        f"- has_markdown:             {by_rule['has_markdown']}",
        f"- has_url:                  {by_rule['has_url']}",
        f"- has_service_id:           {by_rule['has_service_id']}",
        f"- language_mismatch:        {by_rule['language_mismatch']}",
        f"- ungrounded_when_empty:    {by_rule['ungrounded_when_empty']}",
        f"- ALL_PASSED:               {by_rule['all_passed']}",
        "",
        "## By (Category × Language) — pass rate",
    ]
    for key in sorted(by_cell.keys()):
        bucket = by_cell[key]
        passed = sum(1 for x in bucket if x.alignment and x.alignment.all_align_rules_passed)
        lines.append(f"- {key[0]} / {key[1]}: {passed}/{len(bucket)}")

    lines.append("")
    lines.append("## First 10 failures (for eyeball)")
    failures = [r for r in records
                if not r.error and r.alignment and not r.alignment.all_align_rules_passed][:10]
    for r in failures:
        a = r.alignment
        flags = ",".join(k for k, v in {
            "sent>3": a.exceeds_sentence_limit,
            "md": a.has_markdown,
            "url": a.has_url,
            "svc_id": a.has_service_id,
            "lang!=": not a.language_match,
            "ungrounded": a.grounded_refusal_when_empty is False,
        }.items() if v)
        lines.append(f"\n### {r.id} ({r.category} / {r.language}) [{flags}]")
        lines.append(f"Q: {_truncate(r.question, 200)}")
        lines.append(f"A: {_truncate(r.answer, 300)}")

    (run_dir / "align_report.md").write_text("\n".join(lines), encoding="utf-8")


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Nusuk RAG QA evaluation runner.")
    p.add_argument("--csv", required=True, help="Path to the guardrail CSV (utf-8-sig).")
    p.add_argument("--out", required=True, help="Output directory for this run.")
    p.add_argument("--total", type=int, default=100, help="Stratified sample size.")
    p.add_argument("--top-k", type=int, default=12)
    p.add_argument("--concurrency", type=int, default=2)
    p.add_argument("--seed", type=int, default=7)
    p.add_argument("--retries", type=int, default=3,
                   help="Max retries per row after the first attempt (default: 3).")
    p.add_argument("--resume", action="store_true",
                   help="Skip IDs already present in out/responses.jsonl.")
    return p.parse_args()


async def amain() -> int:
    args = _parse_args()

    csv_path = Path(args.csv)
    if not csv_path.exists():
        raise SystemExit(f"csv not found: {csv_path}")

    run_dir = Path(args.out)
    run_dir.mkdir(parents=True, exist_ok=True)
    run_id = run_dir.name or datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")

    base_prompt, prompt_hash = _read_system_prompt()
    if not base_prompt:
        raise SystemExit(f"system prompt empty (looked at {DEFAULT_SYSTEM_PROMPT_PATH})")

    llm_cfg = _llm_settings()

    rows = load_csv(csv_path)
    sample_path = run_dir / "sample.csv"
    if args.resume and sample_path.exists():
        sample = load_csv(sample_path)
        logger.info("resume: reusing sample.csv (%d rows)", len(sample))
    else:
        sample = stratified_sample(rows, total=args.total, seed=args.seed)
        write_sample_csv(sample, sample_path)
        logger.info("sample: %d rows written to %s", len(sample), sample_path)

    cfg_snapshot = {
        "run_id": run_id,
        "csv": str(csv_path),
        "sample_size": len(sample),
        "seed": args.seed,
        "top_k": args.top_k,
        "concurrency": args.concurrency,
        "retries": args.retries,
        "model": llm_cfg["model"],
        "llm_url": llm_cfg["url"],
        "temperature": llm_cfg["temperature"],
        "max_tokens": llm_cfg["max_tokens"],
        "reasoning_effort": llm_cfg["reasoning_effort"],
        "system_prompt_hash": prompt_hash,
        "milvus_collection": os.environ.get("MILVUS_COLLECTION"),
        "rerank_service_url": os.environ.get("RERANK_SERVICE_URL"),
        "embedding_service_url": os.environ.get("EMBEDDING_SERVICE_URL"),
        "started_at": datetime.now(timezone.utc).isoformat(),
    }
    (run_dir / "config.json").write_text(
        json.dumps(cfg_snapshot, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    records = await _run(
        sample,
        run_dir,
        base_prompt=base_prompt,
        prompt_hash=prompt_hash,
        llm_cfg=llm_cfg,
        top_k=args.top_k,
        concurrency=args.concurrency,
        run_id=run_id,
        resume=args.resume,
        max_attempts=args.retries + 1,
    )

    if args.resume:
        all_records: list[ResponseRecord] = []
        with (run_dir / "responses.jsonl").open(encoding="utf-8") as f:
            for line in f:
                d = json.loads(line)
                a = d.get("alignment") or {}
                d["alignment"] = Alignment(**a) if a else None
                _known = {f.name for f in dataclasses.fields(Timings)}
                d["timings_ms"] = Timings(**{k: v for k, v in d.get("timings_ms", {}).items() if k in _known})
                d["retrieved"] = [
                    RetrievedHit(**{k: v for k, v in h.items() if k != "text_preview"})
                    if "text_preview" in h
                    else RetrievedHit(**h)
                    for h in d.get("retrieved", [])
                ]
                all_records.append(ResponseRecord(**d))
        records = all_records

    _write_summary(records, run_dir, cfg_snapshot)
    _write_align_report(records, run_dir)
    logger.info("done. results in %s", run_dir)
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(amain()))
