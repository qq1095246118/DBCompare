#!/usr/bin/env python3
"""Backtest V1/V2 multi-timeframe staged exit models on fixed entries.

Decision timing is causal: features use completed bars only and every decision
is filled at the next 5m open.  Entry samples are held fixed to isolate the
effect of replacing the original daily exit rules.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import statistics
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


HERE = Path(__file__).resolve().parent
DATA_DIR = HERE / "intraday-all-data"
MANIFEST = DATA_DIR / "manifest.json"
TRADES_CSV = HERE / "trades.csv"

FRICTION_PER_SIDE = 0.002
LEVERAGE = 2.0
MAX_HOLD_DAYS = 14
SCORE_ARM_MFE = 0.50
BREAKEVEN_ARM_MFE = 0.20
HARD_TRAIL_DRAWDOWN = 0.15
EMERGENCY_TRAIL_DRAWDOWN = 0.30
BASE_THRESHOLDS = ((0.90, 1.00), (0.85, 0.75), (0.75, 0.50), (0.60, 0.25))


def output_paths(model_version: str) -> tuple[Path, Path, Path]:
    suffix = "" if model_version == "v1" else "-v2"
    return (
        HERE / f"multitimeframe-exit{suffix}-trades.csv",
        HERE / f"multitimeframe-exit{suffix}-events.csv",
        HERE / f"multitimeframe-exit{suffix}-report.md",
    )


def read_rows(path: Path) -> list[dict[str, Any]]:
    with path.open(encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))
    numeric = (
        "open_time_ms",
        "close_time_ms",
        "open",
        "high",
        "low",
        "close",
        "volume",
        "taker_buy_volume",
    )
    for row in rows:
        for field in numeric:
            row[field] = float(row[field])
    return rows


def ema(values: list[float], span: int) -> list[float]:
    alpha = 2 / (span + 1)
    output = []
    current = values[0]
    for value in values:
        current = alpha * value + (1 - alpha) * current
        output.append(current)
    return output


def atr(rows: list[dict[str, Any]], index: int, period: int = 14) -> float:
    true_ranges = []
    start = max(0, index - period + 1)
    for position in range(start, index + 1):
        row = rows[position]
        previous_close = rows[position - 1]["close"] if position else row["open"]
        true_ranges.append(
            max(
                row["high"] - row["low"],
                abs(row["high"] - previous_close),
                abs(row["low"] - previous_close),
            )
        )
    return statistics.mean(true_ranges)


def clip(value: float, lower: float = 0.0, upper: float = 1.0) -> float:
    return max(lower, min(upper, value))


def iso_time(ms: float) -> str:
    return datetime.fromtimestamp(ms / 1000, timezone.utc).isoformat(timespec="seconds")


def net_return(entry: float, exit_: float) -> float:
    return exit_ * (1 - FRICTION_PER_SIDE) / (entry * (1 + FRICTION_PER_SIDE)) - 1


def latest_completed(rows: list[dict[str, Any]], pointer: int, time_ms: float) -> int:
    while pointer + 1 < len(rows) and rows[pointer + 1]["close_time_ms"] <= time_ms:
        pointer += 1
    return pointer


def volume_ratio(rows: list[dict[str, Any]], index: int, lookback: int = 20) -> float:
    history = [row["volume"] for row in rows[max(0, index - lookback) : index]]
    if not history:
        return 1.0
    base = statistics.median(history)
    return rows[index]["volume"] / base if base else 1.0


def upper_wick_share(row: dict[str, Any]) -> float:
    bar_range = row["high"] - row["low"]
    return (row["high"] - max(row["open"], row["close"])) / bar_range if bar_range else 0.0


def prepare_context(case: dict[str, Any]) -> dict[str, Any]:
    intervals = {
        interval: read_rows(DATA_DIR / details["file"])
        for interval, details in case["intervals"].items()
    }
    one_hour = intervals["1h"]
    four_hour = intervals["4h"]
    return {
        "rows": intervals,
        "ema1h8": ema([row["close"] for row in one_hour], 8),
        "ema4h6": ema([row["close"] for row in four_hour], 6),
        "ema4h18": ema([row["close"] for row in four_hour], 18),
    }


def multi_timeframe_features(
    context: dict[str, Any], pointers: dict[str, int], five_index: int, running_high: float
) -> dict[str, Any]:
    rows = context["rows"]
    five = rows["5m"][five_index]
    decision_ms = five["close_time_ms"]
    for interval in ("15m", "1h", "4h"):
        pointers[interval] = latest_completed(rows[interval], pointers[interval], decision_ms)

    index15 = pointers["15m"]
    index1h = pointers["1h"]
    index4h = pointers["4h"]
    failure15 = False
    if index15 >= 4:
        bar15 = rows["15m"][index15]
        previous15 = rows["15m"][index15 - 1]
        failure15 = (
            bar15["high"] <= max(row["high"] for row in rows["15m"][index15 - 4 : index15])
            and bar15["close"] < previous15["close"]
        )

    weak1h = False
    if index1h >= 8:
        bar1h = rows["1h"][index1h]
        weak1h = (
            bar1h["close"] < context["ema1h8"][index1h]
            or context["ema1h8"][index1h] < context["ema1h8"][index1h - 1]
        )

    def regime_at(position: int) -> str:
        if position < 18:
            return "neutral"
        bar4h = rows["4h"][position]
        momentum4h = bar4h["close"] / rows["4h"][position - 2]["close"] - 1
        if (
            context["ema4h6"][position] > context["ema4h18"][position]
            and bar4h["close"] > context["ema4h6"][position]
            and momentum4h > 0
        ):
            return "strong"
        if context["ema4h6"][position] < context["ema4h18"][position] or momentum4h < 0:
            return "weak"
        return "neutral"

    regime4h = regime_at(index4h)
    weak4h_confirmed = regime4h == "weak" and regime_at(index4h - 1) == "weak"

    dd = max(0.0, 1 - five["close"] / running_high)
    wick = upper_wick_share(five)
    vr = volume_ratio(rows["5m"], five_index)
    buy_share = five["taker_buy_volume"] / five["volume"] if five["volume"] else 0.5
    sell_pressure = clip((0.50 - buy_share) / 0.15)
    dd_score = clip(dd / 0.10)
    wick_score = clip(wick / 0.50)
    volume_score = clip(math.log(max(vr, 1.0)) / math.log(10))
    score = (
        0.30 * dd_score
        + 0.20 * wick_score
        + 0.15 * volume_score
        + 0.10 * sell_pressure
        + 0.15 * float(failure15)
        + 0.10 * float(weak1h)
    )
    return {
        "score": score,
        "drawdown": dd,
        "volume_ratio": vr,
        "upper_wick_share": wick,
        "taker_buy_share": buy_share,
        "failure15": failure15,
        "weak1h": weak1h,
        "regime4h": regime4h,
        "weak4h_confirmed": weak4h_confirmed,
    }


def target_fraction(score: float, features: dict[str, Any]) -> float:
    adjustment = 0.10 if features["regime4h"] == "strong" else -0.10 if features["regime4h"] == "weak" else 0.0
    for threshold, fraction in BASE_THRESHOLDS:
        if score < threshold + adjustment:
            continue
        if fraction == 1.00 and not (features["failure15"] and features["weak1h"]):
            continue
        return fraction
    return 0.0


def v2_drawdown_target(features: dict[str, Any]) -> tuple[float, str]:
    """Cumulative position fraction to sell after a 15% peak drawdown."""
    score = float(features["score"])
    failure15 = bool(features["failure15"])
    weak1h = bool(features["weak1h"])
    regime = features["regime4h"]
    if regime == "strong":
        target = 0.25
        if failure15:
            target = 0.50
        if weak1h:
            target = 0.75
        if failure15 and weak1h and features["weak4h_confirmed"] and score >= 0.85:
            target = 1.00
        return target, "V2强趋势分层回撤"
    if regime == "neutral":
        target = 0.50
        if failure15:
            target = 0.75
        if failure15 and weak1h and features["weak4h_confirmed"] and score >= 0.85:
            target = 1.00
        return target, "V2中性趋势分层回撤"
    target = 0.75
    if failure15 and weak1h and features["weak4h_confirmed"] and score >= 0.85:
        target = 1.00
    return target, "V2弱趋势分层回撤"


def extreme_runner_trail(mfe: float) -> float | None:
    """Return the causal trailing drawdown for an extreme-MFE V2 runner.

    The threshold is selected from the running MFE known at the current 5m
    close.  A larger already-realised excursion therefore tightens, rather
    than loosens, the protection on the final 25% runner.
    """
    if mfe >= 9.00:
        return 0.05
    if mfe >= 7.00:
        return 0.10
    if mfe >= 5.00:
        return 0.20
    return None


def backtest_case(
    case: dict[str, Any], old_trade: dict[str, str], model_version: str = "v1"
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    context = prepare_context(case)
    rows = context["rows"]
    five = rows["5m"]
    entry_time = datetime.fromisoformat(case["entry_date"]).replace(tzinfo=timezone.utc)
    entry_ms = int(entry_time.timestamp() * 1000)
    entry_index = next(index for index, row in enumerate(five) if row["open_time_ms"] >= entry_ms)
    entry_price = float(case["entry_price"])
    data_open = five[entry_index]["open"]
    if abs(data_open / entry_price - 1) > 0.001:
        raise ValueError(f"{case['case_id']} entry mismatch: expected {entry_price}, got {data_open}")

    pointers = {"15m": -1, "1h": -1, "4h": -1}
    for interval in pointers:
        pointers[interval] = latest_completed(rows[interval], -1, entry_ms - 1)
    initial_atr = atr(rows["15m"], max(0, pointers["15m"]))
    initial_stop = entry_price - max(2.5 * initial_atr, 0.08 * entry_price)
    active_stop = initial_stop
    running_high = entry_price
    running_low = entry_price
    sold_fraction = 0.0
    pending: dict[str, Any] | None = None
    events: list[dict[str, Any]] = []
    decision_count = 0
    full_exit_time = None
    last_decision_reason = ""
    last_index = entry_index
    theoretical_end_ms = int((entry_time + timedelta(days=MAX_HOLD_DAYS)).timestamp() * 1000)

    for index in range(entry_index, len(five)):
        row = five[index]
        last_index = index
        if pending is not None:
            delta = min(pending["target_fraction"] - sold_fraction, 1 - sold_fraction)
            if delta > 1e-12:
                sold_fraction += delta
                event = {
                    "case_id": case["case_id"],
                    "symbol": case["symbol"],
                    "signal_date": case["signal_date"],
                    "entry_date": case["entry_date"],
                    "decision_time_utc": pending["decision_time_utc"],
                    "decision_close_time_ms": pending["decision_close_time_ms"],
                    "execution_time_utc": iso_time(row["open_time_ms"]),
                    "execution_open_time_ms": int(row["open_time_ms"]),
                    "execution_price": row["open"],
                    "sold_fraction": delta,
                    "cumulative_sold_fraction": sold_fraction,
                    "reason": pending["reason"],
                    **pending["features"],
                }
                events.append(event)
                last_decision_reason = pending["reason"]
            pending = None
            if sold_fraction >= 1 - 1e-12:
                full_exit_time = row["open_time_ms"]
                break

        running_high = max(running_high, row["high"])
        running_low = min(running_low, row["low"])
        mfe = running_high / entry_price - 1
        if mfe >= BREAKEVEN_ARM_MFE:
            active_stop = max(active_stop, entry_price * (1 + 2 * FRICTION_PER_SIDE))
        features = multi_timeframe_features(context, pointers, index, running_high)

        target = sold_fraction
        reason = ""
        if row["close_time_ms"] >= theoretical_end_ms - 1:
            target = 1.0
            reason = "持有满14天"
        elif row["close"] <= active_stop:
            target = 1.0
            reason = "保护止损"
        elif (
            model_version == "v2"
            and sold_fraction >= 0.75 - 1e-12
            and (runner_trail := extreme_runner_trail(mfe)) is not None
            and features["drawdown"] >= runner_trail
        ):
            target = 1.0
            reason = f"V2极端MFE runner回撤{runner_trail:.0%}"
        elif model_version == "v2" and features["drawdown"] >= EMERGENCY_TRAIL_DRAWDOWN:
            full_confirmation = (
                features["failure15"]
                and features["weak1h"]
                and features["weak4h_confirmed"]
                and features["score"] >= 0.85
            )
            target = 1.0 if full_confirmation else max(target, 0.75)
            reason = "V2峰值回撤30%紧急退出" if full_confirmation else "V2峰值回撤30%保留runner"
        elif model_version == "v1" and features["drawdown"] >= HARD_TRAIL_DRAWDOWN:
            target = 1.0
            reason = "5m收盘距持仓最高价回撤15%"
        elif model_version == "v2" and features["drawdown"] >= HARD_TRAIL_DRAWDOWN:
            staged_target, staged_reason = v2_drawdown_target(features)
            target = max(target, staged_target)
            if target > sold_fraction:
                reason = staged_reason
        elif mfe >= SCORE_ARM_MFE:
            score_target = target_fraction(features["score"], features)
            if model_version == "v2" and score_target >= 1.0:
                full_confirmation = (
                    features["failure15"]
                    and features["weak1h"]
                    and features["weak4h_confirmed"]
                    and features["score"] >= 0.85
                )
                score_target = 1.0 if full_confirmation else 0.75
            target = max(target, score_target)
            if features["regime4h"] == "strong" and sold_fraction == 0:
                target = min(target, 0.50)
            if target > sold_fraction:
                reason = "多周期退出评分"

        if target > sold_fraction and index + 1 < len(five):
            decision_count += 1
            pending = {
                "target_fraction": target,
                "decision_time_utc": iso_time(row["close_time_ms"]),
                "decision_close_time_ms": int(row["close_time_ms"]),
                "reason": reason,
                "features": {
                    **features,
                    "mfe_at_decision": mfe,
                    "active_stop": active_stop,
                },
            }

    if sold_fraction < 1 - 1e-12:
        # A right-censored case is reported, not silently treated as a valid full exit.
        row = five[last_index]
        events.append(
            {
                "case_id": case["case_id"],
                "symbol": case["symbol"],
                "signal_date": case["signal_date"],
                "entry_date": case["entry_date"],
                "decision_time_utc": "",
                "decision_close_time_ms": "",
                "execution_time_utc": iso_time(row["close_time_ms"]),
                "execution_open_time_ms": int(row["close_time_ms"]),
                "execution_price": row["close"],
                "sold_fraction": 1 - sold_fraction,
                "cumulative_sold_fraction": 1.0,
                "reason": "数据截止日强制估值",
                "score": "",
                "drawdown": "",
                "volume_ratio": "",
                "upper_wick_share": "",
                "taker_buy_share": "",
                "failure15": "",
                "weak1h": "",
                "regime4h": "",
                "mfe_at_decision": "",
                "active_stop": active_stop,
            }
        )
        last_decision_reason = "数据截止日强制估值"

    weighted_exit = sum(float(event["execution_price"]) * float(event["sold_fraction"]) for event in events)
    gross_return = weighted_exit / entry_price - 1
    one_x_net = sum(
        net_return(entry_price, float(event["execution_price"])) * float(event["sold_fraction"])
        for event in events
    )
    exit_limit_ms = full_exit_time if full_exit_time is not None else five[last_index]["close_time_ms"]
    held_rows = [row for row in five[entry_index:] if row["open_time_ms"] <= exit_limit_ms]
    mae = min(row["low"] for row in held_rows) / entry_price - 1
    actual_mfe = max(row["high"] for row in held_rows) / entry_price - 1
    benchmark_rows = [
        row for row in five[entry_index:] if row["open_time_ms"] < theoretical_end_ms
    ]
    theoretical_mfe = max(row["high"] for row in benchmark_rows) / entry_price - 1
    capture = gross_return / theoretical_mfe if theoretical_mfe > 0 else None
    forced_mark = any(event["reason"] == "数据截止日强制估值" for event in events)
    summary = {
        "case_id": case["case_id"],
        "model_version": model_version,
        "symbol": case["symbol"],
        "group": old_trade["group"],
        "signal_date": case["signal_date"],
        "entry_date": case["entry_date"],
        "entry_price": entry_price,
        "initial_atr15m": initial_atr,
        "initial_stop": initial_stop,
        "exit_time_utc": events[-1]["execution_time_utc"],
        "weighted_exit_price": weighted_exit,
        "exit_events": len(events),
        "last_exit_reason": last_decision_reason,
        "gross_return_1x": gross_return,
        "net_return_1x": one_x_net,
        "net_return_2x": LEVERAGE * one_x_net,
        "mae_1x": mae,
        "mfe_until_exit_1x": actual_mfe,
        "theoretical_14d_mfe_1x": theoretical_mfe,
        "mfe_capture_ratio": capture,
        "old_net_return_1x": float(old_trade["net_return"]),
        "old_net_return_2x": LEVERAGE * float(old_trade["net_return"]),
        "new_minus_old_2x": LEVERAGE * (one_x_net - float(old_trade["net_return"])),
        "right_censored_mark": forced_mark,
        "coverage_ratio": case["coverage_ratio"],
        "decision_count": decision_count,
    }
    return summary, events


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    fields: list[str] = []
    for row in rows:
        for field in row:
            if field not in fields:
                fields.append(field)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def pct(value: float | None) -> str:
    return "—" if value is None else f"{value * 100:+.2f}%"


def render_report(
    trades: list[dict[str, Any]],
    events: list[dict[str, Any]],
    model_version: str,
    output_trades: Path,
    output_events: Path,
) -> str:
    valid = [trade for trade in trades if not trade["right_censored_mark"]]
    new = [trade["net_return_2x"] for trade in valid]
    old = [trade["old_net_return_2x"] for trade in valid]
    wins = sum(value > 0 for value in new)
    improved = sum(trade["new_minus_old_2x"] > 0 for trade in valid)
    reason_counts = Counter(event["reason"] for event in events if event["cumulative_sold_fraction"] >= 1 - 1e-12)
    captures = [
        trade["mfe_capture_ratio"]
        for trade in valid
        if trade["mfe_capture_ratio"] is not None and trade["theoretical_14d_mfe_1x"] >= 0.20
    ]
    winner_captures = [
        trade["mfe_capture_ratio"]
        for trade in valid
        if trade["mfe_capture_ratio"] is not None and trade["gross_return_1x"] > 0
    ]
    trimmed_new = statistics.mean(sorted(new)[1:-1])
    trimmed_old = statistics.mean(sorted(old)[1:-1])
    lines = [
        f"# 多周期量化退出策略 {model_version.upper()} 回测",
        "",
        "## 回测口径",
        "",
        "- 固定原策略的26笔入场，只替换退出方法，以隔离退出模型的贡献；不因提前退出而新增原先被仓位上限挡住的入场。",
        "- 每5分钟收盘计算退出评分；15m、1h、4h只使用当时已经完成的K线；所有卖出均在下一根5m开盘成交。",
        f"- 双边各 {FRICTION_PER_SIDE * 100:.2f}% 摩擦；结果按 {LEVERAGE:.0f}× 杠杆线性换算，未计资金费率、穿价和爆仓。",
        (
            "- V1：退出评分在MFE达到50%后启动；MFE达到20%后抬高保护位；15%持仓峰值回撤直接清仓。"
            if model_version == "v1"
            else "- V2：强趋势下15%峰值回撤按25%/50%/75%分层减仓；最后25%为runner。runner的运行MFE达到500%/700%/900%后，分别采用20%/10%/5%的峰值回撤保护；未达到极端MFE时，仍需4h连续两根转弱、15m与1h同时转弱且评分≥0.85才清仓。"
        ),
        "- 两版均设置14天最长持有期，MFE达到20%后把保护位抬至覆盖双边摩擦。",
        "",
        "## 总体结果",
        "",
        f"| 指标 | 原日线退出 | 多周期退出{model_version.upper()} |",
        "|---|---:|---:|",
        f"| 有效交易数 | {len(valid)} | {len(valid)} |",
        f"| 2×等权平均收益 | {pct(statistics.mean(old))} | {pct(statistics.mean(new))} |",
        f"| 2×收益中位数 | {pct(statistics.median(old))} | {pct(statistics.median(new))} |",
        f"| 胜率 | {sum(value > 0 for value in old) / len(old):.1%} | {wins / len(new):.1%} |",
        f"| 最佳单笔 | {pct(max(old))} | {pct(max(new))} |",
        f"| 最差单笔 | {pct(min(old))} | {pct(min(new))} |",
        f"| 相对旧退出改善的交易 | — | {improved}/{len(valid)} |",
        f"| 去除最好/最差各1笔后的2×均值 | {pct(trimmed_old)} | {pct(trimmed_new)} |",
        f"| MFE≥20%交易的平均捕获率 | — | {pct(statistics.mean(captures)) if captures else '—'} |",
        f"| 盈利交易的平均MFE捕获率 | — | {pct(statistics.mean(winner_captures)) if winner_captures else '—'} |",
        "",
        "注意：等权平均是每笔交易分配相同本金后的横截面均值，不是把26笔无视时间重叠后顺序复利。",
        "",
        "## 每笔结果",
        "",
        "| 币种 | 信号日 | 退出时刻UTC | 分批次数 | 退出原因 | 2×新收益 | 2×旧收益 | 改善 | 14天MFE | 捕获率 |",
        "|---|---|---|---:|---|---:|---:|---:|---:|---:|",
    ]
    for trade in trades:
        lines.append(
            f"| {trade['symbol']} | {trade['signal_date']} | {trade['exit_time_utc']} | "
            f"{trade['exit_events']} | {trade['last_exit_reason']} | {pct(trade['net_return_2x'])} | "
            f"{pct(trade['old_net_return_2x'])} | {pct(trade['new_minus_old_2x'])} | "
            f"{pct(trade['theoretical_14d_mfe_1x'])} | {pct(trade['mfe_capture_ratio'])} |"
        )
    lines.extend(
        [
            "",
            "## 完整退出原因",
            "",
        ]
    )
    for reason, count in reason_counts.most_common():
        lines.append(f"- {reason}: {count}笔")
    if model_version == "v2":
        focus = {
            (trade["symbol"], trade["signal_date"]): trade
            for trade in trades
        }
        lines.extend(["", "## 三个重点样本", ""])
        for symbol, signal_date in (
            ("CYS", "2026-03-21"),
            ("BULLA", "2026-04-11"),
            ("VELVET", "2026-06-06"),
        ):
            trade = focus[(symbol, signal_date)]
            lines.append(
                f"- **{symbol}**：V2为 {pct(trade['net_return_2x'])}，原日线退出为 "
                f"{pct(trade['old_net_return_2x'])}，分{trade['exit_events']}次退出，"
                f"14天MFE捕获率 {pct(trade['mfe_capture_ratio'])}。"
            )
    lines.extend(
        [
            "",
            "## 结果解释",
            "",
            f"- 这是预先写死公式后的{model_version.upper()}诊断，不对单个币回看调参。若结果不理想，应先判断盈利激活、分层回撤或趋势确认的问题。",
            "- MFE捕获率使用完整14天窗口内的事后最高价，仅用于评价；策略决策没有读取该最高价。负收益或理论MFE很小时，捕获率可能为负或不稳定。",
            "- 数据截止强制估值的交易不进入总体统计；详细逐次减仓时刻及评分见事件CSV。",
            "",
            f"- 交易汇总：`{output_trades.name}`",
            f"- 分批退出事件：`{output_events.name}`",
            f"- 数据清单：`{MANIFEST.relative_to(HERE)}`",
        ]
    )
    return "\n".join(lines) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model-version", choices=("v1", "v2"), default="v2")
    args = parser.parse_args()
    output_trades, output_events, output_report = output_paths(args.model_version)
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    with TRADES_CSV.open(encoding="utf-8", newline="") as handle:
        old_trades = list(csv.DictReader(handle))
    if len(manifest["cases"]) != len(old_trades):
        raise ValueError("manifest/trades length mismatch")
    summaries = []
    events = []
    for case, old_trade in zip(manifest["cases"], old_trades, strict=True):
        if case["symbol"] != old_trade["symbol"] or case["signal_date"] != old_trade["signal_date"]:
            raise ValueError(f"case order mismatch: {case['case_id']}")
        summary, case_events = backtest_case(case, old_trade, args.model_version)
        summaries.append(summary)
        events.extend(case_events)
    write_csv(output_trades, summaries)
    write_csv(output_events, events)
    output_report.write_text(
        render_report(summaries, events, args.model_version, output_trades, output_events),
        encoding="utf-8",
    )
    print(f"wrote {output_trades}")
    print(f"wrote {output_events}")
    print(f"wrote {output_report}")


if __name__ == "__main__":
    main()
