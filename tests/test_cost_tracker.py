"""Tests for the cost tracker.

On resume the harness re-runs phases that were interrupted, but a prior
cost_tracker implementation accumulated values per agent name. That
meant the cost restored from state plus the cost of the re-run got
summed, leading to double-counted phase costs and a budget gate that
gradually loosened.
"""

from __future__ import annotations

import pytest

from src.orchestration.cost_tracker import CostTracker


def test_cost_tracker_starts_empty():
    tracker = CostTracker(100.0)
    assert tracker.total_cost == 0.0
    assert tracker.breakdown == {}
    assert tracker.is_over_budget() is False


def test_cost_tracker_records_each_phase_cost():
    tracker = CostTracker(100.0)
    tracker.add("planner", 0.1)
    tracker.add("generator_r1", 0.5)

    assert tracker.breakdown == {"planner": 0.1, "generator_r1": 0.5}
    assert tracker.total_cost == pytest.approx(0.6)


def test_cost_tracker_replaces_phase_cost_on_repeat_add():
    """A phase that was restored from state and then re-run on resume
    must end up with the latest cost, not the sum of the two runs."""
    tracker = CostTracker(100.0)
    tracker.add("planner", 0.1)
    tracker.add("generator_r1", 0.5)  # restored from a previous state file

    # Resume re-runs generator_r1 from scratch; cost is the new run, not 0.5+0.7
    tracker.add("generator_r1", 0.7)

    assert tracker.breakdown == {"planner": 0.1, "generator_r1": 0.7}
    assert tracker.total_cost == pytest.approx(0.8)


def test_cost_tracker_budget_gate_uses_replaced_total():
    tracker = CostTracker(1.0)
    tracker.add("generator_r1", 0.6)
    assert tracker.is_over_budget() is False
    # Replace with a smaller value: total must come down, not stay summed.
    tracker.add("generator_r1", 0.2)
    assert tracker.total_cost == pytest.approx(0.2)
    assert tracker.is_over_budget() is False
    assert tracker.remaining() == pytest.approx(0.8)


# --- warn as we approach the budget cap ---


def test_cost_tracker_warns_at_eighty_percent(caplog):
    tracker = CostTracker(1.0)
    tracker.add("planner", 0.4)
    caplog.clear()
    tracker.add("generator_r1", 0.45)  # total = 0.85 → 85%
    assert any("budget" in record.message.lower() for record in caplog.records), (
        f"expected a budget warning around 80%, got: {[r.message for r in caplog.records]}"
    )


def test_cost_tracker_warning_only_fires_on_threshold_crossings(caplog):
    tracker = CostTracker(1.0)
    tracker.add("planner", 0.4)  # 40%, no warn
    assert not any("budget" in record.message.lower() for record in caplog.records)
    caplog.clear()
    tracker.add("generator_r1", 0.5)  # 90%, warn
    assert any("budget" in record.message.lower() for record in caplog.records)
