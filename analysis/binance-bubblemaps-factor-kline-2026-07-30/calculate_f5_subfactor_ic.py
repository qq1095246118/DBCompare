#!/usr/bin/env python3
"""Calculate T+N maximum-gain Rank IC for the F5a-F5h alert factors."""

from __future__ import annotations

import csv
import json
import math
import re
import statistics
from collections import defaultdict
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parent
INPUT_HTML = ROOT / "factor-kline-dashboard.html"
OUTPUT_CSV = ROOT / "f5-subfactor-forward-ic.csv"
OUTPUT_MD = ROOT / "f5-subfactor-ic-report.md"
HORIZONS = (1, 3, 5, 7, 14, 30)
MIN_ASSETS = 5
FACTOR_LABELS = {
    "f5a": "F5a 相对历史规模异常",
    "f5b": "F5b Cluster冲击异常",
    "f5c": "F5c 内外部属性异常",
    "f5d": "F5d 流向异常",
    "f5e": "F5e 特殊对手方异常",
    "f5f": "F5f 后续路径",
    "f5g": "F5g 巨额转账持续性异常",
    "f5h": "F5h 市场位置异常",
}


def future_max_gain(
    bars: list[dict[str, Any]], index: int, horizon: int
) -> float | None:
    """Maximum intraday-high gain in D+1..D+N versus D close."""
    if not bars[index]["c"] or index + horizon >= len(bars):
        return None
    future_highs = [
        float(bars[future_index]["h"])
        for future_index in range(index + 1, index + horizon + 1)
        if bars[future_index].get("h") is not None
    ]
    if not future_highs:
        return None
    return max(future_highs) / float(bars[index]["c"]) - 1


def future_mean_close_deviation(
    bars: list[dict[str, Any]], index: int, horizon: int
) -> float | None:
    """Mean D+1..D+N close deviation versus the D close anchor."""
    anchor_close = bars[index].get("c")
    if not anchor_close or index + horizon >= len(bars):
        return None
    future_closes = [
        bars[future_index].get("c")
        for future_index in range(index + 1, index + horizon + 1)
    ]
    if any(value is None for value in future_closes):
        return None
    anchor = float(anchor_close)
    return statistics.mean(float(value) / anchor - 1 for value in future_closes)


def future_volume_price_confirmation(
    bars: list[dict[str, Any]], index: int, horizon: int
) -> float | None:
    """Best same-day positive price deviation times positive volume expansion."""
    anchor_close = bars[index].get("c")
    anchor_volume = bars[index].get("v")
    if (
        not anchor_close
        or not anchor_volume
        or index + horizon >= len(bars)
    ):
        return None
    scores: list[float] = []
    for future_index in range(index + 1, index + horizon + 1):
        future_close = bars[future_index].get("c")
        future_volume = bars[future_index].get("v")
        if future_close is None or future_volume is None:
            return None
        positive_price_deviation = max(
            float(future_close) / float(anchor_close) - 1,
            0.0,
        )
        positive_volume_expansion = max(
            float(future_volume) / float(anchor_volume) - 1,
            0.0,
        )
        scores.append(positive_price_deviation * positive_volume_expansion)
    return max(scores) if scores else None


def load_dataset() -> dict[str, Any]:
    source = INPUT_HTML.read_text(encoding="utf-8")
    match = re.search(
        r"const DATA = (\{.*?\});\nconst FACTORS",
        source,
        flags=re.DOTALL,
    )
    if match is None:
        raise ValueError("dashboard DATA payload unavailable")
    return json.loads(match.group(1))


def average_ranks(values: list[float]) -> list[float]:
    order = sorted(range(len(values)), key=values.__getitem__)
    ranks = [0.0] * len(values)
    start = 0
    while start < len(order):
        end = start + 1
        while end < len(order) and values[order[end]] == values[order[start]]:
            end += 1
        average_rank = ((start + 1) + end) / 2
        for position in range(start, end):
            ranks[order[position]] = average_rank
        start = end
    return ranks


def pearson(left: list[float], right: list[float]) -> float | None:
    if len(left) != len(right) or len(left) < 2:
        return None
    left_mean = statistics.mean(left)
    right_mean = statistics.mean(right)
    left_centered = [value - left_mean for value in left]
    right_centered = [value - right_mean for value in right]
    denominator = math.sqrt(
        sum(value * value for value in left_centered)
        * sum(value * value for value in right_centered)
    )
    if denominator == 0:
        return None
    return (
        sum(
            left_value * right_value
            for left_value, right_value in zip(
                left_centered, right_centered
            )
        )
        / denominator
    )


def spearman(left: list[float], right: list[float]) -> float | None:
    return pearson(average_ranks(left), average_ranks(right))


def newey_west_tstat(values: list[float], lag: int) -> float | None:
    if len(values) < 3:
        return None
    mean_value = statistics.mean(values)
    centered = [value - mean_value for value in values]
    size = len(values)
    long_run_variance = sum(value * value for value in centered) / size
    maximum_lag = min(lag, size - 1)
    for offset in range(1, maximum_lag + 1):
        covariance = (
            sum(
                centered[index] * centered[index - offset]
                for index in range(offset, size)
            )
            / size
        )
        weight = 1 - offset / (maximum_lag + 1)
        long_run_variance += 2 * weight * covariance
    if long_run_variance <= 0:
        return None
    return mean_value / math.sqrt(long_run_variance / size)


def build_panel(dataset: dict[str, Any]) -> tuple[list[dict[str, Any]], dict[str, int]]:
    rows = []
    trigger_counts = {factor: 0 for factor in FACTOR_LABELS}
    for token in dataset["tokens"]:
        bars = token["bars"]
        for index, bar in enumerate(bars):
            alerts = bar["f5x"]["alerts"]
            for factor in FACTOR_LABELS:
                trigger_counts[factor] += int(bool(alerts[factor]))
            forward = {}
            for horizon in HORIZONS:
                forward[horizon] = future_max_gain(bars, index, horizon)
            rows.append(
                {
                    "symbol": token["symbol"],
                    "date": bar["d"],
                    "alerts": alerts,
                    "forward": forward,
                }
            )
    return rows, trigger_counts


def calculate(
    rows: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], dict[int, int]]:
    by_date: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        by_date[row["date"]].append(row)

    eligible_dates = {}
    for horizon in HORIZONS:
        eligible_dates[horizon] = sum(
            sum(row["forward"][horizon] is not None for row in date_rows)
            >= MIN_ASSETS
            for date_rows in by_date.values()
        )

    results = []
    for factor in FACTOR_LABELS:
        for horizon in HORIZONS:
            daily_ics = []
            observation_count = 0
            if factor != "f5f":
                for date_rows in by_date.values():
                    usable = [
                        row
                        for row in date_rows
                        if row["forward"][horizon] is not None
                    ]
                    if len(usable) < MIN_ASSETS:
                        continue
                    factor_values = [
                        float(bool(row["alerts"][factor])) for row in usable
                    ]
                    if len(set(factor_values)) < 2:
                        continue
                    forward_returns = [
                        float(row["forward"][horizon]) for row in usable
                    ]
                    ic = spearman(factor_values, forward_returns)
                    if ic is not None:
                        daily_ics.append(ic)
                        observation_count += len(usable)

            mean_ic = statistics.mean(daily_ics) if daily_ics else None
            median_ic = statistics.median(daily_ics) if daily_ics else None
            standard_deviation = (
                statistics.stdev(daily_ics) if len(daily_ics) >= 2 else None
            )
            results.append(
                {
                    "factor": factor.upper(),
                    "factor_name": FACTOR_LABELS[factor],
                    "horizon_days": horizon,
                    "mean_rank_ic": mean_ic,
                    "median_rank_ic": median_ic,
                    "ic_std": standard_deviation,
                    "newey_west_tstat": newey_west_tstat(
                        daily_ics, horizon - 1
                    ),
                    "positive_ic_rate": (
                        sum(value > 0 for value in daily_ics)
                        / len(daily_ics)
                        if daily_ics
                        else None
                    ),
                    "valid_ic_days": len(daily_ics),
                    "eligible_dates": eligible_dates[horizon],
                    "valid_day_ratio": (
                        len(daily_ics) / eligible_dates[horizon]
                        if eligible_dates[horizon]
                        else None
                    ),
                    "observations": observation_count,
                }
            )
    return results, eligible_dates


def number(value: float | None, digits: int = 6) -> str:
    return "" if value is None else f"{value:.{digits}f}"


def write_csv(results: list[dict[str, Any]]) -> None:
    fieldnames = list(results[0])
    with OUTPUT_CSV.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in results:
            writer.writerow(
                {
                    key: number(value)
                    if isinstance(value, float)
                    else value
                    for key, value in row.items()
                }
            )


def report_cell(row: dict[str, Any]) -> str:
    if row["mean_rank_ic"] is None:
        return "N/A"
    tstat = (
        "N/A"
        if row["newey_west_tstat"] is None
        else f"{row['newey_west_tstat']:+.2f}"
    )
    return (
        f"{row['mean_rank_ic']:+.3f}"
        f" / {tstat}"
        f" / {row['valid_ic_days']}"
    )


def write_report(
    dataset: dict[str, Any],
    results: list[dict[str, Any]],
    trigger_counts: dict[str, int],
    eligible_dates: dict[int, int],
) -> None:
    lookup = {
        (row["factor"].lower(), row["horizon_days"]): row for row in results
    }
    tested = [
        row
        for row in results
        if row["mean_rank_ic"] is not None and row["valid_ic_days"] >= 20
    ]
    strongest = sorted(
        tested,
        key=lambda row: abs(row["mean_rank_ic"]),
        reverse=True,
    )[:8]
    lines = [
        "# F5 子因子 T+N 窗口最大涨幅横截面 Rank IC",
        "",
        f"- 数据生成时间：`{dataset['generated_at']}`",
        f"- 币种数：{len(dataset['tokens'])}",
        f"- 逐日记录数：{sum(len(token['bars']) for token in dataset['tokens'])}",
        f"- 前瞻周期：{', '.join(f'T+{value}' for value in HORIZONS)}",
        "- 因子值：HTML 中各 F5 子因子的异常高亮状态，异常为 1，普通为 0。",
        "- 预测目标：`max(high[D+1…D+N]) ÷ close[D] - 1`。",
        (
            f"- IC：每天在至少 {MIN_ASSETS} 个有完整未来窗口的币之间计算"
            " Spearman Rank IC，再对每日 IC 取均值。"
        ),
        "- t 值：Newey-West 调整，滞后阶数为 `N-1`。",
        "",
        "## 结果总表",
        "",
        "单元格为 `平均 Rank IC / Newey-West t / 有效IC天数`。",
        "",
        "| 因子 | "
        + " | ".join(f"T+{horizon}" for horizon in HORIZONS)
        + " |",
        "|---|"
        + "|".join("---:" for _ in HORIZONS)
        + "|",
    ]
    for factor, label in FACTOR_LABELS.items():
        cells = [
            report_cell(lookup[(factor, horizon)]) for horizon in HORIZONS
        ]
        lines.append(f"| {label} | " + " | ".join(cells) + " |")

    lines.extend(
        [
            "",
            "## 有效样本",
            "",
            "| 因子 | 异常记录数 | "
            + " | ".join(f"T+{horizon}有效IC天数" for horizon in HORIZONS)
            + " |",
            "|---|---:|"
            + "|".join("---:" for _ in HORIZONS)
            + "|",
        ]
    )
    for factor, label in FACTOR_LABELS.items():
        valid_days = [
            str(lookup[(factor, horizon)]["valid_ic_days"])
            for horizon in HORIZONS
        ]
        lines.append(
            f"| {label} | {trigger_counts[factor]} | "
            + " | ".join(valid_days)
            + " |"
        )

    lines.extend(
        [
            "",
            "各周期可形成的基础横截面日期数（尚未要求因子在币种间有差异）："
            + "；".join(
                f"T+{horizon}={eligible_dates[horizon]}"
                for horizon in HORIZONS
            )
            + "。",
            "",
            "## 当前绝对值较高的结果",
            "",
        ]
    )
    if strongest:
        for row in strongest:
            direction = "未来窗口最大涨幅偏高" if row["mean_rank_ic"] > 0 else "未来窗口最大涨幅偏低"
            tstat = row["newey_west_tstat"]
            tstat_text = "N/A" if tstat is None else f"{tstat:+.2f}"
            lines.append(
                f"- {row['factor_name']}，T+{row['horizon_days']}："
                f"平均 IC `{row['mean_rank_ic']:+.3f}`，"
                f"NW t `{tstat_text}`，有效 {row['valid_ic_days']} 天；"
                f"异常币相对对应{direction}。"
            )
    else:
        lines.append("- 没有达到至少 20 个有效 IC 日的结果。")

    lines.extend(
        [
            "",
            "## 解释限制",
            "",
            "- F5f 没有实时可计算值，因此不计算 IC，也不会用零值冒充有效因子。",
            "- F5c 与 F5d 的当前异常定义可能高度重合，不能把两者相似的 IC 当作相互独立证据。",
            "- 二元因子只有在同一天不同币之间同时存在 0 和 1 时才能形成横截面 IC，因此有效天数显著少于总交易日。",
            "- T+3 以上的前瞻收益相互重叠；Newey-West t 值做了时间相关调整，但样本只有 13 个币，仍属于探索性结果。",
            "- Cluster 成员集合来自当前 Bubblemaps 截面，仍存在幸存者偏差和成员集合前视偏差。",
            "- 最大涨幅使用未来窗口最高价，是事后机会空间，不代表能够按最高价成交，也不等同于可实现策略收益。",
            "- IC 尚未扣除手续费、滑点、冲击成本和资金费率。",
            "",
            f"逐项明细见 `{OUTPUT_CSV.name}`。",
        ]
    )
    OUTPUT_MD.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> None:
    dataset = load_dataset()
    rows, trigger_counts = build_panel(dataset)
    results, eligible_dates = calculate(rows)
    write_csv(results)
    write_report(dataset, results, trigger_counts, eligible_dates)
    print(f"wrote {OUTPUT_CSV}")
    print(f"wrote {OUTPUT_MD}")


if __name__ == "__main__":
    main()
