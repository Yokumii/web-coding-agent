from __future__ import annotations

import math
from dataclasses import dataclass


@dataclass
class GradingCriterion:
    name: str
    description: str
    weight: float
    threshold: float


CRITERIA: list[GradingCriterion] = [
    GradingCriterion(
        name="design_quality",
        description=(
            "Does the design feel coherent, intentional, and emotionally "
            "distinct rather than assembled from generic parts?"
        ),
        weight=0.30,
        threshold=6.0,
    ),
    GradingCriterion(
        name="functionality",
        description=(
            "Does the application actually work when you use it? Can users "
            "complete core tasks without errors?"
        ),
        weight=0.25,
        threshold=6.0,
    ),
    GradingCriterion(
        name="originality",
        description=(
            "Does the interface show deliberate creative choices rather than "
            "template layouts, stock defaults, or generic AI patterns?"
        ),
        weight=0.25,
        threshold=5.0,
    ),
    GradingCriterion(
        name="craft",
        description=(
            "Are the fundamentals executed well: typography, spacing, color, "
            "responsiveness, contrast, and interaction polish?"
        ),
        weight=0.20,
        threshold=6.0,
    ),
]


def check_grades(grades: dict) -> bool:
    """Return True iff every criterion has a real numeric score >= its threshold.

    Robust against malformed agent output: missing keys, non-dict ``criteria``
    block, non-dict ``score_data``, ``None`` / string / ``bool`` / ``NaN`` /
    ``inf`` scores all fail closed.
    """
    criteria_block = grades.get("criteria") if isinstance(grades, dict) else None
    if not isinstance(criteria_block, dict):
        return False

    for criterion in CRITERIA:
        score_data = criteria_block.get(criterion.name)
        if not isinstance(score_data, dict):
            return False
        score = score_data.get("score")
        # bool is a subclass of int in Python; reject it explicitly so that
        # a stray ``True``/``False`` in the JSON does not satisfy a threshold.
        if isinstance(score, bool) or not isinstance(score, (int, float)):
            return False
        score_float = float(score)
        if math.isnan(score_float) or math.isinf(score_float):
            return False
        if score_float < criterion.threshold:
            return False
    return True
