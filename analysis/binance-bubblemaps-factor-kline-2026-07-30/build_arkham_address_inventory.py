#!/usr/bin/env python3
"""Build auditable Arkham address and path-review queues for all samples."""

from __future__ import annotations

import csv
import importlib.util
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parent
BUILDER_PATH = ROOT / "build_dashboard.py"
ADDRESS_OUTPUT = ROOT / "arkham-all-transfer-addresses.csv"
PATH_OUTPUT = ROOT / "arkham-high-impact-path-seeds.csv"


def load_builder():
    spec = importlib.util.spec_from_file_location("factor_dashboard", BUILDER_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError("dashboard builder unavailable")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def load_states(builder) -> dict[str, dict[str, Any]]:
    engine = builder.load_engine()
    groups = (
        builder.load_states(
            engine,
            builder.IN_SAMPLE_SNAPSHOT,
            builder.IN_SAMPLE_CONFIG,
            builder.IN_SAMPLE_SYMBOLS,
        )[0],
        builder.load_states(
            engine,
            builder.OUT_SAMPLE_SNAPSHOT,
            builder.OUT_SAMPLE_CONFIG,
            builder.OUT_SAMPLE_SYMBOLS,
        )[0],
        builder.load_states(
            engine,
            builder.ADDITIONAL_SAMPLE_SNAPSHOT,
            builder.ADDITIONAL_SAMPLE_CONFIG,
            builder.ADDITIONAL_SAMPLE_SYMBOLS,
        )[0],
    )
    return {symbol: state for group in groups for symbol, state in group.items()}


def sample_group(builder, symbol: str) -> str:
    if symbol in builder.IN_SAMPLE_SYMBOLS:
        return "样本内"
    return "样本外"


def initial_address_row(symbol: str, group: str, chain: str, address: str) -> dict[str, Any]:
    return {
        "symbol": symbol,
        "sample_group": group,
        "chain": chain,
        "address": address,
        "sent_transfer_count": 0,
        "received_transfer_count": 0,
        "max_sent_amount": 0.0,
        "max_received_amount": 0.0,
        "first_transfer_date": None,
        "last_transfer_date": None,
        "local_labels": set(),
        "local_entities": set(),
        "local_is_cex": False,
        "high_impact_transfer_count": 0,
        "arkham_status": "pending_web_review",
        "arkham_entity": "",
        "arkham_label": "",
        "arkham_is_cex": "",
        "arkham_reviewed_at": "",
        "path_status": "pending",
        "minimum_cex_hops": "",
        "cex_destination": "",
        "evidence_tx_hashes": "",
        "notes": "",
    }


def note_metadata(builder, row: dict[str, Any], metadata: dict[str, Any]) -> None:
    label = str(metadata.get("label") or "").strip()
    entity = str(metadata.get("entity_id") or "").strip()
    if label:
        row["local_labels"].add(label)
    if entity:
        row["local_entities"].add(entity)
    if builder.endpoint_type(metadata) == "CEX":
        row["local_is_cex"] = True


def update_dates(row: dict[str, Any], day) -> None:
    if row["first_transfer_date"] is None or day < row["first_transfer_date"]:
        row["first_transfer_date"] = day
    if row["last_transfer_date"] is None or day > row["last_transfer_date"]:
        row["last_transfer_date"] = day


def build_queues(builder, states):
    addresses: dict[tuple[str, str, str], dict[str, Any]] = {}
    path_rows = []

    for symbol, state in states.items():
        group = sample_group(builder, symbol)
        cluster_amount = float(state["cluster_amount"])
        impact_threshold = cluster_amount * 0.001
        for record in state["records"]:
            amount = float(record["amount"])
            high_impact = amount >= impact_threshold
            for field, count_field, max_field in (
                ("from_address", "sent_transfer_count", "max_sent_amount"),
                ("to_address", "received_transfer_count", "max_received_amount"),
            ):
                address = record[field]
                key = (symbol, record["chain"], address)
                row = addresses.setdefault(
                    key,
                    initial_address_row(symbol, group, record["chain"], address),
                )
                row[count_field] += 1
                row[max_field] = max(float(row[max_field]), amount)
                row["high_impact_transfer_count"] += int(high_impact)
                update_dates(row, record["day"])
                metadata = state["address_metadata"].get(
                    builder.endpoint_key(record, field), {}
                )
                note_metadata(builder, row, metadata)

            if high_impact:
                context = builder.transfer_context(state, record)
                path_rows.append(
                    {
                        "symbol": symbol,
                        "sample_group": group,
                        "date": record["day"].isoformat(),
                        "chain": record["chain"],
                        "amount": amount,
                        "cluster_amount": cluster_amount,
                        "impact_threshold_0_1pct": impact_threshold,
                        "from_address": record["from_address"],
                        "to_address": record["to_address"],
                        "tx_hash": record["tx_hash"],
                        "local_structure": context["structure"],
                        "local_direction": context["direction"],
                        "arkham_path_status": "pending",
                        "minimum_cex_hops": "",
                        "cex_destination": "",
                        "cex_amount": "",
                        "evidence_path_tx_hashes": "",
                        "reviewed_at": "",
                        "notes": "",
                    }
                )

    return list(addresses.values()), path_rows


def serializable_address(row: dict[str, Any]) -> dict[str, Any]:
    output = dict(row)
    output["first_transfer_date"] = row["first_transfer_date"].isoformat()
    output["last_transfer_date"] = row["last_transfer_date"].isoformat()
    output["local_labels"] = "; ".join(sorted(row["local_labels"]))
    output["local_entities"] = "; ".join(sorted(row["local_entities"]))
    output["local_is_cex"] = str(bool(row["local_is_cex"])).lower()
    return output


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    if not rows:
        raise ValueError(f"no rows for {path.name}")
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def main() -> None:
    builder = load_builder()
    states = load_states(builder)
    address_rows, path_rows = build_queues(builder, states)
    serialized = [serializable_address(row) for row in address_rows]
    serialized.sort(key=lambda row: (row["symbol"], row["chain"], row["address"]))
    path_rows.sort(
        key=lambda row: (
            -float(row["amount"]) / float(row["cluster_amount"]),
            row["symbol"],
            row["date"],
            row["tx_hash"],
        )
    )
    write_csv(ADDRESS_OUTPUT, serialized)
    write_csv(PATH_OUTPUT, path_rows)
    unique_addresses = {
        (row["chain"], row["address"]) for row in serialized
    }
    print(
        f"wrote {ADDRESS_OUTPUT}: {len(serialized)} symbol-address rows, "
        f"{len(unique_addresses)} unique chain-address pairs"
    )
    print(f"wrote {PATH_OUTPUT}: {len(path_rows)} high-impact transfers")


if __name__ == "__main__":
    main()
