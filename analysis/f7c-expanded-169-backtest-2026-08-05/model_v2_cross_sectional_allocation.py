#!/usr/bin/env python3
"""Walk-forward cross-sectional ranking and event-driven V2 allocation study."""

from __future__ import annotations

import csv
import importlib.util
import json
import math
import random
import statistics
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


HERE = Path(__file__).resolve().parent
OLD = HERE.parent / "f7c-strategy-backtest-2026-08-01"
PREDICTIONS_CSV = HERE / "v2-cross-sectional-predictions.csv"
FOLDS_CSV = HERE / "v2-cross-sectional-folds.csv"
COEFFICIENTS_CSV = HERE / "v2-cross-sectional-coefficients.csv"
BACKTESTS_CSV = HERE / "v2-cross-sectional-allocation-backtests.csv"
ATTRIBUTION_CSV = HERE / "v2-cross-sectional-allocation-attribution.csv"
SUMMARY_JSON = HERE / "v2-cross-sectional-summary.json"
REPORT_MD = HERE / "v2-cross-sectional-report.md"

FRICTION_PER_SIDE = 0.002
LEVERAGE = 2.0
BAR_MS = 5 * 60 * 1000
INITIAL_EQUITY = 1.0

RANK_FEATURES = [
    "log_signal_volume_ratio",
    "log_flow_to_signal",
    "flow_normal_0_to_5pct",
    "flow_normal_above_5pct",
    "flow_normal_above_10pct",
    "return_7d",
    "return_7d_above_20pct",
    "return_7d_above_50pct",
    "recent_7d_runup",
    "recent_5x_volume",
    "log_prior7_max_volume_ratio",
]

FEATURE_LABELS = {
    "log_signal_volume_ratio": "信号日成交量倍数",
    "log_flow_to_signal": "流入/信号日成交量",
    "flow_normal_0_to_5pct": "流入/日常量0%—5%",
    "flow_normal_above_5pct": "流入/日常量超过5%",
    "flow_normal_above_10pct": "流入/日常量超过10%",
    "return_7d": "前7日涨幅",
    "return_7d_above_20pct": "前7日涨幅超过20%",
    "return_7d_above_50pct": "前7日涨幅超过50%",
    "recent_7d_runup": "近7日最高涨幅",
    "recent_5x_volume": "此前7日5倍量",
    "log_prior7_max_volume_ratio": "此前7日最大量倍数",
}


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
        if not fields:
            return
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


def timestamp(day: str) -> int:
    return int(datetime.fromisoformat(day).replace(tzinfo=timezone.utc).timestamp() * 1000)


def average_ranks(values: list[float]) -> list[float]:
    order = sorted(range(len(values)), key=values.__getitem__)
    ranks = [0.0] * len(values)
    start = 0
    while start < len(order):
        end = start + 1
        while end < len(order) and values[order[end]] == values[order[start]]:
            end += 1
        rank = (start + end - 1) / 2
        for index in order[start:end]:
            ranks[index] = rank
        start = end
    return ranks


def percentile_ranks(values: list[float], singleton: float = 0.5) -> list[float]:
    if len(values) == 1:
        return [singleton]
    return [rank / (len(values) - 1) for rank in average_ranks(values)]


def correlation(left: list[float], right: list[float]) -> float:
    if len(left) < 2:
        return float("nan")
    left_mean = statistics.mean(left)
    right_mean = statistics.mean(right)
    numerator = sum((a - left_mean) * (b - right_mean) for a, b in zip(left, right, strict=True))
    denominator = math.sqrt(
        sum((a - left_mean) ** 2 for a in left)
        * sum((b - right_mean) ** 2 for b in right)
    )
    return numerator / denominator if denominator else 0.0


def max_drawdown(values: list[float]) -> float:
    peak = values[0]
    worst = 0.0
    for value in values:
        peak = max(peak, value)
        worst = min(worst, value / peak - 1)
    return worst


def prepare_rows() -> list[dict[str, Any]]:
    failure_model = load_module("v2_xsec_failure_model", HERE / "model_v2_failure_walkforward.py")
    rows = failure_model.prepare_rows()
    return rows


def rank_features(rows: list[dict[str, Any]]) -> dict[str, dict[str, float]]:
    output: dict[str, dict[str, float]] = {row["case_id"]: {} for row in rows}
    by_date: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        by_date[row["entry_date"]].append(row)
    for date_rows in by_date.values():
        for feature in RANK_FEATURES:
            values = [float(row["engineered"][feature]) for row in date_rows]
            ranks = percentile_ranks(values)
            for row, rank in zip(date_rows, ranks, strict=True):
                output[row["case_id"]][feature] = rank - 0.5
    return output


def target_ranks(rows: list[dict[str, Any]]) -> dict[str, float]:
    output: dict[str, float] = {}
    by_date: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        by_date[row["entry_date"]].append(row)
    for date_rows in by_date.values():
        if len(date_rows) < 2:
            continue
        values = [float(row["target_return_2x"]) for row in date_rows]
        for row, rank in zip(date_rows, percentile_ranks(values), strict=True):
            output[row["case_id"]] = rank - 0.5
    return output


def fit_ridge(
    rows: list[dict[str, Any]], ranks: dict[str, dict[str, float]], targets: dict[str, float]
) -> list[float]:
    eligible = [row for row in rows if row["case_id"] in targets]
    design = [[1.0] + [ranks[row["case_id"]][feature] for feature in RANK_FEATURES] for row in eligible]
    labels = [targets[row["case_id"]] for row in eligible]
    weights = [0.0] * (len(RANK_FEATURES) + 1)
    rate = 0.08
    penalty = 4.0
    previous = float("inf")
    for iteration in range(6000):
        predictions = [sum(w * x for w, x in zip(weights, vector, strict=True)) for vector in design]
        gradients = []
        for column in range(len(weights)):
            gradient = 2 * sum(
                (prediction - label) * vector[column]
                for prediction, label, vector in zip(predictions, labels, design, strict=True)
            ) / len(design)
            if column:
                gradient += 2 * penalty * weights[column] / len(design)
            gradients.append(gradient)
        weights = [weight - rate * gradient for weight, gradient in zip(weights, gradients, strict=True)]
        if iteration % 100 == 0:
            loss = statistics.mean((prediction - label) ** 2 for prediction, label in zip(predictions, labels, strict=True))
            loss += penalty * sum(weight * weight for weight in weights[1:]) / len(design)
            if abs(previous - loss) < 1e-11:
                break
            previous = loss
    return weights


def predict_ridge(weights: list[float], row: dict[str, Any], ranks: dict[str, dict[str, float]]) -> float:
    return weights[0] + sum(
        weight * ranks[row["case_id"]][feature]
        for weight, feature in zip(weights[1:], RANK_FEATURES, strict=True)
    )


def walk_forward(rows: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    ranks = rank_features(rows)
    predictions: list[dict[str, Any]] = []
    folds: list[dict[str, Any]] = []
    coefficients: list[dict[str, Any]] = []
    for month in sorted({row["entry_date"][:7] for row in rows}):
        cutoff = datetime.fromisoformat(month + "-01T00:00:00+00:00")
        train = [
            row for row in rows
            if row["completed"] and datetime.fromisoformat(row["exit_time_utc"]) < cutoff
        ]
        targets = target_ranks(train)
        if len(targets) < 30:
            continue
        weights = fit_ridge(train, ranks, targets)
        test = [row for row in rows if row["entry_date"][:7] == month]
        test_output = []
        for row in test:
            test_output.append(
                {
                    "case_id": row["case_id"],
                    "symbol": row["symbol"],
                    "signal_date": row["signal_date"],
                    "entry_date": row["entry_date"],
                    "fold_month": month,
                    "train_completed": len(train),
                    "train_rank_rows": len(targets),
                    "xsec_score": predict_ridge(weights, row, ranks),
                    "f7c_share": float(row["f7c_share"]),
                    "signal_range_pct": float(row["signal_range_pct"]),
                    "market_median_return_7d": float(row["market_median_return_7d"]),
                    "market_positive_breadth_7d": float(row["market_positive_breadth_7d"]),
                    "completed": row["completed"],
                    "target_return_2x": row["target_return_2x"] if row["completed"] else "",
                }
            )
        by_date: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for row in test_output:
            by_date[row["entry_date"]].append(row)
        for date_rows in by_date.values():
            score_ranks = percentile_ranks([float(row["xsec_score"]) for row in date_rows], singleton=1.0)
            for row, score_rank in zip(date_rows, score_ranks, strict=True):
                row["xsec_rank_pct"] = score_rank
        predictions.extend(test_output)
        completed_test = [row for row in test_output if row["completed"]]
        metrics = cross_sectional_metrics(completed_test, "xsec_score")
        folds.append(
            {
                "month": month,
                "train_completed": len(train),
                "train_rank_rows": len(targets),
                "test_signals": len(test_output),
                "completed_test": len(completed_test),
                **metrics,
            }
        )
        for feature, coefficient in zip(RANK_FEATURES, weights[1:], strict=True):
            coefficients.append(
                {
                    "month": month,
                    "feature": feature,
                    "label": FEATURE_LABELS[feature],
                    "coefficient": coefficient,
                }
            )
    return predictions, folds, coefficients


def cross_sectional_metrics(rows: list[dict[str, Any]], score_field: str) -> dict[str, Any]:
    by_date: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        by_date[row["entry_date"]].append(row)
    ics = []
    spreads = []
    observations = 0
    for date_rows in by_date.values():
        if len(date_rows) < 2:
            continue
        scores = [float(row[score_field]) for row in date_rows]
        returns = [float(row["target_return_2x"]) for row in date_rows]
        ic = correlation(average_ranks(scores), average_ranks(returns))
        ics.append(ic)
        order = sorted(range(len(date_rows)), key=scores.__getitem__)
        spreads.append(returns[order[-1]] - returns[order[0]])
        observations += len(date_rows)
    ic_mean = statistics.mean(ics) if ics else float("nan")
    ic_std = statistics.pstdev(ics) if len(ics) >= 2 else float("nan")
    return {
        "xsec_dates": len(ics),
        "xsec_observations": observations,
        "mean_rank_ic": ic_mean,
        "rank_ic_ir": ic_mean / ic_std if len(ics) >= 2 and ic_std else float("nan"),
        "positive_ic_rate": statistics.mean(ic > 0 for ic in ics) if ics else float("nan"),
        "mean_top_bottom_return_spread": statistics.mean(spreads) if spreads else float("nan"),
    }


def attach_classifier_scores(predictions: list[dict[str, Any]]) -> None:
    classifier_rows = {
        row["case_id"]: row
        for row in read_csv(HERE / "v2-failure-model-walkforward-predictions.csv")
    }
    missing = {row["case_id"] for row in predictions} - set(classifier_rows)
    if missing:
        raise ValueError(f"missing classifier scores for {len(missing)} cases")
    by_date: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in predictions:
        row["classifier_score"] = float(classifier_rows[row["case_id"]]["success_probability"])
        by_date[row["entry_date"]].append(row)
    for date_rows in by_date.values():
        ranks = percentile_ranks(
            [float(row["classifier_score"]) for row in date_rows], singleton=1.0
        )
        for row, rank in zip(date_rows, ranks, strict=True):
            row["classifier_rank_pct"] = rank


def aligned_event_time(event: dict[str, Any]) -> int:
    return int(float(event["execution_open_time_ms"])) // BAR_MS * BAR_MS


def scenario_config(name: str) -> dict[str, Any]:
    configs = {
        "baseline_f7c_k3_equal": dict(order="f7c", top_half=False, rank_field="xsec_rank_pct", max_positions=3, margin=0.30, inverse_risk=False, dynamic=False),
        "xsec_k3_equal_all": dict(order="xsec", top_half=False, rank_field="xsec_rank_pct", max_positions=3, margin=0.30, inverse_risk=False, dynamic=False),
        "xsec_k3_equal_tophalf": dict(order="xsec", top_half=True, rank_field="xsec_rank_pct", max_positions=3, margin=0.30, inverse_risk=False, dynamic=False),
        "xsec_k5_equal_tophalf": dict(order="xsec", top_half=True, rank_field="xsec_rank_pct", max_positions=5, margin=0.18, inverse_risk=False, dynamic=False),
        "xsec_k8_equal_tophalf": dict(order="xsec", top_half=True, rank_field="xsec_rank_pct", max_positions=8, margin=0.1125, inverse_risk=False, dynamic=False),
        "xsec_k5_invvol_tophalf": dict(order="xsec", top_half=True, rank_field="xsec_rank_pct", max_positions=5, margin=0.18, inverse_risk=True, dynamic=False),
        "xsec_dynamic_invvol_tophalf": dict(order="xsec", top_half=True, rank_field="xsec_rank_pct", max_positions=5, margin=0.18, inverse_risk=True, dynamic=True),
        "f7c_dynamic_invvol_all": dict(order="f7c", top_half=False, rank_field="xsec_rank_pct", max_positions=5, margin=0.18, inverse_risk=True, dynamic=True),
        "xsec_dynamic_invvol_all": dict(order="xsec", top_half=False, rank_field="xsec_rank_pct", max_positions=5, margin=0.18, inverse_risk=True, dynamic=True),
        "classifier_k3_equal_tophalf": dict(order="classifier", top_half=True, rank_field="classifier_rank_pct", max_positions=3, margin=0.30, inverse_risk=False, dynamic=False),
        "classifier_k5_invvol_tophalf": dict(order="classifier", top_half=True, rank_field="classifier_rank_pct", max_positions=5, margin=0.18, inverse_risk=True, dynamic=False),
        "classifier_dynamic_invvol_all": dict(order="classifier", top_half=False, rank_field="classifier_rank_pct", max_positions=5, margin=0.18, inverse_risk=True, dynamic=True),
        "classifier_dynamic_invvol_tophalf": dict(order="classifier", top_half=True, rank_field="classifier_rank_pct", max_positions=5, margin=0.18, inverse_risk=True, dynamic=True),
        "random_dynamic_invvol_all": dict(order="random", top_half=False, rank_field="xsec_rank_pct", max_positions=5, margin=0.18, inverse_risk=True, dynamic=True),
    }
    return configs[name]


def simulate_allocation(
    name: str,
    predictions: list[dict[str, Any]],
    cases: dict[str, dict[str, Any]],
    events: list[dict[str, str]],
    bar_maps: dict[str, dict[int, dict[str, float]]],
) -> dict[str, Any]:
    config = scenario_config(name)
    entries_by_time: dict[int, list[dict[str, Any]]] = defaultdict(list)
    exits_by_time: dict[int, list[dict[str, Any]]] = defaultdict(list)
    prediction_by_id = {row["case_id"]: row for row in predictions}
    for case_id, row in prediction_by_id.items():
        if config["top_half"] and float(row[config["rank_field"]]) < 0.5:
            continue
        entries_by_time[cases[case_id]["entry_time_ms"]].append({**cases[case_id], **row})
    for event in events:
        if event["case_id"] in prediction_by_id:
            exits_by_time[aligned_event_time(event)].append(event)
    start = min(entries_by_time)
    end = max(exits_by_time)
    balance = INITIAL_EQUITY
    positions: dict[str, dict[str, Any]] = {}
    active_symbols: set[str] = set()
    attribution: dict[str, dict[str, Any]] = {}
    curve: list[dict[str, Any]] = []
    skipped = defaultdict(int)

    def mark_equity(time_ms: int, field: str) -> float:
        equity = balance
        for case_id, position in positions.items():
            bar = bar_maps[case_id].get(time_ms)
            if bar is None:
                raise ValueError(f"missing {case_id} bar at {time_ms}")
            equity += position["remaining_fraction"] * position["notional"] * (
                bar[field] / position["entry_price"] - 1
            )
        return equity

    for time_ms in range(start, end + BAR_MS, BAR_MS):
        for event in exits_by_time.get(time_ms, []):
            case_id = event["case_id"]
            if case_id not in positions:
                continue
            position = positions[case_id]
            fraction = min(float(event["sold_fraction"]), position["remaining_fraction"])
            notional_piece = fraction * position["notional"]
            price = float(event["execution_price"])
            gross_pnl = notional_piece * (price / position["entry_price"] - 1)
            exit_fee = notional_piece * (price / position["entry_price"]) * FRICTION_PER_SIDE
            balance += gross_pnl - exit_fee
            position["remaining_fraction"] -= fraction
            item = attribution[case_id]
            item["exit_pnl_after_exit_fee"] += gross_pnl - exit_fee
            item["exit_events"] += 1
            item["exit_time_utc"] = datetime.fromtimestamp(time_ms / 1000, timezone.utc).isoformat()
            if position["remaining_fraction"] <= 1e-12:
                active_symbols.remove(position["symbol"])
                del positions[case_id]

        equity_open = mark_equity(time_ms, "open") if positions else balance
        new_candidates = entries_by_time.get(time_ms, [])
        if config["order"] == "f7c":
            new_candidates = sorted(new_candidates, key=lambda row: (-float(row["f7c_share"]), row["case_id"]))
        elif config["order"] == "classifier":
            new_candidates = sorted(new_candidates, key=lambda row: (-float(row["classifier_score"]), row["case_id"]))
        elif config["order"] == "random":
            new_candidates = sorted(new_candidates, key=lambda row: (-float(row["random_score"]), row["case_id"]))
        else:
            new_candidates = sorted(new_candidates, key=lambda row: (-float(row["xsec_score"]), row["case_id"]))
        for candidate in new_candidates:
            if candidate["symbol"] in active_symbols:
                skipped["same_symbol"] += 1
                continue
            max_positions = config["max_positions"]
            margin_budget = 0.90
            margin_fraction = config["margin"]
            if config["dynamic"]:
                market_return = float(candidate["market_median_return_7d"])
                breadth = float(candidate["market_positive_breadth_7d"])
                if market_return > 0 and breadth >= 0.55:
                    max_positions, margin_budget, margin_fraction = 5, 0.90, 0.18
                elif breadth >= 0.35 or market_return > 0:
                    max_positions, margin_budget, margin_fraction = 3, 0.60, 0.20
                else:
                    max_positions, margin_budget, margin_fraction = 2, 0.30, 0.15
            if len(positions) >= max_positions:
                skipped["position_limit"] += 1
                continue
            if config["inverse_risk"]:
                risk = max(float(candidate["signal_range_pct"]), 0.02)
                risk_scale = math.sqrt(0.08 / risk)
                margin_fraction *= max(0.50, min(1.50, risk_scale))
                margin_fraction = min(margin_fraction, 0.30)
            desired_margin = margin_fraction * equity_open
            if config["dynamic"] or config["inverse_risk"]:
                active_margin = sum(
                    position["margin"] * position["remaining_fraction"] for position in positions.values()
                )
                free_margin = max(0.0, margin_budget * equity_open - active_margin)
                margin = min(desired_margin, free_margin)
            else:
                margin = desired_margin
            if margin < 0.03 * equity_open:
                skipped["risk_budget"] += 1
                continue
            notional = LEVERAGE * margin
            entry_fee = notional * FRICTION_PER_SIDE
            balance -= entry_fee
            case_id = candidate["case_id"]
            positions[case_id] = {
                **candidate,
                "margin": margin,
                "notional": notional,
                "remaining_fraction": 1.0,
            }
            active_symbols.add(candidate["symbol"])
            attribution[case_id] = {
                "scenario": name,
                "case_id": case_id,
                "symbol": candidate["symbol"],
                "signal_date": candidate["signal_date"],
                "entry_time_utc": datetime.fromtimestamp(time_ms / 1000, timezone.utc).isoformat(),
                "entry_equity": equity_open,
                "margin": margin,
                "margin_fraction_of_equity": margin / equity_open,
                "notional": notional,
                "xsec_score": candidate["xsec_score"],
                "xsec_rank_pct": candidate["xsec_rank_pct"],
                "entry_fee": entry_fee,
                "exit_pnl_after_exit_fee": 0.0,
                "exit_events": 0,
                "exit_time_utc": "",
            }

        close_equity = mark_equity(time_ms, "close") if positions else balance
        gross_exposure = sum(
            position["remaining_fraction"] * position["notional"]
            * bar_maps[case_id][time_ms]["close"] / position["entry_price"]
            for case_id, position in positions.items()
        )
        curve.append(
            {
                "equity": close_equity,
                "open_positions": len(positions),
                "gross_leverage": gross_exposure / close_equity if close_equity > 0 else float("inf"),
            }
        )
    if positions:
        raise ValueError(f"{name} has open positions: {sorted(positions)}")
    attribution_rows = []
    for item in attribution.values():
        item["total_net_pnl"] = item["exit_pnl_after_exit_fee"] - item["entry_fee"]
        item["return_on_allocated_margin"] = item["total_net_pnl"] / item["margin"]
        attribution_rows.append(item)
    values = [INITIAL_EQUITY] + [row["equity"] for row in curve]
    invested = [row for row in curve if row["open_positions"]]
    return {
        "scenario": name,
        "candidate_signals": len(entries_by_time) and sum(len(value) for value in entries_by_time.values()),
        "executed_trades": len(attribution_rows),
        "win_rate": statistics.mean(item["total_net_pnl"] > 0 for item in attribution_rows),
        "average_margin_fraction": statistics.mean(item["margin_fraction_of_equity"] for item in attribution_rows),
        "final_equity": balance,
        "total_return": balance - 1,
        "max_drawdown_5m": max_drawdown(values),
        "max_gross_leverage": max(row["gross_leverage"] for row in curve),
        "average_open_positions": statistics.mean(row["open_positions"] for row in invested),
        "average_invested_gross_leverage": statistics.mean(row["gross_leverage"] for row in invested),
        "skipped_same_symbol": skipped["same_symbol"],
        "skipped_position_limit": skipped["position_limit"],
        "skipped_risk_budget": skipped["risk_budget"],
        "attribution": attribution_rows,
    }


def run_backtests(predictions: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    e2e = load_module("v2_xsec_e2e", HERE / "run_v2_end_to_end.py")
    weighted = load_module("v2_xsec_weighted", OLD / "backtest_portfolio_weighted.py")
    candidates = read_csv(HERE / "v2-all-signal-input-trades.csv")
    summaries = read_csv(HERE / "v2-all-signal-exit-trades.csv")
    events = read_csv(HERE / "v2-all-signal-exit-events.csv")
    case_rows, _ = e2e.case_inputs(candidates, summaries, events)
    cases = {row["case_id"]: row for row in case_rows}
    manifest = json.loads((HERE / "intraday-all-signals-data/manifest.json").read_text(encoding="utf-8"))
    manifest_by_id = {case["case_id"]: case for case in manifest["cases"]}
    needed_ids = {row["case_id"] for row in predictions}
    bar_maps = {
        case_id: weighted.read_bar_map(
            HERE / "intraday-all-signals-data" / manifest_by_id[case_id]["intervals"]["5m"]["file"]
        )
        for case_id in needed_ids
    }
    names = [
        "baseline_f7c_k3_equal",
        "xsec_k3_equal_all",
        "xsec_k3_equal_tophalf",
        "xsec_k5_equal_tophalf",
        "xsec_k8_equal_tophalf",
        "xsec_k5_invvol_tophalf",
        "xsec_dynamic_invvol_tophalf",
        "f7c_dynamic_invvol_all",
        "xsec_dynamic_invvol_all",
        "classifier_k3_equal_tophalf",
        "classifier_k5_invvol_tophalf",
        "classifier_dynamic_invvol_all",
        "classifier_dynamic_invvol_tophalf",
    ]
    results = []
    attribution = []
    attribution_by_name: dict[str, list[dict[str, Any]]] = {}
    for name in names:
        result = simulate_allocation(name, predictions, cases, events, bar_maps)
        scenario_attribution = result.pop("attribution")
        attribution.extend(scenario_attribution)
        attribution_by_name[name] = scenario_attribution
        results.append(result)
    for name in ("xsec_dynamic_invvol_all", "classifier_dynamic_invvol_all"):
        best_case = max(
            attribution_by_name[name], key=lambda row: float(row["total_net_pnl"])
        )["case_id"]
        reduced = [row for row in predictions if row["case_id"] != best_case]
        result = simulate_allocation(name, reduced, cases, events, bar_maps)
        scenario_attribution = result.pop("attribution")
        result["scenario"] = f"{name}_without_best_trade"
        result["removed_best_case"] = best_case
        attribution.extend(
            {**row, "scenario": result["scenario"]} for row in scenario_attribution
        )
        results.append(result)
    random_results = []
    for seed in range(30):
        rng = random.Random(20260805 + seed)
        randomized = [{**row, "random_score": rng.random()} for row in predictions]
        result = simulate_allocation(
            "random_dynamic_invvol_all", randomized, cases, events, bar_maps
        )
        result.pop("attribution")
        random_results.append(result)
    random_returns = [float(row["total_return"]) for row in random_results]
    random_drawdowns = [float(row["max_drawdown_5m"]) for row in random_results]
    random_executed = [float(row["executed_trades"]) for row in random_results]
    random_summary = {
            "scenario": "random_dynamic_invvol_all_30_median",
            "candidate_signals": len(predictions),
            "executed_trades": statistics.median(random_executed),
            "win_rate": statistics.median(float(row["win_rate"]) for row in random_results),
            "average_margin_fraction": statistics.median(
                float(row["average_margin_fraction"]) for row in random_results
            ),
            "final_equity": 1 + statistics.median(random_returns),
            "total_return": statistics.median(random_returns),
            "max_drawdown_5m": statistics.median(random_drawdowns),
            "max_gross_leverage": statistics.median(
                float(row["max_gross_leverage"]) for row in random_results
            ),
            "average_open_positions": statistics.median(
                float(row["average_open_positions"]) for row in random_results
            ),
            "average_invested_gross_leverage": statistics.median(
                float(row["average_invested_gross_leverage"]) for row in random_results
            ),
            "skipped_same_symbol": "",
            "skipped_position_limit": "",
            "skipped_risk_budget": "",
            "random_return_p10": sorted(random_returns)[2],
            "random_return_p90": sorted(random_returns)[-3],
        }
    for model_name in ("xsec_dynamic_invvol_all", "classifier_dynamic_invvol_all"):
        model_return = next(
            float(row["total_return"]) for row in results if row["scenario"] == model_name
        )
        random_summary[f"{model_name}_percentile_vs_random"] = sum(
            value <= model_return for value in random_returns
        ) / len(random_returns)
    results.append(random_summary)
    return results, attribution


def fmt(value: float) -> str:
    return f"{value * 100:.2f}%"


def render_report(
    predictions: list[dict[str, Any]],
    folds: list[dict[str, Any]],
    coefficients: list[dict[str, Any]],
    backtests: list[dict[str, Any]],
) -> str:
    completed = [row for row in predictions if row["completed"]]
    xsec = cross_sectional_metrics(completed, "xsec_score")
    classifier = cross_sectional_metrics(completed, "classifier_score")
    f7c = cross_sectional_metrics(completed, "f7c_share")
    coefficient_by_feature: dict[str, list[float]] = defaultdict(list)
    for row in coefficients:
        coefficient_by_feature[row["feature"]].append(float(row["coefficient"]))
    coefficient_summary = sorted(
        (
            (
                feature,
                statistics.mean(values),
                sum(value > 0 for value in values),
                len(values),
            )
            for feature, values in coefficient_by_feature.items()
        ),
        key=lambda row: abs(row[1]),
        reverse=True,
    )
    lines = [
        "# V2截面排序与仓位分配研究",
        "",
        "## 方法",
        "",
        "- 将同一入场日视为一个截面；特征和未来V2收益均先转成截面百分位排名。",
        "- 使用扩展窗口岭回归预测相对收益排名；每月只使用月初前已经退出的交易训练。",
        "- 市场趋势和上涨广度不参与币种间排序，而只用于动态总风险预算，这是标准的alpha/risk分层。",
        "- 逆风险权重暂以信号日振幅作为事前风险代理；动态预算阈值是预设基准，不是已训练的最优参数。",
        "- OOS从2026-02开始；单币日没有截面比较对象，排名记为最高但不会贡献IC。",
        "",
        "## 总体截面能力",
        "",
        "| 排序分数 | 截面日 | 观察交易 | 平均Rank IC | ICIR | 正IC占比 | 第一名减最后一名收益 |",
        "|---|---:|---:|---:|---:|---:|---:|",
        f"| 截面岭回归 | {xsec['xsec_dates']} | {xsec['xsec_observations']} | {xsec['mean_rank_ic']:+.3f} | {xsec['rank_ic_ir']:+.3f} | {fmt(xsec['positive_ic_rate'])} | {fmt(xsec['mean_top_bottom_return_spread'])} |",
        f"| 两阶段概率Alpha | {classifier['xsec_dates']} | {classifier['xsec_observations']} | {classifier['mean_rank_ic']:+.3f} | {classifier['rank_ic_ir']:+.3f} | {fmt(classifier['positive_ic_rate'])} | {fmt(classifier['mean_top_bottom_return_spread'])} |",
        f"| 原F7c排序 | {f7c['xsec_dates']} | {f7c['xsec_observations']} | {f7c['mean_rank_ic']:+.3f} | {f7c['rank_ic_ir']:+.3f} | {fmt(f7c['positive_ic_rate'])} | {fmt(f7c['mean_top_bottom_return_spread'])} |",
        "",
        "## 逐月OOS Rank IC",
        "",
        "| 月份 | 训练交易 | 截面日 | 平均Rank IC | 正IC占比 | Top-Bottom |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    for row in folds:
        lines.append(
            f"| {row['month']} | {row['train_completed']} | {row['xsec_dates']} | "
            f"{float(row['mean_rank_ic']):+.3f} | {fmt(float(row['positive_ic_rate']))} | "
            f"{fmt(float(row['mean_top_bottom_return_spread']))} |"
        )
    lines.extend(
        [
            "",
            "## 仓位分配回放",
            "",
            "| 方案 | 候选 | 执行 | 胜率 | 平均单仓保证金 | 总收益 | MDD | 最大杠杆 | 平均持仓数 |",
            "|---|---:|---:|---:|---:|---:|---:|---:|---:|",
        ]
    )
    for row in backtests:
        lines.append(
            f"| {row['scenario']} | {row['candidate_signals']} | {row['executed_trades']} | "
            f"{fmt(float(row['win_rate']))} | {fmt(float(row['average_margin_fraction']))} | "
            f"{fmt(float(row['total_return']))} | {fmt(float(row['max_drawdown_5m']))} | "
            f"{float(row['max_gross_leverage']):.2f}x | {float(row['average_open_positions']):.2f} |"
        )
    random_control = next(
        row for row in backtests if row["scenario"] == "random_dynamic_invvol_all_30_median"
    )
    lines.extend(
        [
            "",
            "## 排序稳健性",
            "",
            f"- 动态风险预算下随机排序30次：收益中位数{fmt(float(random_control['total_return']))}，约10%—90%区间[{fmt(float(random_control['random_return_p10']))}, {fmt(float(random_control['random_return_p90']))}]。",
            f"- 截面岭回归排序位于随机对照第{fmt(float(random_control['xsec_dynamic_invvol_all_percentile_vs_random']))}分位；概率Alpha排序位于第{fmt(float(random_control['classifier_dynamic_invvol_all_percentile_vs_random']))}分位。",
        ]
    )
    for name in ("xsec_dynamic_invvol_all", "classifier_dynamic_invvol_all"):
        base = next(row for row in backtests if row["scenario"] == name)
        reduced = next(
            row for row in backtests if row["scenario"] == f"{name}_without_best_trade"
        )
        lines.append(
            f"- `{name}`：原收益{fmt(float(base['total_return']))}；移除最佳交易后{fmt(float(reduced['total_return']))}（移除{reduced['removed_best_case']}）。"
        )
    lines.extend(
        [
            "",
            "## 截面模型系数",
            "",
            "正值表示在同日候选币中排名越高，模型越倾向给予更高未来收益排名。",
            "",
            "| 特征 | 平均系数 | 正系数训练时点 |",
            "|---|---:|---:|",
        ]
    )
    for feature, value, positive, count in coefficient_summary:
        lines.append(f"| {FEATURE_LABELS[feature]} | {value:+.3f} | {positive}/{count} |")
    best = max(backtests, key=lambda row: float(row["total_return"]))
    lines.extend(
        [
            "",
            "## 结论",
            "",
            f"- 本轮收益最高方案为`{best['scenario']}`：总收益{fmt(float(best['total_return']))}、MDD{fmt(float(best['max_drawdown_5m']))}。",
            "- 先看Rank IC是否跨月稳定，再看组合收益；若收益改善但IC不稳定，应视为仓位路径或少数大赢家效应。",
            "- 截面只有13个交易日达到5个以上候选，日截面偏薄；生产版应把竞争集合扩展为“当前新信号+仍持有仓位”，再研究换仓成本。",
            "",
            "## 数据产物",
            "",
            f"- OOS截面预测：`{PREDICTIONS_CSV.name}`",
            f"- 逐月IC：`{FOLDS_CSV.name}`",
            f"- 系数：`{COEFFICIENTS_CSV.name}`",
            f"- 仓位回放：`{BACKTESTS_CSV.name}`",
            f"- 交易归因：`{ATTRIBUTION_CSV.name}`",
        ]
    )
    return "\n".join(lines) + "\n"


def main() -> None:
    rows = prepare_rows()
    predictions, folds, coefficients = walk_forward(rows)
    attach_classifier_scores(predictions)
    backtests, attribution = run_backtests(predictions)
    write_csv(PREDICTIONS_CSV, predictions)
    write_csv(FOLDS_CSV, folds)
    write_csv(COEFFICIENTS_CSV, coefficients)
    write_csv(BACKTESTS_CSV, backtests)
    write_csv(ATTRIBUTION_CSV, attribution)
    summary = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "method": "monthly_walk_forward_cross_sectional_rank_ridge",
        "predictions": len(predictions),
        "completed_oos": sum(row["completed"] for row in predictions),
        "backtests": backtests,
    }
    SUMMARY_JSON.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    REPORT_MD.write_text(
        render_report(predictions, folds, coefficients, backtests), encoding="utf-8"
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
