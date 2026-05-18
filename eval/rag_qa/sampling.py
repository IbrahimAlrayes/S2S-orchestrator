from __future__ import annotations

import csv
import random
from collections import defaultdict
from pathlib import Path
from typing import Iterator


def load_csv(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))


def stratified_sample(
    rows: list[dict[str, str]],
    *,
    total: int,
    seed: int = 7,
    strata_cols: tuple[str, ...] = ("Category", "Language"),
) -> list[dict[str, str]]:
    """Pick `total` rows balanced across the cartesian product of strata_cols.

    Distributes quota as evenly as possible across cells. When some cells are
    smaller than the per-cell quota, the shortfall is redistributed to cells
    that still have headroom — so a target like 5000 is honored even when
    cell sizes are unbalanced.
    """
    rng = random.Random(seed)

    buckets: dict[tuple[str, ...], list[dict[str, str]]] = defaultdict(list)
    for r in rows:
        key = tuple(r[c] for c in strata_cols)
        buckets[key].append(r)

    if not buckets:
        return []

    total = min(total, len(rows))
    quota: dict[tuple[str, ...], int] = {k: 0 for k in buckets}
    active = set(buckets)
    remaining = total

    while remaining > 0 and active:
        per_cell = max(1, remaining // len(active))
        added = 0
        for k in list(active):
            headroom = len(buckets[k]) - quota[k]
            give = min(per_cell, headroom, remaining)
            if give <= 0:
                active.discard(k)
                continue
            quota[k] += give
            remaining -= give
            added += give
            if quota[k] >= len(buckets[k]):
                active.discard(k)
        if added == 0:
            break

    out: list[dict[str, str]] = []
    for k, bucket in buckets.items():
        out.extend(rng.sample(bucket, quota[k]))

    rng.shuffle(out)
    return out


def write_sample_csv(rows: list[dict[str, str]], path: Path) -> None:
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    with path.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)
