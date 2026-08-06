#!/usr/bin/env python3
"""Analyze ex-post best exits for selected F7c strategy trades."""

from __future__ import annotations

import csv
import statistics
import sys
from pathlib import Path
from typing import Any


HERE = Path(__file__).resolve().parent
DASHBOARD_DIR = HERE.parent / "binance-bubblemaps-factor-kline-2026-07-30"
sys.path.insert(0, str(DASHBOARD_DIR))
import calculate_f5_subfactor_ic as dashboard  # noqa: E402


CASES = (
    ("CYS", "2026-03-21"),
    ("BULLA", "2026-04-11"),
    ("VELVET", "2026-06-06"),
)
FRICTION_PER_SIDE = 0.002
OUTPUT_CSV = HERE / "mfe-exit-case-bars.csv"
OUTPUT_MD = HERE / "mfe-exit-case-report.md"


def pct(value: float) -> str:
    return f"{value * 100:+.2f}%"


def net_return(entry: float, exit_: float) -> float:
    return exit_ * (1 - FRICTION_PER_SIDE) / (
        entry * (1 + FRICTION_PER_SIDE)
    ) - 1


def analyze() -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    dataset = dashboard.load_dataset()
    tokens = {token["symbol"]: token for token in dataset["tokens"]}
    with (HERE / "trades.csv").open(encoding="utf-8", newline="") as handle:
        trades = list(csv.DictReader(handle))

    summaries = []
    rows = []
    for symbol, signal_date in CASES:
        trade = next(
            row
            for row in trades
            if row["symbol"] == symbol and row["signal_date"] == signal_date
        )
        token = tokens[symbol]
        bars = token["bars"]
        entry_index = next(
            index
            for index, bar in enumerate(bars)
            if bar["d"] == trade["entry_date"]
        )
        holding_window = bars[entry_index : entry_index + 14]
        executable_opens = bars[entry_index + 1 : entry_index + 15]
        entry_price = float(trade["entry_price"])
        cluster_amount = float(token["cluster_amount"])
        holding_high = -float("inf")
        holding_volume_high = -float("inf")
        for offset, bar in enumerate(holding_window):
            holding_high = max(holding_high, float(bar["h"]))
            holding_volume_high = max(holding_volume_high, float(bar["v"]))
            f7c = float((bar.get("cex") or {}).get("net_7d") or 0) / cluster_amount
            rows.append(
                {
                    "symbol": symbol,
                    "signal_date": signal_date,
                    "entry_date": trade["entry_date"],
                    "holding_day": offset + 1,
                    "date": bar["d"],
                    "open": bar["o"],
                    "high": bar["h"],
                    "low": bar["l"],
                    "close": bar["c"],
                    "volume": bar["v"],
                    "f7c_share": f7c,
                    "day_high_return": float(bar["h"]) / entry_price - 1,
                    "close_return": float(bar["c"]) / entry_price - 1,
                    "holding_mfe": holding_high / entry_price - 1,
                    "close_drawdown_from_holding_high": (
                        float(bar["c"]) / holding_high - 1
                    ),
                    "volume_is_holding_high": float(bar["v"]) >= holding_volume_high,
                }
            )

        peak_bar = max(holding_window, key=lambda bar: float(bar["h"]))
        close_bar = max(holding_window, key=lambda bar: float(bar["c"]))
        open_bar = max(executable_opens, key=lambda bar: float(bar["o"]))
        decision_index = next(
            index for index, bar in enumerate(bars) if bar["d"] == open_bar["d"]
        ) - 1
        decision_bar = bars[decision_index]
        history = bars[entry_index : decision_index + 1]
        decision_holding_high = max(float(bar["h"]) for bar in history)
        previous_bar = bars[decision_index - 1] if decision_index > entry_index else None
        previous_seven_volumes = [
            float(bar["v"])
            for bar in bars[max(0, entry_index - 7) : entry_index]
        ]
        summaries.append(
            {
                "symbol": symbol,
                "signal_date": signal_date,
                "entry_date": trade["entry_date"],
                "entry_price": entry_price,
                "current_exit_date": trade["exit_date"],
                "current_net_return": float(trade["net_return"]),
                "theoretical_peak_date": peak_bar["d"],
                "theoretical_peak_price": float(peak_bar["h"]),
                "mfe": float(peak_bar["h"]) / entry_price - 1,
                "best_close_date": close_bar["d"],
                "best_close_price": float(close_bar["c"]),
                "best_close_return": float(close_bar["c"]) / entry_price - 1,
                "best_open_date": open_bar["d"],
                "best_open_price": float(open_bar["o"]),
                "best_open_gross_return": float(open_bar["o"]) / entry_price - 1,
                "best_open_net_return": net_return(
                    entry_price, float(open_bar["o"])
                ),
                "decision_date": decision_bar["d"],
                "decision_close_return": float(decision_bar["c"]) / entry_price - 1,
                "decision_high_drawdown": (
                    float(decision_bar["c"]) / decision_holding_high - 1
                ),
                "decision_volume_change": (
                    float(decision_bar["v"]) / float(previous_bar["v"]) - 1
                    if previous_bar is not None
                    else None
                ),
                "decision_high_failed": (
                    float(decision_bar["h"])
                    < max(float(bar["h"]) for bar in history[:-1])
                    if len(history) >= 2
                    else False
                ),
                "entry_volume_vs_prior_median": (
                    float(holding_window[0]["v"])
                    / statistics.median(previous_seven_volumes)
                    if previous_seven_volumes
                    else None
                ),
            }
        )
    return summaries, rows


def write_csv(rows: list[dict[str, Any]]) -> None:
    with OUTPUT_CSV.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def write_report(summaries: list[dict[str, Any]]) -> None:
    lines = [
        "# 三个F7c交易的MFE与最佳卖点研究",
        "",
        "- 窗口：买入日起14个持有交易日。",
        "- 理论MFE：窗口内最高价相对买入价的最大涨幅，只能事后知道。",
        "- 最佳次日开盘：窗口内能够用前一日收盘信息决定的、事后收益最高的开盘卖点。",
        f"- 净收益按每边{FRICTION_PER_SIDE * 100:.2f}%摩擦计算。",
        "",
        "## 最佳卖点总表",
        "",
        "| 币种 | 信号日 | 买入 | 理论MFE最高点 | MFE | 最佳收盘 | 最佳次日开盘 | 最佳开盘净收益 | 当前净收益 |",
        "|---|---|---|---|---:|---|---|---:|---:|",
    ]
    for row in summaries:
        lines.append(
            f"| {row['symbol']} | {row['signal_date']} | "
            f"{row['entry_date']} @ {row['entry_price']:.8g} | "
            f"{row['theoretical_peak_date']} @ {row['theoretical_peak_price']:.8g} | "
            f"{pct(row['mfe'])} | "
            f"{row['best_close_date']} @ {row['best_close_price']:.8g} "
            f"({pct(row['best_close_return'])}) | "
            f"{row['best_open_date']} @ {row['best_open_price']:.8g} | "
            f"{pct(row['best_open_net_return'])} | "
            f"{pct(row['current_net_return'])} |"
        )

    lines.extend(["", "## 在最佳开盘前一日已经能看到什么", ""])
    for row in summaries:
        observations = [
            f"截至 `{row['decision_date']}` 收盘收益 {pct(row['decision_close_return'])}",
            f"收盘距持仓最高价 {pct(row['decision_high_drawdown'])}",
        ]
        if row["decision_volume_change"] is not None:
            observations.append(
                f"成交量较前日 {pct(row['decision_volume_change'])}"
            )
        observations.append(
            "当日最高价未创新高"
            if row["decision_high_failed"]
            else "当日最高价仍创新高或尚无比较日"
        )
        lines.append(
            f"- **{row['symbol']}**：`{row['best_open_date']}` 开盘是最佳可执行卖点；"
            + "；".join(observations)
            + "。"
        )

    lines.extend(
        [
            "",
            "## 初步结论",
            "",
            "- CYS的最佳可执行卖点是2026-03-24开盘。前一日已经出现成交量下降、日内最高价未创新高，但收盘仍处于盈利状态；当前规则等到收盘回撤确认后再于下一日卖出，晚了一天。",
            "- BULLA的最佳可执行卖点是2026-04-13开盘。入场首日已获得较高MFE，且收盘从日内高位明显回落，属于首日冲高兑现型。",
            "- VELVET的最佳可执行卖点是2026-06-12开盘。此前价格连续加速，2026-06-11收盘已从持仓最高价回撤约12%，第二天开盘仍保留大部分利润；等待成交量真正缩小会太晚。",
            "- 三笔交易不能直接用同一个固定MFE阈值：BULLA适合较早锁盈，VELVET若在首次MFE超过100%时卖出会严重过早。下一步应对全部交易按MFE区间分层，再研究各层适用的峰值回撤和止损。",
            "- 最佳卖点是事后标签，不是可直接交易的规则；只能用于训练无未来函数的退出条件。",
            "",
            f"逐日明细见 `{OUTPUT_CSV.name}`。",
        ]
    )
    OUTPUT_MD.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> None:
    summaries, rows = analyze()
    write_csv(rows)
    write_report(summaries)
    print(f"wrote {OUTPUT_CSV}")
    print(f"wrote {OUTPUT_MD}")


if __name__ == "__main__":
    main()
