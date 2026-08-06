#!/usr/bin/env python3
"""Compute auditable direct CEX boundary events and daily net flow."""

from __future__ import annotations

import argparse
import csv
import importlib.util
import json
from collections import defaultdict
from pathlib import Path


ROOT = Path(__file__).resolve().parent
PROJECT_ROOT = ROOT.parents[1]
BUILDER_PATH = (
    PROJECT_ROOT
    / "analysis/binance-bubblemaps-factor-kline-2026-07-30/build_dashboard.py"
)


def load_builder():
    spec = importlib.util.spec_from_file_location("factor_dashboard", BUILDER_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError("factor dashboard builder unavailable")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def write_csv(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def cex_labels(path: Path) -> dict[tuple[str, str], dict[str, str]]:
    labels = {}
    for row in read_csv(path):
        if row.get("arkham_is_cex") != "true" and row.get("local_is_cex") != "true":
            continue
        labels[(row["chain"], row["address"].lower())] = {
            "label": row.get("arkham_label") or row.get("local_labels") or row["address"],
            "entity_id": row.get("arkham_entity") or row.get("local_entities") or "",
            "is_cex": True,
        }
    return labels


def apply_labels(states, labels) -> None:
    for state in states.values():
        for record in state["records"]:
            for field in ("from_address", "to_address"):
                metadata = labels.get((record["chain"], record[field].lower()))
                if metadata:
                    state["address_metadata"][
                        (record["chain"], record["token_address"], record[field])
                    ] = metadata


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--snapshot", type=Path, default=ROOT / "bubblemaps-snapshot")
    parser.add_argument("--config", type=Path, default=ROOT / "expanded_universe_config.json")
    parser.add_argument("--labels", type=Path, default=ROOT / "arkham-review/arkham-label-queue.csv")
    parser.add_argument(
        "--multihop-reviews",
        type=Path,
        default=ROOT / "arkham-review/multihop-path-reviews.csv",
    )
    parser.add_argument("--output-dir", type=Path, default=ROOT / "cex-flow")
    args = parser.parse_args()

    builder = load_builder()
    config = json.loads(args.config.read_text(encoding="utf-8"))
    symbols = tuple(config["symbols"])
    states, metadata = builder.load_states(
        builder.load_engine(), args.snapshot, args.config, symbols
    )
    if metadata["missing_targets"]:
        raise ValueError(f"missing targets: {metadata['missing_targets']}")
    labels = cex_labels(args.labels)
    apply_labels(states, labels)
    manifest = json.loads((args.snapshot / "manifest.json").read_text(encoding="utf-8"))
    coverage = {
        row["requested_token_address"].lower(): (
            int(row["available_member_count"]), int(row["ordinary_member_count"])
        )
        for row in manifest["tokens"]
    }

    events = []
    seen = set()
    for symbol, state in states.items():
        for record in state["records"]:
            context = builder.transfer_context(state, record)
            direction = context["direction"]
            if direction not in {"流入CEX", "流出CEX"}:
                continue
            identity = (
                symbol,
                record["chain"],
                record["tx_hash"].lower(),
                direction,
                round(float(record["amount"]), 8),
            )
            if identity in seen:
                continue
            seen.add(identity)
            cex_address = record["to_address"] if direction == "流入CEX" else record["from_address"]
            label = labels.get((record["chain"], cex_address.lower()), {})
            available, total = coverage.get(record["token_address"].lower(), (0, 0))
            events.append(
                {
                    "symbol": symbol,
                    "date": record["day"].isoformat(),
                    "timestamp_ms": record["timestamp_ms"],
                    "chain": record["chain"],
                    "token_address": record["token_address"],
                    "direction": direction,
                    "amount": float(record["amount"]),
                    "from_address": record["from_address"],
                    "to_address": record["to_address"],
                    "cex_address": cex_address,
                    "cex_label": label.get("label", cex_address),
                    "tx_hash": record["tx_hash"],
                    "path_type": "direct",
                    "available_member_count": available,
                    "ordinary_member_count": total,
                    "member_coverage_pct": round(available / total * 100, 6) if total else 0,
                }
            )
    events.sort(key=lambda row: (row["date"], row["symbol"], row["tx_hash"]))

    if args.multihop_reviews.is_file() and args.multihop_reviews.stat().st_size:
        for row in read_csv(args.multihop_reviews):
            if row.get("status") != "confirmed_cex_boundary":
                continue
            direction = row.get("direction", "")
            if direction not in {"流入CEX", "流出CEX"}:
                raise ValueError("confirmed multi-hop event is missing a valid direction")
            tx_hash = row.get("boundary_tx_hash", "").lower()
            amount = float(row.get("cex_amount") or 0)
            if not tx_hash or amount <= 0 or not row.get("boundary_date"):
                raise ValueError("confirmed multi-hop event is missing boundary evidence")
            identity = (
                row["symbol"],
                row["chain"],
                tx_hash,
                direction,
                round(amount, 8),
            )
            if identity in seen:
                continue
            seen.add(identity)
            available, total = coverage.get(row["token_address"].lower(), (0, 0))
            events.append(
                {
                    "symbol": row["symbol"],
                    "date": row["boundary_date"],
                    "timestamp_ms": row.get("boundary_timestamp_ms", ""),
                    "chain": row["chain"],
                    "token_address": row["token_address"],
                    "direction": direction,
                    "amount": amount,
                    "from_address": "",
                    "to_address": "",
                    "cex_address": row.get("cex_address", ""),
                    "cex_label": row.get("cex_label") or row.get("cex_address", ""),
                    "tx_hash": row["boundary_tx_hash"],
                    "path_type": "multi_hop_boundary",
                    "available_member_count": available,
                    "ordinary_member_count": total,
                    "member_coverage_pct": round(available / total * 100, 6) if total else 0,
                }
            )
        events.sort(key=lambda row: (row["date"], row["symbol"], row["tx_hash"]))

    daily = defaultdict(
        lambda: {"inflow_to_cex": 0.0, "outflow_from_cex": 0.0, "event_count": 0, "labels": set()}
    )
    for event in events:
        row = daily[(event["symbol"], event["date"])]
        if event["direction"] == "流入CEX":
            row["inflow_to_cex"] += event["amount"]
        else:
            row["outflow_from_cex"] += event["amount"]
        row["event_count"] += 1
        row["labels"].add(event["cex_label"])
    daily_rows = []
    for (symbol, day), row in sorted(daily.items()):
        inflow = row["inflow_to_cex"]
        outflow = row["outflow_from_cex"]
        daily_rows.append(
            {
                "symbol": symbol,
                "date": day,
                "inflow_to_cex": round(inflow, 8),
                "outflow_from_cex": round(outflow, 8),
                "net_inflow_to_cex": round(inflow - outflow, 8),
                "event_count": row["event_count"],
                "cex_labels": "; ".join(sorted(row["labels"])),
                "coverage_status": "partial" if manifest["status"] != "success" else "complete",
            }
        )

    write_csv(args.output_dir / "direct-cex-events.csv", events)
    write_csv(args.output_dir / "daily-cex-net-flows.csv", daily_rows)
    print(
        f"{len(events)} direct CEX events across {len(daily_rows)} symbol-days; "
        f"snapshot status={manifest['status']}"
    )


if __name__ == "__main__":
    main()
