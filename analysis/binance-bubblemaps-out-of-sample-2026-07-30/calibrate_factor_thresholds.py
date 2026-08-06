#!/usr/bin/env python3
"""Calibrate F1-F6 observation thresholds and validate them out of sample."""

from __future__ import annotations

import importlib.util
import json
import math
import statistics
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable


ROOT = Path(__file__).resolve().parent
PROJECT_ROOT = ROOT.parents[1]
ENGINE_PATH = (
    PROJECT_ROOT
    / "analysis/binance-bubblemaps-daily-events-2026-07-30/"
    "analyze_daily_events.py"
)
IN_SAMPLE_SNAPSHOT = Path(
    "/tmp/dbcompare-doc-validation-2026-07-29/"
    "full-market-retry/2026-07-29"
)
IN_SAMPLE_CONFIG = (
    PROJECT_ROOT / "validation/lark-chain-data-2026-07-29/events.json"
)
OUT_SAMPLE_SNAPSHOT = ROOT / "bubblemaps-snapshot"
OUT_SAMPLE_CONFIG = ROOT / "out_of_sample_config.json"
OUTPUT_JSON = ROOT / "factor-threshold-results.json"
OUTPUT_MD = ROOT / "factor-threshold-report.md"
COMMON_CUTOFF = date(2026, 7, 28)
IN_SAMPLE_SYMBOLS = ("SIREN", "RAVE", "BIRB", "VELVET", "DEXE")
COOLDOWN_DAYS = 7


def load_engine():
    spec = importlib.util.spec_from_file_location("factor_engine", ENGINE_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError("factor engine unavailable")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def finite_ratio(value: float | None, numerator: float) -> float:
    if value is not None and math.isfinite(value):
        return value
    return math.inf if numerator > 0 else 0.0


def load_group(
    engine,
    snapshot: Path,
    config_path: Path,
    symbols: tuple[str, ...] | None,
    group: str,
) -> list[dict[str, Any]]:
    config = json.loads(config_path.read_text(encoding="utf-8"))
    selected = symbols or tuple(config["symbols"])
    targets = {
        symbol: config["symbols"][symbol]["targets"] for symbol in selected
    }
    states, metadata = engine.load_bubblemaps(snapshot, targets)
    if metadata["missing_targets"]:
        raise ValueError(f"missing targets: {metadata['missing_targets']}")
    panel = []
    for symbol in selected:
        state = states[symbol]
        bars = engine.fetch_daily_bars(symbol, COMMON_CUTOFF)
        by_date = {bar["date"]: bar for bar in bars}
        dates = [bar["date"] for bar in bars]
        for index in range(35, len(bars) - 6):
            anchor = dates[index]
            previous = bars[index - 1]["close"]
            if previous <= 0:
                continue
            features = engine.window_features(state, anchor)
            pre = features["pre"]
            ratios = features["ratios"]
            cluster_amount = float(state["cluster_amount"])
            max_share = (
                float(pre["max_transfer"]) / cluster_amount
                if cluster_amount > 0
                else 0.0
            )
            net = float(pre["net_external_flow"])
            net_share = abs(net) / cluster_amount if cluster_amount > 0 else 0.0
            forward = {}
            for horizon in (1, 3, 7):
                target = bars[index + horizon - 1]["close"]
                forward[f"return_{horizon}d_pct"] = (target / previous - 1) * 100
            highs = [bars[index + offset]["high"] for offset in range(7)]
            lows = [bars[index + offset]["low"] for offset in range(7)]
            panel.append(
                {
                    "group": group,
                    "symbol": symbol,
                    "date": anchor,
                    "f1": finite_ratio(
                        ratios["amount"], float(pre["amount"])
                    ),
                    "f2": finite_ratio(ratios["count"], pre["count"]),
                    "f3": finite_ratio(
                        ratios["active_addresses"], pre["active_addresses"]
                    ),
                    "f4": finite_ratio(
                        ratios["new_addresses"], pre["new_addresses"]
                    ),
                    "f5": finite_ratio(
                        ratios["max_transfer"], float(pre["max_transfer"])
                    ),
                    "f5_cluster_share": max_share,
                    "f6": finite_ratio(ratios["abs_net_flow"], abs(net)),
                    "f6_cluster_share": net_share,
                    "f6_direction": (
                        "inflow" if net > 0 else "outflow" if net < 0 else "flat"
                    ),
                    "active_addresses": pre["active_addresses"],
                    "new_addresses": pre["new_addresses"],
                    **forward,
                    "max_runup_7d_pct": (max(highs) / previous - 1) * 100,
                    "max_drawdown_7d_pct": (min(lows) / previous - 1) * 100,
                }
            )
    return panel


def threshold_specs() -> list[dict[str, Any]]:
    specs = []
    for factor in ("f1", "f2"):
        for threshold in (2, 3, 5, 10):
            specs.append(
                {
                    "factor": factor.upper(),
                    "threshold": threshold,
                    "label": f"{factor.upper()}≥{threshold}",
                    "predicate": lambda row, f=factor, t=threshold: row[f] >= t,
                }
            )
    for threshold in (1.5, 2, 3, 5):
        specs.append(
            {
                "factor": "F3",
                "threshold": threshold,
                "label": f"F3≥{threshold}且活跃地址≥10",
                "predicate": lambda row, t=threshold: (
                    row["f3"] >= t and row["active_addresses"] >= 10
                ),
            }
        )
        specs.append(
            {
                "factor": "F4",
                "threshold": threshold,
                "label": f"F4≥{threshold}且新地址≥5",
                "predicate": lambda row, t=threshold: (
                    row["f4"] >= t and row["new_addresses"] >= 5
                ),
            }
        )
    for threshold in (2, 3, 5, 10):
        specs.append(
            {
                "factor": "F5",
                "threshold": threshold,
                "label": f"F5≥{threshold}且单笔占Cluster≥0.5%",
                "predicate": lambda row, t=threshold: (
                    row["f5"] >= t and row["f5_cluster_share"] >= 0.005
                ),
            }
        )
        for direction, chinese in (("inflow", "净流入"), ("outflow", "净流出")):
            specs.append(
                {
                    "factor": f"F6_{direction}",
                    "threshold": threshold,
                    "label": (
                        f"F6≥{threshold}且占Cluster≥1%，方向={chinese}"
                    ),
                    "predicate": lambda row, t=threshold, d=direction: (
                        row["f6"] >= t
                        and row["f6_cluster_share"] >= 0.01
                        and row["f6_direction"] == d
                    ),
                }
            )
    return specs


def episode_triggers(
    rows: list[dict[str, Any]], predicate: Callable[[dict], bool]
) -> list[dict[str, Any]]:
    by_symbol: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        by_symbol[row["symbol"]].append(row)
    selected = []
    for symbol_rows in by_symbol.values():
        symbol_rows.sort(key=lambda row: row["date"])
        last_trigger: date | None = None
        active_previous = False
        for row in symbol_rows:
            active = predicate(row)
            is_new_episode = active and not active_previous
            cooldown_clear = (
                last_trigger is None
                or (row["date"] - last_trigger).days >= COOLDOWN_DAYS
            )
            if active and (is_new_episode or cooldown_clear):
                selected.append(row)
                last_trigger = row["date"]
            active_previous = active
    return selected


def summarize(rows: list[dict[str, Any]]) -> dict[str, Any]:
    if not rows:
        return {
            "count": 0,
            "median_1d": None,
            "median_3d": None,
            "median_7d": None,
            "mean_7d": None,
            "win_rate_7d": None,
            "median_runup_7d": None,
            "median_drawdown_7d": None,
            "events": [],
        }
    returns_7d = [row["return_7d_pct"] for row in rows]
    return {
        "count": len(rows),
        "median_1d": statistics.median(row["return_1d_pct"] for row in rows),
        "median_3d": statistics.median(row["return_3d_pct"] for row in rows),
        "median_7d": statistics.median(returns_7d),
        "mean_7d": statistics.mean(returns_7d),
        "win_rate_7d": sum(value > 0 for value in returns_7d) / len(rows) * 100,
        "median_runup_7d": statistics.median(
            row["max_runup_7d_pct"] for row in rows
        ),
        "median_drawdown_7d": statistics.median(
            row["max_drawdown_7d_pct"] for row in rows
        ),
        "events": [
            {
                "symbol": row["symbol"],
                "date": row["date"],
                "return_1d_pct": row["return_1d_pct"],
                "return_3d_pct": row["return_3d_pct"],
                "return_7d_pct": row["return_7d_pct"],
            }
            for row in rows
        ],
    }


def direction_label(train: dict, test: dict) -> str:
    if train["count"] < 5 or test["count"] < 3:
        return "样本不足"
    train_median = train["median_7d"]
    test_median = test["median_7d"]
    combined_win = (
        train["win_rate_7d"] * train["count"]
        + test["win_rate_7d"] * test["count"]
    ) / (train["count"] + test["count"])
    if train_median > 0 and test_median > 0 and combined_win >= 55:
        return "买入观察"
    if train_median < 0 and test_median < 0 and combined_win <= 45:
        return "卖出风险观察"
    return "方向不稳定"


def serializable(value: Any) -> Any:
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, float) and not math.isfinite(value):
        return "from_zero"
    if isinstance(value, dict):
        return {key: serializable(item) for key, item in value.items()}
    if isinstance(value, list):
        return [serializable(item) for item in value]
    return value


def fmt(value: float | None) -> str:
    return "N/A" if value is None else f"{value:+.2f}%"


def build_report(results: dict[str, Any]) -> str:
    recommended = [
        row
        for row in results["thresholds"]
        if row["classification"] in {"买入观察", "卖出风险观察"}
    ]
    lines = [
        "# F1–F6 买入/卖出观察阈值校准",
        "",
        "- 因子窗截至 D-1；收益从 D-1 收盘开始计算，避免使用事件日价格决定信号。",
        "- 样本内：SIREN、RAVE、BIRB、VELVET、DEXE；样本外：SOON、ESPORTS、KOMA。",
        "- 同一因子持续触发时按 7 天冷却合并，减少重叠窗口重复计数。",
        "- “买入观察/卖出风险观察”要求样本内与样本外的 7 日中位收益方向一致；它不是自动交易指令。",
        "",
        "## 可保留的观察阈值",
        "",
    ]
    if not recommended:
        lines.append("- 当前没有任何单因子阈值同时通过样本内与样本外方向一致性要求。")
    else:
        lines.extend(
            [
                "| 分类 | 阈值 | 样本内次数 | 样本内7日中位 | 样本外次数 | 样本外7日中位 | 合并7日胜率 |",
                "|---|---|---:|---:|---:|---:|---:|",
            ]
        )
        for row in recommended:
            train = row["in_sample"]
            test = row["out_of_sample"]
            combined_win = (
                train["win_rate_7d"] * train["count"]
                + test["win_rate_7d"] * test["count"]
            ) / (train["count"] + test["count"])
            lines.append(
                f"| {row['classification']} | {row['label']} | "
                f"{train['count']} | {fmt(train['median_7d'])} | "
                f"{test['count']} | {fmt(test['median_7d'])} | "
                f"{combined_win:.1f}% |"
            )
    lines.extend(
        [
            "",
            "## 当前定义阈值的表现",
            "",
            "| 因子 | 当前阈值 | 判断 | 样本内次数 | 内7日中位 | 样本外次数 | 外7日中位 |",
            "|---|---|---|---:|---:|---:|---:|",
        ]
    )
    current_labels = {
        "F1≥3",
        "F2≥3",
        "F3≥2且活跃地址≥10",
        "F4≥2且新地址≥5",
        "F5≥3且单笔占Cluster≥0.5%",
        "F6≥3且占Cluster≥1%，方向=净流入",
        "F6≥3且占Cluster≥1%，方向=净流出",
    }
    for row in results["thresholds"]:
        if row["label"] not in current_labels:
            continue
        train = row["in_sample"]
        test = row["out_of_sample"]
        lines.append(
            f"| {row['factor']} | {row['label']} | {row['classification']} | "
            f"{train['count']} | {fmt(train['median_7d'])} | "
            f"{test['count']} | {fmt(test['median_7d'])} |"
        )
    lines.extend(
        [
            "",
            "## 使用原则",
            "",
            "- F1–F5 本身没有天然买卖方向；只有在样本内和样本外后续收益方向一致时，才赋予观察标签。",
            "- F6 必须结合方向：净流入与净流出分开校准，不能只使用绝对值。",
            "- 单因子仅用于观察。正式规则应至少加入价格位置、成交量、上市初期过滤和模式标签，并进行前向验证。",
            "",
        ]
    )
    return "\n".join(lines)


def main() -> None:
    engine = load_engine()
    in_sample = load_group(
        engine,
        IN_SAMPLE_SNAPSHOT,
        IN_SAMPLE_CONFIG,
        IN_SAMPLE_SYMBOLS,
        "in_sample",
    )
    out_sample = load_group(
        engine,
        OUT_SAMPLE_SNAPSHOT,
        OUT_SAMPLE_CONFIG,
        None,
        "out_of_sample",
    )
    thresholds = []
    for spec in threshold_specs():
        train = summarize(episode_triggers(in_sample, spec["predicate"]))
        test = summarize(episode_triggers(out_sample, spec["predicate"]))
        thresholds.append(
            {
                "factor": spec["factor"],
                "threshold": spec["threshold"],
                "label": spec["label"],
                "classification": direction_label(train, test),
                "in_sample": train,
                "out_of_sample": test,
            }
        )
    results = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "common_cutoff": COMMON_CUTOFF,
        "cooldown_days": COOLDOWN_DAYS,
        "in_sample_panel_rows": len(in_sample),
        "out_of_sample_panel_rows": len(out_sample),
        "thresholds": thresholds,
    }
    ready = serializable(results)
    OUTPUT_JSON.write_text(
        json.dumps(ready, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    OUTPUT_MD.write_text(build_report(ready), encoding="utf-8")
    print(f"wrote {OUTPUT_JSON}")
    print(f"wrote {OUTPUT_MD}")


if __name__ == "__main__":
    main()
