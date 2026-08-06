#!/usr/bin/env python3
"""Backtest 2x long entries followed by a causal daily-volume contraction exit."""

from __future__ import annotations

import json
import statistics
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from analyze_price_events import (
    EVENTS_PATH,
    PRICE_EVENTS_PATH,
    RESULTS_PATH as PRICE_RESULTS_PATH,
    parse_day,
    select_price_series,
    spot_bars,
)


ROOT = Path(__file__).resolve().parent
RESULTS_PATH = ROOT / "backtest-volume-results.json"
REPORT_PATH = ROOT / "backtest-volume-report.md"

LEVERAGE = 2.0
ONE_WAY_COST_RATE = 0.002  # 10 bps fee + 10 bps slippage on notional, per side.
ROUND_TRIP_EQUITY_COST_PCT = LEVERAGE * ONE_WAY_COST_RATE * 2 * 100
MAX_HOLD_DAYS = 30
AS_OF_DAY = date(2026, 7, 28)  # Last fully closed UTC day at run time.
SPOT_PREFERRED = {"SYN", "DEXE"}


def pct(numerator: float, denominator: float) -> float:
    return (numerator / denominator - 1.0) * 100.0


def fmt(value: float | None) -> str:
    if value is None:
        return "N/A"
    return f"{value:+.2f}%"


def source_label(source: dict[str, Any] | None) -> str:
    if not source:
        return "无行情"
    if source.get("kind") == "Binance Web3 DEX":
        return f"Web3/{source.get('chain')}"
    return source.get("kind", "未知").replace("Binance ", "")


def backtest_trade(
    symbol: str,
    event_date: date,
    signal: dict[str, Any],
    source: dict[str, Any] | None,
    bars: list[dict[str, Any]],
) -> dict[str, Any]:
    by_day = {parse_day(bar["date"]): bar for bar in bars}
    if signal["basis"] == "窗口起点":
        confirmation_day = event_date - timedelta(days=1)
        confirmation_rule = "W-1 完整窗口在事件前一日收盘确认"
    else:
        confirmation_day = parse_day(signal["date"])
        confirmation_rule = "异常交易日在当日收盘确认"
    entry_day = confirmation_day + timedelta(days=1)
    entry_bar = by_day.get(entry_day)
    trade: dict[str, Any] = {
        "symbol": symbol,
        "signal_date": signal["date"],
        "signal_basis": signal["basis"],
        "signal": signal["signal"],
        "confirmation_date": confirmation_day.isoformat(),
        "confirmation_rule": confirmation_rule,
        "event_date": event_date.isoformat(),
        "entry_date": entry_day.isoformat(),
        "source": source,
        "status": "not_entered",
    }
    if entry_bar is None:
        trade["reason"] = "确认后的下一自然日没有可用日 K"
        return trade

    entry_price = float(entry_bar["open"])
    trade["entry_price"] = entry_price
    trade["entry_volume"] = float(entry_bar.get("volume") or 0)
    trade["price_quality"] = (
        "低流动性"
        if source
        and source.get("kind") == "Binance Web3 DEX"
        and (
            float(entry_bar.get("volume") or 0) < 10_000
            or (
                entry_bar.get("trade_count") is not None
                and int(entry_bar["trade_count"]) < 50
            )
        )
        else "正常"
    )

    last_day = min(entry_day + timedelta(days=MAX_HOLD_DAYS), AS_OF_DAY)
    holding = []
    current = entry_day
    while current <= last_day:
        bar = by_day.get(current)
        if bar:
            holding.append(bar)
        current += timedelta(days=1)
    if not holding:
        trade["reason"] = "入场后没有可用日 K"
        return trade

    liquidation_price = entry_price * 0.5
    exit_signal_day: date | None = None
    exit_day: date | None = None
    exit_price: float | None = None
    exit_reason: str | None = None
    peak_volume_day: date | None = None
    peak_volume = -1.0

    for index, bar in enumerate(holding):
        bar_day = parse_day(bar["date"])
        if float(bar["low"]) <= liquidation_price:
            exit_day = bar_day
            exit_price = liquidation_price
            exit_reason = "2x 理想化强平（盘中最低价触及入场价的50%）"
            break

        volume = float(bar.get("volume") or 0)
        if volume > peak_volume:
            peak_volume = volume
            peak_volume_day = bar_day

        if index == 0:
            continue
        previous = holding[index - 1]
        previous_day = parse_day(previous["date"])
        previous_volume = float(previous.get("volume") or 0)
        prior_max = max(float(item.get("volume") or 0) for item in holding[:index])
        if volume < previous_volume and previous_volume >= prior_max:
            exit_signal_day = bar_day
            next_day = bar_day + timedelta(days=1)
            next_bar = by_day.get(next_day)
            if next_bar:
                exit_day = next_day
                exit_price = float(next_bar["open"])
                exit_reason = (
                    f"{previous_day.isoformat()} 成交量创持仓新高，"
                    f"{bar_day.isoformat()} 首次缩量，下一日开盘卖出"
                )
            else:
                exit_day = bar_day
                exit_price = float(bar["close"])
                exit_reason = "缩量已确认，但下一日无日 K；按确认日收盘标记"
            break

    if exit_day is None:
        last_bar = holding[-1]
        exit_day = parse_day(last_bar["date"])
        exit_price = float(last_bar["close"])
        exit_reason = (
            "达到最大持有期收盘退出"
            if exit_day >= entry_day + timedelta(days=MAX_HOLD_DAYS)
            else "截至回测日仍未触发，按最后完整日收盘标记"
        )

    underlying_return = pct(exit_price, entry_price)
    if exit_price <= liquidation_price:
        leveraged_gross = -100.0
        leveraged_net = -100.0
    else:
        leveraged_gross = LEVERAGE * underlying_return
        leveraged_net = leveraged_gross - ROUND_TRIP_EQUITY_COST_PCT

    lows = [float(bar["low"]) for bar in holding if parse_day(bar["date"]) <= exit_day]
    highs = [float(bar["high"]) for bar in holding if parse_day(bar["date"]) <= exit_day]
    trade.update(
        {
            "status": (
                "open_marked"
                if exit_reason.startswith("截至回测日")
                else "closed"
            ),
            "exit_signal_date": exit_signal_day.isoformat() if exit_signal_day else None,
            "exit_date": exit_day.isoformat(),
            "exit_price": exit_price,
            "exit_reason": exit_reason,
            "peak_volume_date_seen": peak_volume_day.isoformat() if peak_volume_day else None,
            "peak_volume_seen": peak_volume if peak_volume >= 0 else None,
            "holding_days": (exit_day - entry_day).days,
            "underlying_return_pct": underlying_return,
            "leveraged_gross_return_pct": leveraged_gross,
            "leveraged_net_return_pct": leveraged_net,
            "leveraged_mae_pct": max(-100.0, LEVERAGE * pct(min(lows), entry_price)),
            "leveraged_mfe_pct": LEVERAGE * pct(max(highs), entry_price),
            "round_trip_equity_cost_pct": ROUND_TRIP_EQUITY_COST_PCT,
        }
    )
    return trade


def summarize(trades: list[dict[str, Any]]) -> dict[str, Any]:
    entered = [trade for trade in trades if trade["status"] != "not_entered"]
    returns = [trade["leveraged_net_return_pct"] for trade in entered]
    return {
        "trades": len(entered),
        "wins": sum(value > 0 for value in returns),
        "losses": sum(value <= 0 for value in returns),
        "win_rate_pct": (sum(value > 0 for value in returns) / len(returns) * 100) if returns else None,
        "mean_return_pct": statistics.mean(returns) if returns else None,
        "median_return_pct": statistics.median(returns) if returns else None,
        "equal_weight_portfolio_return_pct": statistics.mean(returns) if returns else None,
        "best_return_pct": max(returns) if returns else None,
        "worst_return_pct": min(returns) if returns else None,
        "liquidations": sum("强平" in trade.get("exit_reason", "") for trade in entered),
        "average_holding_days": (
            statistics.mean(trade["holding_days"] for trade in entered) if entered else None
        ),
    }


def build_report(output: dict[str, Any]) -> str:
    all_summary = output["summary"]["all"]
    robust_summary = output["summary"]["normal_liquidity"]
    conservative_summary = output["summary"]["normal_liquidity_ex_velvet"]
    lines = [
        "# 2×杠杆：异常确认后买入、成交量峰值缩小后卖出",
        "",
        f"- 回测生成时间：{output['generated_at']}",
        f"- 最后完整 UTC 日：{output['as_of_day']}",
        "- 入场：异常确认后的下一自然日开盘。",
        "- 离场：昨日成交量为持仓以来最高、今日成交量首次下降；今日收盘确认，下一自然日开盘卖出。",
        f"- 杠杆：{LEVERAGE:.0f}×固定名义杠杆；每边手续费+滑点 {ONE_WAY_COST_RATE * 100:.2f}%（权益往返成本 {ROUND_TRIP_EQUITY_COST_PCT:.2f}%）。",
        "- 强平：盘中最低价触及入场价的50%时，按权益归零处理；未模拟交易所维持保证金、资金费率和跳空。",
        "",
        "## 汇总",
        "",
        "| 口径 | 交易数 | 胜率 | 平均每笔 | 中位数 | 最好 | 最差 | 平均持有 | 强平 |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|",
        f"| 全部样本 | {all_summary['trades']} | {fmt(all_summary['win_rate_pct'])} | "
        f"{fmt(all_summary['mean_return_pct'])} | {fmt(all_summary['median_return_pct'])} | "
        f"{fmt(all_summary['best_return_pct'])} | {fmt(all_summary['worst_return_pct'])} | "
        f"{all_summary['average_holding_days']:.2f}天 | {all_summary['liquidations']} |",
        f"| 剔除低流动性 | {robust_summary['trades']} | {fmt(robust_summary['win_rate_pct'])} | "
        f"{fmt(robust_summary['mean_return_pct'])} | {fmt(robust_summary['median_return_pct'])} | "
        f"{fmt(robust_summary['best_return_pct'])} | {fmt(robust_summary['worst_return_pct'])} | "
        f"{robust_summary['average_holding_days']:.2f}天 | {robust_summary['liquidations']} |",
        f"| 再剔除 VELVET 极端样本 | {conservative_summary['trades']} | "
        f"{fmt(conservative_summary['win_rate_pct'])} | "
        f"{fmt(conservative_summary['mean_return_pct'])} | "
        f"{fmt(conservative_summary['median_return_pct'])} | "
        f"{fmt(conservative_summary['best_return_pct'])} | "
        f"{fmt(conservative_summary['worst_return_pct'])} | "
        f"{conservative_summary['average_holding_days']:.2f}天 | "
        f"{conservative_summary['liquidations']} |",
        "",
        "## 逐笔交易",
        "",
        "| Token | 行情/成交量源 | 异常确认 | 买入 | 成交量峰值 | 缩量确认 | 卖出 | 持有 | 标的涨跌 | 2×净收益 | MAE(2×) | 质量 |",
        "|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|",
    ]
    for trade in output["trades"]:
        if trade["status"] == "not_entered":
            lines.append(
                f"| {trade['symbol']} | {source_label(trade.get('source'))} | "
                f"{trade['confirmation_date']} | N/A | N/A | N/A | "
                f"N/A | N/A | N/A | N/A | N/A | 未入场 |"
            )
            continue
        lines.append(
            f"| {trade['symbol']} | {source_label(trade.get('source'))} | "
            f"{trade['confirmation_date']} | {trade['entry_date']} | "
            f"{trade.get('peak_volume_date_seen') or 'N/A'} | "
            f"{trade.get('exit_signal_date') or 'N/A'} | {trade['exit_date']} | "
            f"{trade['holding_days']}天 | {fmt(trade['underlying_return_pct'])} | "
            f"{fmt(trade['leveraged_net_return_pct'])} | {fmt(trade['leveraged_mae_pct'])} | "
            f"{trade['price_quality']} |"
        )
    lines.extend(
        [
            "",
            "## 重要解释",
            "",
            "- 这不是“先知道全样本最大成交量在哪一天”的事后最优卖法；它只使用当时已经完成的日线。",
            "- 但样本本身来自已知价格异动事件，且没有纳入日常出现却未伴随行情的假阳性，因此仍是事件条件回测，不是样本外策略业绩。",
            "- 同时发生的交易按等权独立仓位汇总，平均每笔收益也等于等权组合收益；没有把14笔交易顺序复利。",
            "- DEX 的成交量口径用于判断相对放大/缩小，不与 CEX 成交量绝对值混合比较。",
            "- 2×杠杆放大价格误差和滑点；低流动性样本的实际成交结果可能显著差于日 K 回测。",
            "",
        ]
    )
    return "\n".join(lines)


def main() -> None:
    events = json.loads(EVENTS_PATH.read_text())["symbols"]
    price_config = json.loads(PRICE_EVENTS_PATH.read_text())
    price_results = json.loads(PRICE_RESULTS_PATH.read_text())
    sources = {token["symbol"]: token.get("source") for token in price_results["tokens"]}
    signals = price_config["signal_definitions"]
    trades: list[dict[str, Any]] = []

    for key, signal in signals.items():
        symbol, event_text = key.split("|", 1)
        event_day = parse_day(event_text)
        if signal["basis"] == "窗口起点":
            confirmation_day = event_day - timedelta(days=1)
        else:
            confirmation_day = parse_day(signal["date"])
        start = confirmation_day - timedelta(days=2)
        end = min(confirmation_day + timedelta(days=MAX_HOLD_DAYS + 3), AS_OF_DAY)
        if symbol in SPOT_PREFERRED:
            bars = spot_bars(symbol, start, end)
            if bars:
                source = {
                    "kind": "Binance Spot",
                    "market": "spot",
                    "pair": f"{symbol}USDT",
                    "quote": "USDT",
                }
                errors = []
            else:
                source, bars, errors = select_price_series(
                    symbol, events[symbol], start, end
                )
        else:
            source, bars, errors = select_price_series(symbol, events[symbol], start, end)
        trade = backtest_trade(symbol, event_day, signal, source, bars)
        trade["fetch_errors"] = errors
        trades.append(trade)

    entered = [trade for trade in trades if trade["status"] != "not_entered"]
    normal = [trade for trade in entered if trade.get("price_quality") != "低流动性"]
    conservative = [trade for trade in normal if trade["symbol"] != "VELVET"]
    output = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "as_of_day": AS_OF_DAY.isoformat(),
        "parameters": {
            "leverage": LEVERAGE,
            "one_way_cost_rate": ONE_WAY_COST_RATE,
            "round_trip_equity_cost_pct": ROUND_TRIP_EQUITY_COST_PCT,
            "max_hold_days": MAX_HOLD_DAYS,
            "entry": "next UTC day open after anomaly confirmation",
            "exit": (
                "if previous day volume is the highest since entry and current day volume "
                "contracts, exit at next UTC day open"
            ),
            "liquidation": "intraday low <= 50% of entry price",
        },
        "summary": {
            "all": summarize(entered),
            "normal_liquidity": summarize(normal),
            "normal_liquidity_ex_velvet": summarize(conservative),
        },
        "trades": trades,
    }
    RESULTS_PATH.write_text(json.dumps(output, ensure_ascii=False, indent=2) + "\n")
    REPORT_PATH.write_text(build_report(output))
    print(f"wrote {RESULTS_PATH}")
    print(f"wrote {REPORT_PATH}")


if __name__ == "__main__":
    main()
