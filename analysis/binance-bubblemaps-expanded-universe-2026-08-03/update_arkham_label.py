#!/usr/bin/env python3
"""Update one reviewed address in the auditable Arkham label queue."""

from __future__ import annotations

import argparse
import csv
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parent
QUEUE = ROOT / "arkham-review/arkham-label-queue.csv"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--chain", required=True)
    parser.add_argument("--address", required=True)
    parser.add_argument("--symbol", default="")
    parser.add_argument("--entity", required=True)
    parser.add_argument("--label", required=True)
    parser.add_argument("--is-cex", choices=("true", "false", "unknown"), required=True)
    parser.add_argument("--evidence", required=True)
    parser.add_argument(
        "--status",
        choices=(
            "reviewed_web",
            "reviewed_web_unlabeled",
            "reviewed_pg_snapshot",
            "reviewed_arkham_api",
            "reviewed_arkham_api_unlabeled",
            "confirmed_from_local_metadata",
        ),
        default="",
        help="Optional auditable review source/status override.",
    )
    parser.add_argument("--notes", default="")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    with QUEUE.open(encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))
        fieldnames = list(rows[0])
    matches = [
        row for row in rows
        if row["chain"] == args.chain and row["address"].lower() == args.address.lower()
    ]
    if len(matches) > 1:
        raise ValueError(f"expected one queue row, found {len(matches)}")
    if matches:
        row = matches[0]
    else:
        row = {field: "" for field in fieldnames}
        row.update(
            {
                "chain": args.chain,
                "address": args.address.lower(),
                "symbols": args.symbol,
                "path_count": "0",
                "max_cluster_share_pct": "0",
                "total_path_amount": "0",
                "local_is_cex": "false",
                "high_impact_transfer_count": "0",
            }
        )
        rows.append(row)
    is_known = args.is_cex != "unknown"
    cex_value = args.is_cex if is_known else ""
    status = args.status or ("reviewed_web" if is_known else "reviewed_web_unlabeled")
    row.update(
        {
            "arkham_status": status,
            "arkham_entity": args.entity,
            "arkham_label": args.label,
            "arkham_is_cex": cex_value,
            "arkham_reviewed_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "minimum_cex_hops": "0" if args.is_cex == "true" else "",
            "cex_destination": args.label if args.is_cex == "true" else "",
            "evidence": args.evidence,
            "notes": args.notes,
        }
    )
    temporary = QUEUE.with_suffix(".csv.tmp")
    with temporary.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
    temporary.replace(QUEUE)
    print(f"updated {args.chain}:{args.address} -> {args.label}; CEX={args.is_cex}")


if __name__ == "__main__":
    main()
