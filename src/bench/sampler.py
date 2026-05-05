from __future__ import annotations

import json
import random
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class Sample:
    id: str
    instruction: str
    application_type: str | None
    primary_category: str | None
    raw: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> "Sample":
        category = row.get("Category") or {}
        primary = category.get("primary_category") if isinstance(category, dict) else None
        return cls(
            id=str(row.get("id", "")),
            instruction=str(row.get("instruction", "")),
            application_type=row.get("application_type"),
            primary_category=primary,
            raw=row,
        )


_STRATA_FIELDS = {
    "application_type": lambda s: s.application_type,
    "primary_category": lambda s: s.primary_category,
}


def load_jsonl(path: Path) -> list[Sample]:
    samples: list[Sample] = []
    with Path(path).open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            samples.append(Sample.from_row(json.loads(line)))
    return samples


def write_jsonl(samples: list[Sample], path: Path) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with Path(path).open("w", encoding="utf-8") as f:
        for sample in samples:
            f.write(json.dumps(sample.raw, ensure_ascii=False) + "\n")


def stratified_sample(
    samples: list[Sample],
    strata: str,
    per_stratum: int,
    seed: int,
) -> list[Sample]:
    if strata not in _STRATA_FIELDS:
        raise ValueError(
            f"unsupported strata field: {strata!r}; "
            f"expected one of {sorted(_STRATA_FIELDS)}"
        )
    key_fn = _STRATA_FIELDS[strata]

    buckets: dict[Any, list[Sample]] = defaultdict(list)
    for sample in samples:
        buckets[key_fn(sample)].append(sample)

    rng = random.Random(seed)
    picked: list[Sample] = []
    for key in sorted(buckets.keys(), key=lambda k: ("" if k is None else str(k))):
        bucket = buckets[key]
        n = min(per_stratum, len(bucket))
        picked.extend(rng.sample(bucket, n))
    return picked


def filter_by_ids(samples: list[Sample], ids: list[str]) -> list[Sample]:
    by_id = {s.id: s for s in samples}
    return [by_id[i] for i in ids if i in by_id]


def take_first_n(samples: list[Sample], n: int) -> list[Sample]:
    return list(samples[:n])
