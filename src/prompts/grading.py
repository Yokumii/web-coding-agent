from __future__ import annotations

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
    """Return True if all criteria pass their thresholds."""
    for criterion in CRITERIA:
        score_data = grades.get("criteria", {}).get(criterion.name)
        if not score_data:
            return False
        if score_data.get("score", 0) < criterion.threshold:
            return False
    return True
