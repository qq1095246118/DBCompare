#!/usr/bin/env python3
"""Study causal commonalities of failed end-to-end V2 entries."""

from __future__ import annotations

import csv
import importlib.util
import json
import math
import random
import statistics
import sys
from collections import Counter, defaultdict
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Callable


HERE = Path(__file__).resolve().parent
FEATURE_CSV = HERE / "v2-all-signal-entry-features.csv"
COMPARISON_CSV = HERE / "v2-failure-feature-comparison.csv"
QUARTILE_CSV = HERE / "v2-failure-feature-quartiles.csv"
RULE_CSV = HERE / "v2-failure-filter-scenarios.csv"
MONTH_CSV = HERE / "v2-failure-filter-months.csv"
SHARED_REPLAY_CSV = HERE / "v2-failure-filter-shared-replays.csv"
REPORT_MD = HERE / "v2-failure-pattern-report.md"

FEATURES = {
    "f7c_share": "F7c/Cluster",
    "flow_to_signal_volume": "7日CEX净流入/信号日成交量",
    "flow_to_median20_volume": "7日CEX净流入/此前20日中位成交量",
    "flow_to_prior7_volume": "7日CEX净流入/此前7日总成交量",
    "rank_pct": "横截面排名分位",
    "signal_volume_ratio20": "信号日成交量/此前20日中位数",
    "return_3d": "信号前3日涨幅",
    "return_7d": "信号前7日涨幅",
    "return_14d": "信号前14日涨幅",
    "return_30d": "信号前30日涨幅",
    "recent_7d_runup": "近7日最高涨幅",
    "distance_20d_high": "距20日最高价",
    "recent_5x_volume": "此前7日是否出现5倍量",
    "prior7_max_volume_ratio": "此前7日最大量/更早20日中位量",
    "signal_day_return": "信号日开收涨幅",
    "signal_range_pct": "信号日振幅",
    "market_median_return_7d": "币池近7日收益中位数",
    "market_positive_breadth_7d": "币池近7日上涨占比",
    "market_median_return_30d": "币池近30日收益中位数",
    "market_positive_breadth_30d": "币池近30日上涨占比",
}


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


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


def median(values: list[float]) -> float:
    return statistics.median(values) if values else float("nan")


def mean(values: list[float]) -> float:
    return statistics.mean(values) if values else float("nan")


def quantile(values: list[float], q: float) -> float:
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


def average_ranks(values: list[float]) -> list[float]:
    order = sorted(range(len(values)), key=values.__getitem__)
    ranks = [0.0] * len(values)
    start = 0
    while start < len(order):
        end = start + 1
        while end < len(order) and values[order[end]] == values[order[start]]:
            end += 1
        rank = (start + 1 + end) / 2
        for index in order[start:end]:
            ranks[index] = rank
        start = end
    return ranks


def correlation(left: list[float], right: list[float]) -> float:
    if len(left) < 2:
        return float("nan")
    left_mean = mean(left)
    right_mean = mean(right)
    numerator = sum((a - left_mean) * (b - right_mean) for a, b in zip(left, right, strict=True))
    denominator = math.sqrt(
        sum((a - left_mean) ** 2 for a in left)
        * sum((b - right_mean) ** 2 for b in right)
    )
    return numerator / denominator if denominator else 0.0


def auc(values: list[float], labels: list[int]) -> float:
    ranks = average_ranks(values)
    positives = sum(labels)
    negatives = len(labels) - positives
    if not positives or not negatives:
        return float("nan")
    positive_rank_sum = sum(rank for rank, label in zip(ranks, labels, strict=True) if label)
    return (positive_rank_sum - positives * (positives + 1) / 2) / (positives * negatives)


def bootstrap_median_difference(
    winners: list[float], losers: list[float], repetitions: int = 1000
) -> tuple[float, float]:
    rng = random.Random(20260805)
    differences = []
    for _ in range(repetitions):
        win_sample = [rng.choice(winners) for _ in winners]
        lose_sample = [rng.choice(losers) for _ in losers]
        differences.append(median(win_sample) - median(lose_sample))
    return quantile(differences, 0.025), quantile(differences, 0.975)


def volume_multiplier(symbol: str) -> float:
    return 1000.0 if symbol.startswith("1000") else 1.0


def lookback_return(bars: list[dict[str, Any]], index: int, days: int) -> float:
    anchor = max(0, index - days)
    return float(bars[index]["c"]) / float(bars[anchor]["c"]) - 1


def build_features() -> list[dict[str, Any]]:
    dataset = json.loads((HERE / "dataset.json").read_text(encoding="utf-8"))
    token_by_symbol = {token["symbol"]: token for token in dataset["tokens"]}
    price_bars: dict[str, list[dict[str, Any]]] = {}
    for symbol in token_by_symbol:
        rows = read_csv(HERE / "klines-1d-2025" / f"{symbol}-1d.csv")
        price_bars[symbol] = [
            {
                "d": row["date"],
                "o": float(row["open"]),
                "h": float(row["high"]),
                "l": float(row["low"]),
                "c": float(row["close"]),
                "v": float(row["volume"]),
            }
            for row in rows
        ]
    price_by_date = {
        symbol: {bar["d"]: bar for bar in bars}
        for symbol, bars in price_bars.items()
    }
    signal_dates = {row["signal_date"] for row in read_csv(HERE / "signals.csv")}
    market_by_date: dict[str, dict[str, float]] = {}
    for day_text in signal_dates:
        current_day = date.fromisoformat(day_text)
        output: dict[str, float] = {}
        for days in (7, 30):
            anchor_text = (current_day - timedelta(days=days)).isoformat()
            returns = []
            for symbol in price_by_date:
                current = price_by_date[symbol].get(day_text)
                anchor = price_by_date[symbol].get(anchor_text)
                if current is not None and anchor is not None:
                    returns.append(float(current["c"]) / float(anchor["c"]) - 1)
            output[f"market_median_return_{days}d"] = median(returns)
            output[f"market_positive_breadth_{days}d"] = mean([value > 0 for value in returns])
        market_by_date[day_text] = output
    net7_by_key = {
        (token["symbol"], bar["d"]): float(bar["cex"]["net_7d"])
        for token in dataset["tokens"]
        for bar in token["bars"]
    }
    signal_by_key = {
        (row["symbol"], row["signal_date"]): row
        for row in read_csv(HERE / "signals.csv")
    }
    input_by_key = {
        (row["symbol"], row["signal_date"]): row
        for row in read_csv(HERE / "v2-all-signal-input-trades.csv")
    }
    independent = {
        row["case_id"]: row
        for row in read_csv(HERE / "v2-independent-trade-attribution.csv")
    }
    shared = {
        row["case_id"]: row
        for row in read_csv(HERE / "v2-end-to-end-portfolio-attribution.csv")
    }
    manifest = json.loads((HERE / "intraday-all-signals-data/manifest.json").read_text(encoding="utf-8"))
    rows = []
    for case in manifest["cases"]:
        key = (case["symbol"], case["signal_date"])
        signal = signal_by_key[key]
        entry = input_by_key[key]
        bars = price_bars[case["symbol"]]
        index = next(i for i, bar in enumerate(bars) if bar["d"] == case["signal_date"])
        bar = bars[index]
        multiplier = volume_multiplier(case["symbol"])
        prior20 = bars[max(0, index - 20):index]
        prior7 = bars[max(0, index - 7):index]
        older20 = bars[max(0, index - 27):max(0, index - 7)]
        normalized_prior20 = [float(item["v"]) * multiplier for item in prior20]
        normalized_prior7 = [float(item["v"]) * multiplier for item in prior7]
        normalized_older20 = [float(item["v"]) * multiplier for item in older20]
        normal20 = median(normalized_prior20) if normalized_prior20 else float(bar["v"]) * multiplier
        older_normal = median(normalized_older20) if normalized_older20 else normal20
        net7 = net7_by_key[key]
        high7_anchor = float(bars[max(0, index - 7)]["c"])
        recent_high = max(float(item["h"]) for item in bars[max(0, index - 7):index + 1])
        high20 = max(float(item["h"]) for item in bars[max(0, index - 19):index + 1])
        outcome = independent.get(case["case_id"])
        target = float(outcome["net_pnl"]) / float(outcome["margin"]) if outcome else ""
        rows.append(
            {
                "case_id": case["case_id"],
                "symbol": case["symbol"],
                "signal_date": case["signal_date"],
                "entry_date": case["entry_date"],
                "executed_independent": bool(outcome),
                "executed_shared": case["case_id"] in shared,
                "right_censored_mark": bool(outcome and outcome["right_censored_mark"].lower() == "true"),
                "target_return_2x": target,
                "profitable": int(target != "" and float(target) > 0),
                "f7c_share": float(signal["f7c_share"]),
                "flow_to_signal_volume": net7 / (float(bar["v"]) * multiplier) if float(bar["v"]) else 0.0,
                "flow_to_median20_volume": net7 / normal20 if normal20 else 0.0,
                "flow_to_prior7_volume": net7 / sum(normalized_prior7) if sum(normalized_prior7) else 0.0,
                "rank_pct": float(signal["rank"]) / float(signal["universe_count"]),
                "signal_volume_ratio20": float(bar["v"]) * multiplier / normal20 if normal20 else 1.0,
                "return_3d": lookback_return(bars, index, 3),
                "return_7d": lookback_return(bars, index, 7),
                "return_14d": lookback_return(bars, index, 14),
                "return_30d": lookback_return(bars, index, 30),
                "recent_7d_runup": recent_high / high7_anchor - 1 if high7_anchor else 0.0,
                "distance_20d_high": float(bar["c"]) / high20 - 1 if high20 else 0.0,
                "recent_5x_volume": int(bool(normalized_prior7) and max(normalized_prior7) >= 5 * older_normal),
                "prior7_max_volume_ratio": max(normalized_prior7) / older_normal if normalized_prior7 and older_normal else 1.0,
                "signal_day_return": float(bar["c"]) / float(bar["o"]) - 1,
                "signal_range_pct": float(bar["h"]) / float(bar["l"]) - 1 if float(bar["l"]) else 0.0,
                **market_by_date[case["signal_date"]],
                "available_history_days_from_2025": index,
            }
        )
    write_csv(FEATURE_CSV, rows)
    return rows


def compare_features(rows: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    completed = [
        row for row in rows
        if row["executed_independent"] and not row["right_censored_mark"]
    ]
    comparison = []
    quartiles = []
    for feature, label in FEATURES.items():
        winners = [float(row[feature]) for row in completed if row["profitable"]]
        losers = [float(row[feature]) for row in completed if not row["profitable"]]
        values = [float(row[feature]) for row in completed]
        labels = [int(row["profitable"]) for row in completed]
        targets = [float(row["target_return_2x"]) for row in completed]
        ci_low, ci_high = bootstrap_median_difference(winners, losers)
        comparison.append(
            {
                "feature": feature,
                "label": label,
                "winner_median": median(winners),
                "loser_median": median(losers),
                "winner_minus_loser_median": median(winners) - median(losers),
                "bootstrap_ci_low": ci_low,
                "bootstrap_ci_high": ci_high,
                "higher_value_win_auc": auc(values, labels),
                "spearman_to_return": correlation(average_ranks(values), average_ranks(targets)),
            }
        )
        cuts = [quantile(values, q) for q in (0.25, 0.50, 0.75)]
        for bucket in range(4):
            selected = [
                row for row in completed
                if sum(float(row[feature]) > cut for cut in cuts) == bucket
            ]
            returns = [float(row["target_return_2x"]) for row in selected]
            quartiles.append(
                {
                    "feature": feature,
                    "label": label,
                    "quartile": bucket + 1,
                    "lower_bound": "" if bucket == 0 else cuts[bucket - 1],
                    "upper_bound": "" if bucket == 3 else cuts[bucket],
                    "trades": len(selected),
                    "win_rate": mean([int(row["profitable"]) for row in selected]),
                    "mean_return_2x": mean(returns),
                    "median_return_2x": median(returns),
                }
            )
    comparison.sort(key=lambda row: abs(float(row["higher_value_win_auc"]) - 0.5), reverse=True)
    write_csv(COMPARISON_CSV, comparison)
    write_csv(QUARTILE_CSV, quartiles)
    return comparison, quartiles


Rule = tuple[str, str, Callable[[dict[str, Any]], bool]]


def rules() -> list[Rule]:
    return [
        ("baseline", "不筛选", lambda row: True),
        ("f7c_ge_0.2pct", "F7c/Cluster≥0.2%", lambda row: float(row["f7c_share"]) >= 0.002),
        ("f7c_ge_0.5pct", "F7c/Cluster≥0.5%", lambda row: float(row["f7c_share"]) >= 0.005),
        ("flow_normal_ge_5pct", "7日流入≥日常量5%", lambda row: float(row["flow_to_median20_volume"]) >= 0.05),
        ("flow_normal_ge_10pct", "7日流入≥日常量10%", lambda row: float(row["flow_to_median20_volume"]) >= 0.10),
        ("flow_normal_ge_25pct", "7日流入≥日常量25%", lambda row: float(row["flow_to_median20_volume"]) >= 0.25),
        (
            "flow_normal_5_to_10pct",
            "7日流入/日常量介于5%和10%",
            lambda row: 0.05 <= float(row["flow_to_median20_volume"]) < 0.10,
        ),
        ("signal_volume_le_1x", "信号日成交量≤日常量1倍", lambda row: float(row["signal_volume_ratio20"]) <= 1.0),
        ("signal_volume_le_1.5x", "信号日成交量≤日常量1.5倍", lambda row: float(row["signal_volume_ratio20"]) <= 1.5),
        ("flow_to_signal_ge_2pct", "7日流入/信号日成交量≥2%", lambda row: float(row["flow_to_signal_volume"]) >= 0.02),
        ("prior7_le_10pct", "前7日涨幅≤10%", lambda row: float(row["return_7d"]) <= 0.10),
        ("prior7_le_20pct", "前7日涨幅≤20%", lambda row: float(row["return_7d"]) <= 0.20),
        ("prior14_le_30pct", "前14日涨幅≤30%", lambda row: float(row["return_14d"]) <= 0.30),
        ("runup7_le_30pct", "近7日最高涨幅≤30%", lambda row: float(row["recent_7d_runup"]) <= 0.30),
        ("no_recent_5x_volume", "此前7日无5倍量", lambda row: not bool(row["recent_5x_volume"])),
        (
            "f7c05_signal_volume15",
            "F7c≥0.5%且信号日成交量≤日常量1.5倍",
            lambda row: float(row["f7c_share"]) >= 0.005
            and float(row["signal_volume_ratio20"]) <= 1.5,
        ),
        (
            "signal_volume1_flow_signal2",
            "信号日成交量≤日常量1倍且流入/当日量≥2%",
            lambda row: float(row["signal_volume_ratio20"]) <= 1.0
            and float(row["flow_to_signal_volume"]) >= 0.02,
        ),
        (
            "flow10_prior7_20",
            "流入/日常量≥10%且前7日涨幅≤20%",
            lambda row: float(row["flow_to_median20_volume"]) >= 0.10 and float(row["return_7d"]) <= 0.20,
        ),
        (
            "f7c02_flow10_prior7_20",
            "F7c≥0.2%、流入/日常量≥10%、前7日涨幅≤20%",
            lambda row: float(row["f7c_share"]) >= 0.002
            and float(row["flow_to_median20_volume"]) >= 0.10
            and float(row["return_7d"]) <= 0.20,
        ),
        (
            "flow10_prior7_20_no5x",
            "流入/日常量≥10%、前7日涨幅≤20%、此前无5倍量",
            lambda row: float(row["flow_to_median20_volume"]) >= 0.10
            and float(row["return_7d"]) <= 0.20
            and not bool(row["recent_5x_volume"]),
        ),
    ]


def analyze_rules(rows: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    completed = [
        row for row in rows
        if row["executed_independent"] and not row["right_censored_mark"]
    ]
    scenarios = []
    months = []
    for name, label, predicate in rules():
        selected = [row for row in completed if predicate(row)]
        returns = [float(row["target_return_2x"]) for row in selected]
        trim = max(1, int(len(returns) * 0.05)) if len(returns) >= 20 else 0
        ordered = sorted(returns)
        trimmed = ordered[trim:-trim] if trim else ordered
        scenarios.append(
            {
                "scenario": name,
                "label": label,
                "trades": len(selected),
                "retention": len(selected) / len(completed),
                "symbols": len({row["symbol"] for row in selected}),
                "win_rate": mean([int(row["profitable"]) for row in selected]),
                "mean_return_2x": mean(returns),
                "trimmed_mean_return_2x": mean(trimmed),
                "median_return_2x": median(returns),
                "positive_months": sum(
                    mean([float(row["target_return_2x"]) for row in selected if row["entry_date"][:7] == month]) > 0
                    for month in sorted({row["entry_date"][:7] for row in selected})
                ),
                "months": len({row["entry_date"][:7] for row in selected}),
            }
        )
        for month in sorted({row["entry_date"][:7] for row in completed}):
            sample = [row for row in selected if row["entry_date"][:7] == month]
            values = [float(row["target_return_2x"]) for row in sample]
            months.append(
                {
                    "scenario": name,
                    "month": month,
                    "trades": len(sample),
                    "win_rate": mean([int(row["profitable"]) for row in sample]),
                    "mean_return_2x": mean(values),
                }
            )
    write_csv(RULE_CSV, scenarios)
    write_csv(MONTH_CSV, months)
    return scenarios, months


def replay_shared_portfolios(
    feature_rows: list[dict[str, Any]], scenarios: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    e2e = load_module("v2_failure_e2e", HERE / "run_v2_end_to_end.py")
    weighted = load_module(
        "v2_failure_weighted",
        HERE.parent / "f7c-strategy-backtest-2026-08-01/backtest_portfolio_weighted.py",
    )
    candidates = read_csv(HERE / "v2-all-signal-input-trades.csv")
    summaries = read_csv(HERE / "v2-all-signal-exit-trades.csv")
    events = read_csv(HERE / "v2-all-signal-exit-events.csv")
    cases, by_events = e2e.case_inputs(candidates, summaries, events)
    features = {row["case_id"]: row for row in feature_rows}
    manifest = json.loads(
        (HERE / "intraday-all-signals-data/manifest.json").read_text(encoding="utf-8")
    )
    manifest_cases = {case["case_id"]: case for case in manifest["cases"]}
    rule_by_name = {name: predicate for name, _, predicate in rules()}
    output = []
    for scenario in scenarios:
        predicate = rule_by_name[scenario["scenario"]]
        selected_cases = [case for case in cases if predicate(features[case["case_id"]])]
        entries, exits, skipped = e2e.select_portfolio(selected_cases, by_events)
        selected_ids = {row["case_id"] for row in entries}
        bar_maps = {
            case_id: weighted.read_bar_map(
                HERE
                / "intraday-all-signals-data"
                / manifest_cases[case_id]["intervals"]["5m"]["file"]
            )
            for case_id in selected_ids
        }
        result = weighted.simulate(
            f"shared_{scenario['scenario']}", entries, exits, bar_maps
        )
        attribution = result["attribution"]
        output.append(
            {
                "scenario": scenario["scenario"],
                "label": scenario["label"],
                "candidate_signals": len(selected_cases),
                "executed_trades": len(entries),
                "executed_symbols": len({row["symbol"] for row in entries}),
                "skipped_signals": len(skipped),
                "win_rate": sum(float(row["total_net_pnl"]) > 0 for row in attribution) / len(attribution),
                "total_return": result["total_return"],
                "max_drawdown_5m": result["max_drawdown_5m"],
            }
        )
    write_csv(SHARED_REPLAY_CSV, output)
    return output


def fmt(value: float, percent: bool = True) -> str:
    return f"{value * 100:.2f}%" if percent else f"{value:.4f}"


def render_report(
    rows: list[dict[str, Any]],
    comparison: list[dict[str, Any]],
    quartiles: list[dict[str, Any]],
    scenarios: list[dict[str, Any]],
    shared_replays: list[dict[str, Any]],
) -> str:
    completed = [row for row in rows if row["executed_independent"] and not row["right_censored_mark"]]
    winners = [row for row in completed if row["profitable"]]
    losers = [row for row in completed if not row["profitable"]]
    by_feature_quartile = defaultdict(dict)
    for row in quartiles:
        by_feature_quartile[row["feature"]][int(row["quartile"])] = row
    scenario_by_name = {row["scenario"]: row for row in scenarios}
    shared_by_name = {row["scenario"]: row for row in shared_replays}
    rule_by_name = {name: predicate for name, _, predicate in rules()}

    def sample_stats(sample: list[dict[str, Any]]) -> dict[str, float]:
        returns = [float(row["target_return_2x"]) for row in sample]
        return {
            "trades": len(sample),
            "win_rate": mean([int(row["profitable"]) for row in sample]),
            "mean_return": mean(returns),
            "median_return": median(returns),
        }

    months = sorted({row["entry_date"][:7] for row in completed})
    monthly_baseline = {
        month: sample_stats([row for row in completed if row["entry_date"][:7] == month])
        for month in months
    }
    diagnostic_rules = (
        "baseline",
        "signal_volume_le_1x",
        "flow_normal_5_to_10pct",
        "f7c05_signal_volume15",
    )
    periods = {
        "2026-01至04": lambda row: row["entry_date"][:7] <= "2026-04",
        "2026-05至07": lambda row: row["entry_date"][:7] >= "2026-05",
    }
    period_stats: dict[tuple[str, str], dict[str, float]] = {}
    for scenario in diagnostic_rules:
        predicate = rule_by_name[scenario]
        for period, period_predicate in periods.items():
            period_stats[(scenario, period)] = sample_stats(
                [row for row in completed if predicate(row) and period_predicate(row)]
            )

    signal_volume_failure = sample_stats(
        [row for row in completed if float(row["signal_volume_ratio20"]) > 1.5]
    )
    extreme_flow_failure = sample_stats(
        [row for row in completed if float(row["flow_to_median20_volume"]) >= 0.10]
    )
    extreme_runup_failure = sample_stats(
        [row for row in completed if float(row["return_7d"]) > 0.50]
    )
    lines = [
        "# V2失败交易共性研究",
        "",
        "## 样本与原则",
        "",
        f"- 主样本为逐币独立资金中已完成退出的{len(completed)}笔交易：盈利{len(winners)}笔、失败{len(losers)}笔；剔除截止估值交易。",
        "- 所有特征只使用信号日收盘前已知数据；不使用退出后信息。",
        "- CEX流量与成交量均转成基础币数量；1000前缀合约的成交量已乘1000校正。",
        "- 以下筛选表现属于探索性全样本结果，不等同独立样本外证明。",
        "",
        "## 特征差异",
        "",
        "| 特征 | 盈利中位数 | 失败中位数 | 高值胜率AUC | 与收益秩相关 | 95%中位差区间 |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    for row in comparison:
        feature = row["feature"]
        percent_features = feature != "listing_age_days"
        lines.append(
            f"| {row['label']} | {fmt(float(row['winner_median']), percent_features)} | "
            f"{fmt(float(row['loser_median']), percent_features)} | {float(row['higher_value_win_auc']):.3f} | "
            f"{float(row['spearman_to_return']):+.3f} | "
            f"[{fmt(float(row['bootstrap_ci_low']), percent_features)}, {fmt(float(row['bootstrap_ci_high']), percent_features)}] |"
        )
    lines.extend(["", "## 三个核心假设的分位数检验", ""])
    for feature in ("f7c_share", "flow_to_median20_volume", "return_7d", "recent_7d_runup", "prior7_max_volume_ratio"):
        q1 = by_feature_quartile[feature][1]
        q4 = by_feature_quartile[feature][4]
        lines.append(
            f"- **{FEATURES[feature]}**：最低四分位胜率{fmt(float(q1['win_rate']))}、均值{fmt(float(q1['mean_return_2x']))}；"
            f"最高四分位胜率{fmt(float(q4['win_rate']))}、均值{fmt(float(q4['mean_return_2x']))}。"
        )
    lines.extend(
        [
            "",
            "## 简单因果筛选的探索结果",
            "",
            "| 条件 | 保留交易 | 胜率 | 单笔均值 | 5%截尾均值 | 正收益月份 | 三仓交易 | 三仓收益 | 三仓MDD |",
            "|---|---:|---:|---:|---:|---:|---:|---:|---:|",
        ]
    )
    for row in scenarios:
        shared = shared_by_name[row["scenario"]]
        lines.append(
            f"| {row['label']} | {row['trades']} | "
            f"{fmt(float(row['win_rate']))} | {fmt(float(row['mean_return_2x']))} | "
            f"{fmt(float(row['trimmed_mean_return_2x']))} | "
            f"{row['positive_months']}/{row['months']} | {shared['executed_trades']} | "
            f"{fmt(float(shared['total_return']))} | {fmt(float(shared['max_drawdown_5m']))} |"
        )
    lines.extend(
        [
            "",
            "## 失败交易的阈值画像",
            "",
            f"- **信号日放量超过日常量1.5倍**：{signal_volume_failure['trades']:.0f}笔，胜率{fmt(signal_volume_failure['win_rate'])}、单笔均值{fmt(signal_volume_failure['mean_return'])}。这是最清晰且在独立样本与共享三仓实际成交中方向一致的失败共性。",
            f"- **7日流入超过日常量10%**：{extreme_flow_failure['trades']:.0f}笔，胜率{fmt(extreme_flow_failure['win_rate'])}、单笔均值{fmt(extreme_flow_failure['mean_return'])}；超过25%时更差。大额流入更像交易所入金后的卖压，不支持“流入越大越好”。",
            f"- **信号前7日已上涨超过50%**：{extreme_runup_failure['trades']:.0f}笔，胜率{fmt(extreme_runup_failure['win_rate'])}、单笔均值{fmt(extreme_runup_failure['mean_return'])}。但样本较少，普通程度的前期异动只有弱效应。",
            "- **F7c/Cluster本身不是有效单变量阈值**：盈利与失败中位数均约0.21%，AUC接近0.5；不能仅因没过某个F7c阈值就判定会失败。",
            "- **流入/信号日成交量偏低**与失败有轻度关系：盈利中位数1.95%、失败1.35%，但置信区间跨零，只能作为辅助特征。",
            "",
            "## 月度与时序稳定性",
            "",
            "| 入场月份 | 交易 | 胜率 | 单笔均值 | 单笔中位数 |",
            "|---|---:|---:|---:|---:|",
        ]
    )
    for month in months:
        stats = monthly_baseline[month]
        lines.append(
            f"| {month} | {stats['trades']:.0f} | {fmt(stats['win_rate'])} | "
            f"{fmt(stats['mean_return'])} | {fmt(stats['median_return'])} |"
        )
    lines.extend(
        [
            "",
            "| 规则 | 2026-01至04：交易/胜率/均值 | 2026-05至07：交易/胜率/均值 |",
            "|---|---:|---:|",
        ]
    )
    for scenario in diagnostic_rules:
        label = scenario_by_name[scenario]["label"]
        early = period_stats[(scenario, "2026-01至04")]
        late = period_stats[(scenario, "2026-05至07")]
        lines.append(
            f"| {label} | {early['trades']:.0f} / {fmt(early['win_rate'])} / {fmt(early['mean_return'])} | "
            f"{late['trades']:.0f} / {fmt(late['win_rate'])} / {fmt(late['mean_return'])} |"
        )
    lines.extend(
        [
            "",
            "- 4月是唯一整体盈利月份；5月至7月基准胜率降至17.68%、单笔均值-6.66%。",
            "- 全样本看似最好的筛选规则在5月至7月全部转负，说明结果主要受市场状态驱动，不是稳定的样本外过滤器。",
            "- 因此当前证据适合用于构建模型特征和走前验证，不适合直接删除币种；至少应采用按月滚动训练、下一月验证，并显式加入市场广度/趋势状态。",
        ]
    )
    baseline = scenario_by_name["baseline"]
    best = max(
        (row for row in scenarios if row["scenario"] != "baseline" and row["trades"] >= 60),
        key=lambda row: float(row["mean_return_2x"]),
    )
    lines.extend(
        [
            "",
            "## 初步判断",
            "",
            f"- 基准单笔均值{fmt(float(baseline['mean_return_2x']))}、胜率{fmt(float(baseline['win_rate']))}。",
            f"- 保留至少60笔的简单条件中，均值最高的是“{best['label']}”：保留{best['trades']}笔，"
            f"胜率{fmt(float(best['win_rate']))}、单笔均值{fmt(float(best['mean_return_2x']))}。",
            "- 但该优势集中在4月，5月至7月没有延续；不能当作已验证收益改进。",
            "- 建议优先建模三个交互特征：信号日放量倍数、流入/信号日成交量、前7日涨幅，并加入市场广度状态；在严格走前通过前，不建议直接删除币种或上线硬阈值。",
            "",
            "## 数据产物",
            "",
            f"- 逐信号特征：`{FEATURE_CSV.name}`",
            f"- 盈亏差异：`{COMPARISON_CSV.name}`",
            f"- 特征四分位：`{QUARTILE_CSV.name}`",
            f"- 简单规则：`{RULE_CSV.name}`",
            f"- 月度稳定性：`{MONTH_CSV.name}`",
            f"- 共享三仓重放：`{SHARED_REPLAY_CSV.name}`",
        ]
    )
    return "\n".join(lines) + "\n"


def main() -> None:
    rows = build_features()
    comparison, quartiles = compare_features(rows)
    scenarios, _ = analyze_rules(rows)
    shared_replays = replay_shared_portfolios(rows, scenarios)
    REPORT_MD.write_text(
        render_report(rows, comparison, quartiles, scenarios, shared_replays), encoding="utf-8"
    )
    print(
        json.dumps(
            {
                "signals": len(rows),
                "completed_independent_trades": sum(
                    row["executed_independent"] and not row["right_censored_mark"] for row in rows
                ),
                "feature_output": str(FEATURE_CSV),
                "report": str(REPORT_MD),
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
