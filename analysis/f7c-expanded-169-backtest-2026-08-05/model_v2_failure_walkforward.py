#!/usr/bin/env python3
"""Causal monthly walk-forward model for V2 trade failure risk."""

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
FEATURE_INPUT = HERE / "v2-all-signal-entry-features.csv"
ATTRIBUTION_INPUT = HERE / "v2-independent-trade-attribution.csv"
PREDICTIONS_CSV = HERE / "v2-failure-model-walkforward-predictions.csv"
FOLDS_CSV = HERE / "v2-failure-model-folds.csv"
FEATURES_CSV = HERE / "v2-failure-model-feature-coefficients.csv"
ABLATION_CSV = HERE / "v2-failure-model-ablations.csv"
FILTER_CSV = HERE / "v2-failure-model-filter-results.csv"
MONTH_FILTER_CSV = HERE / "v2-failure-model-filter-months.csv"
SHARED_CSV = HERE / "v2-failure-model-shared-replays.csv"
SUMMARY_JSON = HERE / "v2-failure-model-summary.json"
REPORT_MD = HERE / "v2-failure-model-report.md"

FEATURE_LABELS = {
    "log_signal_volume_ratio": "信号日成交量/20日中位量（对数）",
    "log_flow_to_signal": "CEX流入/信号日成交量（对数）",
    "flow_normal_0_to_5pct": "流入/日常量0%—5%的幅度",
    "flow_normal_above_5pct": "流入/日常量超过5%的幅度",
    "flow_normal_above_10pct": "流入/日常量超过10%的幅度",
    "return_7d": "前7日涨幅",
    "return_7d_above_20pct": "前7日涨幅超过20%的幅度",
    "return_7d_above_50pct": "前7日涨幅超过50%的幅度",
    "recent_7d_runup": "近7日最高涨幅",
    "recent_5x_volume": "此前7日是否出现5倍量",
    "log_prior7_max_volume_ratio": "此前7日最大量倍数（对数）",
    "market_median_return_7d": "币池近7日收益中位数",
    "market_positive_breadth_7d": "币池近7日上涨占比",
    "market_trend_breadth_interaction": "市场趋势×上涨广度",
}

FEATURE_GROUPS = {
    "volume": [
        "log_signal_volume_ratio",
        "recent_5x_volume",
        "log_prior7_max_volume_ratio",
    ],
    "cex": [
        "log_flow_to_signal",
        "flow_normal_0_to_5pct",
        "flow_normal_above_5pct",
        "flow_normal_above_10pct",
    ],
    "price_action": [
        "return_7d",
        "return_7d_above_20pct",
        "return_7d_above_50pct",
        "recent_7d_runup",
    ],
    "market": [
        "market_median_return_7d",
        "market_positive_breadth_7d",
        "market_trend_breadth_interaction",
    ],
}
FEATURE_GROUPS["full"] = [
    feature for group in ("volume", "cex", "price_action", "market")
    for feature in FEATURE_GROUPS[group]
]

KEEP_FRACTIONS = (0.25, 0.40, 0.50, 0.67, 0.75)
MIN_TRAIN = 40


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


def clip(value: float, lower: float, upper: float) -> float:
    return max(lower, min(upper, value))


def signed_log_scaled(value: float, scale: float) -> float:
    return math.copysign(math.log1p(abs(value) / scale), value)


def engineered(row: dict[str, Any]) -> dict[str, float]:
    volume_ratio = clip(float(row["signal_volume_ratio20"]), 0.10, 20.0)
    flow_signal = clip(float(row["flow_to_signal_volume"]), -1.0, 1.0)
    flow_normal = clip(float(row["flow_to_median20_volume"]), -1.0, 1.0)
    return_7d = clip(float(row["return_7d"]), -0.60, 1.50)
    runup = clip(float(row["recent_7d_runup"]), -0.20, 2.00)
    prior_volume = clip(float(row["prior7_max_volume_ratio"]), 0.10, 30.0)
    market_return = clip(float(row["market_median_return_7d"]), -0.25, 0.20)
    breadth = clip(float(row["market_positive_breadth_7d"]), 0.0, 1.0)
    return {
        "log_signal_volume_ratio": math.log(volume_ratio),
        "log_flow_to_signal": signed_log_scaled(flow_signal, 0.01),
        "flow_normal_0_to_5pct": clip(flow_normal / 0.05, -10.0, 1.0),
        "flow_normal_above_5pct": clip(max(flow_normal - 0.05, 0.0) / 0.05, 0.0, 10.0),
        "flow_normal_above_10pct": clip(max(flow_normal - 0.10, 0.0) / 0.10, 0.0, 9.0),
        "return_7d": return_7d,
        "return_7d_above_20pct": max(return_7d - 0.20, 0.0),
        "return_7d_above_50pct": max(return_7d - 0.50, 0.0),
        "recent_7d_runup": runup,
        "recent_5x_volume": float(str(row["recent_5x_volume"]).lower() in {"1", "true"}),
        "log_prior7_max_volume_ratio": math.log(prior_volume),
        "market_median_return_7d": market_return,
        "market_positive_breadth_7d": breadth,
        "market_trend_breadth_interaction": market_return * (breadth - 0.5),
    }


def percentile(values: list[float], q: float) -> float:
    ordered = sorted(values)
    if not ordered:
        return float("nan")
    position = (len(ordered) - 1) * q
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    weight = position - lower
    return ordered[lower] * (1 - weight) + ordered[upper] * weight


def sigmoid(value: float) -> float:
    return 1.0 / (1.0 + math.exp(-clip(value, -30.0, 30.0)))


def fit_logistic(
    rows: list[dict[str, Any]], features: list[str], penalty: float = 0.8
) -> dict[str, Any]:
    raw = [[row["engineered"][feature] for feature in features] for row in rows]
    labels = [float(row["profitable"]) for row in rows]
    means = [statistics.mean(column) for column in zip(*raw, strict=True)]
    stds = [statistics.pstdev(column) for column in zip(*raw, strict=True)]
    stds = [value if value >= 1e-9 else 1.0 for value in stds]
    design = [
        [1.0] + [(value - mean) / std for value, mean, std in zip(vector, means, stds, strict=True)]
        for vector in raw
    ]
    weights = [0.0] * (len(features) + 1)
    rate = 0.08
    previous_loss = float("inf")
    for iteration in range(6000):
        probabilities = [sigmoid(sum(w * x for w, x in zip(weights, vector, strict=True))) for vector in design]
        gradients = []
        for column in range(len(weights)):
            gradient = sum(
                (probability - label) * vector[column]
                for probability, label, vector in zip(probabilities, labels, design, strict=True)
            ) / len(rows)
            if column:
                gradient += penalty * weights[column] / len(rows)
            gradients.append(gradient)
        weights = [weight - rate * gradient for weight, gradient in zip(weights, gradients, strict=True)]
        if iteration % 100 == 0:
            loss = -sum(
                label * math.log(max(probability, 1e-12))
                + (1 - label) * math.log(max(1 - probability, 1e-12))
                for probability, label in zip(probabilities, labels, strict=True)
            ) / len(rows)
            loss += penalty * sum(weight * weight for weight in weights[1:]) / (2 * len(rows))
            if abs(previous_loss - loss) < 1e-10:
                break
            previous_loss = loss
    model = {"features": features, "means": means, "stds": stds, "weights": weights}
    model["train_probabilities"] = predict(model, rows)
    return model


def predict(model: dict[str, Any], rows: list[dict[str, Any]]) -> list[float]:
    output = []
    for row in rows:
        vector = [row["engineered"][feature] for feature in model["features"]]
        standardized = [
            (value - mean) / std
            for value, mean, std in zip(vector, model["means"], model["stds"], strict=True)
        ]
        output.append(sigmoid(model["weights"][0] + sum(
            weight * value
            for weight, value in zip(model["weights"][1:], standardized, strict=True)
        )))
    return output


def auc(labels: list[int], probabilities: list[float]) -> float:
    positives = [probability for label, probability in zip(labels, probabilities, strict=True) if label]
    negatives = [probability for label, probability in zip(labels, probabilities, strict=True) if not label]
    if not positives or not negatives:
        return float("nan")
    score = sum((positive > negative) + 0.5 * (positive == negative) for positive in positives for negative in negatives)
    return score / (len(positives) * len(negatives))


def brier(labels: list[int], probabilities: list[float]) -> float:
    return statistics.mean((probability - label) ** 2 for label, probability in zip(labels, probabilities, strict=True))


def log_loss(labels: list[int], probabilities: list[float]) -> float:
    return -statistics.mean(
        label * math.log(max(probability, 1e-12))
        + (1 - label) * math.log(max(1 - probability, 1e-12))
        for label, probability in zip(labels, probabilities, strict=True)
    )


def prepare_rows() -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    attribution = {row["case_id"]: row for row in read_csv(ATTRIBUTION_INPUT)}
    for raw in read_csv(FEATURE_INPUT):
        outcome = attribution.get(raw["case_id"])
        completed = bool(outcome and outcome["right_censored_mark"].lower() == "false")
        rows.append(
            {
                **raw,
                "completed": completed,
                "profitable": int(completed and float(outcome["net_pnl"]) > 0),
                "target_return_2x": (
                    float(outcome["net_pnl"]) / float(outcome["margin"]) if completed else None
                ),
                "exit_time_utc": outcome["exit_time_utc"] if completed else "",
                "engineered": engineered(raw),
            }
        )
    return sorted(rows, key=lambda row: (row["entry_date"], row["case_id"]))


def walk_forward(
    rows: list[dict[str, Any]], group_name: str
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    features = FEATURE_GROUPS[group_name]
    predictions: list[dict[str, Any]] = []
    folds: list[dict[str, Any]] = []
    coefficients: list[dict[str, Any]] = []
    months = sorted({row["entry_date"][:7] for row in rows})
    for month in months:
        month_start = datetime.fromisoformat(month + "-01T00:00:00+00:00")
        train = [
            row for row in rows
            if row["completed"] and datetime.fromisoformat(row["exit_time_utc"]) < month_start
        ]
        test_all = [row for row in rows if row["entry_date"][:7] == month]
        if len(train) < MIN_TRAIN or len({row["profitable"] for row in train}) < 2:
            continue
        model = fit_logistic(train, features)
        probabilities = predict(model, test_all)
        thresholds = {
            fraction: percentile(model["train_probabilities"], 1 - fraction)
            for fraction in KEEP_FRACTIONS
        }
        fold_predictions = []
        for row, probability in zip(test_all, probabilities, strict=True):
            output = {
                "case_id": row["case_id"],
                "symbol": row["symbol"],
                "signal_date": row["signal_date"],
                "entry_date": row["entry_date"],
                "fold_month": month,
                "train_count": len(train),
                "completed": row["completed"],
                "profitable": row["profitable"] if row["completed"] else "",
                "target_return_2x": row["target_return_2x"] if row["completed"] else "",
                "success_probability": probability,
                "failure_probability": 1 - probability,
            }
            for fraction, threshold in thresholds.items():
                output[f"select_top_{round(fraction * 100)}"] = probability >= threshold
                output[f"threshold_top_{round(fraction * 100)}"] = threshold
            fold_predictions.append(output)
        predictions.extend(fold_predictions)
        completed_test = [row for row in fold_predictions if row["completed"]]
        if completed_test:
            labels = [int(row["profitable"]) for row in completed_test]
            scores = [float(row["success_probability"]) for row in completed_test]
            folds.append(
                {
                    "model": group_name,
                    "month": month,
                    "train_count": len(train),
                    "test_signals": len(test_all),
                    "completed_test": len(completed_test),
                    "base_win_rate": statistics.mean(labels),
                    "auc": auc(labels, scores),
                    "brier": brier(labels, scores),
                    "log_loss": log_loss(labels, scores),
                }
            )
        for feature, coefficient in zip(features, model["weights"][1:], strict=True):
            coefficients.append(
                {
                    "model": group_name,
                    "month": month,
                    "feature": feature,
                    "label": FEATURE_LABELS[feature],
                    "standardized_success_coefficient": coefficient,
                }
            )
    return predictions, folds, coefficients


def aggregate_model_metrics(predictions: list[dict[str, Any]], group_name: str) -> dict[str, Any]:
    completed = [row for row in predictions if row["completed"]]
    labels = [int(row["profitable"]) for row in completed]
    probabilities = [float(row["success_probability"]) for row in completed]
    base_probability = statistics.mean(labels)
    return {
        "model": group_name,
        "oos_completed_trades": len(completed),
        "oos_months": ",".join(sorted({row["fold_month"] for row in completed})),
        "base_win_rate": base_probability,
        "auc": auc(labels, probabilities),
        "brier": brier(labels, probabilities),
        "constant_base_brier": statistics.mean((base_probability - label) ** 2 for label in labels),
        "log_loss": log_loss(labels, probabilities),
    }


def filter_results(predictions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    completed = [row for row in predictions if row["completed"]]
    output = []
    scenarios = [("no_filter", "不筛选", lambda row: True)] + [
        (
            f"top_{round(fraction * 100)}",
            f"保留模型评分最高{round(fraction * 100)}%附近",
            lambda row, fraction=fraction: bool(row[f"select_top_{round(fraction * 100)}"]),
        )
        for fraction in KEEP_FRACTIONS
    ]
    scenarios.extend(
        [
            (
                "bottom_50",
                "模型评分较低约50%（反向对照）",
                lambda row: not bool(row["select_top_50"]),
            ),
            (
                "bottom_25",
                "模型评分最低约25%（反向对照）",
                lambda row: float(row["success_probability"])
                <= float(row["threshold_top_75"]),
            ),
        ]
    )
    total_winners = sum(int(row["profitable"]) for row in completed)
    total_failures = len(completed) - total_winners
    for name, label, predicate in scenarios:
        selected = [row for row in completed if predicate(row)]
        returns = [float(row["target_return_2x"]) for row in selected]
        selected_winners = sum(int(row["profitable"]) for row in selected)
        selected_failures = len(selected) - selected_winners
        output.append(
            {
                "scenario": name,
                "label": label,
                "trades": len(selected),
                "retention": len(selected) / len(completed),
                "win_rate": selected_winners / len(selected),
                "mean_return_2x": statistics.mean(returns),
                "median_return_2x": statistics.median(returns),
                "failure_rejected_rate": 1 - selected_failures / total_failures,
                "winner_rejected_rate": 1 - selected_winners / total_winners,
                "positive_months": sum(
                    statistics.mean(
                        float(row["target_return_2x"])
                        for row in selected if row["fold_month"] == month
                    ) > 0
                    for month in sorted({row["fold_month"] for row in selected})
                ),
                "months": len({row["fold_month"] for row in selected}),
            }
        )
    return output


def monthly_filter_results(predictions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    completed = [row for row in predictions if row["completed"]]
    output = []
    scenarios = [
        ("no_filter", lambda row: True),
        ("top_25", lambda row: bool(row["select_top_25"])),
        ("top_50", lambda row: bool(row["select_top_50"])),
        ("top_67", lambda row: bool(row["select_top_67"])),
    ]
    for month in sorted({row["fold_month"] for row in completed}):
        month_rows = [row for row in completed if row["fold_month"] == month]
        for name, predicate in scenarios:
            selected = [row for row in month_rows if predicate(row)]
            returns = [float(row["target_return_2x"]) for row in selected]
            output.append(
                {
                    "month": month,
                    "scenario": name,
                    "trades": len(selected),
                    "win_rate": statistics.mean(int(row["profitable"]) for row in selected),
                    "mean_return_2x": statistics.mean(returns),
                    "median_return_2x": statistics.median(returns),
                }
            )
    return output


def replay_shared(predictions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    e2e = load_module("v2_failure_model_e2e", HERE / "run_v2_end_to_end.py")
    weighted = load_module("v2_failure_model_weighted", OLD / "backtest_portfolio_weighted.py")
    candidates = read_csv(HERE / "v2-all-signal-input-trades.csv")
    summaries = read_csv(HERE / "v2-all-signal-exit-trades.csv")
    events = read_csv(HERE / "v2-all-signal-exit-events.csv")
    cases, by_events = e2e.case_inputs(candidates, summaries, events)
    prediction_by_id = {row["case_id"]: row for row in predictions}
    manifest = json.loads((HERE / "intraday-all-signals-data/manifest.json").read_text(encoding="utf-8"))
    manifest_by_id = {case["case_id"]: case for case in manifest["cases"]}
    scenarios: list[tuple[str, str, set[str]]] = [
        ("no_filter", "OOS月份不筛选", set(prediction_by_id))
    ]
    for fraction in KEEP_FRACTIONS:
        key = f"select_top_{round(fraction * 100)}"
        scenarios.append(
            (
                f"top_{round(fraction * 100)}",
                f"保留模型评分最高{round(fraction * 100)}%附近",
                {case_id for case_id, row in prediction_by_id.items() if bool(row[key])},
            )
        )
    scenarios.extend(
        [
            (
                "bottom_50",
                "模型评分较低约50%（反向对照）",
                {
                    case_id for case_id, row in prediction_by_id.items()
                    if not bool(row["select_top_50"])
                },
            ),
            (
                "bottom_25",
                "模型评分最低约25%（反向对照）",
                {
                    case_id for case_id, row in prediction_by_id.items()
                    if float(row["success_probability"]) <= float(row["threshold_top_75"])
                },
            ),
        ]
    )
    needed_ids = set().union(*(selected for _, _, selected in scenarios))
    bar_maps = {
        case_id: weighted.read_bar_map(
            HERE / "intraday-all-signals-data" / manifest_by_id[case_id]["intervals"]["5m"]["file"]
        )
        for case_id in needed_ids
    }
    output = []

    def run_scenario(name: str, label: str, selected_ids: set[str]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        selected_cases = [case for case in cases if case["case_id"] in selected_ids]
        entries, exits, skipped = e2e.select_portfolio(selected_cases, by_events)
        result = weighted.simulate(name, entries, exits, {case_id: bar_maps[case_id] for case_id in selected_ids})
        attribution = result["attribution"]
        return (
            {
                "scenario": name,
                "label": label,
                "candidate_signals": len(selected_ids),
                "executed_trades": len(entries),
                "executed_symbols": len({row["symbol"] for row in entries}),
                "skipped_signals": len(skipped),
                "win_rate": sum(float(row["total_net_pnl"]) > 0 for row in attribution) / len(attribution),
                "total_return": result["total_return"],
                "max_drawdown_5m": result["max_drawdown_5m"],
            },
            attribution,
        )

    attribution_by_scenario: dict[str, list[dict[str, Any]]] = {}
    ids_by_scenario = {name: selected for name, _, selected in scenarios}
    for name, label, selected_ids in scenarios:
        result_row, attribution = run_scenario(name, label, selected_ids)
        output.append(result_row)
        attribution_by_scenario[name] = attribution

    by_month: dict[str, list[str]] = defaultdict(list)
    for case_id, row in prediction_by_id.items():
        by_month[row["fold_month"]].append(case_id)
    for fraction in (25, 50):
        scenario_name = f"top_{fraction}"
        selected_ids = ids_by_scenario[scenario_name]
        scenario_attribution = attribution_by_scenario[scenario_name]
        best_case_id = max(
            scenario_attribution, key=lambda row: float(row["total_net_pnl"])
        )["case_id"]
        without_best, _ = run_scenario(
            f"{scenario_name}_without_best_trade",
            f"Top {fraction}%移除最佳交易（{best_case_id}）",
            selected_ids - {best_case_id},
        )
        output.append(without_best)

        selected_by_month: dict[str, list[str]] = defaultdict(list)
        for case_id in selected_ids:
            selected_by_month[prediction_by_id[case_id]["fold_month"]].append(case_id)
        random_results = []
        for seed in range(30):
            rng = random.Random(20260805 + fraction * 100 + seed)
            random_ids: set[str] = set()
            for month, month_ids in by_month.items():
                random_ids.update(rng.sample(month_ids, len(selected_by_month[month])))
            random_row, _ = run_scenario(
                f"random_top{fraction}_seed_{seed}",
                "按月等数量随机对照",
                random_ids,
            )
            random_results.append(random_row)
        random_returns = [float(row["total_return"]) for row in random_results]
        random_drawdowns = [float(row["max_drawdown_5m"]) for row in random_results]
        random_wins = [float(row["win_rate"]) for row in random_results]
        random_executed = [float(row["executed_trades"]) for row in random_results]
        model_return = next(
            float(row["total_return"]) for row in output if row["scenario"] == scenario_name
        )
        output.append(
            {
                "scenario": f"random_top{fraction}_30_median",
                "label": f"Top {fraction}%等密度随机信号（30次中位）",
                "candidate_signals": len(selected_ids),
                "executed_trades": statistics.median(random_executed),
                "executed_symbols": "",
                "skipped_signals": "",
                "win_rate": statistics.median(random_wins),
                "total_return": statistics.median(random_returns),
                "max_drawdown_5m": statistics.median(random_drawdowns),
                "random_return_p10": percentile(random_returns, 0.10),
                "random_return_p90": percentile(random_returns, 0.90),
                "model_return_percentile_vs_random": sum(value <= model_return for value in random_returns) / len(random_returns),
            }
        )
    return output


def fmt(value: float) -> str:
    return f"{value * 100:.2f}%"


def render_report(
    ablations: list[dict[str, Any]],
    folds: list[dict[str, Any]],
    coefficients: list[dict[str, Any]],
    filters: list[dict[str, Any]],
    monthly_filters: list[dict[str, Any]],
    shared: list[dict[str, Any]],
) -> str:
    full = next(row for row in ablations if row["model"] == "full")
    by_feature: dict[str, list[float]] = defaultdict(list)
    for row in coefficients:
        by_feature[row["feature"]].append(float(row["standardized_success_coefficient"]))
    feature_summary = []
    for feature, values in by_feature.items():
        feature_summary.append(
            {
                "feature": feature,
                "mean": statistics.mean(values),
                "positive_folds": sum(value > 0 for value in values),
                "folds": len(values),
            }
        )
    feature_summary.sort(key=lambda row: abs(row["mean"]), reverse=True)
    lines = [
        "# V2失败概率模型：月度走前验证",
        "",
        "## 方法",
        "",
        "- 模型为L2正则逻辑回归，预测交易盈利概率；失败概率为1减去盈利概率。",
        "- 仅使用信号日收盘时已知数据；每月训练集只包含该月开始前已经退出的交易。",
        "- 成交量和流入比例使用截断对数；对流入超过日常量5%/10%、前7日涨幅超过20%/50%加入非线性铰链项。",
        "- OOS从2026-02开始；2026-01没有更早V2交易可训练，未伪造冷启动结果。",
        "- 过滤阈值由当月训练集评分分位数确定，不使用测试月盈亏。",
        "",
        "## 特征组消融",
        "",
        "| 模型 | OOS交易 | AUC | Brier | 常数基准Brier |",
        "|---|---:|---:|---:|---:|",
    ]
    for row in ablations:
        lines.append(
            f"| {row['model']} | {row['oos_completed_trades']} | {float(row['auc']):.3f} | "
            f"{float(row['brier']):.3f} | {float(row['constant_base_brier']):.3f} |"
        )
    lines.extend(
        [
            "",
            "## Full模型逐月OOS",
            "",
            "| 月份 | 训练交易 | 测试交易 | 基准胜率 | AUC | Brier |",
            "|---|---:|---:|---:|---:|---:|",
        ]
    )
    for row in folds:
        if row["model"] != "full":
            continue
        lines.append(
            f"| {row['month']} | {row['train_count']} | {row['completed_test']} | "
            f"{fmt(float(row['base_win_rate']))} | {float(row['auc']):.3f} | {float(row['brier']):.3f} |"
        )
    lines.extend(
        [
            "",
            "## OOS逐笔过滤结果",
            "",
            "| 条件 | 交易 | 保留率 | 胜率 | 单笔均值 | 单笔中位数 | 拒绝失败 | 错拒盈利 | 正收益月份 |",
            "|---|---:|---:|---:|---:|---:|---:|---:|---:|",
        ]
    )
    for row in filters:
        lines.append(
            f"| {row['label']} | {row['trades']} | {fmt(float(row['retention']))} | "
            f"{fmt(float(row['win_rate']))} | {fmt(float(row['mean_return_2x']))} | "
            f"{fmt(float(row['median_return_2x']))} | {fmt(float(row['failure_rejected_rate']))} | "
            f"{fmt(float(row['winner_rejected_rate']))} | {row['positive_months']}/{row['months']} |"
        )
    month_by_key = {
        (row["month"], row["scenario"]): row for row in monthly_filters
    }
    lines.extend(
        [
            "",
            "## Top 50%规则逐月收益",
            "",
            "| 月份 | 基准交易/均值 | Top 50%交易/均值 |",
            "|---|---:|---:|",
        ]
    )
    for month in sorted({row["month"] for row in monthly_filters}):
        baseline = month_by_key[(month, "no_filter")]
        selected = month_by_key[(month, "top_50")]
        lines.append(
            f"| {month} | {baseline['trades']} / {fmt(float(baseline['mean_return_2x']))} | "
            f"{selected['trades']} / {fmt(float(selected['mean_return_2x']))} |"
        )
    lines.extend(
        [
            "",
            "## OOS共享三仓重放",
            "",
            "| 条件 | 候选信号 | 执行交易 | 胜率 | 总收益 | MDD |",
            "|---|---:|---:|---:|---:|---:|",
        ]
    )
    for row in shared:
        lines.append(
            f"| {row['label']} | {row['candidate_signals']} | {row['executed_trades']} | "
            f"{fmt(float(row['win_rate']))} | {fmt(float(row['total_return']))} | "
            f"{fmt(float(row['max_drawdown_5m']))} |"
        )
    lines.extend(
        [
            "",
            "## 稳健性检查",
            "",
        ]
    )
    for fraction in (25, 50):
        without_best = next(
            row for row in shared if row["scenario"] == f"top_{fraction}_without_best_trade"
        )
        random_control = next(
            row for row in shared if row["scenario"] == f"random_top{fraction}_30_median"
        )
        lines.extend(
            [
                f"- Top {fraction}%移除最佳交易后：收益{fmt(float(without_best['total_return']))}、MDD{fmt(float(without_best['max_drawdown_5m']))}。",
                f"- Top {fraction}%等密度随机对照：收益中位数{fmt(float(random_control['total_return']))}，10%—90%区间[{fmt(float(random_control['random_return_p10']))}, {fmt(float(random_control['random_return_p90']))}]；模型分位{fmt(float(random_control['model_return_percentile_vs_random']))}。",
            ]
        )
    lines.extend(
        [
            "",
            "## 标准化系数稳定性",
            "",
            "正系数表示更可能盈利，负系数表示更可能失败。",
            "",
            "| 特征 | 平均系数 | 正系数月份 |",
            "|---|---:|---:|",
        ]
    )
    for row in feature_summary:
        lines.append(
            f"| {FEATURE_LABELS[row['feature']]} | {row['mean']:+.3f} | "
            f"{row['positive_folds']}/{row['folds']} |"
        )
    candidate_filters = [row for row in filters if row["scenario"].startswith("top_")]
    candidate_shared = [row for row in shared if row["scenario"].startswith("top_")]
    best_filter = max(candidate_filters, key=lambda row: float(row["mean_return_2x"]))
    best_shared = max(candidate_shared, key=lambda row: float(row["total_return"]))
    lines.extend(
        [
            "",
            "## 结论",
            "",
            f"- Full模型总体OOS AUC为{float(full['auc']):.3f}，Brier为{float(full['brier']):.3f}；应结合逐月稳定性判断，而不是只看总体数字。",
            f"- 逐笔均值最高的模型分位规则是“{best_filter['label']}”，OOS单笔均值{fmt(float(best_filter['mean_return_2x']))}。",
            f"- 三仓重放收益最高的模型规则是“{best_shared['label']}”，总收益{fmt(float(best_shared['total_return']))}、MDD{fmt(float(best_shared['max_drawdown_5m']))}。",
            "- 当前AUC低于0.5且Brier差于常数基准；分位过滤的改善主要集中在4月，并依赖单笔大赢家，随机对照也未显示显著优势。当前版本只能作为研究评分，不能直接上线。",
            "",
            "## 数据产物",
            "",
            f"- OOS预测：`{PREDICTIONS_CSV.name}`",
            f"- 逐月指标：`{FOLDS_CSV.name}`",
            f"- 系数：`{FEATURES_CSV.name}`",
            f"- 消融：`{ABLATION_CSV.name}`",
            f"- 逐笔过滤：`{FILTER_CSV.name}`",
            f"- 过滤逐月：`{MONTH_FILTER_CSV.name}`",
            f"- 三仓重放：`{SHARED_CSV.name}`",
        ]
    )
    return "\n".join(lines) + "\n"


def main() -> None:
    rows = prepare_rows()
    all_folds: list[dict[str, Any]] = []
    all_coefficients: list[dict[str, Any]] = []
    ablations = []
    full_predictions: list[dict[str, Any]] = []
    for group_name in ("volume", "cex", "price_action", "market", "full"):
        predictions, folds, coefficients = walk_forward(rows, group_name)
        ablations.append(aggregate_model_metrics(predictions, group_name))
        all_folds.extend(folds)
        if group_name == "full":
            full_predictions = predictions
            all_coefficients = coefficients
    filters = filter_results(full_predictions)
    monthly_filters = monthly_filter_results(full_predictions)
    shared = replay_shared(full_predictions)
    write_csv(PREDICTIONS_CSV, full_predictions)
    write_csv(FOLDS_CSV, all_folds)
    write_csv(FEATURES_CSV, all_coefficients)
    write_csv(ABLATION_CSV, ablations)
    write_csv(FILTER_CSV, filters)
    write_csv(MONTH_FILTER_CSV, monthly_filters)
    write_csv(SHARED_CSV, shared)
    summary = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "method": "monthly_expanding_walk_forward_logistic_with_splines",
        "cold_start_month_excluded": "2026-01",
        "ablations": ablations,
        "filter_results": filters,
        "monthly_filter_results": monthly_filters,
        "shared_replays": shared,
    }
    SUMMARY_JSON.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    REPORT_MD.write_text(
        render_report(ablations, all_folds, all_coefficients, filters, monthly_filters, shared),
        encoding="utf-8",
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
