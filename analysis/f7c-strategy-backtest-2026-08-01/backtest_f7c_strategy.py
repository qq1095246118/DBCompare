#!/usr/bin/env python3
"""Causal portfolio backtest for the F7c CEX signed-net-flow strategy."""

from __future__ import annotations

import csv
import math
import statistics
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
DASHBOARD_DIR = ROOT / "binance-bubblemaps-factor-kline-2026-07-30"
sys.path.insert(0, str(DASHBOARD_DIR))
import calculate_f5_subfactor_ic as dashboard  # noqa: E402


OUTPUT_DIR = Path(__file__).resolve().parent
TRADES_CSV = OUTPUT_DIR / "trades.csv"
DAILY_CSV = OUTPUT_DIR / "daily-equity.csv"
SIGNALS_CSV = OUTPUT_DIR / "signals.csv"
REPORT_MD = OUTPUT_DIR / "report.md"
SENSITIVITY_CSV = OUTPUT_DIR / "sensitivity.csv"

MIN_F7C_SHARE = 0.001
TOP_FRACTION = 0.20
MAX_POSITIONS = 3
POSITION_FRACTION = 0.30
MAX_HOLD_DAYS = 14
STOP_LOSS = -0.10
TRAIL_ARM_GAIN = 0.20
TRAIL_DRAWDOWN = 0.10
STRONG_OUTFLOW_SHARE = -0.001
FRICTION_PER_SIDE = 0.002


@dataclass
class Position:
    symbol: str
    group: str
    signal_date: str
    entry_date: str
    entry_price: float
    units: float
    notional: float
    entry_cost: float
    signal_f7c: float
    signal_rank: int
    bars_seen: list[dict[str, Any]] = field(default_factory=list)
    highest_close: float = 0.0
    highest_high: float = 0.0
    lowest_low: float = math.inf
    highest_volume: float = 0.0
    below_median_days: int = 0
    pending_exit_reason: str | None = None
    exit_decision_date: str | None = None


def f7c_value(token: dict[str, Any], bar: dict[str, Any]) -> float | None:
    cluster_amount = float(token.get("cluster_amount") or 0)
    if cluster_amount <= 0:
        return None
    return float((bar.get("cex") or {}).get("net_7d") or 0) / cluster_amount


def build_market(
    dataset: dict[str, Any],
) -> tuple[
    list[str],
    dict[str, dict[str, dict[str, Any]]],
    dict[str, dict[str, Any]],
    dict[str, dict[str, dict[str, Any]]],
]:
    tokens = {token["symbol"]: token for token in dataset["tokens"]}
    bars_by_symbol: dict[str, dict[str, dict[str, Any]]] = {}
    by_date: dict[str, dict[str, dict[str, Any]]] = defaultdict(dict)
    for symbol, token in tokens.items():
        symbol_bars = {bar["d"]: bar for bar in token["bars"]}
        bars_by_symbol[symbol] = symbol_bars
        for date, bar in symbol_bars.items():
            by_date[date][symbol] = bar

    dates = sorted(by_date)
    ranks: dict[str, dict[str, dict[str, Any]]] = {}
    for date in dates:
        values = []
        for symbol in by_date[date]:
            value = f7c_value(tokens[symbol], by_date[date][symbol])
            if value is not None:
                values.append((symbol, value))
        values.sort(key=lambda item: (-item[1], item[0]))
        top_count = max(1, math.ceil(len(values) * TOP_FRACTION))
        day_ranks = {}
        for rank, (symbol, value) in enumerate(values, start=1):
            day_ranks[symbol] = {
                "value": value,
                "rank": rank,
                "count": len(values),
                "top_entry": rank <= top_count and value >= MIN_F7C_SHARE,
                "below_median": rank > math.ceil(len(values) * 0.50),
            }
        ranks[date] = day_ranks
    return dates, bars_by_symbol, tokens, ranks


def open_equity(
    cash: float,
    positions: dict[str, Position],
    date: str,
    bars_by_symbol: dict[str, dict[str, dict[str, Any]]],
) -> float:
    equity = cash
    for symbol, position in positions.items():
        bar = bars_by_symbol[symbol].get(date)
        price = float(bar["o"]) if bar is not None else position.entry_price
        equity += position.units * price
    return equity


def exit_reason_at_close(
    position: Position,
    bar: dict[str, Any],
    rank_info: dict[str, Any] | None,
) -> str | None:
    close = float(bar["c"])
    f7c = None if rank_info is None else float(rank_info["value"])

    if close / position.entry_price - 1 <= STOP_LOSS:
        return "收盘止损-10%"
    if (
        position.highest_close >= position.entry_price * (1 + TRAIL_ARM_GAIN)
        and close <= position.highest_close * (1 - TRAIL_DRAWDOWN)
    ):
        return "盈利20%后最高收盘回撤10%"
    if f7c is not None and f7c <= STRONG_OUTFLOW_SHARE:
        return "F7b净流出超过Cluster 0.1%"
    if f7c is not None and f7c <= 0:
        return "F7c由正转为非正"

    if rank_info is not None and rank_info["below_median"]:
        position.below_median_days += 1
    else:
        position.below_median_days = 0
    if position.below_median_days >= 2:
        return "F7c排名连续2日跌出后50%"

    if position.bars_seen:
        previous = position.bars_seen[-1]
        previous_volume_was_peak = float(previous["v"]) >= position.highest_volume
        volume_shrank = float(bar["v"]) < float(previous["v"])
        close_failed_new_high = close <= position.highest_close
        in_profit = close > position.entry_price
        if (
            previous_volume_was_peak
            and volume_shrank
            and close_failed_new_high
            and in_profit
        ):
            return "持仓成交量峰值后缩小且收盘未创新高"

    if len(position.bars_seen) + 1 >= MAX_HOLD_DAYS:
        return "持有满14个交易日"
    return None


def update_position_bar(position: Position, bar: dict[str, Any]) -> None:
    position.highest_close = max(position.highest_close, float(bar["c"]))
    position.highest_high = max(position.highest_high, float(bar["h"]))
    position.lowest_low = min(position.lowest_low, float(bar["l"]))
    position.highest_volume = max(position.highest_volume, float(bar["v"]))
    position.bars_seen.append(bar)


def run_backtest(dataset: dict[str, Any]) -> dict[str, Any]:
    dates, bars_by_symbol, tokens, ranks = build_market(dataset)
    cash = 1.0
    positions: dict[str, Position] = {}
    trades: list[dict[str, Any]] = []
    signals: list[dict[str, Any]] = []
    daily: list[dict[str, Any]] = []
    pending_entries: list[dict[str, Any]] = []
    eligible_yesterday = {symbol: False for symbol in tokens}
    signal_episodes = 0
    skipped_no_slot = 0
    skipped_no_open = 0
    skipped_already_held = 0

    for date in dates:
        # All decisions below were made at the previous daily close.
        for symbol in list(positions):
            position = positions[symbol]
            if position.pending_exit_reason is None:
                continue
            bar = bars_by_symbol[symbol].get(date)
            if bar is None:
                continue
            exit_price = float(bar["o"])
            gross_proceeds = position.units * exit_price
            exit_cost = gross_proceeds * FRICTION_PER_SIDE
            cash += gross_proceeds - exit_cost
            gross_return = exit_price / position.entry_price - 1
            net_pnl = (
                gross_proceeds
                - exit_cost
                - position.notional
                - position.entry_cost
            )
            net_return = net_pnl / (position.notional + position.entry_cost)
            trades.append(
                {
                    "symbol": symbol,
                    "group": position.group,
                    "signal_date": position.signal_date,
                    "signal_f7c_share": position.signal_f7c,
                    "signal_rank": position.signal_rank,
                    "entry_date": position.entry_date,
                    "entry_price": position.entry_price,
                    "exit_decision_date": position.exit_decision_date,
                    "exit_date": date,
                    "exit_price": exit_price,
                    "entry_notional": position.notional,
                    "entry_cost": position.entry_cost,
                    "exit_cost": exit_cost,
                    "net_pnl": net_pnl,
                    "holding_days": len(position.bars_seen),
                    "gross_return": gross_return,
                    "net_return": net_return,
                    "mae": position.lowest_low / position.entry_price - 1,
                    "mfe": position.highest_high / position.entry_price - 1,
                    "exit_reason": position.pending_exit_reason,
                    "status": "closed",
                }
            )
            del positions[symbol]

        equity_at_open = open_equity(cash, positions, date, bars_by_symbol)
        available_slots = MAX_POSITIONS - len(positions)
        for candidate in sorted(
            pending_entries,
            key=lambda item: (-item["f7c"], item["symbol"]),
        ):
            if candidate["symbol"] in positions:
                continue
            if available_slots <= 0:
                skipped_no_slot += 1
                candidate["event"]["status"] = "skipped_no_slot"
                candidate["event"]["status_reason"] = "组合已满3仓"
                continue
            symbol = candidate["symbol"]
            bar = bars_by_symbol[symbol].get(date)
            if bar is None:
                skipped_no_open += 1
                candidate["event"]["status"] = "skipped_no_open"
                candidate["event"]["status_reason"] = "次日无开盘价"
                continue
            desired_notional = POSITION_FRACTION * equity_at_open
            notional = min(desired_notional, cash / (1 + FRICTION_PER_SIDE))
            if notional <= 0:
                skipped_no_slot += 1
                continue
            entry_price = float(bar["o"])
            entry_cost = notional * FRICTION_PER_SIDE
            cash -= notional + entry_cost
            position = Position(
                symbol=symbol,
                group=tokens[symbol]["group"],
                signal_date=candidate["signal_date"],
                entry_date=date,
                entry_price=entry_price,
                units=notional / entry_price,
                notional=notional,
                entry_cost=entry_cost,
                signal_f7c=candidate["f7c"],
                signal_rank=candidate["rank"],
            )
            positions[symbol] = position
            candidate["event"]["status"] = "entered"
            candidate["event"]["status_reason"] = "次日开盘买入"
            candidate["event"]["entry_date"] = date
            available_slots -= 1
        pending_entries = []

        # Close-time exit decisions use only information available by this close.
        for symbol, position in positions.items():
            bar = bars_by_symbol[symbol].get(date)
            if bar is None or position.pending_exit_reason is not None:
                continue
            reason = exit_reason_at_close(position, bar, ranks[date].get(symbol))
            update_position_bar(position, bar)
            if reason is not None:
                position.pending_exit_reason = reason
                position.exit_decision_date = date

        # Only new threshold-crossing episodes create entries; missed signals expire.
        next_candidates = []
        for symbol, info in ranks[date].items():
            eligible_today = bool(info["top_entry"])
            if eligible_today and not eligible_yesterday.get(symbol, False):
                signal_episodes += 1
                event = {
                    "signal_id": signal_episodes,
                    "symbol": symbol,
                    "group": tokens[symbol]["group"],
                    "signal_date": date,
                    "f7c_share": float(info["value"]),
                    "rank": int(info["rank"]),
                    "universe_count": int(info["count"]),
                    "status": "pending",
                    "status_reason": "等待次日开盘",
                    "entry_date": "",
                }
                signals.append(event)
                if symbol not in positions:
                    next_candidates.append(
                        {
                            "symbol": symbol,
                            "signal_date": date,
                            "f7c": float(info["value"]),
                            "rank": int(info["rank"]),
                            "event": event,
                        }
                    )
                else:
                    skipped_already_held += 1
                    event["status"] = "ignored_already_held"
                    event["status_reason"] = "同币仍在持仓"
            eligible_yesterday[symbol] = eligible_today
        pending_entries = next_candidates

        close_equity = cash
        invested_value = 0.0
        for symbol, position in positions.items():
            bar = bars_by_symbol[symbol].get(date)
            mark = float(bar["c"]) if bar is not None else position.entry_price
            market_value = position.units * mark
            invested_value += market_value
            close_equity += market_value
        daily.append(
            {
                "date": date,
                "equity": close_equity,
                "cash": cash,
                "invested_value": invested_value,
                "exposure": invested_value / close_equity if close_equity else 0,
                "positions": len(positions),
                "symbols": ",".join(sorted(positions)),
            }
        )

    final_date = dates[-1]
    for candidate in pending_entries:
        if candidate["event"]["status"] == "pending":
            candidate["event"]["status"] = "unexecuted_sample_end"
            candidate["event"]["status_reason"] = "样本末无次日开盘"
    for symbol, position in positions.items():
        final_bar = bars_by_symbol[symbol][final_date]
        final_price = float(final_bar["c"])
        trades.append(
            {
                "symbol": symbol,
                "group": position.group,
                "signal_date": position.signal_date,
                "signal_f7c_share": position.signal_f7c,
                "signal_rank": position.signal_rank,
                "entry_date": position.entry_date,
                "entry_price": position.entry_price,
                "exit_decision_date": position.exit_decision_date or "",
                "exit_date": "",
                "exit_price": "",
                "entry_notional": position.notional,
                "entry_cost": position.entry_cost,
                "exit_cost": "",
                "net_pnl": "",
                "holding_days": len(position.bars_seen),
                "gross_return": final_price / position.entry_price - 1,
                "net_return": "",
                "mae": position.lowest_low / position.entry_price - 1,
                "mfe": position.highest_high / position.entry_price - 1,
                "exit_reason": position.pending_exit_reason or "样本截止仍持仓",
                "status": "open",
            }
        )

    return {
        "dates": dates,
        "trades": trades,
        "signals": signals,
        "daily": daily,
        "signal_episodes": signal_episodes,
        "skipped_no_slot": skipped_no_slot,
        "skipped_no_open": skipped_no_open,
        "skipped_already_held": skipped_already_held,
        "pending_entries_at_end": len(pending_entries),
        "open_positions": positions,
    }


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    if not rows:
        return
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def percent(value: float) -> str:
    return f"{value * 100:+.2f}%"


def max_drawdown(equities: list[float]) -> float:
    peak = equities[0]
    worst = 0.0
    for equity in equities:
        peak = max(peak, equity)
        worst = min(worst, equity / peak - 1)
    return worst


def trimmed_mean(values: list[float], fraction: float = 0.10) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    trim = int(len(ordered) * fraction)
    trimmed = ordered[trim : len(ordered) - trim] if trim else ordered
    return statistics.mean(trimmed)


def summarize_result(result: dict[str, Any]) -> dict[str, Any]:
    closed = [
        trade for trade in result["trades"] if trade["status"] == "closed"
    ]
    returns = [float(trade["net_return"]) for trade in closed]
    equities = [float(row["equity"]) for row in result["daily"]]
    return {
        "signal_episodes": result["signal_episodes"],
        "closed_trades": len(closed),
        "win_rate": (
            sum(value > 0 for value in returns) / len(returns)
            if returns
            else 0.0
        ),
        "mean_trade_return": statistics.mean(returns) if returns else 0.0,
        "median_trade_return": statistics.median(returns) if returns else 0.0,
        "trimmed_trade_return": trimmed_mean(returns),
        "portfolio_return": equities[-1] - 1,
        "max_drawdown": max_drawdown(equities),
        "average_exposure": statistics.mean(
            float(row["exposure"]) for row in result["daily"]
        ),
    }


def run_sensitivity(dataset: dict[str, Any]) -> list[dict[str, Any]]:
    global MIN_F7C_SHARE, TOP_FRACTION, MAX_HOLD_DAYS

    original = (MIN_F7C_SHARE, TOP_FRACTION, MAX_HOLD_DAYS)
    rows = []
    try:
        for minimum_share in (0.0005, 0.001, 0.002):
            for top_fraction in (0.10, 0.20, 0.30):
                for maximum_hold in (7, 14, 21):
                    MIN_F7C_SHARE = minimum_share
                    TOP_FRACTION = top_fraction
                    MAX_HOLD_DAYS = maximum_hold
                    summary = summarize_result(run_backtest(dataset))
                    rows.append(
                        {
                            "minimum_f7c_share": minimum_share,
                            "top_fraction": top_fraction,
                            "maximum_hold_days": maximum_hold,
                            **summary,
                        }
                    )
    finally:
        MIN_F7C_SHARE, TOP_FRACTION, MAX_HOLD_DAYS = original
    return rows


def write_report(
    dataset: dict[str, Any],
    result: dict[str, Any],
    sensitivity: list[dict[str, Any]],
) -> None:
    trades = result["trades"]
    closed = [trade for trade in trades if trade["status"] == "closed"]
    opened = [trade for trade in trades if trade["status"] == "open"]
    returns = [float(trade["net_return"]) for trade in closed]
    daily = result["daily"]
    equities = [float(row["equity"]) for row in daily]
    daily_returns = [
        equities[index] / equities[index - 1] - 1
        for index in range(1, len(equities))
    ]
    sharpe = (
        statistics.mean(daily_returns)
        / statistics.stdev(daily_returns)
        * math.sqrt(365)
        if len(daily_returns) >= 2 and statistics.stdev(daily_returns) > 0
        else 0.0
    )
    total_return = equities[-1] - 1
    calendar_days = max(1, len(result["dates"]) - 1)
    annualized = equities[-1] ** (365 / calendar_days) - 1
    exposure = statistics.mean(float(row["exposure"]) for row in daily)
    reason_counts = Counter(trade["exit_reason"] for trade in closed)

    lines = [
        "# F7c连续净流策略：全部观测币组合回测",
        "",
        f"- 数据生成时间：`{dataset['generated_at']}`",
        f"- 回测区间：`{result['dates'][0]}` 至 `{result['dates'][-1]}`",
        f"- 币种数：{len(dataset['tokens'])}",
        "- 买入：F7c ≥ Cluster余额0.1%，且位于当日横截面前20%；仅在首次进入条件时发出信号，下一日开盘买入。",
        "- 组合：最多3仓，每仓按当时组合权益30%建仓；已有持仓不因新信号轮换。",
        "- 卖出：F7c反转、连续2日跌出后50%、持仓量峰后缩量且未创新高、收盘止损10%、盈利20%后最高收盘回撤10%，或持有满14日；均下一日开盘执行。",
        f"- 摩擦：每边名义本金 `{FRICTION_PER_SIDE * 100:.2f}%`，包含手续费与滑点，不计资金费率。",
        "- 杠杆：1×。",
        "",
        "## 组合结果",
        "",
        "| 指标 | 结果 |",
        "|---|---:|",
        f"| 信号段数 | {result['signal_episodes']} |",
        f"| 实际入场 | {len(trades)} |",
        f"| 已平仓 / 样本末仍持仓 | {len(closed)} / {len(opened)} |",
        f"| 因满仓跳过 | {result['skipped_no_slot']} |",
        f"| 因同币仍在持仓而忽略 | {result['skipped_already_held']} |",
        f"| 样本末无次日开盘未执行 | {result['pending_entries_at_end']} |",
        f"| 组合净收益 | {percent(total_return)} |",
        f"| 年化折算 | {percent(annualized)} |",
        f"| 最大回撤 | {percent(max_drawdown(equities))} |",
        f"| 日频Sharpe（365日） | {sharpe:.2f} |",
        f"| 平均资金暴露 | {exposure * 100:.1f}% |",
    ]

    if closed:
        positive = [value for value in returns if value > 0]
        negative = [value for value in returns if value < 0]
        profit_factor = (
            sum(positive) / abs(sum(negative)) if negative else math.inf
        )
        lines.extend(
            [
                "",
                "## 已平仓交易",
                "",
                "| 指标 | 结果 |",
                "|---|---:|",
                f"| 胜率 | {sum(value > 0 for value in returns) / len(returns) * 100:.1f}% |",
                f"| 平均净收益 | {percent(statistics.mean(returns))} |",
                f"| 中位数净收益 | {percent(statistics.median(returns))} |",
                f"| 10%去极值均值 | {percent(trimmed_mean(returns))} |",
                f"| 最好 / 最差 | {percent(max(returns))} / {percent(min(returns))} |",
                f"| 平均持有 | {statistics.mean(float(t['holding_days']) for t in closed):.1f}日 |",
                f"| Profit factor | {profit_factor:.2f} |",
                "",
                "## 退出原因",
                "",
                "| 原因 | 笔数 |",
                "|---|---:|",
            ]
        )
        for reason, count in reason_counts.most_common():
            lines.append(f"| {reason} | {count} |")

        lines.extend(
            [
                "",
                "## 分组结果",
                "",
                "| 分组 | 笔数 | 胜率 | 平均净收益 | 中位数 |",
                "|---|---:|---:|---:|---:|",
            ]
        )
        for group in sorted({trade["group"] for trade in closed}):
            group_returns = [
                float(trade["net_return"])
                for trade in closed
                if trade["group"] == group
            ]
            lines.append(
                f"| {group} | {len(group_returns)} | "
                f"{sum(value > 0 for value in group_returns) / len(group_returns) * 100:.1f}% | "
                f"{percent(statistics.mean(group_returns))} | "
                f"{percent(statistics.median(group_returns))} |"
            )

    lines.extend(
        [
            "",
            "## 逐笔交易",
            "",
            "| 币种 | 分组 | 信号日 | F7c | 排名 | 买入 | 卖出 | 持有 | 净收益 | 退出原因 |",
            "|---|---|---|---:|---:|---|---|---:|---:|---|",
        ]
    )
    for trade in trades:
        net = (
            "持仓中"
            if trade["net_return"] == ""
            else percent(float(trade["net_return"]))
        )
        lines.append(
            f"| {trade['symbol']} | {trade['group']} | {trade['signal_date']} | "
            f"{float(trade['signal_f7c_share']) * 100:.3f}% | {trade['signal_rank']} | "
            f"{trade['entry_date']} | {trade['exit_date'] or '—'} | "
            f"{trade['holding_days']} | {net} | {trade['exit_reason']} |"
        )

    lines.extend(
        [
            "",
            "## 参数敏感性",
            "",
            "其余退出规则不变，仅改变F7c绝对门槛、横截面范围和最长持有期。",
            "",
            "| F7c门槛 | 排名前 | 最长持有 | 已平仓 | 胜率 | 组合净收益 | 最大回撤 | 交易10%去极值均值 |",
            "|---:|---:|---:|---:|---:|---:|---:|---:|",
        ]
    )
    for row in sensitivity:
        lines.append(
            f"| {float(row['minimum_f7c_share']) * 100:.2f}% | "
            f"{float(row['top_fraction']) * 100:.0f}% | "
            f"{row['maximum_hold_days']}日 | {row['closed_trades']} | "
            f"{float(row['win_rate']) * 100:.1f}% | "
            f"{percent(float(row['portfolio_return']))} | "
            f"{percent(float(row['max_drawdown']))} | "
            f"{percent(float(row['trimmed_trade_return']))} |"
        )

    lines.extend(
        [
            "",
            "## 限制",
            "",
            "- F7c是用这批数据筛出的最强因子，因此即使币种被标为样本外，本次策略规则整体仍不是真正独立样本外验证。",
            "- 当前HTML没有逐日“全部CEX地址及多跳路径均已复核完成”的质量字段；只纳入已确认路径，0不一定代表真实无流量。",
            "- Cluster成员集合来自当前截面，存在成员集合前视与幸存者偏差。",
            "- 日线回测不能模拟盘口深度、瞬时价差和市价冲击；小币实际滑点可能高于每边0.20%。",
            "- 年化收益只是将不足一年的样本机械折算，不代表可持续年收益。",
            "",
            f"信号：`{SIGNALS_CSV.name}`；逐笔数据：`{TRADES_CSV.name}`；每日权益：`{DAILY_CSV.name}`；参数敏感性：`{SENSITIVITY_CSV.name}`。",
        ]
    )
    REPORT_MD.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> None:
    dataset = dashboard.load_dataset()
    result = run_backtest(dataset)
    sensitivity = run_sensitivity(dataset)
    write_csv(TRADES_CSV, result["trades"])
    write_csv(SIGNALS_CSV, result["signals"])
    write_csv(DAILY_CSV, result["daily"])
    write_csv(SENSITIVITY_CSV, sensitivity)
    write_report(dataset, result, sensitivity)
    print(f"wrote {TRADES_CSV}")
    print(f"wrote {SIGNALS_CSV}")
    print(f"wrote {DAILY_CSV}")
    print(f"wrote {SENSITIVITY_CSV}")
    print(f"wrote {REPORT_MD}")


if __name__ == "__main__":
    main()
