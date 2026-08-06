#!/usr/bin/env python3
"""Match out-of-sample pre-event windows to original-document signal patterns."""

from __future__ import annotations

import json
import statistics
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parent
SNAPSHOT = ROOT / "bubblemaps-snapshot"
EVENT_RESULTS = ROOT / "results.json"
OUTPUT_JSON = ROOT / "pattern-match-results.json"
OUTPUT_MD = ROOT / "pattern-match-report.md"
ZERO = Decimal(0)

PATTERN_LABELS = {
    "volume_spike": "总量激增（SYN/TAC型）",
    "whale_volume_combo": "巨额单笔+总量放大（BANK型）",
    "hub_net_flow": "枢纽净流冲击（TAC型）",
    "fanout_new_addresses": "批量扇出+新地址（BIRB型）",
    "consolidation": "批量归集（BIRB型）",
    "fixed_amount_batch": "固定金额重复转账（BEAT型）",
    "safe_dispatch": "Safe/多签调度（PLAY/TAIKO型）",
    "cross_chain_sync": "跨链同步（PLAY/ALLO型）",
    "lock_migration": "锁仓/迁移合约转账（VELVET型）",
    "large_without_volume": "大额单笔但总量未放大（DEXE/ALLO/TAIKO型）",
    "cex_flow": "交易所地址集中流动（B/枢纽型）",
}


def parse_day(value: str) -> date:
    return date.fromisoformat(value)


def norm(value: str) -> str:
    return value.lower() if value.lower().startswith("0x") else value


def json_ready(value: Any) -> Any:
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, dict):
        return {key: json_ready(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [json_ready(item) for item in value]
    return value


def median_decimal(values: list[Decimal]) -> Decimal:
    return Decimal(str(statistics.median(values))) if values else ZERO


def ratio(value: Decimal | int, baseline: Decimal | int) -> float | None:
    numerator = Decimal(value)
    denominator = Decimal(baseline)
    if denominator == ZERO:
        return None
    return float(numerator / denominator)


def ratio_text(value: Decimal | int, baseline: Decimal | int) -> str:
    numerator = Decimal(value)
    denominator = Decimal(baseline)
    if denominator == ZERO:
        return "从零启动" if numerator > ZERO else "N/A"
    return f"{numerator / denominator:.2f}x"


def exceeds(
    value: Decimal | int, baseline: Decimal | int, multiplier: Decimal
) -> bool:
    numerator = Decimal(value)
    denominator = Decimal(baseline)
    return (
        numerator > ZERO
        if denominator == ZERO
        else numerator >= denominator * multiplier
    )


def load_symbol(symbol: str) -> dict[str, Any]:
    holders = json.loads(
        (SNAPSHOT / f"clean/{symbol}/holders.json").read_text(encoding="utf-8")
    )
    token = json.loads(
        (SNAPSHOT / f"data/{symbol}/token.json").read_text(encoding="utf-8")
    )
    metadata = {
        norm(row["address"]): row.get("address_details", {})
        for row in holders
    }
    members = {
        norm(member["address"])
        for cluster in token["clusters"]
        for member in cluster["members"]
    }
    cluster_amount = sum(
        (Decimal(str(cluster.get("amount") or 0)) for cluster in token["clusters"]),
        ZERO,
    )
    records = []
    seen = set()
    for path in (SNAPSHOT / f"clean/{symbol}/transfers").glob("*.json"):
        document = json.loads(path.read_text(encoding="utf-8"))
        for row in document.get("transfers", []):
            data = row["data"]
            identity = (
                data["tx_hash"],
                norm(row["from_address"]),
                norm(row["to_address"]),
                str(data["value"]),
                int(data["date"]),
            )
            if identity in seen:
                continue
            seen.add(identity)
            timestamp = datetime.fromtimestamp(
                int(data["date"]) / 1000, tz=timezone.utc
            )
            records.append(
                {
                    "tx_hash": data["tx_hash"],
                    "from": norm(row["from_address"]),
                    "to": norm(row["to_address"]),
                    "amount": Decimal(str(data["value"])),
                    "day": timestamp.date(),
                    "timestamp": timestamp.isoformat(),
                }
            )
    records.sort(key=lambda row: (row["day"], row["timestamp"], row["tx_hash"]))
    first_seen = {}
    for row in records:
        for address in (row["from"], row["to"]):
            first_seen[address] = min(first_seen.get(address, row["day"]), row["day"])
    return {
        "holders": holders,
        "metadata": metadata,
        "members": members,
        "cluster_amount": cluster_amount,
        "records": records,
        "first_seen": first_seen,
        "chains": {token["canonical_chain"]},
    }


def in_window(records: list[dict[str, Any]], start: date, end: date):
    return [row for row in records if start <= row["day"] <= end]


def amount_bucket(value: Decimal) -> str:
    magnitude = max(Decimal("0.0001"), abs(value) * Decimal("0.000001"))
    return format(
        value.quantize(magnitude, rounding=ROUND_HALF_UP).normalize(), "f"
    )


def window_metrics(state: dict[str, Any], start: date, end: date) -> dict[str, Any]:
    rows = in_window(state["records"], start, end)
    by_source: dict[str, set[str]] = defaultdict(set)
    by_target: dict[str, set[str]] = defaultdict(set)
    net: dict[str, Decimal] = defaultdict(lambda: ZERO)
    amount_counts: Counter[str] = Counter()
    new_addresses = set()
    safe_rows = []
    cex_rows = []
    migration_rows = []

    for row in rows:
        by_source[row["from"]].add(row["to"])
        by_target[row["to"]].add(row["from"])
        net[row["to"]] += row["amount"]
        net[row["from"]] -= row["amount"]
        amount_counts[amount_bucket(row["amount"])] += 1
        for address in (row["from"], row["to"]):
            if state["first_seen"].get(address) == row["day"]:
                new_addresses.add(address)
        endpoint_metadata = [
            (address, state["metadata"].get(address, {}))
            for address in (row["from"], row["to"])
        ]
        for address, details in endpoint_metadata:
            label = str(details.get("label") or "")
            lower = label.lower()
            evidence = {
                **row,
                "address": address,
                "label": label,
            }
            if "safe" in lower or "multisig" in lower:
                safe_rows.append(evidence)
            if details.get("is_cex"):
                cex_rows.append(evidence)
            if any(
                keyword in lower
                for keyword in (
                    "staking",
                    "vesting",
                    "lock",
                    "bridge",
                    "migration",
                )
            ):
                migration_rows.append(evidence)

    maximum = max(rows, key=lambda row: row["amount"]) if rows else None
    top_hub = (
        max(net.items(), key=lambda item: abs(item[1])) if net else (None, ZERO)
    )
    top_fanout = (
        max(by_source.items(), key=lambda item: len(item[1]))
        if by_source
        else (None, set())
    )
    top_consolidation = (
        max(by_target.items(), key=lambda item: len(item[1]))
        if by_target
        else (None, set())
    )
    fixed_value, fixed_count = (
        amount_counts.most_common(1)[0] if amount_counts else (None, 0)
    )
    return {
        "start": start,
        "end": end,
        "count": len(rows),
        "amount": sum((row["amount"] for row in rows), ZERO),
        "active_addresses": len(
            {address for row in rows for address in (row["from"], row["to"])}
        ),
        "new_addresses": len(new_addresses),
        "maximum": maximum,
        "top_hub_address": top_hub[0],
        "top_hub_net": top_hub[1],
        "top_fanout_address": top_fanout[0],
        "top_fanout_recipients": len(top_fanout[1]),
        "top_consolidation_address": top_consolidation[0],
        "top_consolidation_senders": len(top_consolidation[1]),
        "fixed_value": fixed_value,
        "fixed_count": fixed_count,
        "safe_rows": safe_rows,
        "cex_rows": cex_rows,
        "migration_rows": migration_rows,
    }


def baseline_weeks(state: dict[str, Any], anchor: date) -> list[dict[str, Any]]:
    start = anchor - timedelta(days=35)
    return [
        window_metrics(
            state,
            start + timedelta(days=7 * index),
            start + timedelta(days=7 * index + 6),
        )
        for index in range(4)
    ]


def mean_decimal(items: list[dict], key: str) -> Decimal:
    return sum((Decimal(item[key]) for item in items), ZERO) / Decimal(len(items))


def mean_count(items: list[dict], key: str) -> Decimal:
    return Decimal(sum(int(item[key]) for item in items)) / Decimal(len(items))


def largest_evidence(rows: list[dict[str, Any]]) -> dict[str, Any] | None:
    return max(rows, key=lambda row: row["amount"]) if rows else None


def metadata_label(state: dict[str, Any], address: str | None) -> str | None:
    if address is None:
        return None
    return state["metadata"].get(address, {}).get("label")


def classify_event(
    state: dict[str, Any], event: dict[str, Any]
) -> dict[str, Any]:
    anchor = parse_day(event["start_date"])
    pre = window_metrics(
        state, anchor - timedelta(days=7), anchor - timedelta(days=1)
    )
    weeks = baseline_weeks(state, anchor)
    baseline = {
        "amount": mean_decimal(weeks, "amount"),
        "count": mean_count(weeks, "count"),
        "new_addresses": mean_count(weeks, "new_addresses"),
        "max_transfer": median_decimal(
            [
                item["maximum"]["amount"] if item["maximum"] else ZERO
                for item in weeks
            ]
        ),
        "hub_abs_net": median_decimal(
            [abs(item["top_hub_net"]) for item in weeks]
        ),
        "fanout_recipients": median_decimal(
            [Decimal(item["top_fanout_recipients"]) for item in weeks]
        ),
        "consolidation_senders": median_decimal(
            [Decimal(item["top_consolidation_senders"]) for item in weeks]
        ),
        "fixed_count": median_decimal(
            [Decimal(item["fixed_count"]) for item in weeks]
        ),
    }
    cluster_amount = state["cluster_amount"]
    maximum = pre["maximum"]
    volume_spike = exceeds(pre["amount"], baseline["amount"], Decimal(3))
    whale = bool(
        maximum
        and exceeds(
            maximum["amount"], baseline["max_transfer"], Decimal(3)
        )
        and cluster_amount > ZERO
        and maximum["amount"] / cluster_amount >= Decimal("0.005")
    )
    hub = bool(
        pre["top_hub_address"]
        and exceeds(
            abs(pre["top_hub_net"]), baseline["hub_abs_net"], Decimal(3)
        )
        and cluster_amount > ZERO
        and abs(pre["top_hub_net"]) / cluster_amount >= Decimal("0.01")
    )
    new_address_burst = bool(
        pre["new_addresses"] >= 5
        and exceeds(
            pre["new_addresses"], baseline["new_addresses"], Decimal(2)
        )
    )
    fanout = bool(
        pre["top_fanout_recipients"] >= 10
        and exceeds(
            pre["top_fanout_recipients"],
            baseline["fanout_recipients"],
            Decimal(3),
        )
        and new_address_burst
    )
    consolidation = bool(
        pre["top_consolidation_senders"] >= 10
        and exceeds(
            pre["top_consolidation_senders"],
            baseline["consolidation_senders"],
            Decimal(3),
        )
    )
    fixed = bool(
        pre["fixed_count"] >= 5
        and exceeds(
            pre["fixed_count"], baseline["fixed_count"], Decimal(3)
        )
    )
    safe_evidence = largest_evidence(pre["safe_rows"])
    safe_dispatch = bool(
        safe_evidence
        and (
            len(pre["safe_rows"]) >= 2
            or (
                cluster_amount > ZERO
                and safe_evidence["amount"] / cluster_amount >= Decimal("0.005")
            )
        )
    )
    cex_evidence = largest_evidence(pre["cex_rows"])
    cex_flow = bool(
        cex_evidence
        and (
            len(pre["cex_rows"]) >= 3
            or (
                cluster_amount > ZERO
                and cex_evidence["amount"] / cluster_amount >= Decimal("0.01")
            )
        )
    )
    migration_evidence = largest_evidence(pre["migration_rows"])
    lock_migration = bool(
        migration_evidence
        and cluster_amount > ZERO
        and migration_evidence["amount"] / cluster_amount >= Decimal("0.005")
    )
    matched = {
        "volume_spike": volume_spike,
        "whale_volume_combo": whale and volume_spike,
        "hub_net_flow": hub,
        "fanout_new_addresses": fanout,
        "consolidation": consolidation,
        "fixed_amount_batch": fixed,
        "safe_dispatch": safe_dispatch,
        "cross_chain_sync": len(state["chains"]) > 1,
        "lock_migration": lock_migration,
        "large_without_volume": whale and not volume_spike,
        "cex_flow": cex_flow,
    }
    matches = [name for name, enabled in matched.items() if enabled]
    return {
        "event": event,
        "pre": pre,
        "baseline_week": baseline,
        "ratios": {
            "amount": ratio(pre["amount"], baseline["amount"]),
            "count": ratio(pre["count"], baseline["count"]),
            "new_addresses": ratio(
                pre["new_addresses"], baseline["new_addresses"]
            ),
        },
        "ratio_text": {
            "amount": ratio_text(pre["amount"], baseline["amount"]),
            "count": ratio_text(pre["count"], baseline["count"]),
            "new_addresses": ratio_text(
                pre["new_addresses"], baseline["new_addresses"]
            ),
        },
        "matches": matches,
        "evidence": {
            "maximum": maximum,
            "hub": {
                "address": pre["top_hub_address"],
                "label": metadata_label(state, pre["top_hub_address"]),
                "net": pre["top_hub_net"],
            },
            "fanout": {
                "address": pre["top_fanout_address"],
                "label": metadata_label(state, pre["top_fanout_address"]),
                "recipients": pre["top_fanout_recipients"],
            },
            "consolidation": {
                "address": pre["top_consolidation_address"],
                "label": metadata_label(
                    state, pre["top_consolidation_address"]
                ),
                "senders": pre["top_consolidation_senders"],
            },
            "fixed_amount": {
                "value": pre["fixed_value"],
                "count": pre["fixed_count"],
            },
            "safe": safe_evidence,
            "cex": cex_evidence,
            "migration": migration_evidence,
        },
    }


def short_address(value: str | None) -> str:
    if not value:
        return "—"
    return value if len(value) <= 16 else f"{value[:8]}…{value[-6:]}"


def fmt_amount(value: Decimal | str | None) -> str:
    if value is None:
        return "—"
    return f"{Decimal(value):,.2f}"


def evidence_text(item: dict[str, Any], match: str) -> str:
    pre = item["pre"]
    evidence = item["evidence"]
    if match == "volume_spike":
        return (
            f"W-1金额 {fmt_amount(pre['amount'])}，"
            f"为基线周 {item['ratio_text']['amount']}"
        )
    if match in {"whale_volume_combo", "large_without_volume"}:
        row = evidence["maximum"]
        return (
            f"{row['day']} 单笔 {fmt_amount(row['amount'])}，"
            f"{short_address(row['from'])}→{short_address(row['to'])}，"
            f"tx {short_address(row['tx_hash'])}"
        )
    if match == "hub_net_flow":
        hub = evidence["hub"]
        direction = "净流入" if Decimal(hub["net"]) > ZERO else "净流出"
        label = f"（{hub['label']}）" if hub.get("label") else ""
        return (
            f"{short_address(hub['address'])}{label} "
            f"{direction} {fmt_amount(abs(Decimal(hub['net'])))}"
        )
    if match == "fanout_new_addresses":
        fanout = evidence["fanout"]
        return (
            f"{short_address(fanout['address'])} 向 "
            f"{fanout['recipients']} 个地址扇出；"
            f"W-1首次可见地址 {pre['new_addresses']} 个"
        )
    if match == "consolidation":
        value = evidence["consolidation"]
        return (
            f"{short_address(value['address'])} 从 "
            f"{value['senders']} 个地址归集"
        )
    if match == "fixed_amount_batch":
        value = evidence["fixed_amount"]
        return f"近似固定金额 {value['value']} 重复 {value['count']} 次"
    if match == "safe_dispatch":
        row = evidence["safe"]
        return (
            f"{row['day']} {row['label']} 转账 "
            f"{fmt_amount(row['amount'])}，tx {short_address(row['tx_hash'])}"
        )
    if match == "cex_flow":
        row = evidence["cex"]
        return (
            f"{row['day']} 涉及 {row['label']}，金额 "
            f"{fmt_amount(row['amount'])}，tx {short_address(row['tx_hash'])}"
        )
    if match == "lock_migration":
        row = evidence["migration"]
        return (
            f"{row['day']} 涉及 {row['label']}，金额 "
            f"{fmt_amount(row['amount'])}，tx {short_address(row['tx_hash'])}"
        )
    if match == "cross_chain_sync":
        return "同一事件窗在两条以上链出现活动"
    return ""


def build_report(results: dict[str, Any]) -> str:
    direction_counts: dict[str, dict[str, int]] = defaultdict(
        lambda: {"up": 0, "down": 0}
    )
    total_events = 0
    total_matched = 0
    for token in results["tokens"]:
        total_events += len(token["events"])
        total_matched += token["matched_event_count"]
        for item in token["events"]:
            direction = (
                "up" if item["event"]["start_return_pct"] > 0 else "down"
            )
            for match in item["matches"]:
                direction_counts[match][direction] += 1
    lines = [
        "# 样本外新币：原文异常信号模式匹配",
        "",
        "- 样本外项目：SOON、ESPORTS、KOMA；已排除原文全部 16 个样本币。",
        "- 事件：币安日收盘涨跌绝对值达到 20%的异动段；检查异动起点前 7 天。",
        "- 链上来源：Bubblemaps Top Holders、Subgraph、当前 Cluster 普通成员历史 transfers。",
        "- 本报告直接匹配原文信号模板，不再使用“任意两个指标即复合信号”的旧结论。",
        "",
        "## 六个链上因子",
        "",
        "| 编号 | 因子 | 连续因子值 | 当前触发条件 |",
        "|---|---|---|---|",
        "| F1 | 转账金额放大因子 | W-1总金额 ÷ 基线四周周均金额 | ≥3 |",
        "| F2 | 转账笔数放大因子 | W-1笔数 ÷ 基线四周周均笔数 | ≥3 |",
        "| F3 | 活跃地址扩张因子 | W-1活跃地址 ÷ 基线四周周均活跃地址 | ≥2，且不少于10个地址 |",
        "| F4 | 新地址扩张因子 | W-1首次可见地址 ÷ 基线四周周均首次可见地址 | ≥2，且不少于5个新地址 |",
        "| F5 | 巨额转账因子 | W-1最大单笔 ÷ 基线周最大单笔中位数 | ≥3，且单笔占Cluster余额≥0.5% |",
        "| F6 | Cluster净流冲击因子 | W-1绝对净流 ÷ 基线周绝对净流中位数 | ≥3，且绝对净流占Cluster余额≥1% |",
        "",
        "F1–F6 是连续数值；达到阈值称为“因子触发”。原文模式位于因子之上，由因子触发加转账拓扑、地址标签或跨链关系组成。完整定义见 `factor-definitions.md`。",
        "",
        "## 主要结果",
        "",
        f"- {total_events} 段样本外日线异动中，{total_matched} 段在前 7 天至少匹配一种原文模式。",
        "- 同一种模式既可能出现在上涨前，也可能出现在下跌前；这里回答的是“结构是否复现”，不是方向预测。",
        "",
        "## 总览",
        "",
        "| Token | 异动段 | 至少匹配一种原文模式 | 反复出现的模式 |",
        "|---|---:|---:|---|",
    ]
    for token in results["tokens"]:
        recurring = "、".join(
            PATTERN_LABELS[name] for name in token["recurring_patterns"]
        ) or "无"
        lines.append(
            f"| {token['symbol']} | {len(token['events'])} | "
            f"{token['matched_event_count']} | {recurring} |"
        )
    lines.extend(
        [
            "",
            "## 模式与随后方向",
            "",
            "| 原文模式 | 上涨段前出现 | 下跌段前出现 | 合计 |",
            "|---|---:|---:|---:|",
        ]
    )
    for name in PATTERN_LABELS:
        counts = direction_counts.get(name, {"up": 0, "down": 0})
        lines.append(
            f"| {PATTERN_LABELS[name]} | {counts['up']} | "
            f"{counts['down']} | {counts['up'] + counts['down']} |"
        )
    lines.extend(["", "## 逐事件模式", ""])
    for token in results["tokens"]:
        lines.extend([f"### {token['symbol']}", ""])
        for item in token["events"]:
            event = item["event"]
            listing_age = (
                parse_day(event["start_date"]) - parse_day(token["bar_start"])
            ).days
            lines.append(
                f"#### {event['start_date']}：起点日 "
                f"{event['start_return_pct']:+.2f}%，段内最大单日 "
                f"{event['max_date']} {event['max_return_pct']:+.2f}%"
            )
            lines.append("")
            if listing_age <= 14:
                lines.append(
                    "- 上市后不足 14 天：链上活动可能主要反映 TGE、初始分发或交易所准备。"
                )
            if not item["matches"]:
                lines.append("- 未匹配到上述原文模式。")
            else:
                for match in item["matches"]:
                    lines.append(
                        f"- **{PATTERN_LABELS[match]}**："
                        f"{evidence_text(item, match)}。"
                    )
            lines.append("")
    lines.extend(
        [
            "## 模板口径",
            "",
            "- SYN/TAC型：W-1 gross transfer amount ≥ 此前四周周均 3x。",
            "- BANK型：满足总量放大，同时最大单笔 ≥ 基线周最大单笔中位数 3x，且 ≥ 当前 Cluster 合计余额 0.5%。",
            "- TAC枢纽型：单地址绝对净流 ≥ 基线 3x，且 ≥ 当前 Cluster 合计余额 1%。",
            "- BIRB型：单地址至少向 10 个地址扇出并达到基线 3x，同时首次可见地址 ≥ 基线 2x；归集单独检查。",
            "- BEAT型：同一近似金额至少重复 5 次并达到基线 3x。",
            "- PLAY/TAIKO型：Bubblemaps 标注的 Safe/多签地址发生至少两笔转账，或单笔 ≥ Cluster 余额 0.5%。",
            "- VELVET型：带 staking/vesting/lock/bridge/migration 标签的地址发生 ≥ Cluster 余额 0.5%的转账。",
            "- DEXE/ALLO/TAIKO弱型：出现上述巨额单笔，但 W-1 总量未达到 3x。",
            "- 跨链型需要同一项目存在多链目标；本次三个项目均只有 BSC 目标，因此不能匹配跨链同步。",
            "",
            "## 限制",
            "",
            "- 地址标签只覆盖 Bubblemaps Top Holders 返回的已标注地址；未标注外部地址不能被可靠识别为 Safe、交易所或锁仓合约。",
            "- 当前 Cluster 成员历史回看存在幸存者偏差；模式相似只表示结构相似，不代表同一主体或因果关系。",
            "",
        ]
    )
    return "\n".join(lines)


def main() -> None:
    event_results = json.loads(EVENT_RESULTS.read_text(encoding="utf-8"))
    tokens = []
    for token in event_results["tokens"]:
        symbol = token["symbol"]
        state = load_symbol(symbol)
        events = [classify_event(state, event) for event in token["episodes"]]
        pattern_counts = Counter(
            match for item in events for match in item["matches"]
        )
        tokens.append(
            {
                "symbol": symbol,
                "bar_start": token["bar_start"],
                "event_count": len(events),
                "matched_event_count": sum(bool(item["matches"]) for item in events),
                "recurring_patterns": sorted(
                    name for name, count in pattern_counts.items() if count >= 2
                ),
                "pattern_counts": dict(pattern_counts),
                "events": events,
            }
        )
    results = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source_snapshot": str(SNAPSHOT),
        "tokens": tokens,
    }
    ready = json_ready(results)
    OUTPUT_JSON.write_text(
        json.dumps(ready, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    OUTPUT_MD.write_text(build_report(ready), encoding="utf-8")
    print(f"wrote {OUTPUT_JSON}")
    print(f"wrote {OUTPUT_MD}")


if __name__ == "__main__":
    main()
