#!/usr/bin/env python3
"""Attribute V2 PnL by symbol and test causal entry/coin filters."""

from __future__ import annotations

import csv
import importlib.util
import json
import math
import statistics
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd


HERE = Path(__file__).resolve().parent
OLD = HERE.parent / "f7c-strategy-backtest-2026-08-01"
FEATURES = [
    "log_f7c_share",
    "rank_pct",
    "log_cluster_amount",
    "signal_open_close_return",
    "return_1d",
    "return_3d",
    "return_7d",
    "range_pct",
    "close_location",
    "volume_ratio_20d",
    "distance_20d_high",
    "volatility_7d",
    "flow_change_cluster_share",
    "positive_flow_days_7d",
    "negative_flow_days_7d",
    "flow_event_count_7d",
    "listing_age_days",
]


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


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def safe_std(values: list[float]) -> float:
    return statistics.pstdev(values) if len(values) >= 2 else 0.0


def build_features() -> pd.DataFrame:
    dataset = json.loads((HERE / "dataset.json").read_text(encoding="utf-8"))
    token_by_symbol = {token["symbol"]: token for token in dataset["tokens"]}
    daily_trade = {
        (row["symbol"], row["signal_date"]): row
        for row in read_csv(HERE / "v2-input-trades.csv")
    }
    signal = {
        (row["symbol"], row["signal_date"]): row
        for row in read_csv(HERE / "signals.csv")
    }
    daily_flow: dict[tuple[str, str], dict[str, float]] = {}
    for row in read_csv(HERE / "cex-flow" / "daily-cex-net-flows.csv"):
        daily_flow[(row["symbol"], row["date"])] = {
            "net": float(row["net_inflow_to_cex"]),
            "events": float(row["event_count"]),
        }
    summaries = [
        row
        for row in read_csv(HERE / "multitimeframe-exit-v2-trades.csv")
        if row["right_censored_mark"].lower() != "true"
    ]
    rows: list[dict[str, Any]] = []
    for summary in summaries:
        symbol = summary["symbol"]
        signal_date = summary["signal_date"]
        trade = daily_trade[(symbol, signal_date)]
        signal_row = signal[(symbol, signal_date)]
        token = token_by_symbol[symbol]
        bars = token["bars"]
        index = next(i for i, bar in enumerate(bars) if bar["d"] == signal_date)
        bar = bars[index]
        previous = bars[max(0, index - 20):index]
        closes = [float(item["c"]) for item in bars]
        highs = [float(item["h"]) for item in bars]
        daily_returns = [
            closes[i] / closes[i - 1] - 1 for i in range(max(1, index - 6), index + 1)
        ]
        volumes = [float(item["v"]) for item in previous]
        volume_median = statistics.median(volumes) if volumes else float(bar["v"])
        high20 = max(highs[max(0, index - 19):index + 1])
        signal_day = datetime.fromisoformat(signal_date).date()
        prior_flows = []
        for offset in range(1, 8):
            day = (signal_day - timedelta(days=offset)).isoformat()
            prior_flows.append(daily_flow.get((symbol, day), {"net": 0.0, "events": 0.0}))
        previous_net7 = float(bars[index - 1]["cex"]["net_7d"]) if index else 0.0
        net7 = float(bar["cex"]["net_7d"])
        cluster = float(token["cluster_amount"])
        close = float(bar["c"])
        low = float(bar["l"])
        high = float(bar["h"])
        output = {
            "case_id": summary["case_id"],
            "symbol": symbol,
            "signal_date": signal_date,
            "entry_date": summary["entry_date"],
            "exit_time_utc": summary["exit_time_utc"],
            "target_return_2x": float(summary["net_return_2x"]),
            "profitable": int(float(summary["net_return_2x"]) > 0),
            "log_f7c_share": math.log(max(float(trade["signal_f7c_share"]), 1e-12)),
            "rank_pct": float(signal_row["rank"]) / float(signal_row["universe_count"]),
            "log_cluster_amount": math.log(max(cluster, 1e-12)),
            "signal_open_close_return": close / float(bar["o"]) - 1,
            "return_1d": close / closes[index - 1] - 1 if index >= 1 else 0.0,
            "return_3d": close / closes[index - 3] - 1 if index >= 3 else 0.0,
            "return_7d": close / closes[index - 7] - 1 if index >= 7 else 0.0,
            "range_pct": high / low - 1 if low else 0.0,
            "close_location": (close - low) / (high - low) if high > low else 0.5,
            "volume_ratio_20d": float(bar["v"]) / volume_median if volume_median else 1.0,
            "distance_20d_high": close / high20 - 1 if high20 else 0.0,
            "volatility_7d": safe_std(daily_returns),
            "flow_change_cluster_share": (net7 - previous_net7) / cluster,
            "positive_flow_days_7d": sum(item["net"] > 0 for item in prior_flows),
            "negative_flow_days_7d": sum(item["net"] < 0 for item in prior_flows),
            "flow_event_count_7d": sum(item["events"] for item in prior_flows),
            "listing_age_days": index,
        }
        rows.append(output)
    return pd.DataFrame(rows).sort_values(["entry_date", "case_id"]).reset_index(drop=True)


def fit_logistic(train: pd.DataFrame) -> dict[str, Any]:
    x = train[FEATURES].to_numpy(dtype=float)
    y = train["profitable"].to_numpy(dtype=float)
    med = np.nanmedian(x, axis=0)
    x = np.where(np.isfinite(x), x, med)
    mean = x.mean(axis=0)
    std = x.std(axis=0)
    std[std < 1e-9] = 1.0
    z = (x - mean) / std
    design = np.column_stack([np.ones(len(z)), z])
    weights = np.zeros(design.shape[1])
    rate = 0.08
    penalty = 0.8
    for _ in range(2500):
        raw = np.clip(design @ weights, -30, 30)
        probability = 1 / (1 + np.exp(-raw))
        gradient = design.T @ (probability - y) / len(y)
        gradient[1:] += penalty * weights[1:] / len(y)
        weights -= rate * gradient
    train_probability = 1 / (1 + np.exp(-np.clip(design @ weights, -30, 30)))
    return {"median": med, "mean": mean, "std": std, "weights": weights, "train_probability": train_probability}


def predict(model: dict[str, Any], frame: pd.DataFrame) -> np.ndarray:
    x = frame[FEATURES].to_numpy(dtype=float)
    x = np.where(np.isfinite(x), x, model["median"])
    z = (x - model["mean"]) / model["std"]
    design = np.column_stack([np.ones(len(z)), z])
    return 1 / (1 + np.exp(-np.clip(design @ model["weights"], -30, 30)))


def auc(y: np.ndarray, probability: np.ndarray) -> float:
    positives = probability[y == 1]
    negatives = probability[y == 0]
    if not len(positives) or not len(negatives):
        return float("nan")
    score = sum((p > n) + 0.5 * (p == n) for p in positives for n in negatives)
    return score / (len(positives) * len(negatives))


def walk_forward(frame: pd.DataFrame) -> tuple[pd.DataFrame, list[dict[str, Any]]]:
    work = frame.copy()
    work["entry_month"] = work["entry_date"].str[:7]
    predictions: list[pd.DataFrame] = []
    coefficients: list[dict[str, Any]] = []
    fractions = (0.25, 0.33, 0.50, 0.67)
    for month in sorted(work["entry_month"].unique()):
        month_start = datetime.fromisoformat(month + "-01").replace(tzinfo=timezone.utc)
        train = work[pd.to_datetime(work["exit_time_utc"], utc=True) < month_start]
        test = work[work["entry_month"] == month].copy()
        if len(train) < 40 or train["profitable"].nunique() < 2:
            continue
        model = fit_logistic(train)
        test["probability"] = predict(model, test)
        for fraction in fractions:
            threshold = float(np.quantile(model["train_probability"], 1 - fraction))
            test[f"select_top_{int(round(fraction * 100))}"] = test["probability"] >= threshold
        test["train_count"] = len(train)
        test["fold_month"] = month
        predictions.append(test)
        for feature, coefficient in zip(FEATURES, model["weights"][1:], strict=True):
            coefficients.append({"month": month, "feature": feature, "standardized_coefficient": coefficient})
    return pd.concat(predictions, ignore_index=True), coefficients


def enforce_capacity(entries: list[dict[str, Any]], exits: list[dict[str, Any]]):
    entries_by_time: dict[int, list[dict[str, Any]]] = defaultdict(list)
    exits_by_time: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for row in entries:
        entries_by_time[row["time_ms"]].append(row)
    for row in exits:
        exits_by_time[row["time_ms"]].append(row)
    active: dict[str, float] = {}
    accepted: set[str] = set()
    kept_entries: list[dict[str, Any]] = []
    skipped: list[str] = []
    for time_ms in sorted(set(entries_by_time) | set(exits_by_time)):
        for row in exits_by_time.get(time_ms, []):
            case_id = row["case_id"]
            if case_id not in active:
                continue
            active[case_id] -= float(row["fraction"])
            if active[case_id] <= 1e-12:
                del active[case_id]
        for row in sorted(entries_by_time.get(time_ms, []), key=lambda item: item["case_id"]):
            if len(active) >= 3:
                skipped.append(row["case_id"])
                continue
            kept_entries.append(row)
            accepted.add(row["case_id"])
            active[row["case_id"]] = 1.0
    return kept_entries, [row for row in exits if row["case_id"] in accepted], skipped


def portfolio_scenarios(frame: pd.DataFrame, oos: pd.DataFrame) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    weighted = load_module("v2_filter_weighted", OLD / "backtest_portfolio_weighted.py")
    manifest = json.loads((HERE / "intraday-all-data" / "manifest.json").read_text())
    case_by_id = {case["case_id"]: case for case in manifest["cases"]}
    events = read_csv(HERE / "multitimeframe-exit-v2-events.csv")
    events_by_case: dict[str, list[dict[str, str]]] = defaultdict(list)
    for event in events:
        events_by_case[event["case_id"]].append(event)
    valid_ids = set(frame["case_id"])
    all_entries: dict[str, dict[str, Any]] = {}
    all_exits: list[dict[str, Any]] = []
    for case_id in valid_ids:
        case = case_by_id[case_id]
        all_entries[case_id] = {
            "time_ms": weighted.timestamp(case["entry_date"]),
            "case_id": case_id,
            "symbol": case["symbol"],
            "signal_date": case["signal_date"],
            "entry_price": float(case["entry_price"]),
        }
        for event in events_by_case[case_id]:
            all_exits.append(
                {
                    "time_ms": int(event["execution_open_time_ms"]),
                    "case_id": case_id,
                    "fraction": float(event["sold_fraction"]),
                    "price": float(event["execution_price"]),
                    "reason": event["reason"],
                }
            )

    oos_ids = set(oos["case_id"])
    scenarios: dict[str, set[str]] = {"oos_no_filter": oos_ids}
    for fraction in (25, 33, 50, 67):
        scenarios[f"oos_logistic_top_{fraction}"] = set(oos.loc[oos[f"select_top_{fraction}"], "case_id"])
    selected_67 = oos[oos["select_top_67"]]
    if len(selected_67):
        best_case = selected_67.sort_values("target_return_2x", ascending=False).iloc[0]["case_id"]
        scenarios["oos_logistic_top_67_without_best_trade"] = set(selected_67["case_id"]) - {best_case}
    for month, group in oos.groupby("fold_month"):
        scenarios[f"month_{month}_no_filter"] = set(group["case_id"])
        scenarios[f"month_{month}_logistic_top_67"] = set(group.loc[group["select_top_67"], "case_id"])

    # Causal symbol-history filters: only outcomes fully known before the next entry.
    by_symbol = {symbol: group.copy() for symbol, group in frame.groupby("symbol")}
    for minimum in (1, 2):
        selected: set[str] = set()
        for _, row in oos.iterrows():
            entry = pd.Timestamp(row["entry_date"], tz="UTC")
            history = by_symbol[row["symbol"]]
            history = history[pd.to_datetime(history["exit_time_utc"], utc=True) < entry]
            if len(history) >= minimum and history["target_return_2x"].mean() <= 0:
                continue
            selected.add(row["case_id"])
        scenarios[f"oos_coin_history_min_{minimum}"] = selected
        selected_rows = oos[oos["case_id"].isin(selected)]
        if len(selected_rows):
            best_case = selected_rows.sort_values("target_return_2x", ascending=False).iloc[0]["case_id"]
            scenarios[f"oos_coin_history_min_{minimum}_without_best_trade"] = selected - {best_case}

    needed_ids = set().union(*scenarios.values())
    bar_maps = {
        case_id: weighted.read_bar_map(
            HERE / "intraday-all-data" / case_by_id[case_id]["intervals"]["5m"]["file"]
        )
        for case_id in needed_ids
    }
    results: list[dict[str, Any]] = []
    attribution_rows: list[dict[str, Any]] = []
    for name, selected_ids in scenarios.items():
        entries = sorted(
            (all_entries[case_id] for case_id in selected_ids),
            key=lambda item: (item["time_ms"], item["case_id"]),
        )
        exits = [row for row in all_exits if row["case_id"] in selected_ids]
        kept_entries, kept_exits, skipped = enforce_capacity(entries, exits)
        if not kept_entries:
            continue
        result = weighted.simulate(name, kept_entries, kept_exits, bar_maps)
        attribution = result["attribution"]
        attribution_rows.extend({**row, "filter_scenario": name} for row in attribution)
        results.append(
            {
                "scenario": name,
                "candidate_signals": len(selected_ids),
                "executed_trades": len(kept_entries),
                "capacity_skipped": len(skipped),
                "win_rate": sum(row["total_net_pnl"] > 0 for row in attribution) / len(attribution),
                "total_return": result["total_return"],
                "max_drawdown_5m": result["max_drawdown_5m"],
                "average_margin_return": statistics.mean(row["return_on_allocated_margin"] for row in attribution),
            }
        )
    return results, attribution_rows


def symbol_attribution() -> pd.DataFrame:
    config = json.loads((HERE / "ready169-config.json").read_text(encoding="utf-8"))
    symbols = list(config["symbols"])
    attribution = pd.read_csv(HERE / "weighted-portfolio-v2-attribution.csv")
    v2 = attribution[attribution["strategy"] == "v2_multitimeframe"]
    diagnostic = pd.read_csv(HERE / "multitimeframe-exit-v2-trades.csv")
    valid = diagnostic[diagnostic["right_censored_mark"] == False]
    executed_ids = set(v2["case_id"])
    rows = []
    for symbol in symbols:
        actual = v2[v2["symbol"] == symbol]
        candidate = valid[valid["symbol"] == symbol]
        contribution = actual["contribution_to_initial_equity"].sum()
        if len(actual):
            classification = "positive" if contribution > 0 else "negative"
        elif len(candidate):
            classification = "capacity_skipped_only"
        else:
            classification = "no_completed_entry"
        rows.append(
            {
                "symbol": symbol,
                "classification": classification,
                "completed_v2_candidates": len(candidate),
                "executed_v2_trades": len(actual),
                "capacity_skipped_candidates": sum(case_id not in executed_ids for case_id in candidate["case_id"]),
                "winning_trades": int((actual["total_net_pnl"] > 0).sum()),
                "portfolio_contribution": contribution,
                "mean_margin_return": actual["return_on_allocated_margin"].mean() if len(actual) else "",
            }
        )
    return pd.DataFrame(rows).sort_values(["classification", "portfolio_contribution"], ascending=[True, False])


def main() -> None:
    symbols = symbol_attribution()
    symbols.to_csv(HERE / "v2-per-symbol-contribution.csv", index=False)
    frame = build_features()
    frame.to_csv(HERE / "v2-entry-feature-dataset.csv", index=False)
    oos, coefficients = walk_forward(frame)
    oos.to_csv(HERE / "v2-filter-walkforward-predictions.csv", index=False)
    write_csv(HERE / "v2-filter-coefficients.csv", coefficients)
    scenarios, scenario_attribution = portfolio_scenarios(frame, oos)
    write_csv(HERE / "v2-filter-backtests.csv", scenarios)
    write_csv(HERE / "v2-filter-portfolio-attribution.csv", scenario_attribution)
    y = oos["profitable"].to_numpy(dtype=int)
    probability = oos["probability"].to_numpy(dtype=float)
    metrics = {
        "complete_diagnostic_trades": len(frame),
        "oos_trades": len(oos),
        "oos_months": sorted(oos["fold_month"].unique().tolist()),
        "oos_auc": auc(y, probability),
        "oos_brier": float(np.mean((probability - y) ** 2)),
        "oos_base_win_rate": float(y.mean()),
        "portfolio_scenarios": scenarios,
    }
    (HERE / "v2-filter-analysis.json").write_text(
        json.dumps(metrics, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(metrics, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
