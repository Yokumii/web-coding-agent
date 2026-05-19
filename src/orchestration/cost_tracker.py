from __future__ import annotations

from src.utils.logger import get_logger

logger = get_logger(__name__)


# 预算占比达到这些阈值时输出告警。每个阈值在单个实例内只提示一次，
# 避免恢复执行后同一区间反复震荡时刷屏。
_BUDGET_WARN_THRESHOLDS = (0.8, 0.9)


class CostTracker:
    """跟踪各阶段累计成本，并提供预算检查。"""

    def __init__(self, max_budget_usd: float) -> None:
        self.max_budget = max_budget_usd
        self.total_cost = 0.0
        self.breakdown: dict[str, float] = {}
        self._warned_thresholds: set[float] = set()

    def add(self, agent_name: str, cost_usd: float) -> None:
        """记录某阶段成本；同名阶段再次写入时覆盖旧值。"""
        self.breakdown[agent_name] = cost_usd
        self.total_cost = sum(self.breakdown.values())
        self._maybe_warn_budget()

    def _maybe_warn_budget(self) -> None:
        """在预算逼近上限时输出一次性告警。"""
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
