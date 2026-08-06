#!/usr/bin/env python3
"""Compare isolated daily price shocks with preceding Bubblemaps activity."""

from __future__ import annotations

import argparse
import json
import math
import statistics
import urllib.parse
import urllib.request
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterable


ROOT = Path(__file__).resolve().parent
PROJECT_ROOT = ROOT.parents[1]
DEFAULT_SNAPSHOT = Path(
    "/tmp/dbcompare-doc-validation-2026-07-29/"
    "full-market-retry/2026-07-29"
)
CANDIDATES_PATH = (
    PROJECT_ROOT
    / "screening/binance-small-volatile-2026-07-30/candidates.json"
)
TARGETS_PATH = (
    PROJECT_ROOT / "validation/lark-chain-data-2026-07-29/events.json"
)
FUTURES_KLINES = "https://fapi.binance.com/fapi/v1/klines"
SELECTED_SYMBOLS = ("SIREN", "RAVE", "BIRB", "VELVET", "DEXE")
EVENT_THRESHOLD_PCT = 20.0
EPISODE_GAP_DAYS = 7
PRE_DAYS = 7
BASELINE_DAYS = 28
HEADERS = {
    "Accept": "application/json",
    "Accept-Encoding": "identity",
    "User-Agent": "dbcompare-bubblemaps-event-study/1.0",
}
ZERO = Decimal(0)


def normalized_address(value: str) -> str:
    return value.lower() if value.lower().startswith("0x") else value


def request_json(url: str) -> Any:
    request = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(request, timeout=35) as response:
        return json.loads(response.read().decode("utf-8"))


def fetch_daily_bars(symbol: str, cutoff: date) -> list[dict[str, Any]]:
    params = {"symbol": f"{symbol}USDT", "interval": "1d", "limit": 1500}
    rows = request_json(f"{FUTURES_KLINES}?{urllib.parse.urlencode(params)}")
    bars: list[dict[str, Any]] = []
    previous_close: float | None = None
    for row in rows:
        if not isinstance(row, list) or len(row) < 7:
            continue
        day = datetime.fromtimestamp(int(row[0]) / 1000, tz=timezone.utc).date()
        close = float(row[4])
        if day > cutoff or not math.isfinite(close) or close <= 0:
            continue
        return_pct = (
            (close / previous_close - 1) * 100
            if previous_close is not None and previous_close > 0
            else None
        )
        bars.append(
            {
                "date": day,
                "open": float(row[1]),
                "high": float(row[2]),
                "low": float(row[3]),
                "close": close,
                "volume": float(row[5]),
                "return_pct": return_pct,
            }
        )
        previous_close = close
    return bars


def event_episodes(bars: list[dict[str, Any]]) -> list[dict[str, Any]]:
    shocks = [
        bar
        for bar in bars
        if bar["return_pct"] is not None
        and abs(bar["return_pct"]) >= EVENT_THRESHOLD_PCT
    ]
    groups: list[list[dict[str, Any]]] = []
    for shock in shocks:
        if (
            not groups
            or (shock["date"] - groups[-1][-1]["date"]).days > EPISODE_GAP_DAYS
        ):
            groups.append([shock])
        else:
            groups[-1].append(shock)
    episodes = []
    for group in groups:
        maximum = max(group, key=lambda item: abs(item["return_pct"]))
        episodes.append(
            {
                "start_date": group[0]["date"],
                "start_return_pct": group[0]["return_pct"],
                "max_date": maximum["date"],
                "max_return_pct": maximum["return_pct"],
                "shock_day_count": len(group),
                "direction": "up" if group[0]["return_pct"] > 0 else "down",
            }
        )
    return episodes


def record_identity(
    chain: str, token_address: str, row: dict[str, Any]
) -> tuple[Any, ...]:
    data = row["data"]
    return (
        chain,
        normalized_address(token_address),
        data["tx_hash"],
        normalized_address(row["from_address"]),
        normalized_address(row["to_address"]),
        str(data["value"]),
        int(data["date"]),
    )


def load_bubblemaps(
    snapshot: Path,
    selected_targets: dict[str, dict[str, list[str]]],
) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    manifest = json.loads((snapshot / "manifest.json").read_text(encoding="utf-8"))
    target_to_symbol: dict[tuple[str, str], str] = {}
    for symbol, targets in selected_targets.items():
        for chain, addresses in targets.items():
            for address in addresses:
                target_to_symbol[(chain, normalized_address(address))] = symbol

    state = {
        symbol: {
            "records": [],
            "seen": set(),
            "members": set(),
            "cluster_amount": ZERO,
            "chains": set(),
            "targets": [],
            "top_cluster_share_percent": [],
            "reported_unique_transfers": 0,
        }
        for symbol in selected_targets
    }
    matched: set[tuple[str, str]] = set()

    for token_entry in manifest.get("tokens", []):
        chain = token_entry["canonical_chain"]
        token_address = token_entry["canonical_token_address"]
        requested_key = (
            token_entry.get("requested_chain", chain),
            normalized_address(
                token_entry.get("requested_token_address", token_address)
            ),
        )
        canonical_key = (chain, normalized_address(token_address))
        symbol = target_to_symbol.get(requested_key) or target_to_symbol.get(
            canonical_key
        )
        if symbol is None:
            continue
        matched.add(requested_key if requested_key in target_to_symbol else canonical_key)
        target_state = state[symbol]
        target_state["chains"].add(chain)
        target_state["targets"].append(f"{chain}:{token_address}")
        target_state["reported_unique_transfers"] += int(
            token_entry.get("unique_transfer_count") or 0
        )
        token = json.loads(
            (snapshot / token_entry["token_file"]).read_text(encoding="utf-8")
        )
        clusters = token.get("clusters", [])
        if clusters:
            target_state["top_cluster_share_percent"].append(
                Decimal(str(clusters[0].get("share_percent") or 0))
            )
        for cluster in clusters:
            target_state["cluster_amount"] += Decimal(str(cluster.get("amount") or 0))
            for member in cluster.get("members", []):
                target_state["members"].add(
                    normalized_address(member["address"])
                )
                transfer_file = member.get("transfer_file")
                if not transfer_file:
                    continue
                transfer_path = snapshot / transfer_file
                if not transfer_path.is_file():
                    # Resumable snapshots publish the cluster document before
                    # every member history is available. Missing files mean
                    # "not captured yet", not an empty transfer history.
                    continue
                document = json.loads(
                    transfer_path.read_text(encoding="utf-8")
                )
                for row in document.get("transfers", []):
                    identity = record_identity(chain, token_address, row)
                    if identity in target_state["seen"]:
                        continue
                    target_state["seen"].add(identity)
                    data = row["data"]
                    event_time = datetime.fromtimestamp(
                        int(data["date"]) / 1000, tz=timezone.utc
                    )
                    target_state["records"].append(
                        {
                            "chain": chain,
                            "token_address": token_address,
                            "tx_hash": data["tx_hash"],
                            "from_address": normalized_address(
                                row["from_address"]
                            ),
                            "to_address": normalized_address(row["to_address"]),
                            "amount": Decimal(str(data["value"])),
                            "day": event_time.date(),
                            "timestamp_ms": int(data["date"]),
                        }
                    )

    missing = sorted(
        f"{chain}:{address}"
        for (chain, address) in target_to_symbol
        if (chain, address) not in matched
    )
    for symbol_state in state.values():
        symbol_state["records"].sort(
            key=lambda item: (item["timestamp_ms"], item["tx_hash"])
        )
        symbol_state.pop("seen")
    metadata = {
        "source": manifest.get("source"),
        "status": manifest.get("status"),
        "captured_at": manifest.get("captured_at"),
        "business_date": manifest.get("business_date"),
        "snapshot": str(snapshot),
        "missing_targets": missing,
    }
    return state, metadata


def records_in_range(
    records: Iterable[dict[str, Any]], start: date, end: date
) -> list[dict[str, Any]]:
    return [row for row in records if start <= row["day"] <= end]


def summarize_window(
    records: list[dict[str, Any]],
    members: set[str],
    first_seen: dict[str, date],
) -> dict[str, Any]:
    addresses = {
        address
        for row in records
        for address in (row["from_address"], row["to_address"])
    }
    inflow = ZERO
    outflow = ZERO
    for row in records:
        source_is_member = row["from_address"] in members
        target_is_member = row["to_address"] in members
        if target_is_member and not source_is_member:
            inflow += row["amount"]
        elif source_is_member and not target_is_member:
            outflow += row["amount"]
    return {
        "count": len(records),
        "amount": sum((row["amount"] for row in records), ZERO),
        "active_addresses": len(addresses),
        "new_addresses": sum(first_seen.get(address) in {row["day"] for row in records}
                             for address in addresses),
        "max_transfer": max((row["amount"] for row in records), default=ZERO),
        "external_inflow": inflow,
        "external_outflow": outflow,
        "net_external_flow": inflow - outflow,
    }


def mean_decimal(values: list[Decimal]) -> Decimal:
    return sum(values, ZERO) / Decimal(len(values)) if values else ZERO


def mean_number(values: list[int]) -> Decimal:
    return Decimal(sum(values)) / Decimal(len(values)) if values else ZERO


def ratio(value: Decimal | int, baseline: Decimal | int) -> float | None:
    denominator = Decimal(baseline)
    if denominator == ZERO:
        return None
    return float(Decimal(value) / denominator)


def burst(
    value: Decimal | int,
    baseline: Decimal | int,
    threshold: float,
) -> bool:
    numerator = Decimal(value)
    denominator = Decimal(baseline)
    if denominator == ZERO:
        return numerator > ZERO
    return numerator / denominator >= Decimal(str(threshold))


def ratio_label(value: Decimal | int, baseline: Decimal | int) -> str:
    numerator = Decimal(value)
    denominator = Decimal(baseline)
    if denominator == ZERO:
        return "从零启动" if numerator > ZERO else "N/A"
    return f"{numerator / denominator:.2f}x"


def window_features(
    state: dict[str, Any], anchor: date
) -> dict[str, Any]:
    records = state["records"]
    members = state["members"]
    first_seen: dict[str, date] = {}
    for row in records:
        for address in (row["from_address"], row["to_address"]):
            first_seen[address] = min(first_seen.get(address, row["day"]), row["day"])

    pre_start = anchor - timedelta(days=PRE_DAYS)
    pre_end = anchor - timedelta(days=1)
    baseline_start = anchor - timedelta(days=PRE_DAYS + BASELINE_DAYS)
    weekly = []
    for index in range(4):
        start = baseline_start + timedelta(days=index * 7)
        weekly.append(
            summarize_window(
                records_in_range(records, start, start + timedelta(days=6)),
                members,
                first_seen,
            )
        )
    pre = summarize_window(
        records_in_range(records, pre_start, pre_end), members, first_seen
    )
    baseline = {
        "count": mean_number([item["count"] for item in weekly]),
        "amount": mean_decimal([item["amount"] for item in weekly]),
        "active_addresses": mean_number(
            [item["active_addresses"] for item in weekly]
        ),
        "new_addresses": mean_number([item["new_addresses"] for item in weekly]),
        "max_transfer": Decimal(
            str(statistics.median([item["max_transfer"] for item in weekly]))
        ),
        "abs_net_external_flow": Decimal(
            str(
                statistics.median(
                    [abs(item["net_external_flow"]) for item in weekly]
                )
            )
        ),
    }
    ratios = {
        "amount": ratio(pre["amount"], baseline["amount"]),
        "count": ratio(pre["count"], baseline["count"]),
        "active_addresses": ratio(
            pre["active_addresses"], baseline["active_addresses"]
        ),
        "new_addresses": ratio(pre["new_addresses"], baseline["new_addresses"]),
        "max_transfer": ratio(pre["max_transfer"], baseline["max_transfer"]),
        "abs_net_flow": ratio(
            abs(pre["net_external_flow"]), baseline["abs_net_external_flow"]
        ),
    }
    ratio_text = {
        "amount": ratio_label(pre["amount"], baseline["amount"]),
        "count": ratio_label(pre["count"], baseline["count"]),
        "active_addresses": ratio_label(
            pre["active_addresses"], baseline["active_addresses"]
        ),
        "new_addresses": ratio_label(
            pre["new_addresses"], baseline["new_addresses"]
        ),
        "max_transfer": ratio_label(
            pre["max_transfer"], baseline["max_transfer"]
        ),
        "abs_net_flow": ratio_label(
            abs(pre["net_external_flow"]),
            baseline["abs_net_external_flow"],
        ),
    }
    cluster_amount = state["cluster_amount"]
    signals = {
        "gross_amount_burst": burst(pre["amount"], baseline["amount"], 3),
        "transfer_count_burst": burst(pre["count"], baseline["count"], 3),
        "active_address_burst": (
            burst(
                pre["active_addresses"],
                baseline["active_addresses"],
                2,
            )
            and pre["active_addresses"] >= 10
        ),
        "new_address_burst": (
            burst(pre["new_addresses"], baseline["new_addresses"], 2)
            and pre["new_addresses"] >= 5
        ),
        "whale_transfer": (
            burst(pre["max_transfer"], baseline["max_transfer"], 3)
            and cluster_amount > ZERO
            and pre["max_transfer"] / cluster_amount >= Decimal("0.005")
        ),
        "net_flow_shock": (
            burst(
                abs(pre["net_external_flow"]),
                baseline["abs_net_external_flow"],
                3,
            )
            and cluster_amount > ZERO
            and abs(pre["net_external_flow"]) / cluster_amount >= Decimal("0.01")
        ),
    }
    signal_names = [name for name, enabled in signals.items() if enabled]
    return {
        "anchor": anchor,
        "pre_start": pre_start,
        "pre_end": pre_end,
        "baseline_start": baseline_start,
        "baseline_end": pre_start - timedelta(days=1),
        "pre": pre,
        "baseline_week_mean": baseline,
        "ratios": ratios,
        "ratio_text": ratio_text,
        "signals": signal_names,
        "signal_count": len(signal_names),
        "composite_signal": len(signal_names) >= 2,
    }


def control_anchors(
    bars: list[dict[str, Any]], episodes: list[dict[str, Any]]
) -> list[date]:
    if not bars:
        return []
    event_dates = [item["start_date"] for item in episodes]
    start = bars[0]["date"] + timedelta(days=PRE_DAYS + BASELINE_DAYS)
    end = bars[-1]["date"]
    anchors = []
    current = start
    while current <= end:
        if all(abs((current - event).days) > 7 for event in event_dates):
            anchors.append(current)
        current += timedelta(days=7)
    return anchors


def json_ready(value: Any) -> Any:
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, set):
        return sorted(value)
    if isinstance(value, dict):
        return {key: json_ready(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_ready(item) for item in value]
    return value


def percent(numerator: int, denominator: int) -> float | None:
    return numerator / denominator * 100 if denominator else None


def fisher_exact_two_sided(
    event_hits: int,
    event_total: int,
    control_hits: int,
    control_total: int,
) -> float:
    from math import comb

    successes = event_hits + control_hits
    total = event_total + control_total
    lower = max(0, successes - control_total)
    upper = min(successes, event_total)
    denominator = comb(total, successes)

    def probability(value: int) -> float:
        return (
            comb(event_total, value)
            * comb(control_total, successes - value)
            / denominator
        )

    observed = probability(event_hits)
    return sum(
        probability(value)
        for value in range(lower, upper + 1)
        if probability(value) <= observed + 1e-15
    )


def fmt_pct(value: float | None) -> str:
    return "N/A" if value is None else f"{value:+.2f}%"


def fmt_ratio(value: float | None) -> str:
    return "N/A" if value is None else f"{value:.2f}x"


SIGNAL_LABELS = {
    "gross_amount_burst": "金额",
    "transfer_count_burst": "笔数",
    "active_address_burst": "活跃地址",
    "new_address_burst": "新地址",
    "whale_transfer": "巨额转账",
    "net_flow_shock": "净流冲击",
}


def build_report(results: dict[str, Any]) -> str:
    metadata = results["bubblemaps_snapshot"]
    summary = results["summary"]
    lines = [
        "# 小币种日线异动前的 Bubblemaps 信号复核",
        "",
        f"- 生成时间：{results['generated_at']}",
        f"- 样本类型：{results['sample_label']}。",
        f"- Bubblemaps 快照：{metadata['captured_at']}，状态 `{metadata['status']}`。",
        f"- 价格截止：{results['price_cutoff']}（UTC 已完成自然日）。",
        f"- 样本：{', '.join(results['selected_symbols'])}。",
        "- 价格事件：币安 USDⓈ-M 永续日收盘相对前一日收盘绝对涨跌 ≥20%；相隔不超过 7 天的异常日合并为同一段，前窗从第一天之前计算。",
        "- 链上窗口：W-1=D-7..D-1；基线为此前 28 天拆成四周后的周均。链上指标全部来自 Bubblemaps。",
        "- 复合信号：六类信号中至少同时触发两类。控制组为远离异常事件至少 7 天的等间隔周锚点。",
        "",
        "## 主要结论",
        "",
        f"- 共识别 {summary['episode_count']} 段独立日线异动，其中上涨起始 {summary['up_episode_count']} 段、下跌起始 {summary['down_episode_count']} 段。",
        f"- 异动前出现复合链上信号 {summary['event_composite_count']} / {summary['episode_count']}（{summary['event_composite_rate_pct']:.1f}%）。",
        f"- 分方向：上涨段 {summary['up_composite_count']} / {summary['up_episode_count']}（{summary['up_composite_rate_pct']:.1f}%）；下跌段 {summary['down_composite_count']} / {summary['down_episode_count']}（{summary['down_composite_rate_pct']:.1f}%）。",
        f"- 控制周出现同样复合信号 {summary['control_composite_count']} / {summary['control_count']}（{summary['control_composite_rate_pct']:.1f}%）。",
        f"- 事件前信号率相对控制组为 {summary['event_vs_control_lift']:.2f}x。",
        f"- 但整体差异的 Fisher 双侧检验 p={summary['fisher_exact_p_value']:.3f}，未达到 5% 显著性；应视为探索性线索，而不是稳定预测器。",
        "",
        "## 分币结果",
        "",
        "| Token | 日线覆盖 | Bubblemaps去重转账 | 异动段 | 异动前复合信号 | 控制周复合信号 | 反复出现的信号 |",
        "|---|---:|---:|---:|---:|---:|---|",
    ]
    for token in results["tokens"]:
        recurring = "、".join(
            SIGNAL_LABELS[name] for name in token["recurring_signals"]
        ) or "无"
        lines.append(
            f"| {token['symbol']} | {token['bar_start']}—{token['bar_end']} | "
            f"{token['unique_transfer_count']:,} | {len(token['episodes'])} | "
            f"{token['event_composite_count']}/{len(token['episodes'])} "
            f"({token['event_composite_rate_pct']:.1f}%) | "
            f"{token['control_composite_count']}/{token['control_count']} "
            f"({token['control_composite_rate_pct']:.1f}%) | {recurring} |"
        )

    lines.extend(
        [
            "",
            "## 每段异动及前置信号",
            "",
            "| Token | 异动起点 | 起点日涨跌 | 段内最大单日 | W-1金额/基线周 | W-1笔数/基线周 | 净流方向 | 信号 |",
            "|---|---:|---:|---:|---:|---:|---:|---|",
        ]
    )
    for token in results["tokens"]:
        for episode in token["episodes"]:
            features = episode["features"]
            net = Decimal(features["pre"]["net_external_flow"])
            net_text = (
                f"净流入 {abs(net):,.0f}"
                if net > ZERO
                else f"净流出 {abs(net):,.0f}"
                if net < ZERO
                else "0"
            )
            signals = "、".join(
                SIGNAL_LABELS[name] for name in features["signals"]
            ) or "无"
            lines.append(
                f"| {token['symbol']} | {episode['start_date']} | "
                f"{fmt_pct(episode['start_return_pct'])} | "
                f"{episode['max_date']} {fmt_pct(episode['max_return_pct'])} | "
                f"{features['ratio_text']['amount']} | "
                f"{features['ratio_text']['count']} | "
                f"{net_text} | {signals} |"
            )

    lines.extend(
        [
            "",
            "## 信号定义",
            "",
            "- 金额/笔数激增：W-1 ≥ 基线周均 3x。",
            "- 活跃地址激增：W-1 ≥ 2x 且至少 10 个地址。",
            "- 新地址激增：W-1 ≥ 2x 且至少 5 个首次可见地址。",
            "- 巨额转账：W-1 最大单笔 ≥ 基线周中位最大单笔 3x，且 ≥ 当前 Cluster 合计余额 0.5%。",
            "- 净流冲击：W-1 绝对外部净流 ≥ 基线周中位数 3x，且 ≥ 当前 Cluster 合计余额 1%。",
            "",
            "## 解释边界",
            "",
            "- Bubblemaps 没有历史 OHLC；日涨跌只负责确定事件日期，价格来自币安官方日 K。",
            "- Bubblemaps 的 Holder/Cluster 是抓取日截面，历史转账只覆盖抓取时仍属于这些 Cluster 的普通成员，存在幸存者偏差与前视成员集合偏差。",
            "- 金额为链上 gross transfer，不等于交易量、买入或新增资金；外部净流按当前 Cluster 成员集合计算。",
            "- 时间先后和统计抬升不能证明操盘或因果关系。",
            "",
        ]
    )
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--snapshot", type=Path, default=DEFAULT_SNAPSHOT)
    parser.add_argument("--config", type=Path, default=TARGETS_PATH)
    parser.add_argument("--output-dir", type=Path, default=ROOT)
    args = parser.parse_args()

    target_config = json.loads(args.config.read_text(encoding="utf-8"))
    selected_symbols = tuple(target_config["symbols"])
    excluded_symbols = set(target_config.get("excluded_in_sample_symbols", []))
    overlap = sorted(set(selected_symbols) & excluded_symbols)
    if overlap:
        raise ValueError(
            f"selected symbols overlap excluded in-sample set: {overlap}"
        )
    selected_targets = {
        symbol: target_config["symbols"][symbol]["targets"]
        for symbol in selected_symbols
    }
    states, metadata = load_bubblemaps(args.snapshot, selected_targets)
    captured_date = datetime.fromisoformat(
        metadata["captured_at"].replace("Z", "+00:00")
    ).date()
    cutoff = captured_date - timedelta(days=1)

    candidate_rows = {
        row["base_asset"]: row
        for row in json.loads(CANDIDATES_PATH.read_text(encoding="utf-8"))[
            "candidates"
        ]
    }
    token_results = []
    all_events = 0
    all_event_composites = 0
    all_controls = 0
    all_control_composites = 0
    up_events = 0
    down_events = 0
    up_composites = 0
    down_composites = 0

    for symbol in selected_symbols:
        state = states[symbol]
        bars = fetch_daily_bars(symbol, cutoff)
        episodes = event_episodes(bars)
        episode_results = []
        signal_frequency: dict[str, int] = defaultdict(int)
        for episode in episodes:
            features = window_features(state, episode["start_date"])
            for signal in features["signals"]:
                signal_frequency[signal] += 1
            episode_results.append({**episode, "features": features})
        controls = [
            window_features(state, anchor)
            for anchor in control_anchors(bars, episodes)
        ]
        event_composites = sum(
            item["features"]["composite_signal"] for item in episode_results
        )
        control_composites = sum(item["composite_signal"] for item in controls)
        recurring_signals = sorted(
            name for name, count in signal_frequency.items() if count >= 2
        )
        token_result = {
            "symbol": symbol,
            "candidate_rank": next(
                (
                    index
                    for index, row in enumerate(candidate_rows.values(), 1)
                    if row["base_asset"] == symbol
                ),
                None,
            ),
            "candidate_market_cap_usd": candidate_rows[symbol]["market_cap_usd"],
            "chains": state["chains"],
            "targets": state["targets"],
            "cluster_amount": state["cluster_amount"],
            "top_cluster_share_percent": state[
                "top_cluster_share_percent"
            ],
            "unique_transfer_count": len(state["records"]),
            "reported_unique_transfer_count": state[
                "reported_unique_transfers"
            ],
            "bar_start": bars[0]["date"] if bars else None,
            "bar_end": bars[-1]["date"] if bars else None,
            "bar_count": len(bars),
            "episodes": episode_results,
            "event_composite_count": event_composites,
            "event_composite_rate_pct": percent(
                event_composites, len(episode_results)
            )
            or 0,
            "control_count": len(controls),
            "control_composite_count": control_composites,
            "control_composite_rate_pct": percent(
                control_composites, len(controls)
            )
            or 0,
            "recurring_signals": recurring_signals,
        }
        token_results.append(token_result)
        all_events += len(episode_results)
        all_event_composites += event_composites
        all_controls += len(controls)
        all_control_composites += control_composites
        up_events += sum(item["direction"] == "up" for item in episode_results)
        down_events += sum(
            item["direction"] == "down" for item in episode_results
        )
        up_composites += sum(
            item["direction"] == "up"
            and item["features"]["composite_signal"]
            for item in episode_results
        )
        down_composites += sum(
            item["direction"] == "down"
            and item["features"]["composite_signal"]
            for item in episode_results
        )

    event_rate = percent(all_event_composites, all_events) or 0
    control_rate = percent(all_control_composites, all_controls) or 0
    fisher_p_value = fisher_exact_two_sided(
        all_event_composites,
        all_events,
        all_control_composites,
        all_controls,
    )
    results = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "sample_label": target_config.get("sample_label", "样本内"),
        "selected_symbols": list(selected_symbols),
        "excluded_in_sample_symbols": sorted(excluded_symbols),
        "price_source": FUTURES_KLINES,
        "price_cutoff": cutoff,
        "event_threshold_pct": EVENT_THRESHOLD_PCT,
        "episode_gap_days": EPISODE_GAP_DAYS,
        "bubblemaps_snapshot": metadata,
        "summary": {
            "episode_count": all_events,
            "up_episode_count": up_events,
            "down_episode_count": down_events,
            "up_composite_count": up_composites,
            "down_composite_count": down_composites,
            "up_composite_rate_pct": percent(up_composites, up_events) or 0,
            "down_composite_rate_pct": (
                percent(down_composites, down_events) or 0
            ),
            "event_composite_count": all_event_composites,
            "event_composite_rate_pct": event_rate,
            "control_count": all_controls,
            "control_composite_count": all_control_composites,
            "control_composite_rate_pct": control_rate,
            "event_vs_control_lift": (
                event_rate / control_rate if control_rate else None
            ),
            "fisher_exact_p_value": fisher_p_value,
        },
        "tokens": token_results,
    }
    ready = json_ready(results)
    args.output_dir.mkdir(parents=True, exist_ok=True)
    results_path = args.output_dir / "results.json"
    report_path = args.output_dir / "report.md"
    results_path.write_text(
        json.dumps(ready, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    report_path.write_text(build_report(ready), encoding="utf-8")
    print(f"wrote {results_path}")
    print(f"wrote {report_path}")


if __name__ == "__main__":
    main()
