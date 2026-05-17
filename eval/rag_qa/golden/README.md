# Golden-set regression check (R-006)

A small frozen sample + a baseline of metrics. Used to catch prompt or
retrieval regressions before they ship.

## Files

| File | Purpose |
|---|---|
| `golden_v1.csv` | 100 stratified rows from the full eval set (10 per Category × Language cell). Frozen — do not edit. Same rows used to build `baseline_v1.json`. |
| `baseline_v1.json` | Recorded alignment + judge metrics from `pilot100_v4` (prompt `v4-2026-05-12`, hash `sha256:0da47283a405de98`). Contains `regression_thresholds` set with ~10% headroom below observed values. |

## Run the regression check

1. Run the golden set through the live pipeline:

```bash
docker compose run --rm --no-deps \
  -v ./eval:/app/eval \
  -v "$PWD/eval/rag_qa/golden/golden_v1.csv:/app/eval/data.csv:ro" \
  --entrypoint python agent \
  -m eval.rag_qa.run \
  --csv /app/eval/data.csv \
  --out /app/eval/results/rag_qa/golden_check \
  --total 100 --seed 7 --concurrency 2 --retries 3
```

This writes `responses.jsonl`, `summary.md`, `align_report.md`, `sample.csv`.

2. (Optional) Run the LLM judge to populate `evaluations.jsonl` if you want
   the judge thresholds checked too. Otherwise alignment-only is fine for
   a fast CI gate.

3. Diff against baseline:

```bash
python -m eval.rag_qa.regression \
  --run eval/results/rag_qa/golden_check \
  --baseline eval/rag_qa/golden/baseline_v1.json
```

Exits `0` if no regression. Exits `1` if any threshold tripped, with a
human-readable diff on stderr.

## Updating the baseline

Bump the prompt version (e.g. `v4-2026-05-12` → `v5-...`), re-run the
golden set, re-run the judge, then regenerate `baseline_vN.json` from
the new run. Commit baseline + prompt change together; the baseline is
forever pinned to a specific `prompt_hash`.

## Where the thresholds came from

`pilot100_v4` raw values, then a soft floor:

| Metric | Observed (v4) | Threshold (v1) | Headroom |
|---|---:|---:|---|
| alignment all_passed | 100 | ≥ 98 | 2-row noise |
| judge mean | 3.86 | ≥ 3.70 | −0.16 |
| judge PASS count | 72 | ≥ 65 | −7 |
| judge score-0 count | 1 | ≤ 2 | +1 |
| hallucinations | 5 | ≤ 12 | +7 |
| contradictions | 10 | ≤ 15 | +5 |

The headroom is deliberately loose for v1 because the judge has run-to-run
variance. Tighten as the agent stabilizes — `regression_thresholds` lives
in `baseline_v1.json` so changes are reviewable.
