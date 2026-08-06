#!/usr/bin/env python3
"""Calculate T+N maximum-gain Rank IC for F1-F6 values and triggers."""

from __future__ import annotations

import csv
import statistics
from collections import defaultdict
from pathlib import Path
from typing import Any

import calculate_f5_subfactor_ic as common


ROOT = Path(__file__).resolve().parent
OUTPUT_CSV = ROOT / "f1-f6-forward-ic.csv"
OUTPUT_MD = ROOT / "f1-f6-ic-report.md"
FACTOR_LABELS = {
    "f1": "F1 转账金额放大",
    "f2": "F2 转账笔数放大",
    "f3": "F3 活跃地址扩张",
    "f4": "F4 新地址扩张",
    "f5": "F5 巨额转账",
    "f6": "F6 绝对净流冲击",
}
VARIANT_LABELS = {
    "value": "连续值",
    "trigger": "异常触发",
}


def build_panel(
    dataset: dict[str, Any],
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    rows = []
    trigger_counts = {factor: 0 for factor in FACTOR_LABELS}
    for token in dataset["tokens"]:
        bars = token["bars"]
        for index, bar in enumerate(bars):
            values = {}
            triggers = {}
            for factor_index, factor in enumerate(FACTOR_LABELS):
                raw_value = bar["f"][factor_index]
                if bar["z"][factor_index]:
                    value = float("inf")
                elif raw_value is None:
                    value = 0.0
                else:
                    value = float(raw_value)
                values[factor] = value
                triggers[factor] = factor_index in bar["sig"]
                trigger_counts[factor] += int(triggers[factor])

            forward = {}
            for horizon in common.HORIZONS:
                forward[horizon] = common.future_max_gain(
                    bars, index, horizon
                )
            rows.append(
                {
                    "symbol": token["symbol"],
                    "date": bar["d"],
                    "values": values,
                    "triggers": triggers,
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

    eligible_dates = {
        horizon: sum(
            sum(row["forward"][horizon] is not None for row in date_rows)
            >= common.MIN_ASSETS
            for date_rows in by_date.values()
        )
        for horizon in common.HORIZONS
    }

    results = []
    for variant in VARIANT_LABELS:
        for factor, factor_label in FACTOR_LABELS.items():
            for horizon in common.HORIZONS:
                daily_ics = []
                observations = 0
                for date_rows in by_date.values():
                    usable = [
                        row
                        for row in date_rows
                        if row["forward"][horizon] is not None
                    ]
                    if len(usable) < common.MIN_ASSETS:
                        continue
                    if variant == "value":
                        factor_values = [
                            float(row["values"][factor]) for row in usable
                        ]
                    else:
                        factor_values = [
                            float(row["triggers"][factor]) for row in usable
                        ]
                    if len(set(factor_values)) < 2:
                        continue
                    forward_returns = [
                        float(row["forward"][horizon]) for row in usable
                    ]
                    ic = common.spearman(factor_values, forward_returns)
                    if ic is not None:
                        daily_ics.append(ic)
                        observations += len(usable)

                results.append(
                    {
                        "variant": variant,
                        "variant_name": VARIANT_LABELS[variant],
                        "factor": factor.upper(),
                        "factor_name": factor_label,
                        "horizon_days": horizon,
                        "mean_rank_ic": (
                            statistics.mean(daily_ics) if daily_ics else None
                        ),
                        "median_rank_ic": (
                            statistics.median(daily_ics)
                            if daily_ics
                            else None
                        ),
                        "ic_std": (
                            statistics.stdev(daily_ics)
                            if len(daily_ics) >= 2
                            else None
                        ),
                        "newey_west_tstat": common.newey_west_tstat(
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
                        "observations": observations,
                    }
                )
    return results, eligible_dates


def number(value: float | None, digits: int = 6) -> str:
    return "" if value is None else f"{value:.{digits}f}"


def write_csv(results: list[dict[str, Any]]) -> None:
    with OUTPUT_CSV.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(results[0]))
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
    tstat = row["newey_west_tstat"]
    return (
        f"{row['mean_rank_ic']:+.3f} / "
        f"{'N/A' if tstat is None else f'{tstat:+.2f}'} / "
        f"{row['valid_ic_days']}"
    )


def write_report(
    dataset: dict[str, Any],
    results: list[dict[str, Any]],
    trigger_counts: dict[str, int],
    eligible_dates: dict[int, int],
) -> None:
    lookup = {
        (
            row["variant"],
            row["factor"].lower(),
            row["horizon_days"],
        ): row
        for row in results
    }
    lines = [
        "# F1–F6 T+N 窗口最大涨幅横截面 Rank IC",
        "",
        f"- 数据生成时间：`{dataset['generated_at']}`",
        f"- 币种数：{len(dataset['tokens'])}",
        f"- 逐日记录数：{sum(len(token['bars']) for token in dataset['tokens'])}",
        (
            "- 预测目标："
            "`max(high[D+1…D+N]) ÷ close[D] - 1`，"
            f"`N={','.join(map(str, common.HORIZONS))}`。"
        ),
        (
            f"- IC：每天在至少 {common.MIN_ASSETS} 个有完整未来窗口的币之间"
            "计算 Spearman Rank IC，再对每日 IC 取均值。"
        ),
        "- t 值：Newey-West 调整，滞后阶数为 `N-1`。",
        "",
        "## 连续值 Rank IC",
        "",
        "单元格为 `平均 Rank IC / Newey-West t / 有效IC天数`。",
        "",
        "| 因子 | "
        + " | ".join(f"T+{horizon}" for horizon in common.HORIZONS)
        + " |",
        "|---|"
        + "|".join("---:" for _ in common.HORIZONS)
        + "|",
    ]
    for factor, label in FACTOR_LABELS.items():
        cells = [
            report_cell(lookup[("value", factor, horizon)])
            for horizon in common.HORIZONS
        ]
        lines.append(f"| {label} | " + " | ".join(cells) + " |")

    lines.extend(
        [
            "",
            "## 异常触发 Rank IC",
            "",
            "异常触发为 1，普通为 0；阈值与 HTML 黄色因子高亮一致。",
            "",
            "| 因子 | "
            + " | ".join(f"T+{horizon}" for horizon in common.HORIZONS)
            + " |",
            "|---|"
            + "|".join("---:" for _ in common.HORIZONS)
            + "|",
        ]
    )
    for factor, label in FACTOR_LABELS.items():
        cells = [
            report_cell(lookup[("trigger", factor, horizon)])
            for horizon in common.HORIZONS
        ]
        lines.append(f"| {label} | " + " | ".join(cells) + " |")

    lines.extend(
        [
            "",
            "## 异常样本数",
            "",
            "| 因子 | 异常记录数 |",
            "|---|---:|",
        ]
    )
    for factor, label in FACTOR_LABELS.items():
        lines.append(f"| {label} | {trigger_counts[factor]} |")

    eligible_text = "；".join(
        f"T+{horizon}={eligible_dates[horizon]}"
        for horizon in common.HORIZONS
    )
    tested = [
        row
        for row in results
        if row["mean_rank_ic"] is not None and row["valid_ic_days"] >= 20
    ]
    strongest = sorted(
        tested,
        key=lambda row: abs(row["mean_rank_ic"]),
        reverse=True,
    )[:12]
    lines.extend(
        [
            "",
            f"基础横截面日期数：{eligible_text}。",
            "",
            "## 当前绝对值较高的结果",
            "",
        ]
    )
    for row in strongest:
        direction = "未来窗口最大涨幅偏高" if row["mean_rank_ic"] > 0 else "未来窗口最大涨幅偏低"
        tstat = row["newey_west_tstat"]
        lines.append(
            f"- {row['factor_name']}（{row['variant_name']}），"
            f"T+{row['horizon_days']}：平均 IC "
            f"`{row['mean_rank_ic']:+.3f}`，NW t "
            f"`{'N/A' if tstat is None else f'{tstat:+.2f}'}`，"
            f"有效 {row['valid_ic_days']} 天；因子较高对应{direction}。"
        )

    lines.extend(
        [
            "",
            "## 口径与限制",
            "",
            "- 连续值使用页面中的 F1–F6 比率；“从零启动”排在当日所有有限比率之上，基线和观察窗同时为零则记为 0。",
            "- F3、F4 的连续值只表示扩张倍数；异常触发值另外包含活跃地址 ≥10、新地址 ≥5 的绝对数量门槛。",
            "- F5 的异常触发同时包含最大单笔占 Cluster ≥0.5% 的门槛。",
            "- F6 连续值是绝对净流冲击，未区分流入和流出；如果两种方向效果相反，当前 IC 会被抵消。",
            "- T+3 以上未来窗口相互重叠；虽然 t 值做了 Newey-West 调整，13 个币的样本仍只适合探索性判断。",
            "- 本报告同时检查 72 个“因子口径 × 周期”组合，未做多重检验校正；不能只挑绝对值最大的结果当作已确认规律。",
            "- 13 个币被合并在同一横截面内，报告不是独立的样本内训练/样本外验证拆分。",
            "- Cluster 成员集合来自当前 Bubblemaps 截面，存在幸存者偏差和成员集合前视偏差。",
            "- 最大涨幅使用未来窗口最高价，是事后机会空间，不代表能够按最高价成交，也不等同于可实现策略收益。",
            "- IC 不包含手续费、滑点、冲击成本和资金费率。",
            "",
            f"逐项明细见 `{OUTPUT_CSV.name}`。",
        ]
    )
    OUTPUT_MD.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> None:
    dataset = common.load_dataset()
    rows, trigger_counts = build_panel(dataset)
    results, eligible_dates = calculate(rows)
    write_csv(results)
    write_report(dataset, results, trigger_counts, eligible_dates)
    print(f"wrote {OUTPUT_CSV}")
    print(f"wrote {OUTPUT_MD}")


if __name__ == "__main__":
    main()
