#!/usr/bin/env python3
"""Calculate directional CEX net-flow T+N maximum-gain Rank IC."""

from __future__ import annotations

import csv
import statistics
from collections import defaultdict
from pathlib import Path
from typing import Any

import calculate_f5_subfactor_ic as common


ROOT = Path(__file__).resolve().parent
OUTPUT_CSV = ROOT / "cex-flow-forward-ic.csv"
OUTPUT_MD = ROOT / "cex-flow-ic-report.md"

VALUE_FACTORS = {
    "net_inflow_share": "F7a CEX净流入强度",
    "net_outflow_share": "F7b CEX净流出强度",
    "signed_net_share": "F7c CEX有符号净流",
}
TRIGGER_FACTORS = {
    "net_inflow_alert": "F7a CEX净流入异常",
    "net_outflow_alert": "F7b CEX净流出异常",
}
FACTOR_INTERPRETATION = {
    "net_inflow_share": "值越高表示 Cluster 净转入 CEX 越多",
    "net_outflow_share": "值越高表示 CEX 净转出至 Cluster 越多",
    "signed_net_share": "正值为净流入 CEX，负值为净流出 CEX",
    "net_inflow_alert": "F7 异常且方向为 CEX 净流入",
    "net_outflow_alert": "F7 异常且方向为 CEX 净流出",
}
VARIANT_LABELS = {
    "value": "连续值",
    "trigger": "异常触发",
}


def build_panel(
    dataset: dict[str, Any],
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    rows: list[dict[str, Any]] = []
    trigger_counts = {factor: 0 for factor in TRIGGER_FACTORS}
    for token in dataset["tokens"]:
        bars = token["bars"]
        cluster_amount = float(token["cluster_amount"])
        if cluster_amount <= 0:
            continue
        for index, bar in enumerate(bars):
            cex = bar.get("cex") or {}
            signed_net_share = float(cex.get("net_7d") or 0) / cluster_amount
            values = {
                "net_inflow_share": max(signed_net_share, 0.0),
                "net_outflow_share": max(-signed_net_share, 0.0),
                "signed_net_share": signed_net_share,
            }
            f7_trigger = 6 in bar.get("sig", [])
            triggers = {
                "net_inflow_alert": f7_trigger and signed_net_share > 0,
                "net_outflow_alert": f7_trigger and signed_net_share < 0,
            }
            for factor, triggered in triggers.items():
                trigger_counts[factor] += int(triggered)

            forward: dict[int, float | None] = {}
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

    results: list[dict[str, Any]] = []
    variants = (
        ("value", VALUE_FACTORS),
        ("trigger", TRIGGER_FACTORS),
    )
    for variant, factors in variants:
        for factor, factor_label in factors.items():
            for horizon in common.HORIZONS:
                daily_ics: list[float] = []
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
                        "factor": factor,
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
                    key: number(value) if isinstance(value, float) else value
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


def add_result_table(
    lines: list[str],
    lookup: dict[tuple[str, str, int], dict[str, Any]],
    variant: str,
    factors: dict[str, str],
) -> None:
    lines.extend(
        [
            "| 因子 | "
            + " | ".join(f"T+{horizon}" for horizon in common.HORIZONS)
            + " |",
            "|---|" + "|".join("---:" for _ in common.HORIZONS) + "|",
        ]
    )
    for factor, label in factors.items():
        cells = [
            report_cell(lookup[(variant, factor, horizon)])
            for horizon in common.HORIZONS
        ]
        lines.append(f"| {label} | " + " | ".join(cells) + " |")


def write_report(
    dataset: dict[str, Any],
    results: list[dict[str, Any]],
    trigger_counts: dict[str, int],
    eligible_dates: dict[int, int],
) -> None:
    lookup = {
        (row["variant"], row["factor"], row["horizon_days"]): row
        for row in results
    }
    lines = [
        "# CEX 净流入 / 净流出 T+N 窗口最大涨幅横截面 Rank IC",
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
        "- 单元格：`平均 Rank IC / Newey-West t / 有效IC天数`。",
        "",
        "## 因子定义",
        "",
        "- **F7a CEX净流入强度**：`max(CEX流入 − CEX流出, 0) ÷ Cluster余额`。",
        "- **F7b CEX净流出强度**：`max(CEX流出 − CEX流入, 0) ÷ Cluster余额`。",
        "- **F7c CEX有符号净流**：`(CEX流入 − CEX流出) ÷ Cluster余额`；正数为净流入，负数为净流出。",
        "- **方向异常**：沿用 HTML 的 F7 门槛，再按净流正负拆成净流入异常和净流出异常。",
        "",
        "## 连续值 Rank IC",
        "",
    ]
    add_result_table(lines, lookup, "value", VALUE_FACTORS)
    lines.extend(["", "## 方向异常 Rank IC", ""])
    add_result_table(lines, lookup, "trigger", TRIGGER_FACTORS)

    lines.extend(
        [
            "",
            "## 方向异常样本数",
            "",
            "| 因子 | 异常记录数 |",
            "|---|---:|",
        ]
    )
    for factor, label in TRIGGER_FACTORS.items():
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
    )[:10]
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
        tstat = row["newey_west_tstat"]
        lines.append(
            f"- {row['factor_name']}（{row['variant_name']}），"
            f"T+{row['horizon_days']}：平均 IC "
            f"`{row['mean_rank_ic']:+.3f}`，NW t "
            f"`{'N/A' if tstat is None else f'{tstat:+.2f}'}`，"
            f"有效 {row['valid_ic_days']} 天；"
            f"{FACTOR_INTERPRETATION[row['factor']]}。"
        )

    lines.extend(
        [
            "",
            "## 口径与限制",
            "",
            "- D 日因子只使用 D-7 至 D-1 的转账；最大涨幅以 D 日收盘为基准，不使用 D 日之后的链上信息。",
            "- 使用占 Cluster 余额的比例，而非原始代币数量，以免不同币种单位和供应量差异破坏横截面可比性。",
            "- 因子同时接收直接 CEX 转账和已确认多跳路径的唯一 CEX 边界交易；路径中间跳不累计，直接记录与多跳记录通过边界交易哈希、方向和金额去重。",
            "- 尚未完成路径复核的记录不会被猜测为 CEX 流量；在全量路径队列完成前，0 仍不一定代表真实无流量。",
            "- F7a 和 F7b 是同一有符号净流的两个单边部分，不能视为彼此独立的证据。",
            "- 正 IC 表示因子值较高的币在同日横截面中，未来窗口最大涨幅排序偏高；负 IC 表示排序偏低。",
            "- T+3 以上未来窗口相互重叠；虽然使用 Newey-West t 值，13 个币的样本仍只适合探索性判断。",
            "- 同时检查多个因子和周期且未做多重检验校正，不能仅凭绝对值最大的结果确定交易规则。",
            "- 最大涨幅使用未来窗口最高价，是事后机会空间，不代表能够按最高价成交，也不等同于可实现策略收益。",
            "- IC 未包含手续费、滑点、冲击成本和资金费率。",
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
