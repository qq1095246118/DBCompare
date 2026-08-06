#!/usr/bin/env python3
"""Analyze intraday exit timing for the selected F7c strategy trades.

The ex-post peak statistics are labels only.  The trailing-exit grid is causal:
it observes a completed bar, then executes at the next bar open.
"""

from __future__ import annotations

import csv
import json
import statistics
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


HERE = Path(__file__).resolve().parent
DATA_DIR = HERE / "intraday-data"
MANIFEST = DATA_DIR / "manifest.json"
SUMMARY_CSV = HERE / "intraday-exit-case-summary.csv"
GRID_CSV = HERE / "intraday-exit-grid.csv"
REPORT_MD = HERE / "intraday-exit-case-report.md"

FRICTION_PER_SIDE = 0.002
HK = timezone(timedelta(hours=8))
INTERVALS = ("5m", "15m", "1h", "4h")
INTERVAL_MINUTES = {"5m": 5, "15m": 15, "1h": 60, "4h": 240}
CASES = (
    {"symbol": "CYS", "signal_date": "2026-03-21", "entry_price": 0.5711},
    {"symbol": "BULLA", "signal_date": "2026-04-11", "entry_price": 0.006812},
    {"symbol": "VELVET", "signal_date": "2026-06-06", "entry_price": 0.18493},
)
ACTIVATIONS = (0.10, 0.20, 0.30, 0.50, 0.80, 1.00, 2.00, 3.00, 5.00, 7.00, 8.00, 9.00)
TRAILING_DRAWDOWNS = (0.02, 0.03, 0.05, 0.08, 0.10, 0.12, 0.15, 0.20, 0.25, 0.30)


def read_rows(path: Path) -> list[dict[str, Any]]:
    with path.open(encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))
    numeric = (
        "open_time_ms",
        "open",
        "high",
        "low",
        "close",
        "volume",
        "quote_volume",
        "trades",
        "taker_buy_volume",
    )
    for row in rows:
        for field in numeric:
            row[field] = float(row[field])
    return rows


def iso_time(ms: float, zone: timezone = timezone.utc) -> str:
    return datetime.fromtimestamp(ms / 1000, zone).isoformat(timespec="minutes")


def pct(value: float | None) -> str:
    return "—" if value is None else f"{value * 100:+.2f}%"


def ratio(value: float | None) -> str:
    return "—" if value is None else f"{value:.2f}x"


def net_return(entry: float, exit_: float) -> float:
    return exit_ * (1 - FRICTION_PER_SIDE) / (entry * (1 + FRICTION_PER_SIDE)) - 1


def median_ratio(rows: list[dict[str, Any]], index: int, field: str, lookback: int = 20) -> float | None:
    history = [float(row[field]) for row in rows[max(0, index - lookback) : index]]
    if not history:
        return None
    base = statistics.median(history)
    return float(rows[index][field]) / base if base else None


def upper_wick_share(row: dict[str, Any]) -> float:
    bar_range = row["high"] - row["low"]
    return (row["high"] - max(row["open"], row["close"])) / bar_range if bar_range else 0.0


def analyze_interval(case: dict[str, Any], interval: str) -> dict[str, Any]:
    rows = read_rows(DATA_DIR / f"{case['symbol']}-{interval}.csv")
    entry = case["entry_price"]
    peak_index = max(range(len(rows)), key=lambda index: rows[index]["high"])
    close_index = max(range(len(rows)), key=lambda index: rows[index]["close"])
    open_index = max(range(1, len(rows)), key=lambda index: rows[index]["open"])
    peak = rows[peak_index]
    next_open = rows[peak_index + 1] if peak_index + 1 < len(rows) else None
    volume_ratio = median_ratio(rows, peak_index, "volume")
    taker_share = peak["taker_buy_volume"] / peak["volume"] if peak["volume"] else None

    result: dict[str, Any] = {
        "symbol": case["symbol"],
        "signal_date": case["signal_date"],
        "interval": interval,
        "entry_time_utc": iso_time(rows[0]["open_time_ms"]),
        "entry_price": entry,
        "bars": len(rows),
        "peak_time_utc": iso_time(peak["open_time_ms"]),
        "peak_time_hk": iso_time(peak["open_time_ms"], HK),
        "peak_high": peak["high"],
        "mfe": peak["high"] / entry - 1,
        "peak_bar_close": peak["close"],
        "peak_bar_close_return": peak["close"] / entry - 1,
        "peak_close_drawdown": peak["close"] / peak["high"] - 1,
        "peak_volume_vs_prior20_median": volume_ratio,
        "peak_upper_wick_share": upper_wick_share(peak),
        "peak_taker_buy_share": taker_share,
        "next_open_time_utc": iso_time(next_open["open_time_ms"]) if next_open else None,
        "next_open_price": next_open["open"] if next_open else None,
        "next_open_net_return": net_return(entry, next_open["open"]) if next_open else None,
        "best_close_time_utc": iso_time(rows[close_index]["open_time_ms"]),
        "best_close_price": rows[close_index]["close"],
        "best_close_gross_return": rows[close_index]["close"] / entry - 1,
        "best_open_time_utc": iso_time(rows[open_index]["open_time_ms"]),
        "best_open_price": rows[open_index]["open"],
        "best_open_net_return": net_return(entry, rows[open_index]["open"]),
    }
    for minutes in (5, 15, 30, 60, 120, 240):
        interval_minutes = INTERVAL_MINUTES[interval]
        if minutes < interval_minutes or minutes % interval_minutes:
            result[f"close_change_after_{minutes}m"] = None
        else:
            future_index = peak_index + minutes // interval_minutes
            result[f"close_change_after_{minutes}m"] = (
                rows[future_index]["close"] / peak["close"] - 1
                if future_index < len(rows)
                else None
            )
    return result


def trailing_exit(
    rows: list[dict[str, Any]], entry: float, activation: float, drawdown: float
) -> dict[str, Any] | None:
    """Signal on completed bar close and fill at the next bar open."""
    running_high = rows[0]["high"]
    for index, row in enumerate(rows[:-1]):
        running_high = max(running_high, row["high"])
        running_mfe = running_high / entry - 1
        close_drawdown = row["close"] / running_high - 1
        if running_mfe >= activation and close_drawdown <= -drawdown:
            fill = rows[index + 1]
            return {
                "decision_time_utc": iso_time(row["open_time_ms"]),
                "execution_time_utc": iso_time(fill["open_time_ms"]),
                "execution_price": fill["open"],
                "gross_return": fill["open"] / entry - 1,
                "net_return": net_return(entry, fill["open"]),
                "running_mfe_at_signal": running_mfe,
                "close_drawdown_at_signal": close_drawdown,
                "volume_vs_prior20_median": median_ratio(rows, index, "volume"),
                "upper_wick_share": upper_wick_share(row),
            }
    return None


def build_grid() -> list[dict[str, Any]]:
    output = []
    for case in CASES:
        entry = case["entry_price"]
        for interval in INTERVALS:
            rows = read_rows(DATA_DIR / f"{case['symbol']}-{interval}.csv")
            for activation in ACTIVATIONS:
                for drawdown in TRAILING_DRAWDOWNS:
                    result = trailing_exit(rows, entry, activation, drawdown)
                    output.append(
                        {
                            "symbol": case["symbol"],
                            "interval": interval,
                            "activation_mfe": activation,
                            "trailing_drawdown": drawdown,
                            "triggered": result is not None,
                            **(result or {}),
                        }
                    )
    return output


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


def render_report(
    summaries: list[dict[str, Any]], grid: list[dict[str, Any]], manifest: dict[str, Any]
) -> str:
    by_symbol = {
        symbol: {row["interval"]: row for row in summaries if row["symbol"] == symbol}
        for symbol in (case["symbol"] for case in CASES)
    }
    lines = [
        "# CYS、BULLA、VELVET 分钟级卖点研究",
        "",
        "## 口径",
        "",
        "- 数据：Binance Futures UM 官方历史归档；统一由原始 5m K线生成 15m、1h、4h，避免不同接口口径不一致。",
        "- 窗口：每笔交易入场后 15 个自然日；时刻默认使用 UTC，同时列出香港时间（UTC+8）。",
        "- `MFE最高点` 是事后标签，不能直接作为实盘卖出规则。`峰值K线收盘/下一根开盘`用于衡量不同周期确认后会损失多少利润。",
        f"- 可执行回测严格使用：本根K线收盘确认，下一根K线开盘成交；净收益扣除双边各 {FRICTION_PER_SIDE * 100:.2f}% 摩擦。",
        "- 表中收益均为标的自身的1×收益，尚未乘2×杠杆，也未计资金费率、滑点扩大和爆仓约束。",
        "",
        "## 各周期对峰值的捕捉",
        "",
        "| 币种 | 周期 | MFE最高时刻（UTC / 香港） | MFE | 峰值K线收盘涨幅 | 收盘距最高价 | 峰值量/前20根中位数 | 上影占比 | 主买占比 | 峰后下一根开盘净收益 |",
        "|---|---|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for case in CASES:
        for interval in INTERVALS:
            row = by_symbol[case["symbol"]][interval]
            lines.append(
                f"| {row['symbol']} | {interval} | {row['peak_time_utc']} / {row['peak_time_hk']} | "
                f"{pct(row['mfe'])} | {pct(row['peak_bar_close_return'])} | {pct(row['peak_close_drawdown'])} | "
                f"{ratio(row['peak_volume_vs_prior20_median'])} | {pct(row['peak_upper_wick_share'])} | "
                f"{pct(row['peak_taker_buy_share'])} | {pct(row['next_open_net_return'])} |"
            )

    lines.extend(
        [
            "",
            "## 三笔交易分别发生了什么",
            "",
            "### CYS：峰值后的下跌发生在 5—15 分钟内",
            "",
        ]
    )
    cys = by_symbol["CYS"]["5m"]
    lines.extend(
        [
            f"- 5m最高点出现在 `{cys['peak_time_utc']}`（香港 `{cys['peak_time_hk']}`），最高价 `{cys['peak_high']:.8g}`，MFE {pct(cys['mfe'])}。",
            f"- 峰值5m收盘仍接近最高价（回撤 {pct(cys['peak_close_drawdown'])}），但随后5分钟收盘下跌 {pct(cys['close_change_after_5m'])}，15分钟累计下跌 {pct(cys['close_change_after_15m'])}。",
            "- 因此CYS的卖点必须由5m执行、15m最多做确认；等1h收盘会丢失显著利润，4h不适合做退出触发。",
            "",
            "### BULLA：典型5m放量长上影冲顶，但一周后又出现第二次高点",
            "",
        ]
    )
    bulla = by_symbol["BULLA"]["5m"]
    lines.extend(
        [
            f"- 首个全局最高点在 `{bulla['peak_time_utc']}`（香港 `{bulla['peak_time_hk']}`），5m成交量为前20根中位数的 {ratio(bulla['peak_volume_vs_prior20_median'])}，上影占 {pct(bulla['peak_upper_wick_share'])}，收盘已离最高价 {pct(bulla['peak_close_drawdown'])}。",
            f"- 峰值后下一根开盘卖出的净收益为 {pct(bulla['next_open_net_return'])}；但窗口内最佳5m开盘在 `{bulla['best_open_time_utc']}`，净收益 {pct(bulla['best_open_net_return'])}，说明之后还有第二波冲高。",
            "- 若目标是控制回撤，首个“巨量 + 长上影 + 收盘离高位”应减仓；若保留趋势仓，则需要让剩余仓位接受回撤以等待二次冲高。",
            "",
            "### VELVET：5m/15m几乎保留全部利润，固定低MFE止盈会过早",
            "",
        ]
    )
    velvet = by_symbol["VELVET"]["5m"]
    lines.extend(
        [
            f"- 最高点在 `{velvet['peak_time_utc']}`（香港 `{velvet['peak_time_hk']}`），最高价 `{velvet['peak_high']:.8g}`，MFE {pct(velvet['mfe'])}。",
            f"- 峰值5m收盘只离最高价 {pct(velvet['peak_close_drawdown'])}，下一根开盘净收益仍有 {pct(velvet['next_open_net_return'])}；随后4小时收盘相对峰值5m收盘下跌 {pct(velvet['close_change_after_240m'])}。",
            "- 如果首次上涨100%或300%就固定止盈，会错过大部分行情。超强趋势需要随新高抬升保护位，用5m或15m的峰值回撤退出，而不是固定收益目标。",
            "",
            "## 可执行框架（待全样本回测，不是最终参数）",
            "",
            "| 用途 | 周期 | 建议 |",
            "|---|---|---|",
            "| 执行层 | 5m | 负责卖出确认与下一根开盘成交；识别峰值回撤、长上影和异常放量。 |",
            "| 确认层 | 15m | 过滤单根5m噪声；连续强趋势中以15m未再创新高、收盘回撤确认减仓。 |",
            "| 趋势层 | 1h | 判断是否仍处于加速上涨或已经转弱，用于决定减仓还是清仓。 |",
            "| 背景层 | 4h | 判断大级别行情位置和支撑，不直接承担峰值卖出。 |",
            "",
            "初步应采用分段退出，而不是寻找一个万能最高点：",
            "",
            "1. 当持仓MFE进入盈利区后，启动随最高价抬升的保护机制；信号只使用已完成K线。",
            "2. 5m出现异常放量、长上影、收盘明显离开新高时先减仓；15m确认未创新高或继续回撤时再减。",
            "3. 1h趋势仍强时保留小部分趋势仓；1h结构转弱后清仓。止损条件另行用低MFE/MAE样本训练。",
            "4. 激活阈值必须按行情强度分层。三个案例的最终MFE约为35%、91%、939%，固定20%、50%或100%的单一阈值都会偏向某一案例。",
            "",
            "## 为什么不能直接套同一组移动止盈参数",
            "",
            "下面固定用“持仓MFE达到30%后，收盘距持仓最高价回撤15%，下一根开盘卖出”，只改变K线周期：",
            "",
            "| 周期 | CYS净收益 | BULLA净收益 | VELVET净收益 |",
            "|---|---:|---:|---:|",
        ]
    )
    selected = {
        (row["symbol"], row["interval"]): row
        for row in grid
        if row["activation_mfe"] == 0.30 and row["trailing_drawdown"] == 0.15
    }
    for interval in INTERVALS:
        values = [selected[(symbol, interval)].get("net_return") for symbol in ("CYS", "BULLA", "VELVET")]
        lines.append(
            f"| {interval} | {pct(values[0])} | {pct(values[1])} | {pct(values[2])} |"
        )
    lines.extend(
        [
            "",
            "这组规则在三币上都盈利，但VELVET在行情早期第一次普通回撤时就退出，只获得约27%—81%，远低于最终可实现的900%左右。这不是周期选择能解决的问题，而是行情强度分层和分批退出的问题。4h看似偶尔能保留更多利润，实质是确认迟、承担更大回撤，不能据此把4h当作精确卖点周期。",
            "",
            "## 当前最重要的结论",
            "",
            "- 5m是这三笔交易最合适的执行周期，15m适合作为确认；1h和4h用于判断趋势，不适合精确卖顶。",
            "- BULLA的量价形态最像可识别的冲顶；CYS的反转太快，必须接受5m级别假信号；VELVET说明强趋势不能仅凭盈利倍数止盈。",
            "- 这三笔只能用于提出候选规则，不能用于定参。下一步应将相同5m/15m特征扩展到全部交易，使用滚动训练或留一币种验证，防止按三个已知峰值过拟合。",
            "",
            "## 输出文件",
            "",
            f"- `{SUMMARY_CSV.name}`：每个币、每个周期的峰值与峰后变化明细。",
            f"- `{GRID_CSV.name}`：不同MFE激活阈值、峰值回撤阈值的无未来函数退出结果。",
            f"- `{MANIFEST.relative_to(HERE)}`：原始归档URL、哈希和各周期覆盖情况。",
            "",
            "## 数据来源",
            "",
        ]
    )
    for case in manifest["cases"]:
        for source in case["sources"]:
            lines.append(f"- {case['symbol']} {source['month']}: {source['url']}")
    return "\n".join(lines) + "\n"


def main() -> None:
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    summaries = [
        analyze_interval(case, interval)
        for case in CASES
        for interval in INTERVALS
    ]
    grid = build_grid()
    write_csv(SUMMARY_CSV, summaries)
    write_csv(GRID_CSV, grid)
    REPORT_MD.write_text(render_report(summaries, grid, manifest), encoding="utf-8")
    print(f"wrote {SUMMARY_CSV}")
    print(f"wrote {GRID_CSV}")
    print(f"wrote {REPORT_MD}")


if __name__ == "__main__":
    main()
