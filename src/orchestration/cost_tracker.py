from __future__ import annotations


class CostTracker:
    """Track token costs across agents and rounds."""

    def __init__(self, max_budget_usd: float) -> None:
        self.max_budget = max_budget_usd
        self.total_cost = 0.0
        self.breakdown: dict[str, float] = {}

    def add(self, agent_name: str, cost_usd: float) -> None:
        self.total_cost += cost_usd
        self.breakdown[agent_name] = self.breakdown.get(agent_name, 0.0) + cost_usd

    def is_over_budget(self) -> bool:
        return self.total_cost >= self.max_budget

    def remaining(self) -> float:
        return max(0.0, self.max_budget - self.total_cost)

    def summary(self) -> str:
        lines = [f"Total: ${self.total_cost:.2f} / ${self.max_budget:.2f}"]
        for name, cost in self.breakdown.items():
            lines.append(f"  {name}: ${cost:.2f}")
        return "\n".join(lines)
