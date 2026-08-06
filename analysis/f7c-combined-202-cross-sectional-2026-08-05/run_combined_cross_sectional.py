#!/usr/bin/env python3
"""Frozen and pooled walk-forward cross-sectional V2 replay on old33 + new169."""

from __future__ import annotations

import csv
import importlib.util
import json
import math
import statistics
import sys
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any


HERE = Path(__file__).resolve().parent
ANALYSIS = HERE.parent
OLD33_ROOT = ANALYSIS / "f7c-expanded-20-backtest-2026-08-04"
OLD33 = OLD33_ROOT / "combined33"
NEW169 = ANALYSIS / "f7c-expanded-169-backtest-2026-08-05"
ORIGINAL = ANALYSIS / "f7c-strategy-backtest-2026-08-01"


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


XSEC = load_module("combined202_xsec_base", NEW169 / "model_v2_cross_sectional_allocation.py")
FAILURE = load_module("combined202_failure_base", NEW169 / "model_v2_failure_walkforward.py")
WEIGHTED = load_module("combined202_weighted", ORIGINAL / "backtest_portfolio_weighted.py")


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


def median(values: list[float]) -> float:
    return statistics.median(values) if values else float("nan")


def mean(values: list[float]) -> float:
    return statistics.mean(values) if values else float("nan")


def lookback_return(bars: list[dict[str, Any]], index: int, days: int) -> float:
    anchor = max(0, index - days)
    return float(bars[index]["c"]) / float(bars[anchor]["c"]) - 1


def volume_multiplier(symbol: str) -> float:
    return 1000.0 if symbol.startswith("1000") else 1.0


def combined_market_features() -> dict[str, dict[str, float]]:
    tokens = []
    for path in (OLD33_ROOT / "combined33-dataset.json", NEW169 / "dataset.json"):
        tokens.extend(json.loads(path.read_text(encoding="utf-8"))["tokens"])
    price_by_symbol = {
        token["symbol"]: {bar["d"]: bar for bar in token["bars"]}
        for token in tokens
    }
    signal_dates = {
        row["signal_date"]
        for path in (OLD33 / "signals.csv", NEW169 / "signals.csv")
        for row in read_csv(path)
    }
    output: dict[str, dict[str, float]] = {}
    for day_text in signal_dates:
        current = date.fromisoformat(day_text)
        item: dict[str, float] = {}
        for days in (7, 30):
            anchor = (current - timedelta(days=days)).isoformat()
            returns = []
            for by_date in price_by_symbol.values():
                if day_text in by_date and anchor in by_date:
                    returns.append(float(by_date[day_text]["c"]) / float(by_date[anchor]["c"]) - 1)
            item[f"market_median_return_{days}d"] = median(returns)
            item[f"market_positive_breadth_{days}d"] = mean([value > 0 for value in returns])
            item[f"market_observations_{days}d"] = len(returns)
        output[day_text] = item
    return output


def new169_rows(market: dict[str, dict[str, float]]) -> list[dict[str, Any]]:
    rows = FAILURE.prepare_rows()
    for row in rows:
        row.update(market[row["signal_date"]])
        row["engineered"] = FAILURE.engineered(row)
        row["source_universe"] = "new169"
    return rows


def old33_rows(market: dict[str, dict[str, float]]) -> list[dict[str, Any]]:
    dataset = json.loads((OLD33_ROOT / "combined33-dataset.json").read_text(encoding="utf-8"))
    token_by_symbol = {token["symbol"]: token for token in dataset["tokens"]}
    signal_by_key = {
        (row["symbol"], row["signal_date"]): row
        for row in read_csv(OLD33 / "signals.csv")
    }
    outcome_by_id = {
        row["case_id"]: row
        for row in read_csv(OLD33 / "multitimeframe-exit-v2-trades.csv")
    }
    manifest = json.loads((OLD33 / "intraday-all-data/manifest.json").read_text(encoding="utf-8"))
    output: list[dict[str, Any]] = []
    for case in manifest["cases"]:
        symbol = case["symbol"]
        signal_date = case["signal_date"]
        signal = signal_by_key[(symbol, signal_date)]
        bars = token_by_symbol[symbol]["bars"]
        index = next(i for i, bar in enumerate(bars) if bar["d"] == signal_date)
        bar = bars[index]
        multiplier = volume_multiplier(symbol)
        prior20 = bars[max(0, index - 20):index]
        prior7 = bars[max(0, index - 7):index]
        older20 = bars[max(0, index - 27):max(0, index - 7)]
        normalized_prior20 = [float(item["v"]) * multiplier for item in prior20]
        normalized_prior7 = [float(item["v"]) * multiplier for item in prior7]
        normalized_older20 = [float(item["v"]) * multiplier for item in older20]
        normal20 = median(normalized_prior20) if normalized_prior20 else float(bar["v"]) * multiplier
        older_normal = median(normalized_older20) if normalized_older20 else normal20
        net7 = float(bar["cex"]["net_7d"])
        high7_anchor = float(bars[max(0, index - 7)]["c"])
        recent_high = max(float(item["h"]) for item in bars[max(0, index - 7):index + 1])
        high20 = max(float(item["h"]) for item in bars[max(0, index - 19):index + 1])
        outcome = outcome_by_id[case["case_id"]]
        completed = str(outcome["right_censored_mark"]).lower() == "false"
        row: dict[str, Any] = {
            "case_id": case["case_id"],
            "symbol": symbol,
            "signal_date": signal_date,
            "entry_date": case["entry_date"],
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
            **market[signal_date],
            "available_history_days_from_2025": index,
            "completed": completed,
            "profitable": int(completed and float(outcome["net_return_2x"]) > 0),
            "target_return_2x": float(outcome["net_return_2x"]) if completed else None,
            "exit_time_utc": outcome["exit_time_utc"] if completed else "",
            "source_universe": "old33",
        }
        row["engineered"] = FAILURE.engineered(row)
        output.append(row)
    return sorted(output, key=lambda row: (row["entry_date"], row["case_id"]))


def walk_forward(
    rows: list[dict[str, Any]], train_universe: str
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    ranks = XSEC.rank_features(rows)
    predictions: list[dict[str, Any]] = []
    folds: list[dict[str, Any]] = []
    coefficients: list[dict[str, Any]] = []
    for month in sorted({row["entry_date"][:7] for row in rows}):
        cutoff = datetime.fromisoformat(month + "-01T00:00:00+00:00")
        train = [
            row for row in rows
            if row["completed"]
            and datetime.fromisoformat(row["exit_time_utc"]) < cutoff
            and (train_universe == "combined202" or row["source_universe"] == "new169")
        ]
        targets = XSEC.target_ranks(train)
        if len(targets) < 30:
            continue
        weights = XSEC.fit_ridge(train, ranks, targets)
        test = [row for row in rows if row["entry_date"][:7] == month]
        month_rows = []
        for row in test:
            month_rows.append({
                "case_id": row["case_id"],
                "symbol": row["symbol"],
                "source_universe": row["source_universe"],
                "signal_date": row["signal_date"],
                "entry_date": row["entry_date"],
                "fold_month": month,
                "train_universe": train_universe,
                "train_completed": len(train),
                "train_rank_rows": len(targets),
                "xsec_score": XSEC.predict_ridge(weights, row, ranks),
                "f7c_share": float(row["f7c_share"]),
                "signal_range_pct": float(row["signal_range_pct"]),
                "market_median_return_7d": float(row["market_median_return_7d"]),
                "market_positive_breadth_7d": float(row["market_positive_breadth_7d"]),
                "completed": row["completed"],
                "target_return_2x": row["target_return_2x"] if row["completed"] else "",
            })
        by_date: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for row in month_rows:
            by_date[row["entry_date"]].append(row)
        for date_rows in by_date.values():
            score_ranks = XSEC.percentile_ranks(
                [float(row["xsec_score"]) for row in date_rows], singleton=1.0
            )
            for row, rank in zip(date_rows, score_ranks, strict=True):
                row["xsec_rank_pct"] = rank
        predictions.extend(month_rows)
        completed_test = [row for row in month_rows if row["completed"]]
        metrics = XSEC.cross_sectional_metrics(completed_test, "xsec_score")
        folds.append({
            "train_universe": train_universe,
            "month": month,
            "train_completed": len(train),
            "test_signals": len(month_rows),
            "completed_test": len(completed_test),
            **metrics,
        })
        for feature, coefficient in zip(XSEC.RANK_FEATURES, weights[1:], strict=True):
            coefficients.append({
                "train_universe": train_universe,
                "month": month,
                "feature": feature,
                "coefficient": coefficient,
            })
    return predictions, folds, coefficients


def timestamp(day: str) -> int:
    return int(datetime.fromisoformat(day).replace(tzinfo=timezone.utc).timestamp() * 1000)


def load_cases_events_bars() -> tuple[dict[str, dict[str, Any]], list[dict[str, str]], dict[str, dict[int, dict[str, float]]]]:
    cases: dict[str, dict[str, Any]] = {}
    events: list[dict[str, str]] = []
    bar_maps: dict[str, dict[int, dict[str, float]]] = {}
    specs = [
        (
            "old33",
            OLD33 / "intraday-all-data",
            OLD33 / "v2-input-trades.csv",
            OLD33 / "multitimeframe-exit-v2-trades.csv",
            OLD33 / "multitimeframe-exit-v2-events.csv",
        ),
        (
            "new169",
            NEW169 / "intraday-all-signals-data",
            NEW169 / "v2-all-signal-input-trades.csv",
            NEW169 / "v2-all-signal-exit-trades.csv",
            NEW169 / "v2-all-signal-exit-events.csv",
        ),
    ]
    for source, data_dir, input_path, summary_path, event_path in specs:
        manifest = json.loads((data_dir / "manifest.json").read_text(encoding="utf-8"))
        inputs = read_csv(input_path)
        summaries = {row["case_id"]: row for row in read_csv(summary_path)}
        if len(inputs) != len(manifest["cases"]):
            raise ValueError(f"{source}: manifest/input mismatch")
        for case, trade in zip(manifest["cases"], inputs, strict=True):
            case_id = case["case_id"]
            if case_id in cases:
                raise ValueError(f"duplicate case id: {case_id}")
            cases[case_id] = {
                "case_id": case_id,
                "symbol": case["symbol"],
                "source_universe": source,
                "signal_date": case["signal_date"],
                "entry_date": case["entry_date"],
                "entry_time_ms": timestamp(case["entry_date"]),
                "entry_price": float(case["entry_price"]),
                "f7c": float(trade["signal_f7c_share"]),
                "rank": int(trade["signal_rank"]),
                "right_censored": str(summaries[case_id]["right_censored_mark"]).lower() == "true",
            }
            bar_maps[case_id] = WEIGHTED.read_bar_map(
                data_dir / case["intervals"]["5m"]["file"]
            )
        events.extend(read_csv(event_path))
    return cases, events, bar_maps


def simulate(label: str, predictions: list[dict[str, Any]], cases, events, bar_maps):
    result = XSEC.simulate_allocation(
        "xsec_dynamic_invvol_all", predictions, cases, events, bar_maps
    )
    attribution = result.pop("attribution")
    result["scenario"] = label
    for row in attribution:
        row["scenario"] = label
        row["source_universe"] = cases[row["case_id"]]["source_universe"]
    return result, attribution


def contribution_rows(attribution: list[dict[str, Any]]) -> list[dict[str, Any]]:
    groups: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in attribution:
        groups[(row["scenario"], row["source_universe"])].append(row)
    output = []
    for (scenario, source), rows in sorted(groups.items()):
        pnls = [float(row["total_net_pnl"]) for row in rows]
        output.append({
            "scenario": scenario,
            "source_universe": source,
            "executed_trades": len(rows),
            "executed_symbols": len({row["symbol"] for row in rows}),
            "win_rate": mean([value > 0 for value in pnls]),
            "pnl_on_initial_equity": sum(pnls),
            "mean_trade_pnl": mean(pnls),
        })
    return output


def fmt(value: float) -> str:
    return f"{value * 100:.2f}%"


def main() -> None:
    market = combined_market_features()
    rows = new169_rows(market) + old33_rows(market)
    rows.sort(key=lambda row: (row["entry_date"], row["case_id"]))
    frozen_predictions, frozen_folds, frozen_coefficients = walk_forward(rows, "new169")
    pooled_predictions, pooled_folds, pooled_coefficients = walk_forward(rows, "combined202")
    cases, events, bar_maps = load_cases_events_bars()
    frozen_result, frozen_attr = simulate(
        "frozen169_ridge_dynamic", frozen_predictions, cases, events, bar_maps
    )
    pooled_result, pooled_attr = simulate(
        "combined202_ridge_dynamic", pooled_predictions, cases, events, bar_maps
    )
    sensitivity_results = []
    sensitivity_attr = []
    for label, predictions_for_model, model_attr in (
        ("frozen169_ridge_dynamic", frozen_predictions, frozen_attr),
        ("combined202_ridge_dynamic", pooled_predictions, pooled_attr),
    ):
        best_case = max(model_attr, key=lambda row: float(row["total_net_pnl"]))["case_id"]
        reduced_result, reduced_attr = simulate(
            f"{label}_without_best",
            [row for row in predictions_for_model if row["case_id"] != best_case],
            cases,
            events,
            bar_maps,
        )
        reduced_result["removed_best_case"] = best_case
        sensitivity_results.append(reduced_result)
        sensitivity_attr.extend(reduced_attr)
    baseline_predictions = [
        {**row, "xsec_score": float(row["f7c_share"]), "xsec_rank_pct": 1.0}
        for row in pooled_predictions
    ]
    baseline = XSEC.simulate_allocation(
        "f7c_dynamic_invvol_all", baseline_predictions, cases, events, bar_maps
    )
    baseline_attr = baseline.pop("attribution")
    baseline["scenario"] = "combined202_f7c_dynamic"
    for row in baseline_attr:
        row["scenario"] = baseline["scenario"]
        row["source_universe"] = cases[row["case_id"]]["source_universe"]
    results = [baseline, frozen_result, pooled_result] + sensitivity_results
    attribution = baseline_attr + frozen_attr + pooled_attr + sensitivity_attr
    contributions = contribution_rows(attribution)
    folds = frozen_folds + pooled_folds
    coefficients = frozen_coefficients + pooled_coefficients
    predictions = frozen_predictions + pooled_predictions
    write_csv(HERE / "combined202-predictions.csv", predictions)
    write_csv(HERE / "combined202-folds.csv", folds)
    write_csv(HERE / "combined202-coefficients.csv", coefficients)
    write_csv(HERE / "combined202-backtests.csv", results)
    write_csv(HERE / "combined202-attribution.csv", attribution)
    write_csv(HERE / "combined202-contributions.csv", contributions)

    completed = [row for row in pooled_predictions if row["completed"]]
    pooled_metrics = XSEC.cross_sectional_metrics(completed, "xsec_score")
    frozen_metrics = XSEC.cross_sectional_metrics(
        [row for row in frozen_predictions if row["completed"]], "xsec_score"
    )
    source_counts = defaultdict(int)
    for row in rows:
        source_counts[row["source_universe"]] += 1
    summary = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "universe": {
            "symbols": 202,
            "old33_symbols": 33,
            "new169_symbols": 169,
            "candidate_windows": dict(source_counts),
            "candidate_windows_total": len(rows),
            "period": "2026-01-01 to 2026-08-03 (old33 source ends 2026-07-29)",
            "oos_start": min(row["entry_date"] for row in pooled_predictions),
        },
        "frozen169_metrics": frozen_metrics,
        "combined202_metrics": pooled_metrics,
        "backtests": results,
        "contributions": contributions,
    }
    (HERE / "combined202-summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    lines = [
        "# 老33币＋新169币：V2截面岭回归与动态仓位",
        "",
        "## 口径",
        "",
        "- 合并币池202币，两个币池无重合。新169币提供492个完整逐信号窗口；老33币提供70个完整V2窗口，合计562个候选。",
        "- 信号期从2026-01开始；岭回归按月扩展窗口训练，OOS从2026-02开始。训练集只使用当月月初前已经退出的交易。",
        "- `frozen169_ridge_dynamic`只用新169币历史拟合月度岭回归，再对202币同日截面排序；动态风险参数完全沿用既有方案。",
        "- `combined202_ridge_dynamic`在202币合并历史上逐月重训；它是扩容后的研究模型，不是额外独立验证。",
        "- 老33币源截止2026-07-29，新169币源截止2026-08-03；老池末端3个不完整退出窗口未纳入。",
        "",
        "## 截面排序能力",
        "",
        "| 模型 | 截面日 | 观察数 | Rank IC | ICIR | 正IC占比 | Top-Bottom |",
        "|---|---:|---:|---:|---:|---:|---:|",
        f"| 冻结169训练 | {frozen_metrics['xsec_dates']} | {frozen_metrics['xsec_observations']} | {frozen_metrics['mean_rank_ic']:+.3f} | {frozen_metrics['rank_ic_ir']:+.3f} | {fmt(frozen_metrics['positive_ic_rate'])} | {fmt(frozen_metrics['mean_top_bottom_return_spread'])} |",
        f"| 合并202走步训练 | {pooled_metrics['xsec_dates']} | {pooled_metrics['xsec_observations']} | {pooled_metrics['mean_rank_ic']:+.3f} | {pooled_metrics['rank_ic_ir']:+.3f} | {fmt(pooled_metrics['positive_ic_rate'])} | {fmt(pooled_metrics['mean_top_bottom_return_spread'])} |",
        "",
        "## 组合回放",
        "",
        "| 方案 | 候选 | 执行 | 胜率 | 收益 | MDD | 平均持仓 |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    for result in results:
        lines.append(
            f"| {result['scenario']} | {result['candidate_signals']} | {result['executed_trades']} | "
            f"{fmt(float(result['win_rate']))} | {fmt(float(result['total_return']))} | "
            f"{fmt(float(result['max_drawdown_5m']))} | {float(result['average_open_positions']):.2f} |"
        )
    lines += [
        "",
        "## 新老币执行贡献",
        "",
        "注意：分组PnL是交易归因之和，不等于各自独立复利收益；总组合收益还受跨组资金占用和复利路径影响。",
        "",
        "| 方案 | 分组 | 交易 | 币数 | 胜率 | PnL/初始权益 |",
        "|---|---|---:|---:|---:|---:|",
    ]
    for row in contributions:
        lines.append(
            f"| {row['scenario']} | {row['source_universe']} | {row['executed_trades']} | "
            f"{row['executed_symbols']} | {fmt(float(row['win_rate']))} | "
            f"{fmt(float(row['pnl_on_initial_equity']))} |"
        )
    lines += [
        "",
        "## 单笔敏感性",
        "",
    ]
    for result in sensitivity_results:
        lines.append(
            f"- `{result['scenario']}`移除{result['removed_best_case']}后：收益"
            f"{fmt(float(result['total_return']))}，MDD{fmt(float(result['max_drawdown_5m']))}。"
        )
    lines += [
        "",
        "## 解释边界",
        "",
        "- 这是同一2026时间段的走步回放，不是跨年份的真正样本外验证。",
        "- 若合并训练收益改善但Rank IC仍不稳定，不能只凭组合路径升级为生产算法。",
        "- 下一步应冻结本次特征、岭惩罚和动态风险阈值，用2025训练/2026验证，或等待2026-08之后新数据做前向检验。",
    ]
    (HERE / "combined202-report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
