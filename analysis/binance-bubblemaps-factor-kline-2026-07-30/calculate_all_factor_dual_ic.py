#!/usr/bin/env python3
"""Calculate all-factor Rank IC against three complementary forward targets."""

from __future__ import annotations

import csv
import statistics
from collections import defaultdict
from pathlib import Path
from typing import Any

import calculate_f5_subfactor_ic as common


ROOT = Path(__file__).resolve().parent
OUTPUT_CSV = ROOT / "all-factor-multi-forward-ic.csv"
OUTPUT_MD = ROOT / "all-factor-multi-ic-report.md"

TARGETS = {
    "max_gain": "窗口最大涨幅",
    "mean_close_deviation": "窗口平均收盘偏离",
    "volume_price_confirmation": "同日量价确认",
}

BASE_FACTORS = {
    "f1": "F1 转账金额放大",
    "f2": "F2 转账笔数放大",
    "f3": "F3 活跃地址扩张",
    "f4": "F4 新地址扩张",
    "f5": "F5 巨额转账",
    "f6": "F6 绝对净流冲击",
}
F5_FACTORS = {
    "f5a": "F5a 相对历史规模",
    "f5b": "F5b Cluster冲击",
    "f5c": "F5c 内外部属性",
    "f5d": "F5d 流向",
    "f5e": "F5e 特殊对手方",
    "f5f": "F5f 后续路径",
    "f5g": "F5g 巨额转账持续性",
    "f5h": "F5h 市场位置",
}
F7_VALUE_FACTORS = {
    "f7a_value": "F7a CEX净流入强度",
    "f7b_value": "F7b CEX净流出强度",
    "f7c_value": "F7c CEX有符号净流",
}
F7_TRIGGER_FACTORS = {
    "f7a_trigger": "F7a CEX净流入",
    "f7b_trigger": "F7b CEX净流出",
}


def factor_specs() -> list[dict[str, str]]:
    specs: list[dict[str, str]] = []
    for factor, label in BASE_FACTORS.items():
        specs.append(
            {
                "key": f"{factor}_value",
                "factor": factor.upper(),
                "label": label,
                "variant": "连续值",
                "group": "F1–F6",
            }
        )
        specs.append(
            {
                "key": f"{factor}_trigger",
                "factor": factor.upper(),
                "label": label,
                "variant": "异常触发",
                "group": "F1–F6",
            }
        )
    for factor, label in F5_FACTORS.items():
        specs.append(
            {
                "key": f"{factor}_trigger",
                "factor": factor.upper(),
                "label": label,
                "variant": "异常触发",
                "group": "F5子因子",
            }
        )
    for key, label in F7_VALUE_FACTORS.items():
        specs.append(
            {
                "key": key,
                "factor": key.split("_")[0].upper(),
                "label": label,
                "variant": "连续值",
                "group": "F7 CEX净流",
            }
        )
    for key, label in F7_TRIGGER_FACTORS.items():
        specs.append(
            {
                "key": key,
                "factor": key.split("_")[0].upper(),
                "label": label,
                "variant": "异常触发",
                "group": "F7 CEX净流",
            }
        )
    return specs


def build_panel(dataset: dict[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for token in dataset["tokens"]:
        bars = token["bars"]
        cluster_amount = float(token.get("cluster_amount") or 0)
        for index, bar in enumerate(bars):
            values: dict[str, float | None] = {}

            for factor_index, factor in enumerate(BASE_FACTORS):
                raw_value = bar["f"][factor_index]
                if bar["z"][factor_index]:
                    continuous_value = float("inf")
                elif raw_value is None:
                    continuous_value = 0.0
                else:
                    continuous_value = float(raw_value)
                values[f"{factor}_value"] = continuous_value
                values[f"{factor}_trigger"] = float(
                    factor_index in bar.get("sig", [])
                )

            alerts = (bar.get("f5x") or {}).get("alerts") or {}
            for factor in F5_FACTORS:
                values[f"{factor}_trigger"] = (
                    None
                    if factor == "f5f"
                    else float(bool(alerts.get(factor)))
                )

            if cluster_amount > 0:
                cex_data = bar.get("cex") or {}
                signed_share = float(cex_data.get("net_7d") or 0) / cluster_amount
                values["f7a_value"] = max(signed_share, 0.0)
                values["f7b_value"] = max(-signed_share, 0.0)
                values["f7c_value"] = signed_share
                f7_trigger = 6 in bar.get("sig", [])
                values["f7a_trigger"] = float(f7_trigger and signed_share > 0)
                values["f7b_trigger"] = float(f7_trigger and signed_share < 0)
            else:
                for key in (*F7_VALUE_FACTORS, *F7_TRIGGER_FACTORS):
                    values[key] = None

            targets: dict[str, dict[int, float | None]] = {
                target: {} for target in TARGETS
            }
            for horizon in common.HORIZONS:
                targets["max_gain"][horizon] = common.future_max_gain(
                    bars, index, horizon
                )
                targets["mean_close_deviation"][horizon] = (
                    common.future_mean_close_deviation(bars, index, horizon)
                )
                targets["volume_price_confirmation"][horizon] = (
                    common.future_volume_price_confirmation(
                        bars, index, horizon
                    )
                )

            rows.append(
                {
                    "symbol": token["symbol"],
                    "date": bar["d"],
                    "values": values,
                    "targets": targets,
                }
            )
    return rows


def calculate(
    rows: list[dict[str, Any]], specs: list[dict[str, str]]
) -> tuple[list[dict[str, Any]], dict[tuple[str, int], int]]:
    by_date: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        by_date[row["date"]].append(row)

    eligible_dates: dict[tuple[str, int], int] = {}
    for target in TARGETS:
        for horizon in common.HORIZONS:
            eligible_dates[(target, horizon)] = sum(
                sum(
                    row["targets"][target][horizon] is not None
                    for row in date_rows
                )
                >= common.MIN_ASSETS
                for date_rows in by_date.values()
            )

    results: list[dict[str, Any]] = []
    for spec in specs:
        for target, target_label in TARGETS.items():
            for horizon in common.HORIZONS:
                daily_ics: list[float] = []
                observations = 0
                for date_rows in by_date.values():
                    usable = [
                        row
                        for row in date_rows
                        if row["values"].get(spec["key"]) is not None
                        and row["targets"][target][horizon] is not None
                    ]
                    if len(usable) < common.MIN_ASSETS:
                        continue
                    factor_values = [
                        float(row["values"][spec["key"]]) for row in usable
                    ]
                    if len(set(factor_values)) < 2:
                        continue
                    target_values = [
                        float(row["targets"][target][horizon]) for row in usable
                    ]
                    ic = common.spearman(factor_values, target_values)
                    if ic is not None:
                        daily_ics.append(ic)
                        observations += len(usable)

                eligible = eligible_dates[(target, horizon)]
                results.append(
                    {
                        "group": spec["group"],
                        "factor": spec["factor"],
                        "factor_name": spec["label"],
                        "variant": spec["variant"],
                        "factor_key": spec["key"],
                        "target": target,
                        "target_name": target_label,
                        "horizon_days": horizon,
                        "mean_rank_ic": (
                            statistics.mean(daily_ics) if daily_ics else None
                        ),
                        "median_rank_ic": (
                            statistics.median(daily_ics) if daily_ics else None
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
                            sum(value > 0 for value in daily_ics) / len(daily_ics)
                            if daily_ics
                            else None
                        ),
                        "valid_ic_days": len(daily_ics),
                        "eligible_dates": eligible,
                        "valid_day_ratio": (
                            len(daily_ics) / eligible if eligible else None
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


def ic_cell(row: dict[str, Any]) -> str:
    value = row["mean_rank_ic"]
    return "N/A" if value is None else f"{value:+.3f}"


def detailed_cell(row: dict[str, Any]) -> str:
    value = row["mean_rank_ic"]
    if value is None:
        return "N/A"
    tstat = row["newey_west_tstat"]
    return (
        f"{value:+.3f} / "
        f"{'N/A' if tstat is None else f'{tstat:+.2f}'} / "
        f"{row['valid_ic_days']}"
    )


def add_comparison_table(
    lines: list[str],
    specs: list[dict[str, str]],
    lookup: dict[tuple[str, str, int], dict[str, Any]],
) -> None:
    lines.extend(
        [
            "| 因子 | 口径 | "
            + " | ".join(
                f"T+{h} 峰值/偏离/量价" for h in common.HORIZONS
            )
            + " |",
            "|---|---|" + "|".join("---:" for _ in common.HORIZONS) + "|",
        ]
    )
    for spec in specs:
        cells = []
        for horizon in common.HORIZONS:
            peak = lookup[(spec["key"], "max_gain", horizon)]
            deviation = lookup[(spec["key"], "mean_close_deviation", horizon)]
            volume_price = lookup[
                (spec["key"], "volume_price_confirmation", horizon)
            ]
            cells.append(
                f"{ic_cell(peak)} / {ic_cell(deviation)} / "
                f"{ic_cell(volume_price)}"
            )
        lines.append(
            f"| {spec['label']} | {spec['variant']} | "
            + " | ".join(cells)
            + " |"
        )


def add_target_detail_table(
    lines: list[str],
    specs: list[dict[str, str]],
    lookup: dict[tuple[str, str, int], dict[str, Any]],
    target: str,
) -> None:
    lines.extend(
        [
            "| 因子 | 口径 | "
            + " | ".join(f"T+{h}" for h in common.HORIZONS)
            + " |",
            "|---|---|" + "|".join("---:" for _ in common.HORIZONS) + "|",
        ]
    )
    for spec in specs:
        cells = [
            detailed_cell(lookup[(spec["key"], target, horizon)])
            for horizon in common.HORIZONS
        ]
        lines.append(
            f"| {spec['label']} | {spec['variant']} | "
            + " | ".join(cells)
            + " |"
        )


def write_report(
    dataset: dict[str, Any],
    specs: list[dict[str, str]],
    results: list[dict[str, Any]],
    eligible_dates: dict[tuple[str, int], int],
) -> None:
    lookup = {
        (row["factor_key"], row["target"], row["horizon_days"]): row
        for row in results
    }
    lines = [
        "# 全因子三目标 T+N 横截面 Rank IC",
        "",
        f"- 数据生成时间：`{dataset['generated_at']}`",
        f"- 币种数：{len(dataset['tokens'])}",
        f"- 逐日记录数：{sum(len(token['bars']) for token in dataset['tokens'])}",
        "- 峰值机会目标：`max(high[D+1…D+N]) ÷ close[D] - 1`。",
        "- 站上信号价目标：`mean(close[D+1…D+N] ÷ close[D] - 1)`。",
        (
            "- 同日量价确认目标："
            "`maxₜ[(close[t]÷close[D]−1)₊ × "
            "(volume[t]÷volume[D]−1)₊]`。"
        ),
        (
            f"- 每天在至少 {common.MIN_ASSETS} 个具有完整未来窗口的币之间"
            "计算 Spearman Rank IC，再对每日 IC 取均值。"
        ),
        "- Newey-West t 值滞后阶数为 `N-1`。",
        "",
        "## 三目标 IC 对照",
        "",
        "每格为 `窗口最大涨幅IC / 窗口平均收盘偏离IC / 同日量价确认IC`。",
        "",
    ]
    add_comparison_table(lines, specs, lookup)

    lines.extend(
        [
            "",
            "## 平均收盘偏离 IC 明细",
            "",
            "每格为 `平均Rank IC / Newey-West t / 有效IC天数`。",
            "",
        ]
    )
    add_target_detail_table(
        lines, specs, lookup, "mean_close_deviation"
    )

    lines.extend(
        [
            "",
            "## 同日量价确认 IC 明细",
            "",
            "每格为 `平均Rank IC / Newey-West t / 有效IC天数`。",
            "",
        ]
    )
    add_target_detail_table(
        lines, specs, lookup, "volume_price_confirmation"
    )

    strongest = sorted(
        (
            row
            for row in results
            if row["target"] == "mean_close_deviation"
            and row["mean_rank_ic"] is not None
            and row["valid_ic_days"] >= 20
        ),
        key=lambda row: abs(row["mean_rank_ic"]),
        reverse=True,
    )[:12]
    strongest_volume_price = sorted(
        (
            row
            for row in results
            if row["target"] == "volume_price_confirmation"
            and row["mean_rank_ic"] is not None
            and row["valid_ic_days"] >= 20
        ),
        key=lambda row: abs(row["mean_rank_ic"]),
        reverse=True,
    )[:12]
    divergences = []
    for spec in specs:
        for horizon in common.HORIZONS:
            peak = lookup[(spec["key"], "max_gain", horizon)]
            deviation = lookup[(spec["key"], "mean_close_deviation", horizon)]
            if (
                peak["mean_rank_ic"] is not None
                and deviation["mean_rank_ic"] is not None
                and peak["mean_rank_ic"] > 0
                and deviation["mean_rank_ic"] <= 0
            ):
                divergences.append((spec, horizon, peak, deviation))
    divergences.sort(
        key=lambda item: item[2]["mean_rank_ic"] - item[3]["mean_rank_ic"],
        reverse=True,
    )

    lines.extend(["", "## 平均偏离 IC 绝对值较高的结果", ""])
    for row in strongest:
        tstat = row["newey_west_tstat"]
        direction = "整体高于" if row["mean_rank_ic"] > 0 else "整体低于"
        lines.append(
            f"- {row['factor_name']}（{row['variant']}）T+{row['horizon_days']}："
            f"IC `{row['mean_rank_ic']:+.3f}`，NW t "
            f"`{'N/A' if tstat is None else f'{tstat:+.2f}'}`；"
            f"因子较高对应未来收盘价相对信号价{direction}。"
        )

    lines.extend(["", "## 量价确认 IC 绝对值较高的结果", ""])
    for row in strongest_volume_price:
        tstat = row["newey_west_tstat"]
        direction = "更强" if row["mean_rank_ic"] > 0 else "更弱"
        lines.append(
            f"- {row['factor_name']}（{row['variant']}）T+{row['horizon_days']}："
            f"IC `{row['mean_rank_ic']:+.3f}`，NW t "
            f"`{'N/A' if tstat is None else f'{tstat:+.2f}'}`；"
            f"因子较高对应未来同日放量上涨组合{direction}。"
        )

    lines.extend(["", "## 峰值为正、但平均偏离不为正", ""])
    if divergences:
        for spec, horizon, peak, deviation in divergences[:12]:
            lines.append(
                f"- {spec['label']}（{spec['variant']}）T+{horizon}："
                f"峰值 IC `{peak['mean_rank_ic']:+.3f}`，"
                f"平均偏离 IC `{deviation['mean_rank_ic']:+.3f}`。"
            )
    else:
        lines.append("- 当前没有符合该条件的组合。")

    eligible_text = "；".join(
        f"T+{h}={eligible_dates[('mean_close_deviation', h)]}"
        for h in common.HORIZONS
    )
    volume_price_eligible_text = "；".join(
        f"T+{h}={eligible_dates[('volume_price_confirmation', h)]}"
        for h in common.HORIZONS
    )
    lines.extend(
        [
            "",
            "## 口径与限制",
            "",
            f"- 平均偏离目标可形成横截面的基础日期数：{eligible_text}。",
            f"- 量价确认目标可形成横截面的基础日期数：{volume_price_eligible_text}。",
            "- 平均偏离使用每日收盘价，不会因一次盘中影线触及高价就给出很高评价。",
            "- 平均偏离为正表示未来窗口的平均收盘价高于信号日收盘价；并不要求窗口内每一天都高于信号价。",
            "- 量价确认必须在同一个交易日同时收盘上涨且成交量扩大；不同日期分别发生不会得分。",
            "- 量价确认以信号日成交量为基准；信号日成交量异常低时分数可能偏高，但 Rank IC 只使用排序。",
            "- 最大涨幅衡量事后峰值机会，平均偏离衡量能否整体站上信号价，量价确认衡量上涨是否伴随增量成交；三者应同时查看。",
            "- F5f 尚无逐日后续路径值，因此保持 N/A；F5c/F5d 及 F5主因子异常/F5g 当前分别高度重合。",
            "- T+3以上未来窗口重叠；虽使用Newey-West调整，13个币仍只适合探索性判断。",
            "- 多因子、多周期同时检验且未做多重检验校正，不能单凭最高 IC 制定交易规则。",
            "- IC 不包含手续费、滑点、冲击成本、资金费率，也不等同于策略收益。",
            "",
            f"逐项机器可读明细见 `{OUTPUT_CSV.name}`。",
        ]
    )
    OUTPUT_MD.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> None:
    dataset = common.load_dataset()
    specs = factor_specs()
    rows = build_panel(dataset)
    results, eligible_dates = calculate(rows, specs)
    write_csv(results)
    write_report(dataset, specs, results, eligible_dates)
    print(f"wrote {OUTPUT_CSV}")
    print(f"wrote {OUTPUT_MD}")


if __name__ == "__main__":
    main()
