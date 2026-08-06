#!/usr/bin/env python3
"""Run a true end-to-end V2 backtest over every F7c signal in the 169 pool.

Unlike ``run_v2_169.py``, this runner does not freeze the entries accepted by
the old daily-exit portfolio.  It evaluates every threshold-crossing signal,
lets V2 exits release capacity, and then admits later signals chronologically.
It also produces a per-symbol independent-sleeve diagnostic that removes
cross-symbol capacity competition while retaining the same entry sizing,
leverage, fees, and one-position-per-symbol rule.
"""

from __future__ import annotations

import argparse
import csv
import importlib.util
import json
import math
import statistics
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


HERE = Path(__file__).resolve().parent
OLD = HERE.parent / "f7c-strategy-backtest-2026-08-01"
DATA_DIR = HERE / "intraday-all-signals-data"
INPUT_TRADES = HERE / "v2-all-signal-input-trades.csv"
EXIT_TRADES = HERE / "v2-all-signal-exit-trades.csv"
EXIT_EVENTS = HERE / "v2-all-signal-exit-events.csv"
PORTFOLIO_EQUITY = HERE / "v2-end-to-end-portfolio-equity.csv"
PORTFOLIO_ATTRIBUTION = HERE / "v2-end-to-end-portfolio-attribution.csv"
PORTFOLIO_SKIPS = HERE / "v2-end-to-end-portfolio-skips.csv"
INDEPENDENT_SYMBOLS = HERE / "v2-independent-symbol-summary.csv"
INDEPENDENT_TRADES = HERE / "v2-independent-trade-attribution.csv"
SUMMARY_JSON = HERE / "v2-end-to-end-summary.json"
REPORT_MD = HERE / "v2-end-to-end-report.md"

FRICTION_PER_SIDE = 0.002
MARGIN_FRACTION = 0.30
LEVERAGE = 2.0
MAX_POSITIONS = 3
BAR_MS = 5 * 60 * 1000


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    fields: list[str] = []
    for row in rows:
        for key in row:
            if key not in fields:
                fields.append(key)
    with path.open("w", encoding="utf-8", newline="") as handle:
        if not fields:
            return
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def timestamp(day: str) -> int:
    return int(
        datetime.fromisoformat(day)
        .replace(tzinfo=timezone.utc)
        .timestamp()
        * 1000
    )


def aligned_event_time(event: dict[str, Any]) -> int:
    """Map cutoff close marks onto their containing 5m bar for simulation."""
    raw = int(float(event["execution_open_time_ms"]))
    return raw // BAR_MS * BAR_MS


def pct(value: float) -> str:
    return f"{value * 100:+.2f}%"


def prepare_candidates() -> list[dict[str, Any]]:
    dataset = json.loads((HERE / "dataset.json").read_text(encoding="utf-8"))
    bars = {
        token["symbol"]: token["bars"]
        for token in dataset["tokens"]
    }
    rows: list[dict[str, Any]] = []
    for signal in read_csv(HERE / "signals.csv"):
        symbol_bars = bars[signal["symbol"]]
        entry = next(
            (bar for bar in symbol_bars if bar["d"] > signal["signal_date"]),
            None,
        )
        if entry is None:
            continue
        rows.append(
            {
                "symbol": signal["symbol"],
                "group": signal["group"],
                "signal_date": signal["signal_date"],
                "signal_f7c_share": signal["f7c_share"],
                "signal_rank": signal["rank"],
                "entry_date": entry["d"],
                "entry_price": entry["o"],
                # The SSH exporter historically accepts closed daily trades.
                # These fields are placeholders only; V2 supplies the exits.
                "exit_decision_date": entry["d"],
                "exit_date": entry["d"],
                "exit_price": entry["o"],
                "entry_notional": 1.0,
                "entry_cost": 0.0,
                "exit_cost": 0.0,
                "net_pnl": 0.0,
                "holding_days": 0,
                "gross_return": 0.0,
                "net_return": 0.0,
                "mae": 0.0,
                "mfe": 0.0,
                "exit_reason": "V2候选占位",
                "status": "closed",
            }
        )
    write_csv(INPUT_TRADES, rows)
    return rows


def run_v2(candidates: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    manifest_path = DATA_DIR / "manifest.json"
    if not manifest_path.is_file():
        raise FileNotFoundError(f"missing all-signal intraday manifest: {manifest_path}")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    cases = manifest["cases"]
    if len(cases) != len(candidates):
        raise ValueError(f"manifest/candidate mismatch: {len(cases)} != {len(candidates)}")

    model = load_module("f7c_v2_end_to_end_exit", OLD / "backtest_multitimeframe_exit.py")
    model.HERE = HERE
    model.DATA_DIR = DATA_DIR
    model.MANIFEST = manifest_path
    model.TRADES_CSV = INPUT_TRADES
    summaries: list[dict[str, Any]] = []
    events: list[dict[str, Any]] = []
    for index, (case, trade) in enumerate(zip(cases, candidates, strict=True), start=1):
        summary, case_events = model.backtest_case(case, trade, "v2")
        summaries.append(summary)
        events.extend(case_events)
        if index % 50 == 0:
            print(f"V2 exits {index}/{len(cases)}", flush=True)
    write_csv(EXIT_TRADES, summaries)
    write_csv(EXIT_EVENTS, events)
    return summaries, events


def case_inputs(
    candidates: list[dict[str, Any]], summaries: list[dict[str, Any]], events: list[dict[str, Any]]
) -> tuple[list[dict[str, Any]], dict[str, list[dict[str, Any]]]]:
    manifest = json.loads((DATA_DIR / "manifest.json").read_text(encoding="utf-8"))
    by_events: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in events:
        by_events[row["case_id"]].append(row)
    summary_by_id = {row["case_id"]: row for row in summaries}
    result = []
    for case, trade in zip(manifest["cases"], candidates, strict=True):
        case_id = case["case_id"]
        result.append(
            {
                "case_id": case_id,
                "symbol": case["symbol"],
                "signal_date": case["signal_date"],
                "entry_date": case["entry_date"],
                "entry_time_ms": timestamp(case["entry_date"]),
                "entry_price": float(case["entry_price"]),
                "f7c": float(trade["signal_f7c_share"]),
                "rank": int(trade["signal_rank"]),
                "right_censored": str(
                    summary_by_id[case_id]["right_censored_mark"]
                ).lower() == "true",
            }
        )
    return result, by_events


def select_portfolio(
    cases: list[dict[str, Any]], by_events: dict[str, list[dict[str, Any]]]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    entries_by_time: dict[int, list[dict[str, Any]]] = defaultdict(list)
    exits_by_time: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for case in cases:
        entries_by_time[case["entry_time_ms"]].append(case)
        for event in by_events[case["case_id"]]:
            exits_by_time[aligned_event_time(event)].append(event)

    active: dict[str, float] = {}
    active_symbol: dict[str, str] = {}
    accepted: set[str] = set()
    selected_entries: list[dict[str, Any]] = []
    selected_exits: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    for time_ms in sorted(set(entries_by_time) | set(exits_by_time)):
        for event in exits_by_time.get(time_ms, []):
            case_id = event["case_id"]
            if case_id not in active:
                continue
            selected_exits.append(
                {
                    "time_ms": time_ms,
                    "case_id": case_id,
                    "fraction": float(event["sold_fraction"]),
                    "price": float(event["execution_price"]),
                    "reason": event["reason"],
                }
            )
            active[case_id] -= float(event["sold_fraction"])
            if active[case_id] <= 1e-12:
                symbol = next(row["symbol"] for row in cases if row["case_id"] == case_id)
                del active[case_id]
                active_symbol.pop(symbol, None)

        candidates = sorted(
            entries_by_time.get(time_ms, []),
            key=lambda row: (-row["f7c"], row["symbol"], row["case_id"]),
        )
        for case in candidates:
            reason = ""
            if case["symbol"] in active_symbol:
                reason = "same_symbol_still_held"
            elif len(active) >= MAX_POSITIONS:
                reason = "portfolio_full_3_positions"
            if reason:
                skipped.append({**case, "skip_reason": reason})
                continue
            case_id = case["case_id"]
            selected_entries.append(
                {
                    "time_ms": time_ms,
                    "case_id": case_id,
                    "symbol": case["symbol"],
                    "signal_date": case["signal_date"],
                    "entry_price": case["entry_price"],
                }
            )
            accepted.add(case_id)
            active[case_id] = 1.0
            active_symbol[case["symbol"]] = case_id
    if active:
        raise ValueError(f"portfolio selection has unclosed cases: {sorted(active)}")
    return selected_entries, selected_exits, skipped


def run_shared_portfolio(
    entries: list[dict[str, Any]], exits: list[dict[str, Any]]
) -> dict[str, Any]:
    weighted = load_module("f7c_v2_end_to_end_weighted", OLD / "backtest_portfolio_weighted.py")
    weighted.DATA_DIR = DATA_DIR
    manifest = json.loads((DATA_DIR / "manifest.json").read_text(encoding="utf-8"))
    selected_ids = {row["case_id"] for row in entries}
    bar_maps = {
        case["case_id"]: weighted.read_bar_map(
            DATA_DIR / case["intervals"]["5m"]["file"]
        )
        for case in manifest["cases"]
        if case["case_id"] in selected_ids
    }
    result = weighted.simulate("v2_end_to_end", entries, exits, bar_maps)
    write_csv(PORTFOLIO_EQUITY, result["curve"])
    write_csv(PORTFOLIO_ATTRIBUTION, result["attribution"])
    return result


def trade_pnl(entry_price: float, notional: float, events: list[dict[str, Any]]) -> float:
    pnl = -notional * FRICTION_PER_SIDE
    for event in events:
        fraction = float(event["sold_fraction"])
        exit_price = float(event["execution_price"])
        piece = fraction * notional
        pnl += piece * (exit_price / entry_price - 1)
        pnl -= piece * (exit_price / entry_price) * FRICTION_PER_SIDE
    return pnl


def run_independent_sleeves(
    all_symbols: list[str],
    cases: list[dict[str, Any]],
    by_events: dict[str, list[dict[str, Any]]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    by_symbol: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for case in cases:
        by_symbol[case["symbol"]].append(case)
    symbol_rows: list[dict[str, Any]] = []
    trade_rows: list[dict[str, Any]] = []
    for symbol in all_symbols:
        equity = 1.0
        occupied_until = -1
        executed = 0
        wins = 0
        skipped_overlap = 0
        censored = 0
        for case in sorted(
            by_symbol.get(symbol, []),
            key=lambda row: (row["entry_time_ms"], -row["f7c"], row["case_id"]),
        ):
            if case["entry_time_ms"] < occupied_until:
                skipped_overlap += 1
                continue
            events = sorted(
                by_events[case["case_id"]], key=aligned_event_time
            )
            margin = MARGIN_FRACTION * equity
            notional = LEVERAGE * margin
            pnl = trade_pnl(case["entry_price"], notional, events)
            equity += pnl
            executed += 1
            wins += pnl > 0
            censored += int(case["right_censored"])
            occupied_until = aligned_event_time(events[-1])
            trade_rows.append(
                {
                    "case_id": case["case_id"],
                    "symbol": symbol,
                    "signal_date": case["signal_date"],
                    "entry_date": case["entry_date"],
                    "entry_equity": equity - pnl,
                    "margin": margin,
                    "notional": notional,
                    "net_pnl": pnl,
                    "return_on_starting_sleeve": pnl,
                    "right_censored_mark": case["right_censored"],
                    "exit_time_utc": events[-1]["execution_time_utc"],
                    "last_exit_reason": events[-1]["reason"],
                }
            )
        total = equity - 1
        symbol_rows.append(
            {
                "symbol": symbol,
                "signal_episodes": len(by_symbol.get(symbol, [])),
                "executed_trades": executed,
                "skipped_same_symbol_overlap": skipped_overlap,
                "right_censored_trades": censored,
                "winning_trades": wins,
                "win_rate": wins / executed if executed else "",
                "final_equity": equity,
                "total_return": total,
                "classification": (
                    "positive" if total > 1e-12 else "negative" if total < -1e-12 else "no_trade"
                ),
            }
        )
    write_csv(INDEPENDENT_SYMBOLS, symbol_rows)
    write_csv(INDEPENDENT_TRADES, trade_rows)
    counts = Counter(row["classification"] for row in symbol_rows)
    combined = statistics.mean(float(row["final_equity"]) for row in symbol_rows) - 1
    return symbol_rows, trade_rows, {
        "positive_symbols": counts["positive"],
        "negative_symbols": counts["negative"],
        "no_trade_symbols": counts["no_trade"],
        "executed_trades": len(trade_rows),
        "equal_169_sleeve_return": combined,
    }


def max_drawdown(curve: list[dict[str, Any]]) -> float:
    peak = 1.0
    worst = 0.0
    for row in curve:
        value = float(row["equity"])
        peak = max(peak, value)
        worst = min(worst, value / peak - 1)
    return worst


def render_report(summary: dict[str, Any], symbols: list[dict[str, Any]]) -> str:
    portfolio = summary["shared_portfolio"]
    independent = summary["independent_sleeves"]
    positive = sorted(
        (row for row in symbols if row["classification"] == "positive"),
        key=lambda row: float(row["total_return"]), reverse=True,
    )
    negative = sorted(
        (row for row in symbols if row["classification"] == "negative"),
        key=lambda row: float(row["total_return"]),
    )
    lines = [
        "# 169币端到端 V2 回测",
        "",
        "## 口径",
        "",
        "- 对169币逐日扫描全部F7c首次入围信号；信号日收盘确认，下一日开盘候选入场。",
        "- V2退出直接释放共享三仓容量；同一时刻先退出再入场，候选按F7c降序选择，未获仓位的信号当日失效。",
        "- 共享组合每仓使用当时权益30%保证金、2×名义杠杆；开平仓各0.20%费用。",
        "- 逐币独立口径为每币初始权益1.0、同样30%保证金与2×杠杆；去除跨币仓位竞争，但同币持仓重叠信号仍跳过。",
        "- 数据截止前未完成14天退出的交易按截止价估值，并单列右删失，不将其误作已完成退出。",
        "",
        "## 信号漏斗",
        "",
        f"- 币池：{summary['universe_symbols']}币；产生信号：{summary['symbols_with_signals']}币 / {summary['signal_candidates']}次。",
        f"- 共享三仓实际执行：{portfolio['executed_symbols']}币 / {portfolio['executed_trades']}笔；容量跳过{portfolio['capacity_skipped']}次，同币仍持有跳过{portfolio['same_symbol_skipped']}次。",
        "",
        "## 共享三仓端到端V2",
        "",
        "| 指标 | 结果 |",
        "|---|---:|",
        f"| 最终权益 | {portfolio['final_equity']:.4f} |",
        f"| 总收益 | {pct(portfolio['total_return'])} |",
        f"| 5m最大回撤 | {pct(portfolio['max_drawdown_5m'])} |",
        f"| 胜率 | {pct(portfolio['win_rate'])} |",
        f"| 平均持仓天数 | {portfolio['average_holding_days']:.2f} |",
        f"| 每30天开仓 | {portfolio['entries_per_30_days']:.2f} |",
        f"| 正/负贡献币 | {portfolio['positive_symbols']} / {portfolio['negative_symbols']} |",
        "",
        "## 169个独立资金袖套",
        "",
        f"- 正贡献{independent['positive_symbols']}币，负贡献{independent['negative_symbols']}币，无交易{independent['no_trade_symbols']}币。",
        f"- 169袖套等初始资金合并收益：{pct(independent['equal_169_sleeve_return'])}；执行{independent['executed_trades']}笔。",
        "",
        "### 独立口径正贡献币",
        "",
        "| 币种 | 交易 | 收益 |",
        "|---|---:|---:|",
    ]
    for row in positive:
        lines.append(f"| {row['symbol']} | {row['executed_trades']} | {pct(float(row['total_return']))} |")
    lines.extend(["", "### 独立口径负贡献币", "", "| 币种 | 交易 | 收益 |", "|---|---:|---:|"])
    for row in negative:
        lines.append(f"| {row['symbol']} | {row['executed_trades']} | {pct(float(row['total_return']))} |")
    lines.extend(
        [
            "",
            "## 产物",
            "",
            f"- 共享组合权益：`{PORTFOLIO_EQUITY.name}`",
            f"- 共享组合归因：`{PORTFOLIO_ATTRIBUTION.name}`",
            f"- 共享组合跳过信号：`{PORTFOLIO_SKIPS.name}`",
            f"- 逐币独立汇总：`{INDEPENDENT_SYMBOLS.name}`",
            f"- 逐币独立逐笔：`{INDEPENDENT_TRADES.name}`",
        ]
    )
    return "\n".join(lines) + "\n"


def run() -> dict[str, Any]:
    candidates = prepare_candidates()
    summaries, events = run_v2(candidates)
    cases, by_events = case_inputs(candidates, summaries, events)
    entries, exits, skipped = select_portfolio(cases, by_events)
    write_csv(PORTFOLIO_SKIPS, skipped)
    portfolio = run_shared_portfolio(entries, exits)

    all_symbols = [token["symbol"] for token in json.loads(
        (HERE / "dataset.json").read_text(encoding="utf-8")
    )["tokens"]]
    symbol_rows, independent_trades, independent = run_independent_sleeves(
        all_symbols, cases, by_events
    )
    attribution = portfolio["attribution"]
    contributions: dict[str, float] = defaultdict(float)
    for row in attribution:
        contributions[row["symbol"]] += float(row["total_net_pnl"])
    holding = [
        (datetime.fromisoformat(row["exit_time_utc"]) - datetime.fromisoformat(row["entry_time_utc"])).total_seconds() / 86400
        for row in attribution
    ]
    elapsed_days = (
        datetime(2026, 8, 3, tzinfo=timezone.utc)
        - datetime(2026, 1, 1, tzinfo=timezone.utc)
    ).days + 1
    capacity = Counter(row["skip_reason"] for row in skipped)
    summary = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "universe_symbols": len(all_symbols),
        "symbols_with_signals": len({row["symbol"] for row in cases}),
        "signal_candidates": len(cases),
        "v2_right_censored_candidates": sum(row["right_censored"] for row in cases),
        "shared_portfolio": {
            "executed_trades": len(entries),
            "executed_symbols": len({row["symbol"] for row in entries}),
            "capacity_skipped": capacity["portfolio_full_3_positions"],
            "same_symbol_skipped": capacity["same_symbol_still_held"],
            "final_equity": portfolio["final_equity"],
            "total_return": portfolio["total_return"],
            "max_drawdown_5m": portfolio["max_drawdown_5m"],
            "min_equity": portfolio["min_equity"],
            "max_gross_leverage": portfolio["max_gross_leverage"],
            "average_invested_gross_leverage": portfolio["average_invested_gross_leverage"],
            "winning_trades": sum(float(row["total_net_pnl"]) > 0 for row in attribution),
            "losing_trades": sum(float(row["total_net_pnl"]) < 0 for row in attribution),
            "win_rate": sum(float(row["total_net_pnl"]) > 0 for row in attribution) / len(attribution),
            "positive_symbols": sum(value > 0 for value in contributions.values()),
            "negative_symbols": sum(value < 0 for value in contributions.values()),
            "average_holding_days": statistics.mean(holding),
            "median_holding_days": statistics.median(holding),
            "entries_per_30_days": len(entries) / elapsed_days * 30,
            "right_censored_executed": sum(
                case["right_censored"] for case in cases if case["case_id"] in {row["case_id"] for row in entries}
            ),
        },
        "independent_sleeves": independent,
    }
    SUMMARY_JSON.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    REPORT_MD.write_text(render_report(summary, symbol_rows), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return summary


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("prepare", "run"), nargs="?", default="run")
    args = parser.parse_args()
    if args.command == "prepare":
        rows = prepare_candidates()
        print(json.dumps({"candidate_trades": len(rows), "output": str(INPUT_TRADES)}, ensure_ascii=False))
        return
    run()


if __name__ == "__main__":
    main()
