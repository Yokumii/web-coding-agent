from __future__ import annotations

from src.utils.logger import get_logger

logger = get_logger(__name__)


# Budget-fraction thresholds at which we want to warn. Each threshold
# fires at most once per CostTracker instance so a re-run that bounces
# around the same level does not spam the log.
_BUDGET_WARN_THRESHOLDS = (0.8, 0.9)


class CostTracker:
    """Track token costs across agents and rounds."""

    def __init__(self, max_budget_usd: float) -> None:
        self.max_budget = max_budget_usd
        self.total_cost = 0.0
        self.breakdown: dict[str, float] = {}
        self._warned_thresholds: set[float] = set()

    def add(self, agent_name: str, cost_usd: float) -> None:
        """Record the cost for a phase, replacing any prior entry.

        Each phase key (``planner`` / ``generator_rN`` / ``evaluator_rN`` /
        ``visual_capture_rN`` / ``visual_score_rN``) is added exactly once
        per harness run for a successful phase, so a repeat ``add`` for
        the same key means we are re-running the phase on resume. Using
        replace-semantics avoids double-counting between the cost
        restored from the previous state file and the fresh re-run cost
        (audit M5).
        """
        self.breakdown[agent_name] = cost_usd
        self.total_cost = sum(self.breakdown.values())
        self._maybe_warn_budget()

    def _maybe_warn_budget(self) -> None:
        """Emit a one-shot warning when total cost crosses 80% / 90%
        of ``max_budget``. ``is_over_budget`` (>=100%) is enforced by
        the harness loop separately (audit L1).
        """
        if self.max_budget <= 0:
            return
        ratio = self.total_cost / self.max_budget
        for threshold in _BUDGET_WARN_THRESHOLDS:
            if ratio >= threshold and threshold not in self._warned_thresholds:
                self._warned_thresholds.add(threshold)
                logger.warning(
                    f"[bold yellow]Budget at {ratio * 100:.0f}%[/] "
                    f"(${self.total_cost:.2f} / ${self.max_budget:.2f})"
                )

    def is_over_budget(self) -> bool:
        return self.total_cost >= self.max_budget

    def remaining(self) -> float:
        return max(0.0, self.max_budget - self.total_cost)

    def summary(self) -> str:
        lines = [f"Total: ${self.total_cost:.2f} / ${self.max_budget:.2f}"]
        for name, cost in self.breakdown.items():
            lines.append(f"  {name}: ${cost:.2f}")
        return "\n".join(lines)
