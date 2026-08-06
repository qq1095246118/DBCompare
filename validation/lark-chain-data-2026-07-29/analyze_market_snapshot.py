"""Recompute event-window metrics from a DBCompare Bubblemaps market snapshot."""

from __future__ import annotations

import argparse
import json
import statistics
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterable


ZERO = Decimal(0)


def normalized_address(value: str) -> str:
    return value.lower() if value.lower().startswith("0x") else value


def generation_dir(root: Path) -> Path:
    if (root / "manifest.json").is_file():
        return root
    candidates = sorted(path.parent for path in root.glob("*/manifest.json"))
    if not candidates:
        raise FileNotFoundError(f"no published generation under {root}")
    return candidates[-1]


def parse_day(value: str) -> date:
    return date.fromisoformat(value)


def decimal_text(value: Decimal) -> str:
    return format(value, "f")


def ratio_text(numerator: Decimal, denominator: Decimal) -> str | None:
    if denominator == ZERO:
        return None
    return decimal_text(numerator / denominator)


def json_ready(value: Any) -> Any:
    if isinstance(value, Decimal):
        return decimal_text(value)
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, dict):
        return {key: json_ready(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_ready(item) for item in value]
    return value


def record_identity(chain: str, token_address: str, row: dict[str, Any]) -> tuple[Any, ...]:
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


def load_snapshot(
    snapshot: Path,
    configuration: dict[str, Any],
) -> tuple[dict[str, list[dict[str, Any]]], dict[str, Any]]:
    manifest = json.loads((snapshot / "manifest.json").read_text(encoding="utf-8"))
    target_to_symbol: dict[tuple[str, str], str] = {}
    expected_targets = 0
    for symbol, spec in configuration["symbols"].items():
        for chain, addresses in spec["targets"].items():
            for address in addresses:
                expected_targets += 1
                target_to_symbol[(chain, normalized_address(address))] = symbol

    records_by_symbol: dict[str, list[dict[str, Any]]] = defaultdict(list)
    seen_by_symbol: dict[str, set[tuple[Any, ...]]] = defaultdict(set)
    coverage: dict[str, Any] = {
        "expected_target_count": expected_targets,
        "captured_target_count": 0,
        "unmatched_captured_targets": [],
        "missing_targets": sorted(
            f"{chain}:{address}"
            for chain, address in target_to_symbol
        ),
        "skipped_tokens": manifest.get("skipped_tokens", []),
        "manifest_status": manifest.get("status"),
        "manifest_captured_at": manifest.get("captured_at"),
        "snapshot": str(snapshot),
        "tokens": {},
    }
    error_path = snapshot / "error.json"
    if error_path.is_file():
        error_document = json.loads(error_path.read_text(encoding="utf-8"))
        errors = error_document.get("errors", [])
        coverage["error_count"] = error_document.get("error_count", len(errors))
        coverage["transfer_error_count"] = sum(
            error.get("stage") == "transfers"
            for error in errors
            if isinstance(error, dict)
        )
        coverage["final_error_count"] = sum(
            error.get("stage") == "final"
            for error in errors
            if isinstance(error, dict)
        )
    else:
        coverage["error_count"] = 0
        coverage["transfer_error_count"] = 0
        coverage["final_error_count"] = 0
    missing = set(coverage["missing_targets"])

    for token_entry in manifest.get("tokens", []):
        chain = token_entry["canonical_chain"]
        token_address = token_entry["canonical_token_address"]
        requested_chain = token_entry.get("requested_chain", chain)
        requested_address = token_entry.get("requested_token_address", token_address)
        requested_identity = (
            requested_chain,
            normalized_address(requested_address),
        )
        canonical_identity = (chain, normalized_address(token_address))
        symbol = target_to_symbol.get(requested_identity)
        if symbol is None:
            symbol = target_to_symbol.get(canonical_identity)
        if symbol is None:
            coverage["unmatched_captured_targets"].append(f"{chain}:{token_address}")
            continue
        matched_identity = (
            requested_identity
            if requested_identity in target_to_symbol
            else canonical_identity
        )
        target_key = f"{matched_identity[0]}:{matched_identity[1]}"
        missing.discard(target_key)
        coverage["captured_target_count"] += 1

        token_path = snapshot / token_entry["token_file"]
        token = json.loads(token_path.read_text(encoding="utf-8"))
        top_cluster_share = None
        if token.get("clusters"):
            top_cluster_share = token["clusters"][0].get("share_percent")
        coverage["tokens"][target_key] = {
            "symbol": symbol,
            "ordinary_member_count": token_entry.get("ordinary_member_count"),
            "supernode_count": token_entry.get("supernode_count"),
            "unique_transfer_count_reported": token_entry.get("unique_transfer_count"),
            "top_cluster_share_percent": top_cluster_share,
        }

        for cluster in token.get("clusters", []):
            for member in cluster.get("members", []):
                transfer_file = member.get("transfer_file")
                if not transfer_file:
                    continue
                transfer_document = json.loads(
                    (snapshot / transfer_file).read_text(encoding="utf-8")
                )
                for row in transfer_document.get("transfers", []):
                    identity = record_identity(chain, token_address, row)
                    if identity in seen_by_symbol[symbol]:
                        continue
                    seen_by_symbol[symbol].add(identity)
                    data = row["data"]
                    event_time = datetime.fromtimestamp(
                        int(data["date"]) / 1000, tz=timezone.utc
                    )
                    records_by_symbol[symbol].append(
                        {
                            "chain": chain,
                            "token_address": token_address,
                            "tx_hash": data["tx_hash"],
                            "from_address": row["from_address"],
                            "to_address": row["to_address"],
                            "amount": Decimal(str(data["value"])),
                            "timestamp_ms": int(data["date"]),
                            "event_time": event_time,
                            "day": event_time.date(),
                        }
                    )

    coverage["missing_targets"] = sorted(missing)
    for records in records_by_symbol.values():
        records.sort(key=lambda row: (row["timestamp_ms"], row["tx_hash"]))
    return records_by_symbol, coverage


def records_in_range(
    records: Iterable[dict[str, Any]],
    start: date,
    end: date,
) -> list[dict[str, Any]]:
    return [row for row in records if start <= row["day"] <= end]


def summarize(
    records: list[dict[str, Any]],
    first_seen: dict[str, date],
) -> dict[str, Any]:
    addresses = {
        normalized_address(address)
        for row in records
        for address in (row["from_address"], row["to_address"])
    }
    amounts = [row["amount"] for row in records]
    maximum = max(records, key=lambda row: row["amount"]) if records else None
    return {
        "count": len(records),
        "amount": sum(amounts, ZERO),
        "active_addresses": len(addresses),
        "first_visible_addresses": sum(
            1
            for address in addresses
            if any(
                first_seen.get(address) == row["day"]
                for row in records
                if address in {
                    normalized_address(row["from_address"]),
                    normalized_address(row["to_address"]),
                }
            )
        ),
        "median_amount": Decimal(str(statistics.median(amounts))) if amounts else ZERO,
        "maximum": (
            {
                "amount": maximum["amount"],
                "chain": maximum["chain"],
                "day": maximum["day"],
                "event_time": maximum["event_time"].isoformat(),
                "tx_hash": maximum["tx_hash"],
                "from_address": maximum["from_address"],
                "to_address": maximum["to_address"],
            }
            if maximum
            else None
        ),
        "by_chain": {
            chain: {
                "count": len(chain_records),
                "amount": sum((row["amount"] for row in chain_records), ZERO),
                "active_addresses": len(
                    {
                        normalized_address(address)
                        for row in chain_records
                        for address in (row["from_address"], row["to_address"])
                    }
                ),
            }
            for chain in sorted({row["chain"] for row in records})
            if (chain_records := [row for row in records if row["chain"] == chain])
        },
    }


def event_result(
    symbol: str,
    records: list[dict[str, Any]],
    event: dict[str, Any],
) -> dict[str, Any]:
    event_start = parse_day(event["date"])
    event_end = parse_day(event.get("end_date", event["date"]))
    pre_start = event_start - timedelta(days=7)
    pre_end = event_start - timedelta(days=1)
    baseline_start = event_start - timedelta(days=35)
    baseline_end = event_start - timedelta(days=8)

    first_seen: dict[str, date] = {}
    for row in records:
        for address in (row["from_address"], row["to_address"]):
            key = normalized_address(address)
            first_seen[key] = min(first_seen.get(key, row["day"]), row["day"])

    baseline = summarize(
        records_in_range(records, baseline_start, baseline_end), first_seen
    )
    pre = summarize(records_in_range(records, pre_start, pre_end), first_seen)
    event_window = summarize(
        records_in_range(records, event_start, event_end), first_seen
    )
    after = summarize(
        records_in_range(
            records,
            event_end + timedelta(days=1),
            event_end + timedelta(days=7),
        ),
        first_seen,
    )
    baseline_week_amount = baseline["amount"] / Decimal(4)
    baseline_week_count = Decimal(baseline["count"]) / Decimal(4)
    event_days = Decimal((event_end - event_start).days + 1)
    pre_daily_amount = pre["amount"] / Decimal(7)
    pre_daily_count = Decimal(pre["count"]) / Decimal(7)

    return {
        "symbol": symbol,
        "event_date": event_start,
        "event_end_date": event_end,
        "rating_in_document": event.get("rating"),
        "note": event.get("note"),
        "available_history": {
            "start": records[0]["day"] if records else None,
            "end": records[-1]["day"] if records else None,
            "record_count": len(records),
        },
        "windows": {
            "baseline": {
                "start": baseline_start,
                "end": baseline_end,
                **baseline,
            },
            "pre": {"start": pre_start, "end": pre_end, **pre},
            "event": {
                "start": event_start,
                "end": event_end,
                **event_window,
            },
            "after": {
                "start": event_end + timedelta(days=1),
                "end": event_end + timedelta(days=7),
                **after,
            },
        },
        "ratios": {
            "pre_amount_vs_baseline_week": ratio_text(
                pre["amount"], baseline_week_amount
            ),
            "pre_count_vs_baseline_week": ratio_text(
                Decimal(pre["count"]), baseline_week_count
            ),
            "event_daily_amount_vs_pre_daily": ratio_text(
                event_window["amount"] / event_days, pre_daily_amount
            ),
            "event_daily_count_vs_pre_daily": ratio_text(
                Decimal(event_window["count"]) / event_days, pre_daily_count
            ),
        },
    }


def format_amount(value: str | Decimal) -> str:
    number = Decimal(value)
    return f"{number:,.4f}".rstrip("0").rstrip(".")


def report_markdown(
    results: dict[str, Any],
    configuration: dict[str, Any],
) -> str:
    coverage = results["coverage"]
    lines = [
        "# Lark 链上文档复算报告",
        "",
        f"- 市场快照：`{coverage['snapshot']}`",
        f"- manifest：`{coverage['manifest_status']}`，采集时间 `{coverage['manifest_captured_at']}`",
        f"- 目标覆盖：{coverage['captured_target_count']} / {coverage['expected_target_count']}",
        f"- 未采集目标：{len(coverage['missing_targets'])}",
        f"- skipped targets：{len(coverage['skipped_tokens'])}",
        f"- 成员 transfer 错误：{coverage['transfer_error_count']}",
        f"- 最终组装错误：{coverage['final_error_count']}",
        "",
        "## 统一口径",
        "",
        "转账按 chain、token、交易哈希、from、to、数量和时间去重。B28 为 D-35..D-8，W-1 为 D-7..D-1；金额是 gross transfer amount，不等同净流入、成交量或新增资金。首次可见地址仅相对当前 cluster 普通成员的可见历史。",
        "",
        "## 事件窗口",
        "",
        "| Token | 事件 | 文档评级 | B28金额 | W-1金额 | W-1/基线周 | 事件金额 | 事件笔数 | 事件活跃地址 |",
        "|---|---:|---|---:|---:|---:|---:|---:|---:|",
    ]
    for item in results["events"]:
        windows = item["windows"]
        ratio = item["ratios"]["pre_amount_vs_baseline_week"]
        lines.append(
            "| {symbol} | {event} | {rating} | {baseline} | {pre} | {ratio} | "
            "{event_amount} | {event_count} | {active} |".format(
                symbol=item["symbol"],
                event=item["event_date"],
                rating=item.get("rating_in_document") or "",
                baseline=format_amount(windows["baseline"]["amount"]),
                pre=format_amount(windows["pre"]["amount"]),
                ratio=(f"{Decimal(ratio):.3f}x" if ratio is not None else "N/A"),
                event_amount=format_amount(windows["event"]["amount"]),
                event_count=windows["event"]["count"],
                active=windows["event"]["active_addresses"],
            )
        )

    lines.extend(["", "## 无精确事件日期", ""])
    for symbol, spec in configuration["symbols"].items():
        if not spec.get("events"):
            lines.append(f"- {symbol}：{spec.get('note', '未配置事件日期')}")

    if coverage["missing_targets"]:
        lines.extend(["", "## 覆盖缺口", ""])
        lines.extend(f"- `{target}`" for target in coverage["missing_targets"])
    lines.append("")
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--market-root", required=True, type=Path)
    parser.add_argument(
        "--config",
        type=Path,
        default=Path(__file__).with_name("events.json"),
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path(__file__).parent,
    )
    args = parser.parse_args()

    configuration = json.loads(args.config.read_text(encoding="utf-8"))
    snapshot = generation_dir(args.market_root)
    records_by_symbol, coverage = load_snapshot(snapshot, configuration)
    events = [
        event_result(symbol, records_by_symbol.get(symbol, []), event)
        for symbol, spec in configuration["symbols"].items()
        for event in spec.get("events", [])
    ]
    results = {"coverage": coverage, "events": events}

    args.output_dir.mkdir(parents=True, exist_ok=True)
    (args.output_dir / "results.json").write_text(
        json.dumps(json_ready(results), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    (args.output_dir / "report.md").write_text(
        report_markdown(json_ready(results), configuration),
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()
