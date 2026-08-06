#!/usr/bin/env python3
"""Append or update an auditable Arkham multi-hop path review."""

from __future__ import annotations

import argparse
import csv
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parent
OUTPUT = ROOT / "arkham-review/multihop-path-reviews.csv"
FIELDS = [
    "symbol",
    "chain",
    "token_address",
    "root_date",
    "root_amount",
    "root_from_address",
    "root_to_address",
    "root_tx_hash",
    "status",
    "max_hops_reviewed",
    "boundary_date",
    "boundary_timestamp_ms",
    "direction",
    "cex_amount",
    "cex_address",
    "cex_label",
    "boundary_tx_hash",
    "boundary_event_id",
    "path_addresses",
    "path_tx_hashes",
    "reviewed_at",
    "source",
    "notes",
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    for field in (
        "symbol",
        "chain",
        "token-address",
        "root-date",
        "root-amount",
        "root-from-address",
        "root-to-address",
        "root-tx-hash",
        "status",
        "path-addresses",
        "path-tx-hashes",
        "notes",
    ):
        parser.add_argument(f"--{field}", required=True)
    parser.add_argument("--max-hops-reviewed", type=int, default=3)
    parser.add_argument("--cex-amount", default="0")
    parser.add_argument("--cex-address", default="")
    parser.add_argument("--cex-label", default="")
    parser.add_argument("--boundary-date", default="")
    parser.add_argument("--boundary-timestamp-ms", default="")
    parser.add_argument("--direction", choices=("", "流入CEX", "流出CEX"), default="")
    parser.add_argument("--boundary-tx-hash", default="")
    parser.add_argument("--boundary-event-id", default="")
    parser.add_argument("--source", default="Arkham web")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    rows = []
    if OUTPUT.is_file() and OUTPUT.stat().st_size:
        with OUTPUT.open(encoding="utf-8", newline="") as handle:
            rows = list(csv.DictReader(handle))
    key = (args.symbol.upper(), args.chain, args.root_tx_hash.lower())
    new_row = {
        "symbol": args.symbol.upper(),
        "chain": args.chain,
        "token_address": args.token_address,
        "root_date": args.root_date,
        "root_amount": args.root_amount,
        "root_from_address": args.root_from_address,
        "root_to_address": args.root_to_address,
        "root_tx_hash": args.root_tx_hash,
        "status": args.status,
        "max_hops_reviewed": args.max_hops_reviewed,
        "boundary_date": args.boundary_date,
        "boundary_timestamp_ms": args.boundary_timestamp_ms,
        "direction": args.direction,
        "cex_amount": args.cex_amount,
        "cex_address": args.cex_address,
        "cex_label": args.cex_label,
        "boundary_tx_hash": args.boundary_tx_hash,
        "boundary_event_id": args.boundary_event_id,
        "path_addresses": args.path_addresses,
        "path_tx_hashes": args.path_tx_hashes,
        "reviewed_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "source": args.source,
        "notes": args.notes,
    }
    replaced = False
    for index, row in enumerate(rows):
        row_key = (row["symbol"].upper(), row["chain"], row["root_tx_hash"].lower())
        if row_key == key:
            rows[index] = new_row
            replaced = True
            break
    if not replaced:
        rows.append(new_row)
    rows.sort(key=lambda row: (row["symbol"], row["root_date"], row["root_tx_hash"]))
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    temporary = OUTPUT.with_suffix(".csv.tmp")
    with temporary.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows(rows)
    temporary.replace(OUTPUT)
    print(f"recorded {key}: {args.status}; CEX amount={args.cex_amount}")


if __name__ == "__main__":
    main()
