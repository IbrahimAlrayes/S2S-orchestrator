"""Golden-set regression check (R-006).

Compares the rule-based alignment of a recent run against a stored baseline.
Optionally pulls judge metrics from `evaluations.jsonl` if present.

Usage
-----
    # 1. Run the golden set:
    #    docker compose run --rm --no-deps \
    #      -v ./eval:/app/eval \
    #      -v "$PWD/agent/prompts/prompts.json:/app/prompts/prompts.json:ro" \
    #      -v "$PWD/eval/rag_qa/golden/golden_v1.csv:/app/eval/data.csv:ro" \
    #      --entrypoint python agent \
    #      -m eval.rag_qa.run \
    #      --csv /app/eval/data.csv --out /app/eval/results/rag_qa/golden_check \
    #      --total 100 --seed 7
    #
    # 2. Check against baseline:
    #    python -m eval.rag_qa.regression \
    #      --run eval/results/rag_qa/golden_check \
    #      --baseline eval/rag_qa/golden/baseline_v1.json

Exit codes
----------
0 — no regression
1 — regression detected (printed diff to stderr)
2 — bad input (run dir missing, baseline missing, etc.)
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path


def _load_alignment_counts(run_dir: Path) -> dict[str, int]:
    """Re-scan responses.jsonl rather than parsing the markdown align_report."""
    counts = {
        "all_passed": 0,
        "exceeds_sentence_limit": 0,
        "has_markdown": 0,
        "has_url": 0,
        "has_service_id": 0,
        "language_mismatch": 0,
        "ungrounded_when_empty": 0,
        "total": 0,
    }
    path = run_dir / "responses.jsonl"
    if not path.exists():
        raise FileNotFoundError(f"missing {path}")
    with path.open(encoding="utf-8") as f:
        for line in f:
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                continue
            counts["total"] += 1
            a = d.get("alignment") or {}
            if a.get("all_align_rules_passed"):
                counts["all_passed"] += 1
            if a.get("exceeds_sentence_limit"):
                counts["exceeds_sentence_limit"] += 1
            if a.get("has_markdown"):
                counts["has_markdown"] += 1
            if a.get("has_url"):
                counts["has_url"] += 1
            if a.get("has_service_id"):
                counts["has_service_id"] += 1
            if not a.get("language_match", True):
                counts["language_mismatch"] += 1
            if a.get("grounded_refusal_when_empty") is False:
                counts["ungrounded_when_empty"] += 1
    return counts


def _load_judge_summary(run_dir: Path) -> dict | None:
    path = run_dir / "evaluations.jsonl"
    if not path.exists():
        return None
    scores: list[int] = []
    labels: Counter[str] = Counter()
    hallucinations = 0
    contradictions = 0
    language_mm = 0
    with path.open(encoding="utf-8") as f:
        for line in f:
            try:
                e = json.loads(line)
            except json.JSONDecodeError:
                continue
            if "score" in e:
                scores.append(int(e["score"]))
            if "label" in e:
                labels[e["label"]] += 1
            if e.get("hallucination_or_unsupported_addition"):
                hallucinations += 1
            if e.get("contradiction"):
                contradictions += 1
            if e.get("language_match") is False:
                language_mm += 1
    if not scores:
        return None
    return {
        "n": len(scores),
        "mean": round(sum(scores) / len(scores), 3),
        "pass_count": labels.get("PASS", 0),
        "partial_count": labels.get("PARTIAL", 0),
        "fail_count": labels.get("FAIL", 0),
        "score_zero_count": sum(1 for s in scores if s == 0),
        "hallucinations": hallucinations,
        "contradictions": contradictions,
        "language_mismatches": language_mm,
    }


def _print_diff(label: str, current, baseline, fail: bool) -> None:
    marker = "✗" if fail else "·"
    sys.stderr.write(f"  {marker} {label:<32s} current={current!r:<10} baseline={baseline!r}\n")


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--run", required=True, help="Run directory (must contain responses.jsonl)")
    p.add_argument("--baseline", required=True, help="Baseline JSON file (e.g. eval/rag_qa/golden/baseline_v1.json)")
    args = p.parse_args()

    run_dir = Path(args.run)
    baseline_path = Path(args.baseline)
    if not run_dir.is_dir():
        sys.stderr.write(f"run dir not found: {run_dir}\n")
        return 2
    if not baseline_path.is_file():
        sys.stderr.write(f"baseline file not found: {baseline_path}\n")
        return 2

    baseline = json.loads(baseline_path.read_text(encoding="utf-8"))
    thresholds = baseline.get("regression_thresholds", {})
    failures: list[str] = []

    try:
        align = _load_alignment_counts(run_dir)
    except FileNotFoundError as exc:
        sys.stderr.write(f"{exc}\n")
        return 2

    sys.stderr.write(f"\nregression-check vs {baseline.get('name','baseline')} "
                     f"(prompt_version={baseline.get('prompt_version','?')})\n")
    sys.stderr.write(f"  total rows: {align['total']}\n")
    sys.stderr.write("\nalignment\n")

    base_align = baseline.get("alignment", {})
    al_min = thresholds.get("alignment_all_passed_min")
    if al_min is not None:
        fail = align["all_passed"] < al_min
        _print_diff("all_passed (>= min)", align["all_passed"], f">={al_min}", fail)
        if fail:
            failures.append(f"alignment.all_passed {align['all_passed']} < min {al_min}")

    for k in ("exceeds_sentence_limit", "has_markdown", "has_url",
              "has_service_id", "language_mismatch", "ungrounded_when_empty"):
        cur = align[k]
        base = base_align.get(k, 0)
        # Increasing failure count is the regression direction
        fail = cur > base + 1  # 1-row tolerance for noise
        _print_diff(k, cur, base, fail)
        if fail:
            failures.append(f"alignment.{k} {cur} > baseline {base} (+1 tolerance)")

    judge = _load_judge_summary(run_dir)
    if judge:
        sys.stderr.write("\njudge\n")
        bj = baseline.get("judge", {})
        for k, key, comparator in [
            ("mean (>= min)", "mean", "min"),
            ("pass_count (>= min)", "pass_count", "min"),
            ("score_zero_count (<= max)", "score_zero_count", "max"),
            ("hallucinations (<= max)", "hallucinations", "max"),
            ("contradictions (<= max)", "contradictions", "max"),
        ]:
            cur = judge[key]
            threshold_key = {
                "mean": "judge_mean_min",
                "pass_count": "judge_pass_min",
                "score_zero_count": "judge_score_zero_max",
                "hallucinations": "hallucinations_max",
                "contradictions": "contradictions_max",
            }[key]
            threshold = thresholds.get(threshold_key)
            if threshold is None:
                _print_diff(k, cur, bj.get(key), False)
                continue
            fail = (cur < threshold) if comparator == "min" else (cur > threshold)
            _print_diff(k, cur, f"{comparator}={threshold}", fail)
            if fail:
                failures.append(f"judge.{key} {cur} {comparator}={threshold}")
    else:
        sys.stderr.write("\njudge: no evaluations.jsonl found — skipping judge thresholds\n")

    sys.stderr.write("\n")
    if failures:
        sys.stderr.write("REGRESSION DETECTED:\n")
        for f in failures:
            sys.stderr.write(f"  - {f}\n")
        return 1
    sys.stderr.write("OK — no regression vs baseline.\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
