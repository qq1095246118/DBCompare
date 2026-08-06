#!/usr/bin/env python3
"""Causal 2x long backtest over every token in the F1-F6 dashboard dataset."""

from __future__ import annotations

import csv
import json
import re
import statistics
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable


ROOT = Path(__file__).resolve().parent
PROJECT_ROOT = ROOT.parents[1]
DASHBOARD_PATH = (
    PROJECT_ROOT
    / "analysis/binance-bubblemaps-factor-kline-2026-07-30/"
    "factor-kline-dashboard.html"
)
RESULTS_PATH = ROOT / "results.json"
TRADES_PATH = ROOT / "trades.csv"
REPORT_PATH = ROOT / "report.md"

LEVERAGE = 2.0
ONE_WAY_NOTIONAL_COST_RATE = 0.002
ROUND_TRIP_EQUITY_COST_PCT = (
    LEVERAGE * ONE_WAY_NOTIONAL_COST_RATE * 2 * 100
)
MAX_HOLD_DAYS = 30

FACTOR_NAMES = {
    0: "F1总转账量",
    1: "F2转账笔数",
    2: "F3活跃地址",
    3: "F4新地址",
    4: "F5巨额转账",
    5: "F6净流冲击",
}


def load_dashboard_data() -> dict[str, Any]:
    text = DASHBOARD_PATH.read_text(encoding="utf-8")
    match = re.search(r"^const DATA = (.*);\nconst FACTORS", text, re.MULTILINE)
    if not match:
        raise ValueError(f"cannot locate embedded DATA in {DASHBOARD_PATH}")
    return json.loads(match.group(1))


def pct(exit_price: float, entry_price: float) -> float:
    return (exit_price / entry_price - 1.0) * 100.0


def fmt_pct(value: float | None) -> str:
    return "N/A" if value is None else f"{value:+.2f}%"


def fmt_num(value: float | None, digits: int = 2) -> str:
    return "N/A" if value is None else f"{value:.{digits}f}"


def episode_start_indices(
    bars: list[dict[str, Any]],
    predicate: Callable[[dict[str, Any]], bool],
) -> list[int]:
    """Collapse consecutive true days into one signal at the first day's close."""
    starts: list[int] = []
    previous_active = False
    for index, bar in enumerate(bars):
        active = predicate(bar)
        if active and not previous_active:
            starts.append(index)
        previous_active = active
    return starts


def signal_candidates(
    token: dict[str, Any], universe: str
) -> list[dict[str, Any]]:
    bars = token["bars"]
    if universe == "composite_2plus":
        indices = episode_start_indices(bars, lambda bar: len(bar["sig"]) >= 2)
        return [
            {
                "index": index,
                "confirmation_date": bars[index]["d"],
                "description": "六因子至少2项触发",
                "factor_indices": bars[index]["sig"],
            }
            for index in indices
        ]
    if universe == "calibrated_buy":
        indices = episode_start_indices(bars, lambda bar: bool(bar["buy"]))
        return [
            {
                "index": index,
                "confirmation_date": bars[index]["d"],
                "description": "；".join(bars[index]["buy"]),
                "factor_indices": bars[index]["sig"],
            }
            for index in indices
        ]
    if universe == "any_factor":
        indices = episode_start_indices(bars, lambda bar: bool(bar["sig"]))
        return [
            {
                "index": index,
                "confirmation_date": bars[index]["d"],
                "description": "六因子至少1项触发",
                "factor_indices": bars[index]["sig"],
            }
            for index in indices
        ]
    if universe == "price_event":
        date_to_index = {bar["d"]: index for index, bar in enumerate(bars)}
        candidates = []
        for event in token["events"]:
            if event["start"] not in date_to_index:
                continue
            index = date_to_index[event["start"]]
            candidates.append(
                {
                    "index": index,
                    "confirmation_date": event["start"],
                    "description": (
                        f"|日涨跌|≥20%，事件#{event['id']}，"
                        f"首日{event['start_return']:+.2f}%"
                    ),
                    "factor_indices": bars[index]["sig"],
                }
            )
        return candidates
    raise ValueError(f"unknown signal universe: {universe}")


def mark_trade(
    *,
    token: dict[str, Any],
    universe: str,
    candidate: dict[str, Any],
) -> dict[str, Any]:
    bars = token["bars"]
    confirmation_index = candidate["index"]
    trade: dict[str, Any] = {
        "universe": universe,
        "symbol": token["symbol"],
        "group": token["group"],
        "confirmation_date": candidate["confirmation_date"],
        "signal_description": candidate["description"],
        "factor_indices": candidate["factor_indices"],
        "factors": [
            FACTOR_NAMES[index] for index in candidate["factor_indices"]
        ],
        "status": "not_entered",
    }

    entry_index = confirmation_index + 1
    if entry_index >= len(bars):
        trade["reason"] = "确认日之后没有下一根日K"
        trade["_terminal_index"] = confirmation_index
        return trade

    entry_bar = bars[entry_index]
    entry_price = float(entry_bar["o"])
    liquidation_price = entry_price * 0.5
    trade.update(
        {
            "entry_date": entry_bar["d"],
            "entry_price": entry_price,
            "entry_volume": float(entry_bar["v"]),
            "liquidation_price": liquidation_price,
        }
    )

    last_index = min(entry_index + MAX_HOLD_DAYS, len(bars) - 1)
    exit_index: int | None = None
    exit_price: float | None = None
    exit_signal_index: int | None = None
    peak_index_seen = entry_index
    peak_volume_seen = float(entry_bar["v"])
    exit_reason: str | None = None

    for current_index in range(entry_index, last_index + 1):
        bar = bars[current_index]
        current_low = float(bar["l"])
        current_volume = float(bar["v"])

        if current_low <= liquidation_price:
            exit_index = current_index
            exit_price = liquidation_price
            exit_reason = "2×理想化强平：盘中最低价触及入场价50%"
            break

        if current_volume > peak_volume_seen:
            peak_volume_seen = current_volume
            peak_index_seen = current_index

        if current_index == entry_index:
            continue

        previous_index = current_index - 1
        previous_volume = float(bars[previous_index]["v"])
        prior_max = max(
            float(item["v"]) for item in bars[entry_index:current_index]
        )
        if current_volume < previous_volume and previous_volume >= prior_max:
            exit_signal_index = current_index
            next_index = current_index + 1
            if next_index < len(bars):
                exit_index = next_index
                exit_price = float(bars[next_index]["o"])
                exit_reason = (
                    f"{bars[previous_index]['d']}成交量为持仓以来最高，"
                    f"{bar['d']}首次缩小，下一日开盘卖出"
                )
            else:
                trade["status"] = "open_waiting_exit"
                trade["reason"] = "缩量已经确认，但样本截止前尚无下一日开盘"
            break

    if exit_index is None and trade["status"] != "open_waiting_exit":
        if last_index < len(bars) - 1:
            exit_index = last_index
            exit_price = float(bars[last_index]["c"])
            exit_reason = "达到30日最大持有期，按当日收盘退出"
        else:
            trade["status"] = "open_marked"
            trade["reason"] = "截至样本末日仍未触发缩量退出"

    terminal_index = exit_index if exit_index is not None else last_index
    observed = bars[entry_index : terminal_index + 1]
    marked_price = (
        exit_price if exit_price is not None else float(bars[terminal_index]["c"])
    )
    underlying_return = pct(marked_price, entry_price)
    liquidated = exit_reason is not None and "强平" in exit_reason
    leveraged_gross = -100.0 if liquidated else LEVERAGE * underlying_return
    leveraged_net = (
        -100.0
        if liquidated
        else leveraged_gross - ROUND_TRIP_EQUITY_COST_PCT
    )

    if exit_index is not None:
        trade["status"] = "closed"
    trade.update(
        {
            "exit_signal_date": (
                bars[exit_signal_index]["d"] if exit_signal_index is not None else None
            ),
            "exit_date": bars[exit_index]["d"] if exit_index is not None else None,
            "exit_price": exit_price,
            "exit_reason": exit_reason,
            "mark_date": bars[terminal_index]["d"],
            "mark_price": marked_price,
            "peak_volume_date_seen": bars[peak_index_seen]["d"],
            "peak_volume_seen": peak_volume_seen,
            "holding_days": terminal_index - entry_index,
            "underlying_return_pct": underlying_return,
            "leveraged_gross_return_pct": leveraged_gross,
            "leveraged_net_return_pct": leveraged_net,
            "leveraged_mae_pct": max(
                -100.0,
                LEVERAGE
                * pct(min(float(item["l"]) for item in observed), entry_price),
            ),
            "leveraged_mfe_pct": LEVERAGE
            * pct(max(float(item["h"]) for item in observed), entry_price),
            "round_trip_equity_cost_pct": ROUND_TRIP_EQUITY_COST_PCT,
            "_terminal_index": terminal_index,
        }
    )
    return trade


def run_token(
    token: dict[str, Any], universe: str
) -> tuple[list[dict[str, Any]], int]:
    """Run one non-pyramiding position per token and count suppressed signals."""
    trades = []
    skipped_while_holding = 0
    next_eligible_confirmation_index = 0
    for candidate in signal_candidates(token, universe):
        confirmation_index = candidate["index"]
        if confirmation_index < next_eligible_confirmation_index:
            skipped_while_holding += 1
            continue
        trade = mark_trade(token=token, universe=universe, candidate=candidate)
        next_eligible_confirmation_index = max(
            confirmation_index + 1, trade["_terminal_index"]
        )
        trades.append(trade)
    return trades, skipped_while_holding


def trimmed_mean(values: list[float], fraction: float = 0.1) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    trim_count = int(len(ordered) * fraction)
    if trim_count == 0:
        return statistics.mean(ordered)
    kept = ordered[trim_count:-trim_count]
    return statistics.mean(kept) if kept else None


def summarize(trades: list[dict[str, Any]]) -> dict[str, Any]:
    closed = [trade for trade in trades if trade["status"] == "closed"]
    returns = [trade["leveraged_net_return_pct"] for trade in closed]
    wins = sum(value > 0 for value in returns)
    return {
        "signals_traded": len(trades),
        "closed_trades": len(closed),
        "open_or_unentered": len(trades) - len(closed),
        "wins": wins,
        "losses": len(closed) - wins,
        "win_rate_pct": wins / len(closed) * 100 if closed else None,
        "mean_return_pct": statistics.mean(returns) if returns else None,
        "median_return_pct": statistics.median(returns) if returns else None,
        "trimmed_10pct_mean_return_pct": trimmed_mean(returns),
        "best_return_pct": max(returns) if returns else None,
        "worst_return_pct": min(returns) if returns else None,
        "average_holding_days": (
            statistics.mean(trade["holding_days"] for trade in closed)
            if closed
            else None
        ),
        "liquidations": sum(
            "强平" in (trade.get("exit_reason") or "") for trade in closed
        ),
        "positive_expectancy": (
            statistics.mean(returns) > 0 if returns else None
        ),
    }


def grouped_summary(
    trades: list[dict[str, Any]], key: str
) -> dict[str, dict[str, Any]]:
    values = sorted({trade[key] for trade in trades})
    return {
        value: summarize([trade for trade in trades if trade[key] == value])
        for value in values
    }


def scope_summary(trades: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {
        "全部样本": summarize(trades),
        "样本内": summarize(
            [trade for trade in trades if trade["group"] == "样本内"]
        ),
        "样本外合计": summarize(
            [trade for trade in trades if trade["group"] != "样本内"]
        ),
    }


def factor_counts(trades: list[dict[str, Any]]) -> dict[str, int]:
    counts = {name: 0 for name in FACTOR_NAMES.values()}
    for trade in trades:
        for factor in trade["factors"]:
            counts[factor] += 1
    return counts


def clean_trade(trade: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in trade.items() if not key.startswith("_")}


def write_csv(trades: list[dict[str, Any]]) -> None:
    fields = [
        "universe",
        "symbol",
        "group",
        "confirmation_date",
        "signal_description",
        "factors",
        "status",
        "entry_date",
        "entry_price",
        "peak_volume_date_seen",
        "exit_signal_date",
        "exit_date",
        "exit_price",
        "holding_days",
        "underlying_return_pct",
        "leveraged_net_return_pct",
        "leveraged_mae_pct",
        "leveraged_mfe_pct",
        "exit_reason",
    ]
    with TRADES_PATH.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for trade in trades:
            row = {field: trade.get(field) for field in fields}
            row["factors"] = " + ".join(trade["factors"])
            writer.writerow(row)


def summary_row(label: str, summary: dict[str, Any]) -> str:
    return (
        f"| {label} | {summary['closed_trades']} | "
        f"{summary['open_or_unentered']} | {fmt_pct(summary['win_rate_pct'])} | "
        f"{fmt_pct(summary['mean_return_pct'])} | "
        f"{fmt_pct(summary['median_return_pct'])} | "
        f"{fmt_pct(summary['trimmed_10pct_mean_return_pct'])} | "
        f"{fmt_pct(summary['best_return_pct'])} | "
        f"{fmt_pct(summary['worst_return_pct'])} | "
        f"{fmt_num(summary['average_holding_days'])} | "
        f"{summary['liquidations']} |"
    )


def build_report(output: dict[str, Any]) -> str:
    universe_labels = {
        "composite_2plus": "复合异常（≥2因子，主口径）",
        "calibrated_buy": "校准买入观察（F1≥10或F3≥5）",
        "any_factor": "任一因子异常（敏感性）",
        "price_event": "价格异动已发生（对照，不是前置信号）",
    }
    primary = output["universes"]["composite_2plus"]
    all_summary = primary["by_scope"]["全部样本"]
    out_summary = primary["by_scope"]["样本外合计"]
    lines = [
        "# 全部样本：六因子异常后 2× 买入、成交量缩小后卖出",
        "",
        f"- 生成时间：{output['generated_at']}",
        f"- 数据：{output['data']['token_count']} 个币，"
        f"{output['data']['daily_bar_count']:,} 根 Binance 合约日 K；"
        f"行情截止 {output['data']['as_of_range']}。",
        "- 主口径：D 日六因子中至少两项达到基础异常阈值，D 日收盘确认；"
        "连续触发合并，只取第一天。D+1 开盘以 2× 做多。",
        "- 离场：若昨日成交量为入场以来最高、今日成交量低于昨日，"
        "则今日收盘确认缩量，下一日开盘卖出。全程只用当时已完成的数据。",
        f"- 摩擦：每边按名义本金 {ONE_WAY_NOTIONAL_COST_RATE * 100:.2f}% "
        f"（手续费+滑点），2× 仓位往返合计扣权益 "
        f"{ROUND_TRIP_EQUITY_COST_PCT:.2f}%；不计资金费率。",
        "- 同一币种不叠加仓位；持仓内的新信号忽略。最大持有 30 日；"
        "盘中最低价触及入场价 50% 按理想化强平、权益 -100% 处理。",
        "",
        "## 先看结论",
        "",
        f"- 全部 13 币的 {all_summary['closed_trades']} 笔已平仓交易："
        f"平均 {fmt_pct(all_summary['mean_return_pct'])}，"
        f"但中位数 {fmt_pct(all_summary['median_return_pct'])}，"
        f"10% 去极值均值 {fmt_pct(all_summary['trimmed_10pct_mean_return_pct'])}。",
        f"- 8 个样本外币合计 {out_summary['closed_trades']} 笔："
        f"胜率 {fmt_pct(out_summary['win_rate_pct'])}，"
        f"平均 {fmt_pct(out_summary['mean_return_pct'])}，"
        f"中位数 {fmt_pct(out_summary['median_return_pct'])}，"
        f"10% 去极值均值 {fmt_pct(out_summary['trimmed_10pct_mean_return_pct'])}。",
        "- 因此不能把全部样本的正平均值解释为稳定策略收益："
        "它主要由样本内少数数倍上涨拉高；样本外和去极值结果均为负。",
        "",
        "主结果是逐笔等权统计，不是假定所有重叠交易都能同时占用整份本金的组合复利。"
        "10%去极值均值会同时删去两端各10%的交易。",
        "",
        "## 四种信号口径",
        "",
        "| 信号口径 | 已平仓 | 未平/未入 | 胜率 | 平均2×净收益 | 中位数 | 10%去极值均值 | 最好 | 最差 | 平均持有(天) | 强平 |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for universe in (
        "composite_2plus",
        "calibrated_buy",
        "any_factor",
        "price_event",
    ):
        lines.append(
            summary_row(
                universe_labels[universe],
                output["universes"][universe]["summary"],
            )
        )

    lines.extend(
        [
            "",
            "价格异动口径把“当天已经涨跌超过20%”当确认，因此只能作为"
            "追涨/抄底对照，不能拿来证明链上因子有预测力。",
            "",
            "## 主口径：全样本 / 样本内 / 样本外",
            "",
            "| 范围 | 已平仓 | 未平/未入 | 胜率 | 平均2×净收益 | 中位数 | 10%去极值均值 | 最好 | 最差 | 平均持有(天) | 强平 |",
            "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
        ]
    )
    for scope, summary in primary["by_scope"].items():
        lines.append(summary_row(scope, summary))

    lines.extend(
        [
            "",
            "样本外再拆分：",
            "",
            "| 分组 | 已平仓 | 未平/未入 | 胜率 | 平均2×净收益 | 中位数 | 10%去极值均值 | 最好 | 最差 | 平均持有(天) | 强平 |",
            "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
        ]
    )
    for group, summary in primary["by_group"].items():
        if group != "样本内":
            lines.append(summary_row(group, summary))

    lines.extend(
        [
            "",
            "## 主口径：逐币结果",
            "",
            "| 币种 | 已平仓 | 未平/未入 | 胜率 | 平均2×净收益 | 中位数 | 10%去极值均值 | 最好 | 最差 | 平均持有(天) | 强平 |",
            "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
        ]
    )
    for symbol, summary in primary["by_symbol"].items():
        lines.append(summary_row(symbol, summary))

    lines.extend(
        [
            "",
            "## 主口径逐笔交易",
            "",
            "| 币种 | 分组 | 异常确认 | 触发因子 | 次日开盘买入 | 成交量峰值 | 缩量确认 | 卖出 | 持有 | 标的收益 | 2×净收益 | MAE(2×) | 状态 |",
            "|---|---|---:|---|---:|---:|---:|---:|---:|---:|---:|---:|---|",
        ]
    )
    for trade in primary["trades"]:
        lines.append(
            f"| {trade['symbol']} | {trade['group']} | "
            f"{trade['confirmation_date']} | "
            f"{' + '.join(trade['factors']) or '无'} | "
            f"{trade.get('entry_date') or 'N/A'} | "
            f"{trade.get('peak_volume_date_seen') or 'N/A'} | "
            f"{trade.get('exit_signal_date') or 'N/A'} | "
            f"{trade.get('exit_date') or 'N/A'} | "
            f"{trade.get('holding_days', 'N/A')} | "
            f"{fmt_pct(trade.get('underlying_return_pct'))} | "
            f"{fmt_pct(trade.get('leveraged_net_return_pct'))} | "
            f"{fmt_pct(trade.get('leveraged_mae_pct'))} | "
            f"{trade['status']} |"
        )

    lines.extend(
        [
            "",
            "## 复核边界",
            "",
            "- 六因子 D 日值只使用 D-7 至 D-1 的链上转账，"
            "不会偷看 D 日收盘后的链上数据；本回测仍保守地等到 D 日收盘才确认。",
            "- 当前链上数据来自本次 Bubblemaps 快照重建的历史成员转账，"
            "不是逐日保存的历史持仓拓扑；成员集合漂移仍可能带来幸存者偏差。",
            "- 日 K 无法知道盘中成交量与最低价的先后顺序；"
            "强平判断优先于日终缩量信号，是偏保守处理。",
            "- 小币 2× 实盘会受资金费率、盘口深度、限价成交、"
            "保险基金和交易所维持保证金规则影响，结果通常会比本表更差。",
            "",
            f"完整机器可读结果见 `{RESULTS_PATH.name}`；"
            f"全部四种口径逐笔记录见 `{TRADES_PATH.name}`。",
            "",
        ]
    )
    return "\n".join(lines)


def main() -> None:
    data = load_dashboard_data()
    universes = (
        "composite_2plus",
        "calibrated_buy",
        "any_factor",
        "price_event",
    )
    output_universes: dict[str, Any] = {}
    all_trades: list[dict[str, Any]] = []

    for universe in universes:
        raw_trades = []
        skipped = 0
        for token in data["tokens"]:
            token_trades, token_skipped = run_token(token, universe)
            raw_trades.extend(token_trades)
            skipped += token_skipped
        raw_trades.sort(
            key=lambda trade: (
                trade["confirmation_date"],
                trade["symbol"],
            )
        )
        cleaned = [clean_trade(trade) for trade in raw_trades]
        output_universes[universe] = {
            "summary": summarize(cleaned),
            "by_scope": scope_summary(cleaned),
            "by_group": grouped_summary(cleaned, "group"),
            "by_symbol": grouped_summary(cleaned, "symbol"),
            "factor_counts_at_confirmation": factor_counts(cleaned),
            "signals_skipped_while_holding": skipped,
            "trades": cleaned,
        }
        all_trades.extend(cleaned)

    first_dates = [
        token["bars"][0]["d"] for token in data["tokens"] if token["bars"]
    ]
    last_dates = [
        token["bars"][-1]["d"] for token in data["tokens"] if token["bars"]
    ]
    output = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "parameters": {
            "leverage": LEVERAGE,
            "one_way_notional_cost_rate": ONE_WAY_NOTIONAL_COST_RATE,
            "round_trip_equity_cost_pct": ROUND_TRIP_EQUITY_COST_PCT,
            "max_hold_days": MAX_HOLD_DAYS,
            "entry": "next daily bar open after confirmation close",
            "exit": (
                "when previous daily volume is the highest since entry and "
                "current daily volume is lower, exit at the next daily bar open"
            ),
            "liquidation": "intraday low <= 50% of entry price",
            "position_rule": "one position per symbol; no pyramiding",
        },
        "data": {
            "dashboard": str(DASHBOARD_PATH.relative_to(PROJECT_ROOT)),
            "dashboard_generated_at": data["generated_at"],
            "price_source": data["price_source"],
            "chain_source": data["chain_source"],
            "factor_window": data["factor_window"],
            "token_count": len(data["tokens"]),
            "daily_bar_count": sum(
                len(token["bars"]) for token in data["tokens"]
            ),
            "price_event_count": sum(
                len(token["events"]) for token in data["tokens"]
            ),
            "as_of_range": f"{min(first_dates)}至{max(last_dates)}",
        },
        "universes": output_universes,
    }
    RESULTS_PATH.write_text(
        json.dumps(output, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    write_csv(all_trades)
    REPORT_PATH.write_text(build_report(output), encoding="utf-8")
    print(f"wrote {RESULTS_PATH}")
    print(f"wrote {TRADES_PATH}")
    print(f"wrote {REPORT_PATH}")


if __name__ == "__main__":
    main()
