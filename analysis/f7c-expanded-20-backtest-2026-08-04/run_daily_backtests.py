#!/usr/bin/env python3
"""Build the frozen-label expanded dataset and run the original F7c daily strategy."""

from __future__ import annotations

import csv
import importlib.util
import json
import sys
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any


HERE = Path(__file__).resolve().parent
ANALYSIS = HERE.parent
EXPANDED = ANALYSIS / "binance-bubblemaps-expanded-universe-2026-08-03"
OLD_STRATEGY_DIR = ANALYSIS / "f7c-strategy-backtest-2026-08-01"
OLD_DASHBOARD_DIR = ANALYSIS / "binance-bubblemaps-factor-kline-2026-07-30"
START = date(2026, 1, 1)
CUTOFF = date(2026, 7, 29)


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


builder = load_module("expanded_builder", OLD_DASHBOARD_DIR / "build_dashboard.py")
daily_model = load_module("f7c_daily_model", OLD_STRATEGY_DIR / "backtest_f7c_strategy.py")


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def build_expanded_dataset() -> dict[str, Any]:
    config = json.loads((EXPANDED / "expanded_universe_config.json").read_text())
    symbols = tuple(config["symbols"])
    states, metadata = builder.load_states(
        builder.load_engine(),
        EXPANDED / "bubblemaps-snapshot",
        EXPANDED / "expanded_universe_config.json",
        symbols,
    )
    if metadata["missing_targets"]:
        raise ValueError(f"missing targets: {metadata['missing_targets']}")

    net_by_symbol_day: dict[str, dict[date, float]] = defaultdict(dict)
    for row in read_csv(EXPANDED / "cex-flow/daily-cex-net-flows.csv"):
        net_by_symbol_day[row["symbol"]][date.fromisoformat(row["date"])] = float(
            row["net_inflow_to_cex"]
        )

    tokens = []
    for symbol in symbols:
        price_rows = read_csv(EXPANDED / f"klines-1d/{symbol}-1d.csv")
        bars = []
        daily_flows = net_by_symbol_day.get(symbol, {})
        for row in price_rows:
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
            raise ValueError(f"no price bars for {symbol}")
        tokens.append(
            {
                "symbol": symbol,
                "group": "第二批20币",
                "cluster_amount": float(states[symbol]["cluster_amount"]),
                "bars": bars,
                "events": [],
            }
        )

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "price_source": "Binance USDⓈ-M Futures 1d",
        "chain_source": "Frozen confirmed CEX labels as of 2026-08-04",
        "display_start": START.isoformat(),
        "cutoff": CUTOFF.isoformat(),
        "tokens": tokens,
    }


def run_variant(dataset: dict[str, Any], output_dir: Path) -> dict[str, Any]:
    output_dir.mkdir(parents=True, exist_ok=True)
    daily_model.TRADES_CSV = output_dir / "trades.csv"
    daily_model.SIGNALS_CSV = output_dir / "signals.csv"
    daily_model.DAILY_CSV = output_dir / "daily-equity.csv"
    daily_model.SENSITIVITY_CSV = output_dir / "sensitivity.csv"
    daily_model.REPORT_MD = output_dir / "report.md"
    result = daily_model.run_backtest(dataset)
    sensitivity = daily_model.run_sensitivity(dataset)
    daily_model.write_csv(daily_model.TRADES_CSV, result["trades"])
    daily_model.write_csv(daily_model.SIGNALS_CSV, result["signals"])
    daily_model.write_csv(daily_model.DAILY_CSV, result["daily"])
    daily_model.write_csv(daily_model.SENSITIVITY_CSV, sensitivity)
    daily_model.write_report(dataset, result, sensitivity)
    summary = daily_model.summarize_result(result)
    summary.update(
        {
            "token_count": len(dataset["tokens"]),
            "start": result["dates"][0],
            "end": result["dates"][-1],
            "entered_trades": len(result["trades"]),
            "open_trades": sum(t["status"] == "open" for t in result["trades"]),
            "skipped_no_slot": result["skipped_no_slot"],
            "skipped_already_held": result["skipped_already_held"],
        }
    )
    (output_dir / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return summary


def main() -> None:
    HERE.mkdir(parents=True, exist_ok=True)
    new_dataset = build_expanded_dataset()
    (HERE / "expanded20-dataset.json").write_text(
        json.dumps(new_dataset, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    new_summary = run_variant(new_dataset, HERE)

    old_dataset = daily_model.dashboard.load_dataset()
    old_tokens = [
        {
            **token,
            "bars": [bar for bar in token["bars"] if bar["d"] <= CUTOFF.isoformat()],
        }
        for token in old_dataset["tokens"]
    ]
    combined = {
        **new_dataset,
        "chain_source": "Original 13-token data plus frozen expanded 20-token labels",
        "tokens": old_tokens + new_dataset["tokens"],
    }
    (HERE / "combined33-dataset.json").write_text(
        json.dumps(combined, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    combined_summary = run_variant(combined, HERE / "combined33")

    output = {"new20": new_summary, "combined33": combined_summary}
    (HERE / "daily-comparison.json").write_text(
        json.dumps(output, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(output, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
