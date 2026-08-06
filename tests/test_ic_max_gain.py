from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = (
    ROOT
    / "analysis/binance-bubblemaps-factor-kline-2026-07-30"
    / "calculate_f5_subfactor_ic.py"
)
SPEC = importlib.util.spec_from_file_location("f5_ic", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
ic = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(ic)


def test_future_max_gain_uses_future_high_and_d_close():
    bars = [
        {"c": 100.0, "h": 110.0},
        {"c": 105.0, "h": 108.0},
        {"c": 101.0, "h": 125.0},
        {"c": 130.0, "h": 140.0},
    ]

    assert ic.future_max_gain(bars, 0, 1) == pytest.approx(0.08)
    assert ic.future_max_gain(bars, 0, 2) == pytest.approx(0.25)


def test_future_max_gain_excludes_anchor_high_and_requires_full_window():
    bars = [
        {"c": 100.0, "h": 999.0},
        {"c": 90.0, "h": 95.0},
        {"c": 92.0, "h": 96.0},
    ]

    assert ic.future_max_gain(bars, 0, 2) == pytest.approx(-0.04)
    assert ic.future_max_gain(bars, 1, 2) is None


def test_future_mean_close_deviation_uses_all_future_closes_against_anchor():
    bars = [
        {"c": 100.0, "h": 110.0},
        {"c": 90.0, "h": 130.0},
        {"c": 110.0, "h": 115.0},
        {"c": 120.0, "h": 125.0},
    ]

    assert ic.future_mean_close_deviation(bars, 0, 1) == pytest.approx(-0.10)
    assert ic.future_mean_close_deviation(bars, 0, 2) == pytest.approx(0.0)


def test_future_mean_close_deviation_requires_full_window():
    bars = [
        {"c": 100.0, "h": 110.0},
        {"c": 90.0, "h": 95.0},
    ]

    assert ic.future_mean_close_deviation(bars, 0, 2) is None


def test_future_volume_price_confirmation_requires_same_day_confirmation():
    bars = [
        {"c": 100.0, "h": 100.0, "v": 1000.0},
        {"c": 120.0, "h": 120.0, "v": 900.0},
        {"c": 90.0, "h": 90.0, "v": 2000.0},
        {"c": 110.0, "h": 110.0, "v": 1500.0},
    ]

    assert ic.future_volume_price_confirmation(bars, 0, 2) == 0.0
    assert ic.future_volume_price_confirmation(bars, 0, 3) == pytest.approx(
        0.05
    )


def test_future_volume_price_confirmation_requires_full_window():
    bars = [
        {"c": 100.0, "h": 100.0, "v": 1000.0},
        {"c": 110.0, "h": 110.0, "v": 1500.0},
    ]

    assert ic.future_volume_price_confirmation(bars, 0, 2) is None
