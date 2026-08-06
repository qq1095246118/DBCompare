#!/usr/bin/env python3
"""Build the Arkham review inventory for an expanded token universe."""

from __future__ import annotations

import argparse
import csv
import importlib.util
from pathlib import Path
from typing import Any


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


def address_row(symbol: str, chain: str, address: str) -> dict[str, Any]:
    return {
        "symbol": symbol,
        "chain": chain,
        "address": address,
        "sent_transfer_count": 0,
        "received_transfer_count": 0,
        "sent_amount": 0.0,
        "received_amount": 0.0,
        "max_sent_amount": 0.0,
        "max_received_amount": 0.0,
        "first_transfer_date": None,
        "last_transfer_date": None,
        "is_cluster_member": False,
        "local_labels": set(),
        "local_entities": set(),
        "local_is_cex": False,
        "high_impact_transfer_count": 0,
        "arkham_status": "pending_web_review",
        "arkham_entity": "",
        "arkham_label": "",
        "arkham_is_cex": "",
        "arkham_reviewed_at": "",
        "minimum_cex_hops": "",
        "cex_destination": "",
        "evidence_tx_hashes": "",
        "notes": "",
    }


def update_date(row: dict[str, Any], day) -> None:
    if row["first_transfer_date"] is None or day < row["first_transfer_date"]:
        row["first_transfer_date"] = day
    if row["last_transfer_date"] is None or day > row["last_transfer_date"]:
        row["last_transfer_date"] = day


def note_metadata(builder, row: dict[str, Any], metadata: dict[str, Any]) -> None:
    label = str(metadata.get("label") or "").strip()
    entity = str(metadata.get("entity_id") or "").strip()
    if label:
        row["local_labels"].add(label)
    if entity:
        row["local_entities"].add(entity)
    if builder.endpoint_type(metadata) == "CEX":
        row["local_is_cex"] = True


def build(builder, states: dict[str, dict[str, Any]]):
    addresses: dict[tuple[str, str, str], dict[str, Any]] = {}
    paths: list[dict[str, Any]] = []
    for symbol, state in states.items():
        cluster_amount = float(state["cluster_amount"])
        impact_threshold = cluster_amount * 0.001
        members = {
            key[2] for key in state["cluster_by_member"]
        }
        for record in state["records"]:
            amount = float(record["amount"])
            high_impact = amount >= impact_threshold
            for field, count_key, amount_key, max_key in (
                ("from_address", "sent_transfer_count", "sent_amount", "max_sent_amount"),
                ("to_address", "received_transfer_count", "received_amount", "max_received_amount"),
            ):
                address = record[field]
                key = (symbol, record["chain"], address)
                row = addresses.setdefault(key, address_row(*key))
                row[count_key] += 1
                row[amount_key] += amount
                row[max_key] = max(float(row[max_key]), amount)
                row["is_cluster_member"] = address in members
                row["high_impact_transfer_count"] += int(high_impact)
                update_date(row, record["day"])
                note_metadata(
                    builder,
                    row,
                    state["address_metadata"].get(builder.endpoint_key(record, field), {}),
                )

            if high_impact:
                context = builder.transfer_context(state, record)
                paths.append(
                    {
                        "symbol": symbol,
                        "date": record["day"].isoformat(),
                        "chain": record["chain"],
                        "token_address": record["token_address"],
                        "amount": amount,
                        "cluster_amount": cluster_amount,
                        "cluster_share_pct": amount / cluster_amount * 100,
                        "from_address": record["from_address"],
                        "to_address": record["to_address"],
                        "tx_hash": record["tx_hash"],
                        "local_structure": context["structure"],
                        "local_direction": context["direction"],
                        "arkham_path_status": "pending",
                        "minimum_cex_hops": "",
                        "cex_destination": "",
                        "cex_amount": "",
                        "boundary_tx_hash": "",
                        "evidence_path_tx_hashes": "",
                        "reviewed_at": "",
                        "notes": "",
                    }
                )
    return list(addresses.values()), paths


def serialize(row: dict[str, Any]) -> dict[str, Any]:
    output = dict(row)
    for key in ("first_transfer_date", "last_transfer_date"):
        if output.get(key) is not None:
            output[key] = output[key].isoformat()
    for key in ("local_labels", "local_entities"):
        output[key] = "; ".join(sorted(output[key]))
    for key in ("is_cluster_member", "local_is_cex"):
        output[key] = str(bool(output[key])).lower()
    return output


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        raise ValueError(f"no rows produced for {path}")
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--snapshot", type=Path, default=ROOT / "bubblemaps-snapshot")
    parser.add_argument("--config", type=Path, default=ROOT / "expanded_universe_config.json")
    parser.add_argument("--output-dir", type=Path, default=ROOT / "arkham-review")
    parser.add_argument(
        "--allow-missing-targets",
        action="store_true",
        help=(
            "Build from available snapshots and report missing configured targets "
            "instead of failing. Intended for staged PG-first imports."
        ),
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    builder = load_builder()
    config = __import__("json").loads(args.config.read_text(encoding="utf-8"))
    configured_symbols = tuple(config["symbols"])
    missing_snapshot_symbols = tuple(
        symbol
        for symbol in configured_symbols
        if not (
            (args.snapshot / "clean" / symbol / "holders.json").is_file()
            and (args.snapshot / "clean" / symbol / "relationships.json").is_file()
        )
    )
    symbols = (
        tuple(
            symbol
            for symbol in configured_symbols
            if symbol not in set(missing_snapshot_symbols)
        )
        if args.allow_missing_targets
        else configured_symbols
    )
    if args.allow_missing_targets and missing_snapshot_symbols:
        print(
            f"warning: skipped {len(missing_snapshot_symbols)} configured symbols "
            "without usable snapshots",
            flush=True,
        )
    states, metadata = builder.load_states(builder.load_engine(), args.snapshot, args.config, symbols)
    if metadata["missing_targets"] and not args.allow_missing_targets:
        raise ValueError(f"missing targets: {metadata['missing_targets']}")
    if metadata["missing_targets"]:
        print(
            f"warning: skipped {len(metadata['missing_targets'])} configured targets "
            "without usable snapshots",
            flush=True,
        )
    address_rows, path_rows = build(builder, states)
    address_rows = [serialize(row) for row in address_rows]
    address_rows.sort(key=lambda row: (row["chain"], row["address"], row["symbol"]))
    path_rows.sort(
        key=lambda row: (-float(row["cluster_share_pct"]), row["symbol"], row["date"])
    )
    write_csv(args.output_dir / "all-transfer-addresses.csv", address_rows)
    write_csv(args.output_dir / "high-impact-path-seeds.csv", path_rows)
    unique_addresses = {(row["chain"], row["address"]) for row in address_rows}
    local_cex = {
        (row["chain"], row["address"])
        for row in address_rows
        if row["local_is_cex"] == "true"
    }
    print(
        f"{len(address_rows)} symbol-address rows; {len(unique_addresses)} unique addresses; "
        f"{len(local_cex)} locally labeled CEX addresses; {len(path_rows)} high-impact paths"
    )


if __name__ == "__main__":
    main()
