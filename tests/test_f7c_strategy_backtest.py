from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = (
    ROOT
    / "analysis/f7c-strategy-backtest-2026-08-01"
    / "backtest_f7c_strategy.py"
)
SPEC = importlib.util.spec_from_file_location("f7c_backtest", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
strategy = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = strategy
SPEC.loader.exec_module(strategy)


def bar(date: str, open_: float, close: float, volume: float) -> dict:
    return {
        "d": date,
        "o": open_,
        "h": max(open_, close) * 1.01,
        "l": min(open_, close) * 0.99,
        "c": close,
        "v": volume,
        "cex": {"net_7d": 0.0},
    }


def test_volume_exit_requires_previous_volume_peak_and_no_new_close_high():
    position = strategy.Position(
        symbol="TEST",
        group="test",
        signal_date="2026-01-01",
        entry_date="2026-01-02",
        entry_price=100.0,
        units=1.0,
        notional=100.0,
        entry_cost=0.2,
        signal_f7c=0.002,
        signal_rank=1,
    )
    previous = bar("2026-01-02", 100.0, 110.0, 1000.0)
    strategy.update_position_bar(position, previous)
    current = bar("2026-01-03", 110.0, 105.0, 900.0)

    reason = strategy.exit_reason_at_close(
        position,
        current,
        {"value": 0.002, "below_median": False},
    )

    assert reason == "持仓成交量峰值后缩小且收盘未创新高"


def test_signal_is_bought_and_reversal_is_sold_at_next_open():
    dates = ["2026-01-01", "2026-01-02", "2026-01-03"]
    tokens = []
    for index in range(5):
        bars = [bar(date, 10.0, 10.0, 1000.0) for date in dates]
        if index == 0:
            bars[0]["cex"]["net_7d"] = 2.0
            bars[1]["cex"]["net_7d"] = -1.0
            bars[1]["o"] = 11.0
            bars[2]["o"] = 9.0
        tokens.append(
            {
                "symbol": f"T{index}",
                "group": "test",
                "cluster_amount": 1000.0,
                "bars": bars,
                "events": [],
            }
        )
    dataset = {"generated_at": "test", "tokens": tokens}

    result = strategy.run_backtest(dataset)
    closed = [trade for trade in result["trades"] if trade["status"] == "closed"]

    assert len(closed) == 1
    assert closed[0]["signal_date"] == "2026-01-01"
    assert closed[0]["entry_date"] == "2026-01-02"
    assert closed[0]["entry_price"] == 11.0
    assert closed[0]["exit_decision_date"] == "2026-01-02"
    assert closed[0]["exit_date"] == "2026-01-03"
    assert closed[0]["exit_price"] == 9.0
    assert closed[0]["exit_reason"] == "F7b净流出超过Cluster 0.1%"
