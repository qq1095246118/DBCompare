#!/usr/bin/env python3
"""Build a standalone interactive daily K-line and F1-F7 review dashboard."""

from __future__ import annotations

import argparse
import csv
import importlib.util
import json
import math
import re
import statistics
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any


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
OUT_SAMPLE_SNAPSHOT = (
    PROJECT_ROOT
    / "analysis/binance-bubblemaps-out-of-sample-2026-07-30/"
    "bubblemaps-snapshot"
)
OUT_SAMPLE_CONFIG = (
    PROJECT_ROOT
    / "analysis/binance-bubblemaps-out-of-sample-2026-07-30/"
    "out_of_sample_config.json"
)
ADDITIONAL_SAMPLE_ROOT = (
    PROJECT_ROOT
    / "analysis/binance-bubblemaps-additional-out-of-sample-2026-07-30"
)
ADDITIONAL_SAMPLE_SNAPSHOT = ADDITIONAL_SAMPLE_ROOT / "bubblemaps-snapshot"
ADDITIONAL_SAMPLE_CONFIG = (
    ADDITIONAL_SAMPLE_ROOT / "additional_out_of_sample_config.json"
)
OUTPUT_HTML = ROOT / "factor-kline-dashboard.html"
CEX_PATH_EVENTS = ROOT / "cex-multihop-boundary-events.csv"
IN_SAMPLE_SYMBOLS = ("SIREN", "RAVE", "BIRB", "VELVET", "DEXE")
OUT_SAMPLE_SYMBOLS = ("SOON", "ESPORTS", "KOMA")
ADDITIONAL_SAMPLE_SYMBOLS = ("CYS", "BULLA", "EVAA", "GWEI", "CLO")
EVENT_THRESHOLD_PCT = 20.0
EPISODE_GAP_DAYS = 7
DISPLAY_START = date(2026, 1, 1)


def load_engine():
    spec = importlib.util.spec_from_file_location("daily_factor_engine", ENGINE_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError("daily factor engine unavailable")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def load_states(engine, snapshot: Path, config_path: Path, symbols: tuple[str, ...]):
    config = json.loads(config_path.read_text(encoding="utf-8"))
    targets = {
        symbol: config["symbols"][symbol]["targets"] for symbol in symbols
    }
    states, metadata = engine.load_bubblemaps(snapshot, targets)
    if metadata["missing_targets"]:
        raise ValueError(f"missing Bubblemaps targets: {metadata['missing_targets']}")
    enrich_state_metadata(engine, snapshot, targets, states)
    return states, metadata


def enrich_state_metadata(
    engine,
    snapshot: Path,
    targets: dict[str, dict[str, list[str]]],
    states: dict[str, dict[str, Any]],
) -> None:
    """Attach cluster membership and holder labels needed by the F5 breakdown."""
    target_to_symbol = {}
    for symbol, symbol_targets in targets.items():
        for chain, addresses in symbol_targets.items():
            for address in addresses:
                target_to_symbol[(chain, engine.normalized_address(address))] = symbol

    manifest = json.loads(
        (snapshot / "manifest.json").read_text(encoding="utf-8")
    )
    for state in states.values():
        state["cluster_by_member"] = {}
        state["address_metadata"] = {}

    for token_entry in manifest.get("tokens", []):
        chain = token_entry["canonical_chain"]
        raw_token_address = token_entry["canonical_token_address"]
        token_address = engine.normalized_address(
            raw_token_address
        )
        requested_key = (
            token_entry.get("requested_chain", chain),
            engine.normalized_address(
                token_entry.get("requested_token_address", token_address)
            ),
        )
        canonical_key = (chain, token_address)
        symbol = target_to_symbol.get(requested_key) or target_to_symbol.get(
            canonical_key
        )
        if symbol is None:
            continue

        token_relative = Path(token_entry["token_file"])
        token = json.loads(
            (snapshot / token_relative).read_text(encoding="utf-8")
        )
        holder_relative = Path(
            "clean", *token_relative.parts[1:-1], "holders.json"
        )
        holder_path = snapshot / holder_relative
        holder_metadata = {}
        if holder_path.is_file():
            for holder in json.loads(holder_path.read_text(encoding="utf-8")):
                holder_metadata[
                    engine.normalized_address(holder["address"])
                ] = holder.get("address_details", {})

        state = states[symbol]
        for cluster_position, cluster in enumerate(token.get("clusters", []), 1):
            cluster_rank = int(
                cluster.get("cluster_rank")
                or cluster.get("cluster_index")
                or cluster_position
            )
            cluster_id = f"{chain}:{token_address}:{cluster_rank}"
            for member in cluster.get("members", []):
                address = engine.normalized_address(member["address"])
                metadata = member.get("metadata") or holder_metadata.get(
                    address, {}
                )
                for token_key in {raw_token_address, token_address}:
                    key = (chain, token_key, address)
                    state["cluster_by_member"][key] = cluster_id
                    state["address_metadata"][key] = metadata


def attach_confirmed_cex_path_events(
    states: dict[str, dict[str, Any]],
    path: Path = CEX_PATH_EVENTS,
) -> None:
    """Attach reviewed multi-hop CEX boundary events to their token states."""
    for state in states.values():
        state["cex_path_events"] = []
    if not path.is_file():
        return
    with path.open(encoding="utf-8", newline="") as handle:
        for row_number, row in enumerate(csv.DictReader(handle), 2):
            if str(row.get("status") or "").strip().lower() != "confirmed":
                continue
            symbol = str(row.get("symbol") or "").strip().upper()
            if symbol not in states:
                continue
            direction = str(row.get("direction") or "").strip()
            if direction not in {"流入CEX", "流出CEX"}:
                raise ValueError(
                    f"{path.name}:{row_number}: invalid CEX direction"
                )
            boundary_event_id = str(
                row.get("boundary_event_id") or ""
            ).strip()
            boundary_tx_hash = str(
                row.get("boundary_tx_hash") or ""
            ).strip()
            if not boundary_event_id or not boundary_tx_hash:
                raise ValueError(
                    f"{path.name}:{row_number}: missing boundary event identity"
                )
            amount = float(row.get("amount") or 0)
            if amount <= 0:
                raise ValueError(
                    f"{path.name}:{row_number}: amount must be positive"
                )
            event_day = date.fromisoformat(
                str(row.get("boundary_date") or "").strip()
            )
            states[symbol]["cex_path_events"].append(
                {
                    "day": event_day,
                    "direction": direction,
                    "amount": amount,
                    "boundary_event_id": boundary_event_id,
                    "boundary_tx_hash": boundary_tx_hash,
                    "cex_label": str(
                        row.get("cex_label") or row.get("cex_address") or ""
                    ).strip(),
                    "hops": int(row.get("hops") or 0),
                    "source": str(row.get("source") or "").strip(),
                }
            )


def episodes_with_bounds(bars: list[dict[str, Any]]) -> list[dict[str, Any]]:
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
    for index, group in enumerate(groups, 1):
        maximum = max(group, key=lambda item: abs(item["return_pct"]))
        episodes.append(
            {
                "id": index,
                "start": group[0]["date"].isoformat(),
                "end": group[-1]["date"].isoformat(),
                "max_date": maximum["date"].isoformat(),
                "max_return": round(maximum["return_pct"], 4),
                "start_return": round(group[0]["return_pct"], 4),
                "direction": "up" if group[0]["return_pct"] > 0 else "down",
                "shock_dates": [item["date"].isoformat() for item in group],
                "shock_returns": [
                    round(item["return_pct"], 4) for item in group
                ],
                "view_start": (
                    group[0]["date"] - timedelta(days=42)
                ).isoformat(),
                "view_end": (
                    group[-1]["date"] + timedelta(days=14)
                ).isoformat(),
                "pre_start": (
                    group[0]["date"] - timedelta(days=7)
                ).isoformat(),
                "pre_end": (
                    group[0]["date"] - timedelta(days=1)
                ).isoformat(),
            }
        )
    return episodes


def finite(value: float | None, digits: int = 4) -> float | None:
    if value is None or not math.isfinite(value):
        return None
    return round(value, digits)


def zero_launches(features: dict[str, Any]) -> list[bool]:
    pre = features["pre"]
    baseline = features["baseline_week_mean"]
    pairs = (
        (pre["amount"], baseline["amount"]),
        (pre["count"], baseline["count"]),
        (pre["active_addresses"], baseline["active_addresses"]),
        (pre["new_addresses"], baseline["new_addresses"]),
        (pre["max_transfer"], baseline["max_transfer"]),
        (
            abs(pre["net_external_flow"]),
            baseline["abs_net_external_flow"],
        ),
    )
    return [float(numerator) > 0 and float(denominator) == 0 for numerator, denominator in pairs]


def endpoint_key(record: dict[str, Any], field: str) -> tuple[str, str, str]:
    return (
        record["chain"],
        record["token_address"],
        record[field],
    )


def endpoint_type(metadata: dict[str, Any]) -> str:
    label = " ".join(
        str(metadata.get(field) or "")
        for field in ("label", "entity_id")
    ).lower()
    if metadata.get("is_cex"):
        return "CEX"
    if metadata.get("is_dex") or any(
        keyword in label for keyword in ("liquidity pool", " lp", "pool", "router")
    ):
        return "DEX/LP"
    if "bridge" in label:
        return "Bridge"
    if "staking" in label or "stake" in label:
        return "Staking"
    if "vesting" in label or "lock" in label:
        return "Vesting/Lock"
    if "safe" in label or "multisig" in label or "multi-sig" in label:
        return "多签"
    if metadata.get("is_contract"):
        return "其他合约"
    return "未知地址"


def transfer_context(
    state: dict[str, Any],
    record: dict[str, Any],
) -> dict[str, Any]:
    """Classify one transfer, giving CEX endpoints priority over clusters."""
    from_key = endpoint_key(record, "from_address")
    to_key = endpoint_key(record, "to_address")
    from_metadata = state["address_metadata"].get(from_key, {})
    to_metadata = state["address_metadata"].get(to_key, {})
    from_is_cex = endpoint_type(from_metadata) == "CEX"
    to_is_cex = endpoint_type(to_metadata) == "CEX"

    if from_is_cex or to_is_cex:
        structure = "CEX相关转账"
        if from_is_cex and to_is_cex:
            direction = "CEX间转移"
        elif to_is_cex:
            direction = "流入CEX"
        else:
            direction = "流出CEX"
        counterpart_keys = [from_key, to_key]
    else:
        from_cluster = state["cluster_by_member"].get(from_key)
        to_cluster = state["cluster_by_member"].get(to_key)
        if from_cluster and to_cluster:
            if from_cluster == to_cluster:
                structure = "同一Cluster内部"
                direction = "内部调仓"
            else:
                structure = "跨Cluster"
                direction = "跨Cluster转移"
            counterpart_keys = [from_key, to_key]
        elif from_cluster:
            structure = "Cluster与外部"
            direction = "Cluster流出"
            counterpart_keys = [to_key]
        elif to_cluster:
            structure = "Cluster与外部"
            direction = "外部流入Cluster"
            counterpart_keys = [from_key]
        else:
            structure = "成员范围未识别"
            direction = "未知"
            counterpart_keys = [from_key, to_key]

    return {
        "structure": structure,
        "direction": direction,
        "counterpart_keys": counterpart_keys,
    }


def cex_window_summary(
    state: dict[str, Any], start: date, end: date
) -> dict[str, Any]:
    """Aggregate unique direct and confirmed multi-hop CEX boundary events."""
    inflow = 0.0
    outflow = 0.0
    transfer_count = 0
    direct_transfer_count = 0
    multihop_transfer_count = 0
    labels: set[str] = set()
    seen_event_ids: set[str] = set()
    seen_transfer_keys: set[tuple[str, str, float]] = set()
    for record in state["records"]:
        if not start <= record["day"] <= end:
            continue
        context = transfer_context(state, record)
        direction = context["direction"]
        if direction not in {"流入CEX", "流出CEX", "CEX间转移"}:
            continue
        amount = float(record["amount"])
        transfer_key = (
            str(record["tx_hash"]).lower(),
            direction,
            round(amount, 8),
        )
        if transfer_key in seen_transfer_keys:
            continue
        seen_transfer_keys.add(transfer_key)
        seen_event_ids.add(
            "direct:"
            + ":".join(
                (
                    str(record["chain"]).lower(),
                    str(record["token_address"]).lower(),
                    str(record["tx_hash"]).lower(),
                    direction,
                    f"{amount:.8f}",
                )
            )
        )
        transfer_count += 1
        direct_transfer_count += 1
        if direction == "流入CEX":
            inflow += amount
        elif direction == "流出CEX":
            outflow += amount
        for key in context["counterpart_keys"]:
            metadata = state["address_metadata"].get(key, {})
            if endpoint_type(metadata) != "CEX":
                continue
            label = str(metadata.get("label") or metadata.get("entity_id") or "")
            if label:
                labels.add(label)

    for event in state.get("cex_path_events", []):
        if not start <= event["day"] <= end:
            continue
        event_id = str(event["boundary_event_id"]).lower()
        direction = str(event["direction"])
        amount = float(event["amount"])
        transfer_key = (
            str(event["boundary_tx_hash"]).lower(),
            direction,
            round(amount, 8),
        )
        if event_id in seen_event_ids or transfer_key in seen_transfer_keys:
            continue
        seen_event_ids.add(event_id)
        seen_transfer_keys.add(transfer_key)
        transfer_count += 1
        multihop_transfer_count += 1
        if direction == "流入CEX":
            inflow += amount
        elif direction == "流出CEX":
            outflow += amount
        label = str(event.get("cex_label") or "").strip()
        if label:
            labels.add(label)
    return {
        "inflow": inflow,
        "outflow": outflow,
        "net": inflow - outflow,
        "transfer_count": transfer_count,
        "direct_transfer_count": direct_transfer_count,
        "multihop_transfer_count": multihop_transfer_count,
        "labels": sorted(labels),
    }


def cex_net_factor(state: dict[str, Any], anchor: date) -> dict[str, Any]:
    """Build one signed CEX net-flow factor using data available before D."""
    current = cex_window_summary(
        state, anchor - timedelta(days=7), anchor - timedelta(days=1)
    )
    baseline_abs_nets = []
    for week_offset in range(1, 5):
        week_end = anchor - timedelta(days=1 + week_offset * 7)
        week_start = week_end - timedelta(days=6)
        baseline_abs_nets.append(
            abs(float(cex_window_summary(state, week_start, week_end)["net"]))
        )

    net = float(current["net"])
    absolute_net = abs(net)
    baseline = statistics.median(baseline_abs_nets)
    zero_launch = absolute_net > 0 and baseline == 0
    burst = None if zero_launch else absolute_net / baseline if baseline > 0 else 0.0
    cluster_amount = float(state["cluster_amount"])
    share_pct = net / cluster_amount * 100 if cluster_amount > 0 else 0.0
    absolute_share_pct = abs(share_pct)
    trigger = absolute_share_pct >= 0.1 and (
        zero_launch or (burst is not None and burst >= 3)
    )
    direction = "CEX净流入" if net > 0 else "CEX净流出" if net < 0 else "CEX净流为零"
    return {
        "inflow_7d": finite(float(current["inflow"]), 4),
        "outflow_7d": finite(float(current["outflow"]), 4),
        "net_7d": finite(net, 4),
        "direction": direction,
        "baseline_abs_net": finite(baseline, 4),
        "burst": finite(burst),
        "zero_launch": zero_launch,
        "share_pct": finite(share_pct, 6),
        "abs_share_pct": finite(absolute_share_pct, 6),
        "trigger": trigger,
        "transfer_count_7d": int(current["transfer_count"]),
        "direct_transfer_count_7d": int(current["direct_transfer_count"]),
        "multihop_transfer_count_7d": int(
            current["multihop_transfer_count"]
        ),
        "labels_7d": current["labels"],
        "coverage": "direct_and_confirmed_multihop_boundary_events",
    }


def longest_consecutive_days(days: list[date]) -> int:
    if not days:
        return 0
    ordered = sorted(set(days))
    longest = current = 1
    for previous, current_day in zip(ordered, ordered[1:]):
        if (current_day - previous).days == 1:
            current += 1
            longest = max(longest, current)
        else:
            current = 1
    return longest


def market_position(
    bars: list[dict[str, Any]], index: int
) -> dict[str, Any]:
    history = bars[max(0, index - 20) : index]
    if not history:
        return {
            "state": "历史不足",
            "return_7d_pct": None,
            "distance_20d_high_pct": None,
            "volume_percentile_20d": None,
            "previous_day_breakout": False,
        }

    last = history[-1]
    return_7d = None
    if len(history) >= 8 and float(history[-8]["close"]) > 0:
        return_7d = (
            float(last["close"]) / float(history[-8]["close"]) - 1
        ) * 100
    high_20d = max(float(item["high"]) for item in history)
    distance_high = (
        (float(last["close"]) / high_20d - 1) * 100 if high_20d > 0 else None
    )
    volumes = [float(item["volume"]) for item in history]
    last_volume = float(last["volume"])
    volume_percentile = (
        sum(value <= last_volume for value in volumes) / len(volumes) * 100
    )
    previous_day_breakout = False
    if len(history) >= 8:
        earlier = history[:-1]
        previous_high = max(float(item["high"]) for item in earlier)
        median_volume = statistics.median(
            float(item["volume"]) for item in earlier
        )
        previous_day_breakout = (
            float(last["close"]) > previous_high
            and median_volume > 0
            and last_volume >= median_volume * 2
        )

    if len(history) < 8:
        state = "历史不足"
    elif previous_day_breakout:
        state = "前一日放量突破"
    elif volume_percentile <= 20:
        state = "极低成交量"
    elif return_7d is not None and return_7d >= 20 and (
        distance_high is not None and distance_high >= -10
    ):
        state = "上涨后高位"
    elif return_7d is not None and return_7d <= -15:
        state = "下跌中"
    else:
        state = "盘整/普通"
    return {
        "state": state,
        "return_7d_pct": finite(return_7d),
        "distance_20d_high_pct": finite(distance_high),
        "volume_percentile_20d": finite(volume_percentile),
        "previous_day_breakout": previous_day_breakout,
    }


def f5_breakdown(
    state: dict[str, Any],
    features: dict[str, Any],
    anchor: date,
    bars: list[dict[str, Any]],
    bar_index: int,
    cex: dict[str, Any],
) -> dict[str, Any]:
    start = anchor - timedelta(days=7)
    end = anchor - timedelta(days=1)
    rows = [
        record
        for record in state["records"]
        if start <= record["day"] <= end
    ]
    maximum = (
        max(
            rows,
            key=lambda record: (
                float(record["amount"]),
                record["timestamp_ms"],
                record["tx_hash"],
            ),
        )
        if rows
        else None
    )
    cluster_amount = float(state["cluster_amount"])
    baseline_max = float(features["baseline_week_mean"]["max_transfer"])
    maximum_amount = float(features["pre"]["max_transfer"])
    f5a_alert = (
        maximum_amount > 0
        if baseline_max == 0
        else maximum_amount / baseline_max >= 3
    )
    f5b_alert = (
        cluster_amount > 0 and maximum_amount / cluster_amount >= 0.005
    )
    whale_event = f5a_alert and f5b_alert

    large_rows = []
    for record in rows:
        amount = float(record["amount"])
        relative_large = (
            amount > 0
            if baseline_max == 0
            else amount >= baseline_max * 3
        )
        cluster_large = (
            cluster_amount > 0 and amount / cluster_amount >= 0.005
        )
        if relative_large and cluster_large:
            large_rows.append(record)
    large_days = sorted({record["day"] for record in large_rows})
    longest_streak = longest_consecutive_days(large_days)
    if not large_days:
        persistence = "无巨额转账"
    elif len(large_days) == 1:
        persistence = "单日孤立"
    elif longest_streak >= 2:
        persistence = "连续多日"
    else:
        persistence = "间歇多日"

    structure = "无可见转账"
    direction = "无"
    counterparty_types: list[str] = []
    counterparty_labels: list[str] = []

    maximum_payload = None
    if maximum is not None:
        context = transfer_context(state, maximum)
        structure = context["structure"]
        direction = context["direction"]
        counterpart_keys = context["counterpart_keys"]

        for key in counterpart_keys:
            metadata = state["address_metadata"].get(key, {})
            kind = endpoint_type(metadata)
            if kind not in counterparty_types:
                counterparty_types.append(kind)
            label = str(metadata.get("label") or metadata.get("entity_id") or "")
            if label and label not in counterparty_labels:
                counterparty_labels.append(label)
        maximum_payload = {
            "amount": finite(float(maximum["amount"]), 4),
            "chain": maximum["chain"],
            "tx_hash": maximum["tx_hash"],
            "from": maximum["from_address"],
            "to": maximum["to_address"],
            "event_at": datetime.fromtimestamp(
                maximum["timestamp_ms"] / 1000, tz=timezone.utc
            ).isoformat(),
        }

    market = market_position(bars, bar_index)
    special_counterparty = any(
        kind
        in {
            "CEX",
            "DEX/LP",
            "Bridge",
            "Staking",
            "Vesting/Lock",
            "多签",
        }
        for kind in counterparty_types
    )
    return {
        "maximum": maximum_payload,
        "structure": structure,
        "direction": direction,
        "counterparty_types": counterparty_types,
        "counterparty_labels": counterparty_labels,
        "cex_transfer_count_7d": cex["transfer_count_7d"],
        "cex_direct_transfer_count_7d": cex[
            "direct_transfer_count_7d"
        ],
        "cex_multihop_transfer_count_7d": cex[
            "multihop_transfer_count_7d"
        ],
        "cex_inflow_7d": cex["inflow_7d"],
        "cex_outflow_7d": cex["outflow_7d"],
        "cex_net_flow_7d": cex["net_7d"],
        "cex_labels_7d": cex["labels_7d"],
        "large_transfer_count_7d": len(large_rows),
        "large_day_count_7d": len(large_days),
        "longest_streak_days": longest_streak,
        "persistence": persistence,
        "future_path": {
            "status": "事后标签；未纳入实时因子",
            "label": None,
            "reason": "需要从最大单笔发生后继续追踪多跳转账，当前数据不完整",
        },
        "market": market,
        "alerts": {
            "f5a": f5a_alert,
            "f5b": f5b_alert,
            "f5c": whale_event
            and structure
            in {"CEX相关转账", "跨Cluster", "Cluster与外部"},
            "f5d": whale_event
            and direction
            in {
                "流入CEX",
                "流出CEX",
                "CEX间转移",
                "跨Cluster转移",
                "外部流入Cluster",
                "Cluster流出",
            },
            "f5e": whale_event and special_counterparty,
            "f5f": False,
            "f5g": whale_event,
            "f5h": whale_event
            and market["state"]
            not in {"盘整/普通", "历史不足"},
        },
    }


def factor_row(
    engine,
    state: dict[str, Any],
    bar: dict[str, Any],
    bars: list[dict[str, Any]],
    bar_index: int,
) -> dict[str, Any]:
    features = engine.window_features(state, bar["date"])
    ratios = features["ratios"]
    pre = features["pre"]
    cluster_amount = float(state["cluster_amount"])
    max_share = (
        float(pre["max_transfer"]) / cluster_amount if cluster_amount > 0 else 0
    )
    net = float(pre["net_external_flow"])
    net_share = abs(net) / cluster_amount if cluster_amount > 0 else 0
    cex = cex_net_factor(state, bar["date"])
    values = [
        ratios["amount"],
        ratios["count"],
        ratios["active_addresses"],
        ratios["new_addresses"],
        ratios["max_transfer"],
        ratios["abs_net_flow"],
        cex["burst"],
    ]
    signal_index = {
        "gross_amount_burst": 0,
        "transfer_count_burst": 1,
        "active_address_burst": 2,
        "new_address_burst": 3,
        "whale_transfer": 4,
        "net_flow_shock": 5,
    }
    triggered = sorted(signal_index[name] for name in features["signals"])
    if cex["trigger"]:
        triggered.append(6)
    triggered.sort()

    buy_observation = []
    sell_risk = []
    f1 = values[0] or 0
    f3 = values[2] or 0
    f4 = values[3] or 0
    if f1 >= 10:
        buy_observation.append("F1≥10")
    if f3 >= 5 and pre["active_addresses"] >= 10:
        buy_observation.append("F3≥5")
    if f3 >= 1.5 and pre["active_addresses"] >= 10:
        sell_risk.append("F3≥1.5")
    if f4 >= 3 and pre["new_addresses"] >= 5:
        sell_risk.append("F4≥3")

    return {
        "d": bar["date"].isoformat(),
        "o": finite(bar["open"], 8),
        "h": finite(bar["high"], 8),
        "l": finite(bar["low"], 8),
        "c": finite(bar["close"], 8),
        "v": finite(bar["volume"], 4),
        "r": finite(bar["return_pct"]),
        "f": [finite(value) for value in values],
        "z": zero_launches(features) + [cex["zero_launch"]],
        "a": [pre["active_addresses"], pre["new_addresses"]],
        "share": [finite(max_share * 100), finite(net_share * 100)],
        "flow": finite(net, 2),
        "flow_dir": "inflow" if net > 0 else "outflow" if net < 0 else "flat",
        "cex": cex,
        "f5x": f5_breakdown(
            state,
            features,
            bar["date"],
            bars,
            bar_index,
            cex,
        ),
        "sig": triggered,
        "buy": buy_observation,
        "sell": sell_risk,
    }


def reused_price_bars(
    reused_dataset: dict[str, Any], symbol: str, cutoff: date
) -> list[dict[str, Any]]:
    token = next(
        (
            item
            for item in reused_dataset.get("tokens", [])
            if item.get("symbol") == symbol
        ),
        None,
    )
    if token is None:
        raise ValueError(f"{symbol}: no embedded OHLCV available for reuse")
    bars = []
    for item in token.get("bars", []):
        day = date.fromisoformat(item["d"])
        if DISPLAY_START <= day <= cutoff:
            bars.append(
                {
                    "date": day,
                    "open": item["o"],
                    "high": item["h"],
                    "low": item["l"],
                    "close": item["c"],
                    "volume": item["v"],
                    "return_pct": item["r"],
                }
            )
    return bars


def build_dataset(reused_dataset: dict[str, Any] | None = None) -> dict[str, Any]:
    engine = load_engine()
    in_states, in_meta = load_states(
        engine, IN_SAMPLE_SNAPSHOT, IN_SAMPLE_CONFIG, IN_SAMPLE_SYMBOLS
    )
    out_states, out_meta = load_states(
        engine, OUT_SAMPLE_SNAPSHOT, OUT_SAMPLE_CONFIG, OUT_SAMPLE_SYMBOLS
    )
    additional_states, additional_meta = load_states(
        engine,
        ADDITIONAL_SAMPLE_SNAPSHOT,
        ADDITIONAL_SAMPLE_CONFIG,
        ADDITIONAL_SAMPLE_SYMBOLS,
    )
    all_states = {
        **in_states,
        **out_states,
        **additional_states,
    }
    attach_confirmed_cex_path_events(all_states)
    groups = (
        ("样本内", IN_SAMPLE_SYMBOLS, in_states, date(2026, 7, 28)),
        ("样本外", OUT_SAMPLE_SYMBOLS, out_states, date(2026, 7, 29)),
        (
            "新增样本外",
            ADDITIONAL_SAMPLE_SYMBOLS,
            additional_states,
            date(2026, 7, 29),
        ),
    )
    tokens = []
    for group_label, symbols, states, cutoff in groups:
        for symbol in symbols:
            if reused_dataset is None:
                bars = engine.fetch_daily_bars(symbol, cutoff)
                selected_bars = [
                    bar
                    for bar in bars
                    if DISPLAY_START <= bar["date"] <= cutoff
                ]
            else:
                selected_bars = reused_price_bars(
                    reused_dataset, symbol, cutoff
                )
            if not selected_bars:
                continue
            episodes = episodes_with_bounds(selected_bars)
            daily = [
                factor_row(
                    engine,
                    states[symbol],
                    bar,
                    selected_bars,
                    bar_index,
                )
                for bar_index, bar in enumerate(selected_bars)
            ]
            tokens.append(
                {
                    "symbol": symbol,
                    "group": group_label,
                    "cluster_amount": finite(
                        float(states[symbol]["cluster_amount"]), 2
                    ),
                    "bars": daily,
                    "events": episodes,
                }
            )
            print(
                f"{symbol}: {len(daily)} daily rows, {len(episodes)} events"
            )
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "price_source": "Binance USDⓈ-M Futures 1d",
        "chain_source": "Bubblemaps cluster member transfer history",
        "event_rule": "abs(daily close return) >= 20%; shocks within 7 days are merged",
        "factor_window": "D-7 through D-1 vs four preceding 7-day baselines",
        "display_start": DISPLAY_START.isoformat(),
        "snapshots": {
            "in_sample": in_meta["captured_at"],
            "out_of_sample": out_meta["captured_at"],
            "additional_out_of_sample": additional_meta["captured_at"],
        },
        "tokens": tokens,
    }


HTML_TEMPLATE = r"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>F1–F7 日K异动复盘</title>
  <style>
    :root {
      color-scheme: dark;
      --bg: #071018;
      --panel: #0d1823;
      --panel-2: #111f2c;
      --line: #233446;
      --text: #e8f0f6;
      --muted: #8fa2b5;
      --up: #20c997;
      --down: #ff647c;
      --accent: #f6c85f;
      --blue: #5fa8ff;
      --violet: #b38cff;
      --grid: rgba(143, 162, 181, .16);
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      background:
        radial-gradient(circle at 80% -10%, rgba(95,168,255,.10), transparent 32rem),
        var(--bg);
      color: var(--text);
      font: 14px/1.45 ui-sans-serif, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    }
    button, select { font: inherit; }
    .app { max-width: 1280px; margin: 0 auto; padding: 24px; }
    .topbar {
      display: flex; align-items: end; justify-content: space-between;
      gap: 18px; margin-bottom: 18px; flex-wrap: wrap;
    }
    h1 { font-size: 22px; line-height: 1.2; margin: 0 0 5px; font-weight: 650; }
    .subtitle { color: var(--muted); font-size: 13px; }
    .controls { display: flex; flex-wrap: wrap; align-items: end; gap: 10px; }
    label { display: grid; gap: 5px; color: var(--muted); font-size: 12px; }
    select, button {
      color: var(--text); background: var(--panel-2); border: 1px solid var(--line);
      border-radius: 8px; min-height: 38px; padding: 7px 10px;
    }
    button { cursor: pointer; }
    button:hover, select:hover { border-color: var(--blue); }
    button:focus-visible, select:focus-visible { outline: 2px solid var(--blue); outline-offset: 2px; }
    .summary {
      display: grid; grid-template-columns: repeat(4,minmax(0,1fr)); gap: 10px; margin-bottom: 14px;
    }
    .metric {
      background: var(--panel); border: 1px solid var(--line); border-radius: 10px;
      padding: 11px 13px; min-width: 0;
    }
    .metric .k { color: var(--muted); font-size: 12px; margin-bottom: 3px; }
    .metric .v { font-size: 16px; font-weight: 650; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
    .chart-shell {
      position: relative; background: var(--panel); border: 1px solid var(--line);
      border-radius: 12px; overflow: hidden;
    }
    #chart { display: block; width: 100%; height: auto; touch-action: none; }
    .tooltip {
      position: absolute; z-index: 3; pointer-events: none; min-width: 190px;
      background: rgba(7,16,24,.95); border: 1px solid var(--line); border-radius: 8px;
      padding: 8px 10px; box-shadow: 0 10px 28px rgba(0,0,0,.28);
      opacity: 0; transform: translateY(3px); transition: opacity .08s ease, transform .08s ease;
    }
    .tooltip.show { opacity: 1; transform: none; }
    .tt-date { font-weight: 650; margin-bottom: 3px; }
    .tt-line { display: flex; justify-content: space-between; gap: 18px; color: var(--muted); font-size: 12px; }
    .tt-line strong { color: var(--text); font-weight: 550; }
    .up { color: var(--up) !important; }
    .down { color: var(--down) !important; }
    .legend {
      display: flex; flex-wrap: wrap; gap: 14px; color: var(--muted);
      font-size: 12px; padding: 10px 13px; border-top: 1px solid var(--line);
    }
    .legend span { display: inline-flex; align-items: center; gap: 6px; }
    .swatch { width: 11px; height: 11px; display: inline-block; border-radius: 2px; }
    .zoom-bar {
      display: grid; grid-template-columns: auto minmax(160px,1fr) auto;
      gap: 12px; align-items: center; padding: 10px 13px;
      border-top: 1px solid var(--line);
    }
    .zoom-actions { display: flex; flex-wrap: wrap; gap: 7px; }
    .zoom-actions button { min-height: 34px; padding: 5px 9px; }
    .pan-control {
      display: grid; grid-template-columns: auto minmax(100px,1fr);
      gap: 8px; align-items: center; color: var(--muted); font-size: 12px;
    }
    #panRange { width: 100%; accent-color: var(--blue); }
    #viewRangeText {
      color: var(--muted); font-size: 12px; white-space: nowrap;
      font-variant-numeric: tabular-nums;
    }
    .factor-panel {
      margin-top: 14px; display: grid; grid-template-columns: 1.15fr 2fr; gap: 14px;
    }
    .selected, .factor-list {
      background: var(--panel); border: 1px solid var(--line); border-radius: 12px; padding: 14px;
    }
    .selected h2, .factor-list h2 { font-size: 14px; margin: 0 0 10px; font-weight: 650; }
    .selected-date { font-size: 18px; font-weight: 650; margin-bottom: 8px; }
    .tags { display: flex; flex-wrap: wrap; gap: 6px; min-height: 25px; }
    .tag { padding: 3px 7px; border-radius: 999px; background: var(--panel-2); border: 1px solid var(--line); font-size: 12px; }
    .tag.alert { color: var(--accent); border-color: rgba(246,200,95,.45); }
    .tag.buy { color: var(--up); border-color: rgba(32,201,151,.45); }
    .tag.sell { color: var(--down); border-color: rgba(255,100,124,.45); }
    .ohlc {
      display: grid; grid-template-columns: repeat(3,1fr); gap: 8px 12px; margin: 12px 0;
    }
    .ohlc div { min-width: 0; }
    .ohlc small { display: block; color: var(--muted); }
    .ohlc strong { display: block; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
    .factor-grid { display: grid; grid-template-columns: repeat(3,minmax(0,1fr)); gap: 8px; }
    .factor {
      min-width: 0; padding: 9px 10px; background: var(--panel-2);
      border-left: 3px solid var(--line); border-radius: 7px;
    }
    .factor.hot { border-left-color: var(--accent); }
    .factor-head { display: flex; justify-content: space-between; gap: 8px; }
    .factor-name { font-weight: 650; }
    .factor-value { font-variant-numeric: tabular-nums; }
    .factor-rule { color: var(--muted); font-size: 11px; margin-top: 4px; }
    .cex-factor-card {
      grid-column: 1 / -1; padding: 12px 13px;
      border-left-width: 4px;
    }
    .cex-factor-card .factor-head { align-items: center; }
    .cex-strength {
      display: flex; align-items: baseline; gap: 5px;
      font-variant-numeric: tabular-nums;
    }
    .cex-strength small {
      color: var(--muted); font-size: 10px; font-weight: 500;
    }
    .cex-strength strong { font-size: 16px; }
    .cex-rule {
      margin-top: 5px; color: var(--muted); font-size: 11px;
    }
    .cex-summary-row {
      display: flex; flex-wrap: wrap; align-items: center;
      gap: 7px 10px; margin-top: 10px;
    }
    .cex-direction {
      display: inline-flex; align-items: center; padding: 3px 8px;
      border: 1px solid var(--line); border-radius: 999px;
      font-size: 11px; font-weight: 700;
    }
    .cex-direction.inflow {
      color: var(--down); border-color: rgba(255,100,124,.45);
      background: rgba(255,100,124,.08);
    }
    .cex-direction.outflow {
      color: var(--up); border-color: rgba(32,201,151,.45);
      background: rgba(32,201,151,.08);
    }
    .cex-direction.flat { color: var(--muted); }
    .cex-cluster-share { color: var(--muted); font-size: 11px; }
    .cex-cluster-share strong { color: var(--text); font-size: 12px; }
    .cex-flow-grid {
      display: grid; grid-template-columns: repeat(3,minmax(0,1fr));
      gap: 7px; margin-top: 9px;
    }
    .cex-flow-item {
      min-width: 0; padding: 7px 9px; border: 1px solid var(--line);
      border-radius: 6px; background: rgba(255,255,255,.018);
    }
    .cex-flow-item span {
      display: block; margin-bottom: 2px; color: var(--muted); font-size: 10px;
    }
    .cex-flow-item strong {
      display: block; overflow: hidden; color: var(--text);
      font-size: 13px; font-variant-numeric: tabular-nums;
      text-overflow: ellipsis; white-space: nowrap;
    }
    .cex-detail-line {
      margin-top: 8px; color: var(--muted); font-size: 11px; line-height: 1.55;
    }
    .cex-detail-line > strong { color: var(--text); }
    .cex-label-list {
      display: flex; flex-wrap: wrap; gap: 5px; margin-top: 5px;
    }
    .cex-label {
      display: inline-block; max-width: 100%; padding: 2px 6px;
      border: 1px solid var(--line); border-radius: 5px;
      color: var(--text); background: rgba(255,255,255,.025);
      overflow-wrap: anywhere;
    }
    .cex-coverage-note {
      margin-top: 8px; padding-top: 7px; border-top: 1px dashed var(--line);
      color: var(--muted); font-size: 10px;
    }
    .f5-breakdown-title {
      margin-top: 14px !important; padding-top: 12px;
      border-top: 1px solid var(--line);
    }
    .f5-grid { grid-template-columns: repeat(2,minmax(0,1fr)); }
    .f5-grid .factor { border-left-color: var(--blue); }
    .f5-grid .factor.causal-note { border-left-color: var(--muted); }
    .f5-grid .factor.hot {
      border-left-color: var(--accent);
      background: rgba(246,200,95,.09);
      box-shadow: inset 0 0 0 1px rgba(246,200,95,.18);
    }
    .f5-grid .factor-path { grid-column: 1 / -1; }
    .factor-alert-badge {
      display: inline-block; margin-left: 6px; padding: 1px 5px;
      border-radius: 999px; color: var(--accent);
      background: rgba(246,200,95,.12); font-size: 10px;
      font-style: normal; vertical-align: 1px;
    }
    .factor-explainer {
      margin-top: 14px; background: var(--panel); border: 1px solid var(--line);
      border-radius: 12px; padding: 16px;
    }
    .factor-explainer h2 { font-size: 16px; margin: 0 0 4px; font-weight: 650; }
    .factor-doc-grid {
      display: grid; grid-template-columns: repeat(2,minmax(0,1fr));
      gap: 10px; margin-top: 14px;
    }
    .factor-doc {
      min-width: 0; background: var(--panel-2); border-radius: 9px;
      padding: 12px 13px; border-top: 3px solid var(--line);
    }
    .factor-doc h3 { font-size: 14px; margin: 0 0 8px; font-weight: 650; }
    .factor-doc dl {
      display: grid; grid-template-columns: 56px minmax(0,1fr);
      gap: 6px 9px; margin: 0;
    }
    .factor-doc dt { color: var(--muted); font-size: 12px; }
    .factor-doc dd { margin: 0; min-width: 0; }
    .factor-doc code {
      color: var(--text); background: rgba(143,162,181,.10);
      border-radius: 4px; padding: 1px 4px; white-space: normal;
    }
    .factor-doc .trigger { color: var(--accent); }
    .factor-doc.f5-breakdown-doc {
      grid-column: 1 / -1; border-top-color: var(--blue);
    }
    .factor-doc.f5-breakdown-doc dl {
      grid-template-columns: 72px minmax(0,1fr);
    }
    .threshold-note {
      margin-top: 12px; padding-top: 12px; border-top: 1px solid var(--line);
      display: grid; grid-template-columns: 1fr 1fr; gap: 12px;
    }
    .threshold-note strong { display: block; margin-bottom: 3px; }
    .threshold-note span { color: var(--muted); font-size: 12px; }
    .method {
      color: var(--muted); font-size: 12px; margin: 14px 2px 0;
      display: flex; gap: 12px; justify-content: space-between; flex-wrap: wrap;
    }
    @media (max-width: 800px) {
      .app { padding: 14px; }
      .summary { grid-template-columns: repeat(2,minmax(0,1fr)); }
      .factor-panel { grid-template-columns: 1fr; }
      .factor-grid { grid-template-columns: repeat(2,minmax(0,1fr)); }
      .f5-grid { grid-template-columns: repeat(2,minmax(0,1fr)); }
      .factor-doc-grid { grid-template-columns: 1fr; }
      .zoom-bar { grid-template-columns: 1fr; }
      #viewRangeText { white-space: normal; }
    }
    @media (max-width: 480px) {
      .summary { grid-template-columns: 1fr 1fr; }
      .controls { width: 100%; }
      .controls label { flex: 1; min-width: 130px; }
      select { width: 100%; }
      .factor-grid { grid-template-columns: 1fr; }
      .f5-grid { grid-template-columns: 1fr; }
      .f5-grid .factor-path { grid-column: auto; }
      .ohlc { grid-template-columns: repeat(2,1fr); }
      .threshold-note { grid-template-columns: 1fr; }
    }
  </style>
</head>
<body>
<main class="app">
  <div class="topbar">
    <div>
      <h1>F1–F7 日 K 异动复盘</h1>
      <div class="subtitle">默认展示 2026 年以来全部日 K；悬停查看逐日因子，滚轮缩放，时间轴平移</div>
    </div>
    <div class="controls">
      <label>币种
        <select id="symbolSelect" aria-label="选择币种"></select>
      </label>
      <label>异动事件
        <select id="eventSelect" aria-label="选择异动事件"></select>
      </label>
      <button id="prevEvent" type="button" aria-label="上一个事件">← 上一个</button>
      <button id="nextEvent" type="button" aria-label="下一个事件">下一个 →</button>
    </div>
  </div>

  <section class="summary" aria-label="当前事件摘要">
    <div class="metric"><div class="k">样本</div><div class="v" id="sampleMetric">—</div></div>
    <div class="metric"><div class="k">异动范围</div><div class="v" id="rangeMetric">—</div></div>
    <div class="metric"><div class="k">段内最大单日</div><div class="v" id="shockMetric">—</div></div>
    <div class="metric"><div class="k">事件前异常因子</div><div class="v" id="factorMetric">—</div></div>
  </section>

  <section class="chart-shell" id="chartShell">
    <svg id="chart" viewBox="0 0 1120 520" role="img" aria-labelledby="chartTitle chartDesc">
      <title id="chartTitle">日 K、成交量和因子异常时间线</title>
      <desc id="chartDesc">绿色为上涨日，红色为下跌日，黄色菱形表示当日存在链上因子异常。</desc>
    </svg>
    <div class="tooltip" id="tooltip" aria-hidden="true"></div>
    <div class="zoom-bar" aria-label="K线缩放和平移">
      <div class="zoom-actions">
        <button id="zoomIn" type="button" aria-label="放大K线">＋ 放大</button>
        <button id="zoomOut" type="button" aria-label="缩小K线">－ 缩小</button>
        <button id="resetZoom" type="button" aria-label="恢复全年视图">全年复位</button>
      </div>
      <label class="pan-control" for="panRange">
        <span>时间轴平移</span>
        <input id="panRange" type="range" min="0" max="0" value="0" step="1">
      </label>
      <span id="viewRangeText">—</span>
    </div>
    <div class="legend" aria-label="图例">
      <span><i class="swatch" style="background:var(--up)"></i>上涨日 K</span>
      <span><i class="swatch" style="background:var(--down)"></i>下跌日 K</span>
      <span><i class="swatch" style="background:rgba(95,168,255,.25);border:1px solid var(--blue)"></i>D-7 至 D-1</span>
      <span><i class="swatch" style="background:rgba(255,100,124,.20);border:1px solid var(--down)"></i>涨跌异动段</span>
      <span><i class="swatch" style="background:var(--accent);transform:rotate(45deg)"></i>至少一个因子异常</span>
    </div>
  </section>

  <section class="factor-panel">
    <div class="selected">
      <h2>悬停日详情</h2>
      <div class="selected-date" id="selectedDate">—</div>
      <div class="tags" id="statusTags"></div>
      <div class="ohlc" id="ohlcGrid"></div>
      <div class="subtitle" id="flowDetail"></div>
    </div>
    <div class="factor-list">
      <h2>七因子（计算窗截至该日 D-1）</h2>
      <div class="factor-grid" id="factorGrid"></div>
      <h2 class="f5-breakdown-title">F5 巨额转账拆分</h2>
      <div class="subtitle">黄色边框和“异常”标记表示该子因子的条件在当前 D 日命中；蓝色边框仅表示普通拆分项。</div>
      <div class="factor-grid f5-grid" id="f5Grid"></div>
    </div>
  </section>

  <section class="factor-explainer" aria-labelledby="factorExplainerTitle">
    <h2 id="factorExplainerTitle">七因子详细口径</h2>
    <div class="subtitle">
      鼠标所在日记为 D；观察窗 W-1 为 D-7 至 D-1，基线为 D-35 至 D-8，并拆成四个 7 日周。基线为零、观察窗大于零时显示“从零启动”。
    </div>
    <div class="factor-doc-grid">
      <article class="factor-doc">
        <h3>F1 · 转账金额放大因子</h3>
        <dl>
          <dt>公式</dt>
          <dd><code>W-1 总转账金额 ÷ 基线四周的周均转账金额</code></dd>
          <dt>异常</dt>
          <dd class="trigger">F1 ≥ 3x</dd>
          <dt>含义</dt>
          <dd>衡量 Cluster 相关地址在 D 日前一周的代币转移规模是否突然放大，适合发现资金搬运、归集或分发活动升温。</dd>
          <dt>注意</dt>
          <dd>这里是链上 gross transfer，同一批代币反复转移会重复计入；它不等于交易所成交量、买入额或新增资金。</dd>
        </dl>
      </article>

      <article class="factor-doc">
        <h3>F2 · 转账笔数放大因子</h3>
        <dl>
          <dt>公式</dt>
          <dd><code>W-1 转账笔数 ÷ 基线四周的周均转账笔数</code></dd>
          <dt>异常</dt>
          <dd class="trigger">F2 ≥ 3x</dd>
          <dt>含义</dt>
          <dd>衡量链上动作频率是否异常增多。金额不大但笔数暴增时，可能对应批量分发、归集、机器人操作或地址网络活化。</dd>
          <dt>注意</dt>
          <dd>高笔数不代表高资金量；dust 转账、合约自动调用和循环转账都可能抬高 F2，需要与 F1 和地址拓扑一起看。</dd>
        </dl>
      </article>

      <article class="factor-doc">
        <h3>F3 · 活跃地址扩张因子</h3>
        <dl>
          <dt>公式</dt>
          <dd><code>W-1 去重活跃地址数 ÷ 基线四周的周均活跃地址数</code></dd>
          <dt>异常</dt>
          <dd class="trigger">F3 ≥ 2x，且 W-1 活跃地址 ≥ 10</dd>
          <dt>含义</dt>
          <dd>衡量参与转账的地址覆盖面是否扩大。F2 与 F3 同时升高，更接近“更多地址参与”，而不只是少数地址高频互转。</dd>
          <dt>注意</dt>
          <dd>活跃地址包含转出方和转入方，也可能包括交易所、路由合约和项目控制的批量地址，不等于独立用户数。</dd>
        </dl>
      </article>

      <article class="factor-doc">
        <h3>F4 · 新地址扩张因子</h3>
        <dl>
          <dt>公式</dt>
          <dd><code>W-1 首次可见地址数 ÷ 基线四周的周均首次可见地址数</code></dd>
          <dt>异常</dt>
          <dd class="trigger">F4 ≥ 2x，且 W-1 首次可见地址 ≥ 5</dd>
          <dt>含义</dt>
          <dd>衡量当前历史记录中第一次出现的地址是否集中增加，可用于观察新的接收端、分发对象或地址群是否进入网络。</dd>
          <dt>注意</dt>
          <dd>“首次可见”不等于地址刚创建、首次买币或新增真实用户；它只表示该地址第一次出现在本次可见转账历史中。</dd>
        </dl>
      </article>

      <article class="factor-doc">
        <h3>F5 · 巨额转账因子</h3>
        <dl>
          <dt>公式</dt>
          <dd><code>W-1 最大单笔 ÷ 基线四周各周最大单笔的中位数</code></dd>
          <dt>异常</dt>
          <dd class="trigger">F5 ≥ 3x，且最大单笔 ≥ Cluster 合计余额的 0.5%</dd>
          <dt>含义</dt>
          <dd>识别相对历史显著放大、且相对当前 Cluster 规模也足够大的单笔活动，通常用于标记鲸鱼转账或项目级资产搬运。</dd>
          <dt>注意</dt>
          <dd>桥接、质押、解锁、迁移、交易所调仓和内部钱包整理都可能触发；必须结合地址标签、去向和后续路径解释。</dd>
        </dl>
      </article>

      <article class="factor-doc">
        <h3>F6 · Cluster 净流冲击因子</h3>
        <dl>
          <dt>公式</dt>
          <dd><code>|外部流入 − 外部流出| ÷ 基线四周绝对净流的中位数</code></dd>
          <dt>异常</dt>
          <dd class="trigger">F6 ≥ 3x，且绝对净流 ≥ Cluster 合计余额的 1%</dd>
          <dt>含义</dt>
          <dd>外部地址 → Cluster 记为流入，Cluster → 外部地址记为流出；正值方向为净流入，负值方向为净流出，页面会单独显示方向。</dd>
          <dt>注意</dt>
          <dd>F6 的因子值使用绝对值，只表示冲击强度，不能脱离方向判断；同时存在巨大流入与流出时，两者还可能互相抵消。</dd>
        </dl>
      </article>

      <article class="factor-doc">
        <h3>F7 · CEX 净流因子</h3>
        <dl>
          <dt>公式</dt>
          <dd><code>|CEX流入 − CEX流出| ÷ 基线四周绝对CEX净流的中位数</code></dd>
          <dt>方向</dt>
          <dd><code>CEX流入 − CEX流出</code> 为正时表示净流入交易所，为负时表示净流出交易所；因子强度使用绝对值，方向单独展示。</dd>
          <dt>异常</dt>
          <dd class="trigger">F7 ≥ 3x 或从零启动，且 |CEX净流| ≥ Cluster 合计余额的 0.1%</dd>
          <dt>含义</dt>
          <dd>衡量 D 日前一周代币流入或流出已识别中心化交易所地址的净规模是否相对历史突然放大。</dd>
          <dt>注意</dt>
          <dd>当前使用直接 CEX 地址标签；已确认的一至两跳路径尚未在全部地址上补齐，因此该值是直接标签口径，不代表完整最终入所金额。</dd>
        </dl>
      </article>

      <article class="factor-doc f5-breakdown-doc">
        <h3>F5a–F5h · 巨额转账的结构化拆分</h3>
        <dl>
          <dt>F5a 规模</dt>
          <dd><code>最大单笔 ÷ 历史四周周最大单笔中位数</code>。即原 F5 连续值，只使用 D-1 及以前的数据。</dd>
          <dt>F5b 冲击</dt>
          <dd><code>最大单笔 ÷ 当前 Cluster 合计余额</code>。用于区分绝对金额很大但对项目地址群影响很小的转账。</dd>
          <dt>F5c 属性</dt>
          <dd>先检查最大单笔两端是否为 CEX 地址；任一端为 CEX 时优先标记为 CEX 相关转账。仅当两端都不是 CEX 时，才按当前 Cluster 划分同 Cluster 内部、跨 Cluster、Cluster 与外部或成员范围未识别。</dd>
          <dt>F5d 流向</dt>
          <dd>优先划分流入 CEX、流出 CEX 或 CEX 间转移；非 CEX 转账再划分内部调仓、跨 Cluster 转移、外部流入 Cluster、Cluster 流出或未知。页面同时汇总 D-7 至 D-1 的 CEX 流入、流出和净流。</dd>
          <dt>F5e 对手</dt>
          <dd>按现有 Bubblemaps 地址元数据识别 CEX、DEX/LP、Bridge、Staking、Vesting/Lock、多签、其他合约或未知地址；标签覆盖不全时会显示未知。</dd>
          <dt>F5f 路径</dt>
          <dd>保留为“事后路径标签”，不计入当日实时因子。完整计算还需持续跟踪该笔之后的多跳转账，当前快照不能可靠覆盖所有外部地址。</dd>
          <dt>F5g 持续</dt>
          <dd>在 D-7 至 D-1 内，对同时满足 F5a ≥ 3x、F5b ≥ 0.5% 的逐笔转账统计笔数、天数和最长连续天数，标记单日孤立、连续多日或间歇多日。</dd>
          <dt>F5h 位置</dt>
          <dd>只用 D-1 及以前的日 K，标记前一日放量突破、上涨后高位、下跌中、极低成交量、盘整/普通或历史不足，不引用 D 日及未来行情。</dd>
        </dl>
      </article>
    </div>
    <div class="threshold-note">
      <div>
        <strong>基础异常阈值</strong>
        <span>黄色菱形和“F1–F7 异常”标签使用上面列出的 3x/2x 及地址数、Cluster 占比门槛。</span>
      </div>
      <div>
        <strong>买卖观察阈值</strong>
        <span>“买入观察/卖出风险”来自历史样本校准，是另一层阈值；同一天可能方向冲突，因此只用于复盘提示，不是自动交易指令。</span>
      </div>
    </div>
  </section>

  <div class="method">
    <span>价格事件：币安永续日收盘涨跌绝对值 ≥20%；7 天内相邻异常日合并。</span>
    <span>链上数据：Bubblemaps 当前 Cluster 成员的历史转账；存在成员集合前视与幸存者偏差。</span>
  </div>
</main>

<script>
const DATA = __DATA__;
const FACTORS = [
  {id:"F1", name:"转账金额", rule:"≥3x", detail:"W-1 / 前四周周均"},
  {id:"F2", name:"转账笔数", rule:"≥3x", detail:"W-1 / 前四周周均"},
  {id:"F3", name:"活跃地址", rule:"≥2x 且 ≥10", detail:"W-1 / 前四周周均"},
  {id:"F4", name:"新地址", rule:"≥2x 且 ≥5", detail:"W-1 / 前四周周均"},
  {id:"F5", name:"巨额转账", rule:"≥3x 且 ≥Cluster 0.5%", detail:"最大单笔 / 基线周中位"},
  {id:"F6", name:"净流冲击", rule:"≥3x 且 ≥Cluster 1%", detail:"绝对净流 / 基线周中位"},
  {id:"F7", name:"CEX净流", rule:"≥3x 且 |净流|≥Cluster 0.1%", detail:"直接CEX净流 / 基线周中位"}
];
const NS = "http://www.w3.org/2000/svg";
const $ = id => document.getElementById(id);
const symbolSelect = $("symbolSelect");
const eventSelect = $("eventSelect");
const chart = $("chart");
const tooltip = $("tooltip");
let currentToken = null;
let currentEvent = null;
let tokenBars = [];
let currentBars = [];
let viewStart = 0;
let viewEnd = 0;
let hoveredIndex = -1;
const MIN_VISIBLE_BARS = 12;

function fmt(value, digits=2) {
  if (value === null || value === undefined || !Number.isFinite(Number(value))) return "N/A";
  const n = Number(value);
  if (Math.abs(n) >= 1e9) return (n/1e9).toFixed(2)+"B";
  if (Math.abs(n) >= 1e6) return (n/1e6).toFixed(2)+"M";
  if (Math.abs(n) >= 1e3) return (n/1e3).toFixed(2)+"K";
  if (Math.abs(n) > 0 && Math.abs(n) < 0.001) return n.toExponential(2);
  return n.toLocaleString("zh-CN",{maximumFractionDigits:digits});
}
function pct(value) {
  if (value === null || value === undefined) return "N/A";
  return `${value >= 0 ? "+" : ""}${Number(value).toFixed(2)}%`;
}
function html(value) {
  return String(value ?? "").replace(/[&<>"']/g, char=>({
    "&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#039;"
  })[char]);
}
function dateShort(value) { return value.slice(5); }
function svgEl(name, attrs={}) {
  const node = document.createElementNS(NS,name);
  Object.entries(attrs).forEach(([key,value]) => node.setAttribute(key,String(value)));
  return node;
}
function css(name) { return getComputedStyle(document.documentElement).getPropertyValue(name).trim(); }

function populateSymbols() {
  symbolSelect.innerHTML = "";
  const groupOrder = {"样本内":0,"样本外":1,"新增样本外":2};
  const ordered = [...DATA.tokens].sort((a,b) => {
    if (a.symbol === "EVAA") return -1;
    if (b.symbol === "EVAA") return 1;
    return groupOrder[a.group]-groupOrder[b.group] || a.symbol.localeCompare(b.symbol);
  });
  ordered.forEach(token => {
    const option = document.createElement("option");
    option.value = token.symbol;
    option.textContent = `${token.symbol} · ${token.group}`;
    symbolSelect.appendChild(option);
  });
  symbolSelect.value = ordered[0].symbol;
}

function setToken(symbol) {
  currentToken = DATA.tokens.find(item => item.symbol === symbol);
  tokenBars = currentToken.bars;
  eventSelect.innerHTML = "";
  const allOption = document.createElement("option");
  allOption.value = "all";
  allOption.textContent = `2026 全周期 · ${currentToken.events.length} 段异动`;
  eventSelect.appendChild(allOption);
  currentToken.events.forEach(event => {
    const option = document.createElement("option");
    option.value = String(event.id);
    const arrow = event.direction === "up" ? "↑" : "↓";
    option.textContent = `#${event.id} ${event.start} ${arrow} ${pct(event.start_return)}`;
    eventSelect.appendChild(option);
  });
  eventSelect.value = "all";
  setEvent("all");
}

function setEvent(id) {
  if(id === "all"){
    currentEvent = null;
    applyView(0,tokenBars.length);
    return;
  }
  currentEvent = currentToken.events.find(item => item.id === Number(id));
  let start = tokenBars.findIndex(bar => bar.d >= currentEvent.view_start);
  if(start < 0) start = 0;
  let end = tokenBars.length;
  for(let i=tokenBars.length-1;i>=0;i--){
    if(tokenBars[i].d <= currentEvent.view_end){
      end = i+1;
      break;
    }
  }
  applyView(start,end,currentEvent.start);
}

function applyView(start,end,preferredDate=null) {
  const total = tokenBars.length;
  let nextStart = Math.max(0,Math.min(Math.floor(start),Math.max(0,total-MIN_VISIBLE_BARS)));
  let nextEnd = Math.max(nextStart+MIN_VISIBLE_BARS,Math.min(total,Math.ceil(end)));
  if(nextEnd > total){
    nextEnd = total;
    nextStart = Math.max(0,nextEnd-MIN_VISIBLE_BARS);
  }
  viewStart = nextStart;
  viewEnd = nextEnd;
  currentBars = tokenBars.slice(viewStart,viewEnd);
  if(!currentBars.length) return;
  let nextHover = preferredDate ? currentBars.findIndex(bar=>bar.d===preferredDate) : -1;
  if(nextHover < 0 && currentEvent){
    nextHover = currentBars.findIndex(bar=>bar.d===currentEvent.start);
  }
  hoveredIndex = nextHover >= 0 ? nextHover : currentBars.length-1;
  updateSummary();
  renderChart();
  updateDetails(currentBars[hoveredIndex]);
  updateZoomControls();
}

function zoomView(scale,focus=.5) {
  const total = tokenBars.length;
  const visible = viewEnd-viewStart;
  const nextVisible = Math.max(
    MIN_VISIBLE_BARS,
    Math.min(total,Math.round(visible*scale))
  );
  const anchor = viewStart+visible*focus;
  const nextStart = Math.round(anchor-nextVisible*focus);
  const preferred = currentBars[hoveredIndex]?.d || null;
  applyView(nextStart,nextStart+nextVisible,preferred);
}

function updateZoomControls() {
  const visible = viewEnd-viewStart;
  const maxStart = Math.max(0,tokenBars.length-visible);
  const pan = $("panRange");
  pan.max = String(maxStart);
  pan.value = String(Math.min(viewStart,maxStart));
  pan.disabled = maxStart === 0;
  $("viewRangeText").textContent = currentBars.length
    ? `${currentBars[0].d} — ${currentBars[currentBars.length-1].d} · ${currentBars.length} 根日 K`
    : "无日 K";
}

function isShockDay(day) {
  const events = currentEvent ? [currentEvent] : currentToken.events;
  return events.some(event=>event.shock_dates.includes(day));
}

function updateSummary() {
  $("sampleMetric").textContent = `${currentToken.symbol} · ${currentToken.group}`;
  if(!currentEvent){
    $("rangeMetric").textContent = `${tokenBars[0].d} — ${tokenBars[tokenBars.length-1].d}`;
    const shock = $("shockMetric");
    shock.textContent = `${currentToken.events.length} 段异动`;
    shock.className = "v";
    $("factorMetric").textContent = "悬停日 K 查看";
    return;
  }
  $("rangeMetric").textContent = currentEvent.start === currentEvent.end
    ? currentEvent.start : `${currentEvent.start} — ${currentEvent.end}`;
  const shock = $("shockMetric");
  shock.textContent = `${pct(currentEvent.max_return)} · ${currentEvent.max_date}`;
  shock.className = `v ${currentEvent.max_return >= 0 ? "up" : "down"}`;
  const preBars = currentToken.bars.filter(bar => bar.d >= currentEvent.pre_start && bar.d <= currentEvent.pre_end);
  const union = new Set(preBars.flatMap(bar => bar.sig));
  $("factorMetric").textContent = union.size ? [...union].sort().map(i => FACTORS[i].id).join(" · ") : "无";
}

function renderChart() {
  chart.innerHTML = '<title id="chartTitle">日 K、成交量和因子异常时间线</title><desc id="chartDesc">绿色为上涨日，红色为下跌日，黄色菱形表示当日存在链上因子异常。</desc>';
  const W=1120,H=520,L=66,R=74,T=36,priceBottom=342,volTop=376,volBottom=452,axisY=476;
  const plotW=W-L-R;
  const highs=currentBars.map(b=>b.h), lows=currentBars.map(b=>b.l), vols=currentBars.map(b=>b.v);
  const minP=Math.min(...lows), maxP=Math.max(...highs), pad=(maxP-minP)*.08 || maxP*.02;
  const lo=minP-pad, hi=maxP+pad, maxV=Math.max(...vols,1);
  const step=plotW/currentBars.length, bodyW=Math.max(2,Math.min(11,step*.62));
  const x=i=>L+step*(i+.5);
  const y=p=>T+(hi-p)/(hi-lo)*(priceBottom-T);
  const vy=v=>volBottom-(v/maxV)*(volBottom-volTop);
  const dateToIndex=d=>currentBars.findIndex(b=>b.d===d);

  const bg=svgEl("rect",{x:L,y:T,width:plotW,height:volBottom-T,fill:"transparent"});
  chart.appendChild(bg);
  if(currentEvent){
    const preStart=Math.max(0,dateToIndex(currentEvent.pre_start));
    const preEnd=dateToIndex(currentEvent.pre_end);
    if(preEnd>=0){
      chart.appendChild(svgEl("rect",{x:L+preStart*step,y:T,width:(preEnd-preStart+1)*step,height:volBottom-T,fill:"rgba(95,168,255,.09)"}));
    }
    const eventStart=Math.max(0,dateToIndex(currentEvent.start));
    let eventEnd=dateToIndex(currentEvent.end);
    if(eventEnd<0) eventEnd=eventStart;
    chart.appendChild(svgEl("rect",{x:L+eventStart*step,y:T,width:(eventEnd-eventStart+1)*step,height:volBottom-T,fill:"rgba(255,100,124,.08)"}));
  } else {
    currentToken.events.forEach(event=>{
      const first=dateToIndex(event.start);
      const last=dateToIndex(event.end);
      if(first>=0 || last>=0){
        const left=first>=0?first:0;
        const right=last>=0?last:currentBars.length-1;
        chart.appendChild(svgEl("rect",{x:L+left*step,y:T,width:(right-left+1)*step,height:volBottom-T,fill:"rgba(255,100,124,.045)"}));
      }
    });
  }

  for(let i=0;i<=4;i++){
    const yy=T+(priceBottom-T)*i/4;
    chart.appendChild(svgEl("line",{x1:L,y1:yy,x2:W-R,y2:yy,stroke:css("--grid"),"stroke-width":1}));
    const val=hi-(hi-lo)*i/4;
    const text=svgEl("text",{x:W-R+8,y:yy+4,fill:css("--muted"),"font-size":11});
    text.textContent=fmt(val,6); chart.appendChild(text);
  }
  chart.appendChild(svgEl("line",{x1:L,y1:volTop-12,x2:W-R,y2:volTop-12,stroke:css("--line"),"stroke-width":1}));

  currentBars.forEach((bar,i)=>{
    const color=bar.c>=bar.o?css("--up"):css("--down");
    chart.appendChild(svgEl("line",{x1:x(i),y1:y(bar.h),x2:x(i),y2:y(bar.l),stroke:color,"stroke-width":Math.max(1,bodyW*.16)}));
    const top=Math.min(y(bar.o),y(bar.c)), height=Math.max(1,Math.abs(y(bar.o)-y(bar.c)));
    chart.appendChild(svgEl("rect",{x:x(i)-bodyW/2,y:top,width:bodyW,height,fill:color,rx:.6}));
    chart.appendChild(svgEl("rect",{x:x(i)-bodyW/2,y:vy(bar.v),width:bodyW,height:volBottom-vy(bar.v),fill:color,opacity:.36}));
    if(bar.sig.length){
      const s=Math.min(4.5,Math.max(3,step*.22));
      chart.appendChild(svgEl("path",{d:`M ${x(i)} ${T+5-s} L ${x(i)+s} ${T+5} L ${x(i)} ${T+5+s} L ${x(i)-s} ${T+5} Z`,fill:css("--accent")}));
    }
    if(isShockDay(bar.d)){
      chart.appendChild(svgEl("path",{d:`M ${x(i)} ${priceBottom+7} l 5 8 h -10 Z`,fill:bar.r>=0?css("--up"):css("--down")}));
    }
  });

  const tickCount=Math.min(7,currentBars.length);
  for(let t=0;t<tickCount;t++){
    const i=Math.round(t*(currentBars.length-1)/(tickCount-1 || 1));
    const text=svgEl("text",{x:x(i),y:axisY,fill:css("--muted"),"font-size":11,"text-anchor":"middle"});
    text.textContent=dateShort(currentBars[i].d); chart.appendChild(text);
  }
  const priceLabel=svgEl("text",{x:12,y:T+8,fill:css("--muted"),"font-size":11});
  priceLabel.textContent="PRICE"; chart.appendChild(priceLabel);
  const volumeLabel=svgEl("text",{x:12,y:volTop+10,fill:css("--muted"),"font-size":11});
  volumeLabel.textContent="VOL"; chart.appendChild(volumeLabel);

  const cross=svgEl("g",{id:"crosshair"});
  cross.appendChild(svgEl("line",{id:"crossV",x1:x(hoveredIndex),y1:T,x2:x(hoveredIndex),y2:volBottom,stroke:css("--muted"),"stroke-width":1,"stroke-dasharray":"3 4",opacity:.7}));
  chart.appendChild(cross);
  const hit=svgEl("rect",{x:L,y:T,width:plotW,height:volBottom-T,fill:"transparent",style:"cursor:crosshair"});
  hit.addEventListener("pointermove",evt=>{
    const pt=chart.createSVGPoint(); pt.x=evt.clientX; pt.y=evt.clientY;
    const local=pt.matrixTransform(chart.getScreenCTM().inverse());
    const i=Math.max(0,Math.min(currentBars.length-1,Math.floor((local.x-L)/step)));
    setHover(i,evt);
  });
  hit.addEventListener("wheel",evt=>{
    evt.preventDefault();
    const pt=chart.createSVGPoint(); pt.x=evt.clientX; pt.y=evt.clientY;
    const local=pt.matrixTransform(chart.getScreenCTM().inverse());
    const focus=Math.max(0,Math.min(1,(local.x-L)/plotW));
    zoomView(evt.deltaY<0?.72:1.38,focus);
    tooltip.classList.remove("show");
  },{passive:false});
  hit.addEventListener("pointerleave",()=>tooltip.classList.remove("show"));
  chart.appendChild(hit);
}

function setHover(index,evt) {
  if(index<0 || index>=currentBars.length) return;
  hoveredIndex=index;
  const line=$("crossV");
  if(line){
    const L=66,R=74,W=1120,step=(W-L-R)/currentBars.length;
    const xx=L+step*(index+.5);
    line.setAttribute("x1",xx); line.setAttribute("x2",xx);
  }
  const bar=currentBars[index];
  updateDetails(bar);
  const shell=$("chartShell").getBoundingClientRect();
  const hot=bar.sig.length ? bar.sig.map(i=>FACTORS[i].id).join(" · ") : "无因子异常";
  tooltip.innerHTML=`<div class="tt-date">${bar.d}</div>
    <div class="tt-line"><span>日涨跌</span><strong class="${bar.r>=0?"up":"down"}">${pct(bar.r)}</strong></div>
    <div class="tt-line"><span>收盘</span><strong>${fmt(bar.c,8)}</strong></div>
    <div class="tt-line"><span>异常因子</span><strong>${hot}</strong></div>`;
  tooltip.classList.add("show");
  tooltip.style.left="0px"; tooltip.style.top="0px";
  const box=tooltip.getBoundingClientRect();
  let left=evt.clientX-shell.left+14, top=evt.clientY-shell.top-box.height-14;
  if(left+box.width>shell.width-8) left=evt.clientX-shell.left-box.width-14;
  if(top<8) top=evt.clientY-shell.top+14;
  tooltip.style.left=`${Math.max(8,left)}px`;
  tooltip.style.top=`${Math.max(8,top)}px`;
}

function updateDetails(bar) {
  $("selectedDate").textContent=bar.d;
  const tags=[];
  if(isShockDay(bar.d)) tags.push(`<span class="tag ${bar.r>=0?"buy":"sell"}">价格异动 ${pct(bar.r)}</span>`);
  if(bar.sig.length) tags.push(...bar.sig.map(i=>`<span class="tag alert">${FACTORS[i].id} 异常</span>`));
  if(bar.buy.length) tags.push(`<span class="tag buy">买入观察：${bar.buy.join("、")}</span>`);
  if(bar.sell.length) tags.push(`<span class="tag sell">卖出风险：${bar.sell.join("、")}</span>`);
  if(bar.buy.length && bar.sell.length) tags.push('<span class="tag alert">阈值方向冲突</span>');
  $("statusTags").innerHTML=tags.length?tags.join(""):'<span class="tag">无异常</span>';
  $("ohlcGrid").innerHTML=[
    ["开",fmt(bar.o,8)],["高",fmt(bar.h,8)],["低",fmt(bar.l,8)],
    ["收",fmt(bar.c,8)],["日涨跌",pct(bar.r)],["成交量",fmt(bar.v)]
  ].map(([k,v])=>`<div><small>${k}</small><strong>${v}</strong></div>`).join("");
  const flowDir=bar.flow_dir==="inflow"?"净流入":bar.flow_dir==="outflow"?"净流出":"净流为零";
  $("flowDetail").textContent=`F6 方向：${flowDir} ${fmt(Math.abs(bar.flow))}；占 Cluster ${fmt(bar.share[1])}%`;
  $("factorGrid").innerHTML=FACTORS.map((factor,i)=>{
    const value=bar.z[i]?"从零启动":bar.f[i]===null?"N/A":`${fmt(bar.f[i])}x`;
    let suffix="";
    if(i===2) suffix=` · ${bar.a[0]} 个`;
    if(i===3) suffix=` · ${bar.a[1]} 个`;
    if(i===4) suffix=` · Cluster ${fmt(bar.share[0])}%`;
    if(i===5) suffix=` · Cluster ${fmt(bar.share[1])}%`;
    if(i===6){
      const cex=bar.cex||{};
      const direction=cex.direction||"CEX净流为零";
      const directionClass=direction==="CEX净流入"?"inflow":direction==="CEX净流出"?"outflow":"flat";
      const labels=cex.labels_7d||[];
      const labelMarkup=labels.length
        ? labels.map(label=>`<span class="cex-label">${html(label)}</span>`).join("")
        : '<span class="cex-label">无已知 CEX 标签</span>';
      return `<div class="factor cex-factor-card ${bar.sig.includes(i)?"hot":""}">
        <div class="factor-head">
          <span class="factor-name">${factor.id} ${factor.name}</span>
          <span class="cex-strength"><small>异常强度</small><strong>${value}</strong></span>
        </div>
        <div class="cex-rule"><strong>异常阈值：</strong>${factor.rule}</div>
        <div class="cex-summary-row">
          <span class="cex-direction ${directionClass}">${html(direction)}</span>
          <span class="cex-cluster-share">绝对净流占 Cluster：<strong>${fmt(cex.abs_share_pct||0)}%</strong></span>
        </div>
        <div class="cex-flow-grid">
          <div class="cex-flow-item"><span>7 日流入 CEX</span><strong>${fmt(cex.inflow_7d||0)}</strong></div>
          <div class="cex-flow-item"><span>7 日流出 CEX</span><strong>${fmt(cex.outflow_7d||0)}</strong></div>
          <div class="cex-flow-item"><span>7 日净流（流入－流出）</span><strong>${fmt(cex.net_7d||0)}</strong></div>
        </div>
        <div class="cex-detail-line">
          <strong>CEX 相关转账：</strong>${cex.transfer_count_7d||0} 笔
          （直接 ${cex.direct_transfer_count_7d||0} / 多跳边界 ${cex.multihop_transfer_count_7d||0}）
        </div>
        <div class="cex-detail-line">
          <strong>涉及的交易所地址：</strong>
          <div class="cex-label-list">${labelMarkup}</div>
        </div>
        <div class="cex-coverage-note">统计口径：直接 CEX 转账 + 已确认多跳路径的唯一 CEX 边界交易；路径中间跳不累计，同一边界事件只计一次。</div>
      </div>`;
    }
    return `<div class="factor ${bar.sig.includes(i)?"hot":""}">
      <div class="factor-head"><span class="factor-name">${factor.id} ${factor.name}</span><span class="factor-value">${value}</span></div>
      <div class="factor-rule">${factor.rule}${suffix}</div>
    </div>`;
  }).join("");
  const f5=bar.f5x||{};
  const maximum=f5.maximum||{};
  const market=f5.market||{};
  const f5Alerts=f5.alerts||{};
  const counterparty=(f5.counterparty_types||[]).join("、")||"无可用标签";
  const counterpartyLabels=(f5.counterparty_labels||[]).join("、");
  const cexLabels=(f5.cex_labels_7d||[]).join("、")||"无已知 CEX 标签";
  const cexSummary=`7日 CEX：流入 ${fmt(f5.cex_inflow_7d||0)} / 流出 ${fmt(f5.cex_outflow_7d||0)} / 净流 ${fmt(f5.cex_net_flow_7d||0)}；${f5.cex_transfer_count_7d||0} 笔（直接 ${f5.cex_direct_transfer_count_7d||0} / 多跳 ${f5.cex_multihop_transfer_count_7d||0}）；${cexLabels}`;
  const f5a=bar.z[4]?"从零启动":bar.f[4]===null?"N/A":`${fmt(bar.f[4])}x`;
  const f5Cards=[
    ["f5a","F5a 相对历史规模",f5a,`最大单笔 ${fmt(maximum.amount)}；异常：≥3x 或从零启动`,""],
    ["f5b","F5b Cluster 冲击",`${fmt(bar.share[0])}%`,"异常：最大单笔 / Cluster 合计余额 ≥0.5%",""],
    ["f5c","F5c 内外部属性",f5.structure||"N/A","CEX 优先；非 CEX 才判断同 Cluster、跨 Cluster 或 Cluster 与外部",""],
    ["f5d","F5d 流向",f5.direction||"N/A",cexSummary,""],
    ["f5e","F5e 对手方类型",counterparty,counterpartyLabels||"异常：完整 F5 命中，且识别为特殊功能地址",""],
    ["f5f","F5f 后续路径","未纳入实时因子","需在转账发生后继续追踪，多跳数据不完整","factor-path causal-note"],
    ["f5g","F5g 持续性",f5.persistence||"N/A",
      `${f5.large_day_count_7d??0} 日 / ${f5.large_transfer_count_7d??0} 笔；最长连续 ${f5.longest_streak_days??0} 日；完整 F5 命中即高亮`,""],
    ["f5h","F5h 市场位置",market.state||"N/A",
      `此前7日 ${pct(market.return_7d_pct)} · 距20日高点 ${pct(market.distance_20d_high_pct)} · 成交量分位 ${fmt(market.volume_percentile_20d)}%；完整 F5 命中且非普通状态时高亮`,""]
  ];
  $("f5Grid").innerHTML=f5Cards.map(([key,name,value,rule,extra])=>{
    const active=Boolean(f5Alerts[key]);
    return `<div class="factor ${active?"hot":""} ${extra}">
      <div class="factor-head"><span class="factor-name">${html(name)}${active?'<em class="factor-alert-badge">异常</em>':""}</span><span class="factor-value">${html(value)}</span></div>
      <div class="factor-rule">${html(rule)}</div>
    </div>`;
  }).join("");
}

symbolSelect.addEventListener("change",()=>setToken(symbolSelect.value));
eventSelect.addEventListener("change",()=>setEvent(eventSelect.value));
$("zoomIn").addEventListener("click",()=>zoomView(.7,.5));
$("zoomOut").addEventListener("click",()=>zoomView(1.4,.5));
$("resetZoom").addEventListener("click",()=>{
  eventSelect.value="all";
  setEvent("all");
});
$("panRange").addEventListener("input",evt=>{
  const visible=viewEnd-viewStart;
  const start=Number(evt.target.value);
  applyView(start,start+visible);
});
$("prevEvent").addEventListener("click",()=>{
  const i=currentEvent
    ? currentToken.events.findIndex(event=>event.id===currentEvent.id)
    : 0;
  const next=(i-1+currentToken.events.length)%currentToken.events.length;
  eventSelect.value=String(currentToken.events[next].id); setEvent(currentToken.events[next].id);
});
$("nextEvent").addEventListener("click",()=>{
  const i=currentEvent
    ? currentToken.events.findIndex(event=>event.id===currentEvent.id)
    : -1;
  const next=(i+1)%currentToken.events.length;
  eventSelect.value=String(currentToken.events[next].id); setEvent(currentToken.events[next].id);
});
window.addEventListener("resize",()=>tooltip.classList.remove("show"));

populateSymbols();
setToken(symbolSelect.value);
</script>
</body>
</html>
"""


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--reuse-data",
        action="store_true",
        help="reuse the DATA payload embedded in the existing HTML",
    )
    args = parser.parse_args()
    ROOT.mkdir(parents=True, exist_ok=True)
    if args.reuse_data:
        if not OUTPUT_HTML.is_file():
            raise FileNotFoundError("existing dashboard is required for --reuse-data")
        existing = OUTPUT_HTML.read_text(encoding="utf-8")
        match = re.search(
            r"const DATA = (\{.*?\});\nconst FACTORS",
            existing,
            flags=re.DOTALL,
        )
        if match is None:
            raise ValueError("existing dashboard DATA payload unavailable")
        dataset = build_dataset(json.loads(match.group(1)))
    else:
        dataset = build_dataset()
    payload = json.dumps(
        dataset,
        ensure_ascii=False,
        separators=(",", ":"),
    ).replace("</script>", "<\\/script>")
    OUTPUT_HTML.write_text(
        HTML_TEMPLATE.replace("__DATA__", payload),
        encoding="utf-8",
    )
    print(f"wrote {OUTPUT_HTML} ({OUTPUT_HTML.stat().st_size:,} bytes)")


if __name__ == "__main__":
    main()
