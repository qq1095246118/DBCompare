#!/usr/bin/env python3
"""Run the frozen F7c daily strategy over the 169 PG-ready expansion symbols."""

from __future__ import annotations

import csv
import importlib.util
import json
import math
import statistics
import subprocess
import sys
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any


HERE = Path(__file__).resolve().parent
ANALYSIS = HERE.parent
PROJECT = ANALYSIS.parent
EXPANDED = ANALYSIS / "binance-bubblemaps-expanded-universe-2026-08-03"
REGISTRY = (
    PROJECT
    / "screening/binance-futures-small-meme-volume-spikes-2026-08-05"
    / "all-233-expansion-registry.json"
)
OLD_STRATEGY = ANALYSIS / "f7c-strategy-backtest-2026-08-01/backtest_f7c_strategy.py"
COMPUTE_FLOWS = EXPANDED / "compute_cex_net_flows.py"
READY_CONFIG = HERE / "ready169-config.json"
KLINE_DIR = HERE / "klines-1d"
FLOW_DIR = HERE / "cex-flow"
DATASET_JSON = HERE / "dataset.json"
PER_SYMBOL_CSV = HERE / "per-symbol-summary.csv"
SUMMARY_JSON = HERE / "summary.json"
START = date(2026, 1, 1)
CUTOFF = date(2026, 8, 3)


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


daily_model = load_module("f7c_daily_169_model", OLD_STRATEGY)


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def prepare_config() -> list[str]:
    registry = json.loads(REGISTRY.read_text(encoding="utf-8"))
    symbols = {
        symbol: spec
        for symbol, spec in registry["symbols"].items()
        if spec["source_readiness"]["postgresql"]["state"] == "pg_ready"
    }
    if len(symbols) != 169:
        raise ValueError(f"expected 169 pg_ready symbols, found {len(symbols)}")
    config = {
        "schema_version": "ready169-backtest-config-v1",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source_registry": str(REGISTRY),
        "symbols": symbols,
    }
    READY_CONFIG.write_text(
        json.dumps(config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return list(symbols)


def rebuild_flows() -> None:
    subprocess.run(
        [
            sys.executable,
            str(COMPUTE_FLOWS),
            "--config",
            str(READY_CONFIG),
            "--labels",
            str(EXPANDED / "arkham-review/arkham-label-queue.csv"),
            "--output-dir",
            str(FLOW_DIR),
        ],
        check=True,
    )


def cluster_amounts(symbols: list[str]) -> dict[str, float]:
    values: dict[str, set[float]] = defaultdict(set)
    for row in read_csv(EXPANDED / "arkham-review/high-impact-path-seeds.csv"):
        if row["symbol"] in symbols:
            values[row["symbol"]].add(float(row["cluster_amount"]))
    missing = sorted(set(symbols) - set(values))
    inconsistent = sorted(symbol for symbol, amounts in values.items() if len(amounts) != 1)
    if missing or inconsistent:
        raise ValueError(
            f"cluster amount audit failed: missing={missing}, inconsistent={inconsistent}"
        )
    return {symbol: next(iter(values[symbol])) for symbol in symbols}


def build_dataset(symbols: list[str]) -> dict[str, Any]:
    manifest = json.loads((KLINE_DIR / "manifest.json").read_text(encoding="utf-8"))
    manifest_symbols = set(manifest["symbols"])
    if manifest_symbols != set(symbols):
        raise ValueError(
            f"kline symbol mismatch: missing={sorted(set(symbols)-manifest_symbols)}, "
            f"extra={sorted(manifest_symbols-set(symbols))}"
        )
    if not all(item.get("complete_through_cutoff") for item in manifest["symbols"].values()):
        raise ValueError("one or more kline symbols are incomplete through cutoff")

    cluster_by_symbol = cluster_amounts(symbols)
    net_by_symbol_day: dict[str, dict[date, float]] = defaultdict(dict)
    for row in read_csv(FLOW_DIR / "daily-cex-net-flows.csv"):
        symbol = row["symbol"]
        if symbol in cluster_by_symbol:
            net_by_symbol_day[symbol][date.fromisoformat(row["date"])] = float(
                row["net_inflow_to_cex"]
            )

    tokens = []
    for symbol in symbols:
        bars = []
        daily_flows = net_by_symbol_day.get(symbol, {})
        for row in read_csv(KLINE_DIR / f"{symbol}-1d.csv"):
            day = date.fromisoformat(row["date"])
            if not START <= day <= CUTOFF:
                continue
            net_7d = sum(
                daily_flows.get(day - timedelta(days=offset), 0.0)
                for offset in range(1, 8)
            )
            bars.append(
                {
                    "d": day.isoformat(),
                    "o": float(row["open"]),
                    "h": float(row["high"]),
                    "l": float(row["low"]),
                    "c": float(row["close"]),
                    "v": float(row["volume"]),
                    "cex": {"net_7d": net_7d},
                }
            )
        if not bars:
            raise ValueError(f"no backtest bars for {symbol}")
        tokens.append(
            {
                "symbol": symbol,
                "group": "新增169币",
                "cluster_amount": cluster_by_symbol[symbol],
                "bars": bars,
                "events": [],
            }
        )
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "price_source": "Binance Vision SSH canonical/legacy/staging-validated",
        "chain_source": "Final PG + Arkham API CEX labels as of 2026-08-05",
        "display_start": START.isoformat(),
        "cutoff": CUTOFF.isoformat(),
        "tokens": tokens,
    }


def product_return(values: list[float]) -> float:
    result = 1.0
    for value in values:
        result *= 1 + value
    return result - 1


def write_per_symbol(dataset: dict[str, Any], result: dict[str, Any]) -> None:
    signals: dict[str, list[dict[str, Any]]] = defaultdict(list)
    trades: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in result["signals"]:
        signals[row["symbol"]].append(row)
    for row in result["trades"]:
        trades[row["symbol"]].append(row)

    flow_counts: dict[str, Counter] = defaultdict(Counter)
    for row in read_csv(FLOW_DIR / "daily-cex-net-flows.csv"):
        counter = flow_counts[row["symbol"]]
        counter["event_days"] += 1
        counter["events"] += int(row["event_count"])

    rows = []
    for token in dataset["tokens"]:
        symbol = token["symbol"]
        symbol_signals = signals.get(symbol, [])
        symbol_trades = trades.get(symbol, [])
        closed = [row for row in symbol_trades if row["status"] == "closed"]
        returns = [float(row["net_return"]) for row in closed]
        holding = [float(row["holding_days"]) for row in closed]
        first_day = date.fromisoformat(token["bars"][0]["d"])
        last_day = date.fromisoformat(token["bars"][-1]["d"])
        active_days = max(1, (last_day - first_day).days + 1)
        rows.append(
            {
                "symbol": symbol,
                "first_date": first_day.isoformat(),
                "last_date": last_day.isoformat(),
                "bars": len(token["bars"]),
                "cex_event_days": flow_counts[symbol]["event_days"],
                "cex_event_count": flow_counts[symbol]["events"],
                "signal_episodes": len(symbol_signals),
                "entered_trades": len(symbol_trades),
                "closed_trades": len(closed),
                "open_trades": len(symbol_trades) - len(closed),
                "win_rate": sum(value > 0 for value in returns) / len(returns) if returns else "",
                "mean_trade_return": statistics.mean(returns) if returns else "",
                "median_trade_return": statistics.median(returns) if returns else "",
                "compounded_trade_return": product_return(returns) if returns else "",
                "portfolio_net_pnl_contribution": sum(float(row["net_pnl"]) for row in closed),
                "average_holding_days": statistics.mean(holding) if holding else "",
                "entries_per_30_active_days": len(symbol_trades) / active_days * 30,
            }
        )
    write_csv(PER_SYMBOL_CSV, rows)


def main() -> None:
    symbols = prepare_config()
    rebuild_flows()
    dataset = build_dataset(symbols)
    DATASET_JSON.write_text(
        json.dumps(dataset, ensure_ascii=False) + "\n", encoding="utf-8"
    )

    daily_model.TRADES_CSV = HERE / "trades.csv"
    daily_model.SIGNALS_CSV = HERE / "signals.csv"
    daily_model.DAILY_CSV = HERE / "daily-equity.csv"
    daily_model.SENSITIVITY_CSV = HERE / "sensitivity.csv"
    daily_model.REPORT_MD = HERE / "report.md"
    result = daily_model.run_backtest(dataset)
    sensitivity = daily_model.run_sensitivity(dataset)
    daily_model.write_csv(daily_model.TRADES_CSV, result["trades"])
    daily_model.write_csv(daily_model.SIGNALS_CSV, result["signals"])
    daily_model.write_csv(daily_model.DAILY_CSV, result["daily"])
    daily_model.write_csv(daily_model.SENSITIVITY_CSV, sensitivity)
    daily_model.write_report(dataset, result, sensitivity)
    write_per_symbol(dataset, result)

    summary = daily_model.summarize_result(result)
    closed = [row for row in result["trades"] if row["status"] == "closed"]
    summary.update(
        {
            "token_count": len(dataset["tokens"]),
            "start": result["dates"][0],
            "end": result["dates"][-1],
            "entered_trades": len(result["trades"]),
            "open_trades": sum(row["status"] == "open" for row in result["trades"]),
            "average_holding_days": statistics.mean(
                float(row["holding_days"]) for row in closed
            ) if closed else 0.0,
            "entries_per_30_calendar_days": len(result["trades"])
            / max(1, (CUTOFF - START).days + 1)
            * 30,
            "skipped_no_slot": result["skipped_no_slot"],
            "skipped_already_held": result["skipped_already_held"],
            "pending_entries_at_end": result["pending_entries_at_end"],
        }
    )
    SUMMARY_JSON.write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
