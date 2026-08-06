#!/usr/bin/env python3
"""Chronological portfolio simulation without equal-weight trade averaging."""

from __future__ import annotations

import csv
import json
import statistics
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


HERE = Path(__file__).resolve().parent
DATA_DIR = HERE / "intraday-all-data"
MANIFEST = DATA_DIR / "manifest.json"
OLD_TRADES = HERE / "trades.csv"
V1_EVENTS = HERE / "multitimeframe-exit-events.csv"
V2_EVENTS = HERE / "multitimeframe-exit-v2-events.csv"
OUTPUT_CURVE = HERE / "weighted-portfolio-v2-equity.csv"
OUTPUT_ATTRIBUTION = HERE / "weighted-portfolio-v2-attribution.csv"
OUTPUT_REPORT = HERE / "weighted-portfolio-v2-report.md"

INITIAL_EQUITY = 1.0
MARGIN_FRACTION = 0.30
LEVERAGE = 2.0
FRICTION_PER_SIDE = 0.002
BAR_MS = 5 * 60 * 1000


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def timestamp(date_text: str) -> int:
    return int(datetime.fromisoformat(date_text).replace(tzinfo=timezone.utc).timestamp() * 1000)


def read_bar_map(path: Path) -> dict[int, dict[str, float]]:
    output = {}
    for raw in read_csv(path):
        key = int(raw["open_time_ms"])
        output[key] = {
            "open": float(raw["open"]),
            "close": float(raw["close"]),
        }
    return output


def max_drawdown(values: list[float]) -> float:
    peak = values[0]
    worst = 0.0
    for value in values:
        peak = max(peak, value)
        worst = min(worst, value / peak - 1)
    return worst


def build_strategy_events(
    cases: list[dict[str, Any]],
    old_trades: list[dict[str, str]],
    model_events: dict[str, list[dict[str, str]]],
) -> tuple[dict[str, list[dict[str, Any]]], dict[str, list[dict[str, Any]]]]:
    strategy_names = ["old_daily", *model_events]
    entries: dict[str, list[dict[str, Any]]] = {name: [] for name in strategy_names}
    exits: dict[str, list[dict[str, Any]]] = {name: [] for name in strategy_names}
    by_strategy_case: dict[str, dict[str, list[dict[str, str]]]] = {}
    for name, rows in model_events.items():
        grouped: dict[str, list[dict[str, str]]] = defaultdict(list)
        for event in rows:
            grouped[event["case_id"]].append(event)
        by_strategy_case[name] = grouped
    for case, old in zip(cases, old_trades, strict=True):
        entry = {
            "time_ms": timestamp(case["entry_date"]),
            "case_id": case["case_id"],
            "symbol": case["symbol"],
            "signal_date": case["signal_date"],
            "entry_price": float(case["entry_price"]),
        }
        entries["old_daily"].append(entry)
        for name in model_events:
            entries[name].append(dict(entry))
        exits["old_daily"].append(
            {
                "time_ms": timestamp(old["exit_date"]),
                "case_id": case["case_id"],
                "fraction": 1.0,
                "price": float(old["exit_price"]),
                "reason": old["exit_reason"],
            }
        )
        for name in model_events:
            for event in by_strategy_case[name][case["case_id"]]:
                exits[name].append(
                    {
                        "time_ms": int(event["execution_open_time_ms"]),
                        "case_id": case["case_id"],
                        "fraction": float(event["sold_fraction"]),
                        "price": float(event["execution_price"]),
                        "reason": event["reason"],
                    }
                )
    return entries, exits


def simulate(
    name: str,
    entries: list[dict[str, Any]],
    exits: list[dict[str, Any]],
    bar_maps: dict[str, dict[int, dict[str, float]]],
) -> dict[str, Any]:
    entries_by_time: dict[int, list[dict[str, Any]]] = defaultdict(list)
    exits_by_time: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for event in entries:
        entries_by_time[event["time_ms"]].append(event)
    for event in exits:
        exits_by_time[event["time_ms"]].append(event)
    start = min(entries_by_time)
    end = max(exits_by_time)
    balance = INITIAL_EQUITY
    positions: dict[str, dict[str, Any]] = {}
    attribution: dict[str, dict[str, Any]] = {}
    curve = []

    def mark_equity(time_ms: int, field: str) -> float:
        equity = balance
        for case_id, position in positions.items():
            bar = bar_maps[case_id].get(time_ms)
            if bar is None:
                raise ValueError(f"missing {case_id} bar at {time_ms}")
            equity += (
                position["remaining_fraction"]
                * position["notional"]
                * (bar[field] / position["entry_price"] - 1)
            )
        return equity

    for time_ms in range(start, end + BAR_MS, BAR_MS):
        for event in exits_by_time.get(time_ms, []):
            position = positions[event["case_id"]]
            fraction = min(event["fraction"], position["remaining_fraction"])
            notional_piece = fraction * position["notional"]
            gross_pnl = notional_piece * (event["price"] / position["entry_price"] - 1)
            exit_fee = notional_piece * (event["price"] / position["entry_price"]) * FRICTION_PER_SIDE
            net_pnl = gross_pnl - exit_fee
            balance += net_pnl
            position["remaining_fraction"] -= fraction
            item = attribution[event["case_id"]]
            item["exit_pnl_after_exit_fee"] += net_pnl
            item["exit_events"] += 1
            item["last_exit_reason"] = event["reason"]
            item["exit_time_utc"] = datetime.fromtimestamp(time_ms / 1000, timezone.utc).isoformat()
            if position["remaining_fraction"] <= 1e-12:
                del positions[event["case_id"]]

        equity_at_open = mark_equity(time_ms, "open") if positions else balance
        for event in entries_by_time.get(time_ms, []):
            if len(positions) >= 3:
                raise ValueError(f"{name} exceeds three positions at {time_ms}")
            margin = MARGIN_FRACTION * equity_at_open
            notional = LEVERAGE * margin
            entry_fee = notional * FRICTION_PER_SIDE
            balance -= entry_fee
            positions[event["case_id"]] = {
                **event,
                "notional": notional,
                "remaining_fraction": 1.0,
            }
            attribution[event["case_id"]] = {
                "strategy": name,
                "case_id": event["case_id"],
                "symbol": event["symbol"],
                "signal_date": event["signal_date"],
                "entry_time_utc": datetime.fromtimestamp(time_ms / 1000, timezone.utc).isoformat(),
                "entry_equity": equity_at_open,
                "margin": margin,
                "notional": notional,
                "entry_fee": entry_fee,
                "exit_pnl_after_exit_fee": 0.0,
                "exit_events": 0,
                "last_exit_reason": "",
                "exit_time_utc": "",
            }

        close_equity = mark_equity(time_ms, "close") if positions else balance
        gross_exposure = 0.0
        for case_id, position in positions.items():
            gross_exposure += (
                position["remaining_fraction"]
                * position["notional"]
                * bar_maps[case_id][time_ms]["close"]
                / position["entry_price"]
            )
        curve.append(
            {
                "strategy": name,
                "time_utc": datetime.fromtimestamp(time_ms / 1000, timezone.utc).isoformat(),
                "equity": close_equity,
                "open_positions": len(positions),
                "gross_exposure": gross_exposure,
                "gross_leverage": gross_exposure / close_equity if close_equity > 0 else float("inf"),
            }
        )

    if positions:
        raise ValueError(f"{name} has open positions at end: {list(positions)}")
    for item in attribution.values():
        item["total_net_pnl"] = item["exit_pnl_after_exit_fee"] - item["entry_fee"]
        item["contribution_to_initial_equity"] = item["total_net_pnl"] / INITIAL_EQUITY
        item["return_on_allocated_margin"] = item["total_net_pnl"] / item["margin"]
    values = [INITIAL_EQUITY] + [row["equity"] for row in curve]
    return {
        "name": name,
        "curve": curve,
        "attribution": list(attribution.values()),
        "final_equity": balance,
        "total_return": balance / INITIAL_EQUITY - 1,
        "max_drawdown_5m": max_drawdown(values),
        "min_equity": min(values),
        "max_gross_leverage": max(row["gross_leverage"] for row in curve),
        "average_invested_gross_leverage": statistics.mean(
            row["gross_leverage"] for row in curve if row["open_positions"]
        ),
    }


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    fields: list[str] = []
    for row in rows:
        for key in row:
            if key not in fields:
                fields.append(key)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def pct(value: float) -> str:
    return f"{value * 100:+.2f}%"


def render_report(results: list[dict[str, Any]]) -> str:
    old, v1, v2 = results
    new_contrib = sorted(v2["attribution"], key=lambda row: row["total_net_pnl"], reverse=True)
    lines = [
        "# 非等权动态资金组合回测",
        "",
        "## 资金口径",
        "",
        f"- 初始权益1.0；每次入场使用当时组合权益的{MARGIN_FRACTION:.0%}作为保证金，单仓{LEVERAGE:.0f}×，即初始名义敞口约为组合权益的{MARGIN_FRACTION * LEVERAGE:.0%}。",
        "- 最多同时3仓；退出先于同一时刻的新入场处理；前一笔盈亏会改变后一笔仓位。",
        f"- 开仓和平仓各收取名义金额的{FRICTION_PER_SIDE:.2%}，未计资金费率、额外滑点和交易所维持保证金。",
        "- 为单独比较退出，仍固定原有26笔入场；提前释放仓位不会补进原策略曾跳过的信号。",
        "",
        "## 组合结果",
        "",
        "| 指标 | 原日线退出 | V1硬回撤 | V2分层退出 |",
        "|---|---:|---:|---:|",
        f"| 最终权益 | {old['final_equity']:.4f} | {v1['final_equity']:.4f} | {v2['final_equity']:.4f} |",
        f"| 组合总收益 | {pct(old['total_return'])} | {pct(v1['total_return'])} | {pct(v2['total_return'])} |",
        f"| 5m盯市最大回撤 | {pct(old['max_drawdown_5m'])} | {pct(v1['max_drawdown_5m'])} | {pct(v2['max_drawdown_5m'])} |",
        f"| 最低权益 | {old['min_equity']:.4f} | {v1['min_equity']:.4f} | {v2['min_equity']:.4f} |",
        f"| 最大总名义杠杆 | {old['max_gross_leverage']:.2f}× | {v1['max_gross_leverage']:.2f}× | {v2['max_gross_leverage']:.2f}× |",
        f"| 持仓期间平均总名义杠杆 | {old['average_invested_gross_leverage']:.2f}× | {v1['average_invested_gross_leverage']:.2f}× | {v2['average_invested_gross_leverage']:.2f}× |",
        "",
        "## 新退出策略的收益贡献",
        "",
        "| 排名 | 币种 | 信号日 | 入场权益 | 名义仓位 | 组合PnL贡献 | 保证金收益 |",
        "|---:|---|---|---:|---:|---:|---:|",
    ]
    for rank, item in enumerate(new_contrib, start=1):
        lines.append(
            f"| {rank} | {item['symbol']} | {item['signal_date']} | {item['entry_equity']:.4f} | "
            f"{item['notional']:.4f} | {pct(item['contribution_to_initial_equity'])} | "
            f"{pct(item['return_on_allocated_margin'])} |"
        )
    lines.extend(
        [
            "",
            "## 解释",
            "",
            "- 这张表去掉了等权：盈利较早的交易会扩大之后的仓位，亏损会缩小之后的仓位。",
            "- V2在分层减仓之外，为最后25%的runner增加极端MFE动态保护：运行MFE达到500%/700%/900%后，峰值回撤阈值依次收紧到20%/10%/5%。",
            "- 该保护只使用当前已完成5m K线可知的运行最高价和MFE，并在下一根5m开盘执行；它不引用事后最高点。",
            "- 最大回撤按5m收盘盯市计算，比只看每笔最终收益更接近真实组合风险，但仍未模拟K线内部爆仓和穿价。",
            "",
            f"- 权益曲线：`{OUTPUT_CURVE.name}`",
            f"- 逐笔资金贡献：`{OUTPUT_ATTRIBUTION.name}`",
        ]
    )
    return "\n".join(lines) + "\n"


def main() -> None:
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    cases = manifest["cases"]
    old_trades = read_csv(OLD_TRADES)
    model_events = {
        "v1_multitimeframe": read_csv(V1_EVENTS),
        "v2_multitimeframe": read_csv(V2_EVENTS),
    }
    entries, exits = build_strategy_events(cases, old_trades, model_events)
    bar_maps = {
        case["case_id"]: read_bar_map(DATA_DIR / case["intervals"]["5m"]["file"])
        for case in cases
    }
    results = [
        simulate(name, entries[name], exits[name], bar_maps)
        for name in ("old_daily", "v1_multitimeframe", "v2_multitimeframe")
    ]
    write_csv(OUTPUT_CURVE, [row for result in results for row in result["curve"]])
    write_csv(OUTPUT_ATTRIBUTION, [row for result in results for row in result["attribution"]])
    OUTPUT_REPORT.write_text(render_report(results), encoding="utf-8")
    print(f"wrote {OUTPUT_CURVE}")
    print(f"wrote {OUTPUT_ATTRIBUTION}")
    print(f"wrote {OUTPUT_REPORT}")


if __name__ == "__main__":
    main()
