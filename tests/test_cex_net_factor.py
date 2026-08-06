from __future__ import annotations

import importlib.util
from datetime import date, timedelta
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = (
    ROOT
    / "analysis/binance-bubblemaps-factor-kline-2026-07-30/build_dashboard.py"
)
SPEC = importlib.util.spec_from_file_location("factor_dashboard", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
dashboard = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(dashboard)


def transfer(day: date, source: str, destination: str, amount: float, index: int):
    return {
        "day": day,
        "chain": "bsc",
        "token_address": "token",
        "from_address": source,
        "to_address": destination,
        "amount": amount,
        "timestamp_ms": index,
        "tx_hash": f"tx-{index}",
    }


def state(records, cluster_amount: float = 100_000.0):
    return {
        "records": records,
        "cluster_amount": cluster_amount,
        "cluster_by_member": {},
        "address_metadata": {
            ("bsc", "token", "binance"): {
                "is_cex": True,
                "label": "Binance Deposit",
            },
            ("bsc", "token", "gate"): {
                "is_cex": True,
                "label": "Gate Hot Wallet",
            },
        },
    }


def test_cex_window_summary_separates_directions_and_excludes_cex_to_cex():
    records = [
        transfer(date(2026, 7, 9), "wallet", "binance", 120, 1),
        transfer(date(2026, 7, 10), "binance", "wallet", 40, 2),
        transfer(date(2026, 7, 11), "binance", "gate", 900, 3),
    ]

    got = dashboard.cex_window_summary(
        state(records), date(2026, 7, 8), date(2026, 7, 14)
    )

    assert got == {
        "inflow": 120.0,
        "outflow": 40.0,
        "net": 80.0,
        "transfer_count": 3,
        "direct_transfer_count": 3,
        "multihop_transfer_count": 0,
        "labels": ["Binance Deposit", "Gate Hot Wallet"],
    }


def test_cex_window_summary_adds_unique_multihop_boundary_once():
    direct = transfer(date(2026, 7, 10), "wallet", "binance", 120, 1)
    current = state([direct])
    current["cex_path_events"] = [
        {
            "day": date(2026, 7, 10),
            "direction": "流入CEX",
            "amount": 120.0,
            "boundary_event_id": "bsc:tx-1:0",
            "boundary_tx_hash": "tx-1",
            "cex_label": "Binance Deposit",
            "hops": 2,
        },
        {
            "day": date(2026, 7, 11),
            "direction": "流入CEX",
            "amount": 80.0,
            "boundary_event_id": "bsc:tx-2:0",
            "boundary_tx_hash": "tx-2",
            "cex_label": "Bitget Deposit",
            "hops": 3,
        },
        {
            "day": date(2026, 7, 11),
            "direction": "流入CEX",
            "amount": 80.0,
            "boundary_event_id": "duplicate-path-to-tx-2",
            "boundary_tx_hash": "tx-2",
            "cex_label": "Bitget Deposit",
            "hops": 3,
        },
    ]

    got = dashboard.cex_window_summary(
        current, date(2026, 7, 8), date(2026, 7, 14)
    )

    assert got["inflow"] == 200.0
    assert got["transfer_count"] == 2
    assert got["direct_transfer_count"] == 1
    assert got["multihop_transfer_count"] == 1
    assert got["labels"] == ["Binance Deposit", "Bitget Deposit"]


def test_cex_net_factor_uses_signed_net_and_four_prior_week_median():
    anchor = date(2026, 7, 16)
    records = []
    for index, amount in enumerate((10, 20, 30, 40), 1):
        records.append(
            transfer(
                anchor - timedelta(days=8 + (index - 1) * 7),
                "wallet",
                "binance",
                amount,
                index,
            )
        )
    records.append(transfer(anchor - timedelta(days=1), "wallet", "binance", 100, 9))

    got = dashboard.cex_net_factor(state(records), anchor)

    assert got["net_7d"] == 100.0
    assert got["direction"] == "CEX净流入"
    assert got["baseline_abs_net"] == 25.0
    assert got["burst"] == 4.0
    assert got["share_pct"] == 0.1
    assert got["trigger"] is True


def test_cex_net_factor_marks_net_outflow_and_zero_launch():
    anchor = date(2026, 7, 16)
    records = [
        transfer(anchor - timedelta(days=1), "binance", "wallet", 150, 1)
    ]

    got = dashboard.cex_net_factor(state(records), anchor)

    assert got["net_7d"] == -150.0
    assert got["direction"] == "CEX净流出"
    assert got["burst"] is None
    assert got["zero_launch"] is True
    assert got["share_pct"] == -0.15
    assert got["trigger"] is True


def test_cex_net_factor_ignores_anchor_day_and_requires_absolute_share():
    anchor = date(2026, 7, 16)
    records = [
        transfer(anchor, "wallet", "binance", 50_000, 1),
        transfer(anchor - timedelta(days=1), "wallet", "binance", 99, 2),
    ]

    got = dashboard.cex_net_factor(state(records), anchor)

    assert got["inflow_7d"] == 99.0
    assert got["share_pct"] == 0.099
    assert got["trigger"] is False
