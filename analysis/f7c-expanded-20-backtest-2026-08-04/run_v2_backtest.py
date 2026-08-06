#!/usr/bin/env python3
"""Fetch intraday data and run the frozen F7c V1/V2 exit and portfolio models."""

from __future__ import annotations

import csv
import importlib.util
import json
import statistics
import sys
from datetime import date
from pathlib import Path
from typing import Any


HERE = Path(__file__).resolve().parent
OLD = HERE.parent / "f7c-strategy-backtest-2026-08-01"
sys.path.insert(0, str(OLD))


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    fields: list[str] = []
    for row in rows:
        for key in row:
            if key not in fields:
                fields.append(key)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def prepare_closed_trades() -> Path:
    rows = [row for row in read_csv(HERE / "trades.csv") if row["status"] == "closed"]
    output = HERE / "v2-input-trades.csv"
    write_csv(output, rows)
    print(f"using {len(rows)} closed fixed entries")
    return output


def fetch_intraday(trades_path: Path) -> None:
    fetcher = load_module("expanded_intraday_fetcher", OLD / "fetch_all_trade_intraday.py")
    fetcher.HERE = HERE
    fetcher.TRADES_CSV = trades_path
    fetcher.DATA_DIR = HERE / "intraday-all-data"
    fetcher.RAW_DIR = fetcher.DATA_DIR / "raw"
    fetcher.MANIFEST = fetcher.DATA_DIR / "manifest.json"
    # Include Aug-01 bars because a next-5m-open forced exit can execute at
    # 2026-08-01T00:00 even when the final completed decision bar is Jul-31.
    fetcher.DATA_CUTOFF = date(2026, 8, 2)
    fetcher.main()


def run_exit_model(trades_path: Path, version: str) -> tuple[list[dict], list[dict]]:
    model = load_module(f"expanded_exit_{version}", OLD / "backtest_multitimeframe_exit.py")
    model.HERE = HERE
    model.DATA_DIR = HERE / "intraday-all-data"
    model.MANIFEST = model.DATA_DIR / "manifest.json"
    model.TRADES_CSV = trades_path
    manifest = json.loads(model.MANIFEST.read_text())
    trades = read_csv(trades_path)
    summaries: list[dict] = []
    events: list[dict] = []
    for case, trade in zip(manifest["cases"], trades, strict=True):
        summary, case_events = model.backtest_case(case, trade, version)
        summaries.append(summary)
        events.extend(case_events)
    suffix = "" if version == "v1" else "-v2"
    write_csv(HERE / f"multitimeframe-exit{suffix}-trades.csv", summaries)
    write_csv(HERE / f"multitimeframe-exit{suffix}-events.csv", events)
    completed = [
        row
        for row in summaries
        if str(row.get("right_censored_mark", "")).lower() != "true"
    ]
    returns = [float(row["net_return_2x"]) for row in completed]
    summary = {
        "version": version,
        "trade_count": len(summaries),
        "completed_count": len(completed),
        "win_rate": sum(value > 0 for value in returns) / len(returns) if returns else 0,
        "mean_return_2x": statistics.mean(returns) if returns else 0,
        "median_return_2x": statistics.median(returns) if returns else 0,
        "best_return_2x": max(returns) if returns else 0,
        "worst_return_2x": min(returns) if returns else 0,
    }
    (HERE / f"multitimeframe-exit{suffix}-summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return summaries, events


def run_weighted_portfolio(trades_path: Path) -> dict[str, Any]:
    weighted = load_module("expanded_weighted", OLD / "backtest_portfolio_weighted.py")
    weighted.HERE = HERE
    weighted.DATA_DIR = HERE / "intraday-all-data"
    weighted.MANIFEST = weighted.DATA_DIR / "manifest.json"
    weighted.OLD_TRADES = trades_path
    weighted.V1_EVENTS = HERE / "multitimeframe-exit-events.csv"
    weighted.V2_EVENTS = HERE / "multitimeframe-exit-v2-events.csv"
    weighted.OUTPUT_CURVE = HERE / "weighted-portfolio-v2-equity.csv"
    weighted.OUTPUT_ATTRIBUTION = HERE / "weighted-portfolio-v2-attribution.csv"
    weighted.OUTPUT_REPORT = HERE / "weighted-portfolio-v2-report.md"

    manifest = json.loads(weighted.MANIFEST.read_text())
    all_cases = manifest["cases"]
    all_old_trades = weighted.read_csv(weighted.OLD_TRADES)
    all_model_events = {
        "v1_multitimeframe": weighted.read_csv(weighted.V1_EVENTS),
        "v2_multitimeframe": weighted.read_csv(weighted.V2_EVENTS),
    }
    forced_ids = {
        row["case_id"]
        for rows in all_model_events.values()
        for row in rows
        if row.get("reason") == "数据截止日强制估值"
    }
    eligible_pairs = [
        (case, trade)
        for case, trade in zip(all_cases, all_old_trades, strict=True)
        if case["case_id"] not in forced_ids
    ]
    cases = [case for case, _ in eligible_pairs]
    old_trades = [trade for _, trade in eligible_pairs]
    eligible_ids = {case["case_id"] for case in cases}
    model_events = {
        "v1_multitimeframe": [
            row for row in all_model_events["v1_multitimeframe"]
            if row["case_id"] in eligible_ids
        ],
        "v2_multitimeframe": [
            row for row in all_model_events["v2_multitimeframe"]
            if row["case_id"] in eligible_ids
        ],
    }
    entries, exits = weighted.build_strategy_events(cases, old_trades, model_events)

    def enforce_capacity(entry_rows: list[dict], exit_rows: list[dict]):
        entries_by_time: dict[int, list[dict]] = {}
        exits_by_time: dict[int, list[dict]] = {}
        for row in entry_rows:
            entries_by_time.setdefault(row["time_ms"], []).append(row)
        for row in exit_rows:
            exits_by_time.setdefault(row["time_ms"], []).append(row)
        active: dict[str, float] = {}
        accepted: set[str] = set()
        kept_entries: list[dict] = []
        skipped: list[str] = []
        for time_ms in sorted(set(entries_by_time) | set(exits_by_time)):
            for row in exits_by_time.get(time_ms, []):
                case_id = row["case_id"]
                if case_id not in active:
                    continue
                active[case_id] -= float(row["fraction"])
                if active[case_id] <= 1e-12:
                    del active[case_id]
            for row in entries_by_time.get(time_ms, []):
                if len(active) >= 3:
                    skipped.append(row["case_id"])
                    continue
                kept_entries.append(row)
                accepted.add(row["case_id"])
                active[row["case_id"]] = 1.0
        kept_exits = [row for row in exit_rows if row["case_id"] in accepted]
        return kept_entries, kept_exits, skipped

    capacity_skips = {}
    for name in entries:
        entries[name], exits[name], skipped = enforce_capacity(entries[name], exits[name])
        capacity_skips[name] = skipped
    bar_maps = {
        case["case_id"]: weighted.read_bar_map(
            weighted.DATA_DIR / case["intervals"]["5m"]["file"]
        )
        for case in cases
    }
    results = [
        weighted.simulate(name, entries[name], exits[name], bar_maps)
        for name in ("old_daily", "v1_multitimeframe", "v2_multitimeframe")
    ]
    weighted.write_csv(
        weighted.OUTPUT_CURVE, [row for result in results for row in result["curve"]]
    )
    weighted.write_csv(
        weighted.OUTPUT_ATTRIBUTION,
        [row for result in results for row in result["attribution"]],
    )
    report = weighted.render_report(results).replace(
        "仍固定原有26笔入场", f"固定本批{len(cases)}笔已平仓入场"
    )
    weighted.OUTPUT_REPORT.write_text(report, encoding="utf-8")
    output = {
        result["name"]: {
            key: result[key]
            for key in (
                "final_equity",
                "total_return",
                "max_drawdown_5m",
                "min_equity",
                "max_gross_leverage",
                "average_invested_gross_leverage",
            )
        }
        for result in results
    }
    for name in output:
        output[name]["capacity_skipped_entries"] = len(capacity_skips[name])
    (HERE / "weighted-portfolio-v2-summary.json").write_text(
        json.dumps(output, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return output


def main() -> None:
    trades_path = prepare_closed_trades()
    if not (HERE / "intraday-all-data/manifest.json").is_file():
        fetch_intraday(trades_path)
    else:
        print("reusing existing intraday manifest")
    run_exit_model(trades_path, "v1")
    run_exit_model(trades_path, "v2")
    result = run_weighted_portfolio(trades_path)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
