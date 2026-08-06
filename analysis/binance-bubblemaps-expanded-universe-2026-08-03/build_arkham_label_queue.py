#!/usr/bin/env python3
"""Build an Arkham review queue from high-impact transfer endpoints.

Only endpoints appearing in ``high-impact-path-seeds.csv`` are eligible for
new review. Previously reviewed low-impact endpoints remain in the file as an
audit trail, but no new low-impact address is added to the pending queue.
"""

from __future__ import annotations

import csv
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parent
REVIEW_ROOT = ROOT / "arkham-review"


def read_rows(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def write_rows(path: Path, rows: list[dict]) -> None:
    if not rows:
        raise ValueError("Arkham queue is empty")
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def main() -> None:
    address_rows = read_rows(REVIEW_ROOT / "all-transfer-addresses.csv")
    path_rows = read_rows(REVIEW_ROOT / "high-impact-path-seeds.csv")
    queue_path = REVIEW_ROOT / "arkham-label-queue.csv"
    existing_reviews = {}
    if queue_path.is_file():
        existing_reviews = {
            (row["chain"], row["address"].lower()): row
            for row in read_rows(queue_path)
            if row.get("arkham_status", "")
            and row.get("arkham_status", "") != "pending_web_review"
        }
    queue = defaultdict(
        lambda: {
            "symbols": set(),
            "path_count": 0,
            "max_cluster_share_pct": 0.0,
            "total_path_amount": 0.0,
            "roles": set(),
            "local_labels": set(),
            "local_entities": set(),
            "local_is_cex": False,
            "high_impact_transfer_count": 0,
        }
    )
    for path in path_rows:
        for field, role in (("from_address", "from"), ("to_address", "to")):
            address = path[field]
            key = (path["chain"], address)
            row = queue[key]
            row["symbols"].add(path["symbol"])
            row["path_count"] += 1
            row["max_cluster_share_pct"] = max(
                row["max_cluster_share_pct"], float(path["cluster_share_pct"])
            )
            row["total_path_amount"] += float(path["amount"])
            row["roles"].add(role)

    # Enrich high-impact endpoints with inventory metadata. Keep previously
    # reviewed low-impact rows only so earlier manual work remains auditable.
    for metadata in address_rows:
        key = (metadata["chain"], metadata["address"])
        if key not in queue and (key[0], key[1].lower()) not in existing_reviews:
            continue
        row = queue[key]
        row["symbols"].add(metadata["symbol"])
        if int(metadata.get("sent_transfer_count") or 0):
            row["roles"].add("from")
        if int(metadata.get("received_transfer_count") or 0):
            row["roles"].add("to")
        row["local_labels"].update(
            item.strip()
            for item in metadata.get("local_labels", "").split(";")
            if item.strip()
        )
        row["local_entities"].update(
            item.strip()
            for item in metadata.get("local_entities", "").split(";")
            if item.strip()
        )
        row["local_is_cex"] = (
            row["local_is_cex"] or metadata.get("local_is_cex") == "true"
        )
        row["high_impact_transfer_count"] += int(
            metadata.get("high_impact_transfer_count") or 0
        )

    # Preserve every completed result even when a refreshed transfer snapshot
    # removes the address from both the current inventory and high-impact set.
    # Such rows remain with path_count=0 as an audit trail and are never put
    # back into the pending queue.
    active_keys = {(chain, address.lower()) for chain, address in queue}
    for normalized_key, reviewed in existing_reviews.items():
        if normalized_key in active_keys:
            continue
        chain, _ = normalized_key
        address = reviewed["address"]
        row = queue[(chain, address)]
        row["symbols"].update(
            item.strip() for item in reviewed.get("symbols", "").split(";") if item.strip()
        )
        row["max_cluster_share_pct"] = float(
            reviewed.get("max_cluster_share_pct") or 0
        )
        row["total_path_amount"] = float(reviewed.get("total_path_amount") or 0)
        row["roles"].update(
            item.strip() for item in reviewed.get("roles", "").split(";") if item.strip()
        )
        row["local_labels"].update(
            item.strip()
            for item in reviewed.get("local_labels", "").split(";")
            if item.strip()
        )
        row["local_entities"].update(
            item.strip()
            for item in reviewed.get("local_entities", "").split(";")
            if item.strip()
        )
        row["local_is_cex"] = reviewed.get("local_is_cex") == "true"
        row["high_impact_transfer_count"] = int(
            reviewed.get("high_impact_transfer_count") or 0
        )

    output = []
    reviewed_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    for (chain, address), row in queue.items():
        local_is_cex = bool(row["local_is_cex"])
        is_zero_address = address.lower() == "0x0000000000000000000000000000000000000000"
        if is_zero_address:
            status = "confirmed_system_address"
            entity = "System"
            label = "Zero Address (mint/burn)"
            arkham_is_cex = "false"
            evidence = "deterministic zero-address classification"
        elif local_is_cex:
            status = "confirmed_from_local_metadata"
            entity = "; ".join(sorted(row["local_entities"]))
            label = "; ".join(sorted(row["local_labels"]))
            arkham_is_cex = "true"
            evidence = "Bubblemaps holder metadata"
        else:
            status = "pending_web_review"
            entity = ""
            label = ""
            arkham_is_cex = ""
            evidence = ""
        output_row = {
                "chain": chain,
                "address": address,
                "symbols": "; ".join(sorted(row["symbols"])),
                "path_count": row["path_count"],
                "max_cluster_share_pct": round(row["max_cluster_share_pct"], 8),
                "total_path_amount": round(row["total_path_amount"], 8),
                "roles": "; ".join(sorted(row["roles"])),
                "local_labels": "; ".join(sorted(row["local_labels"])),
                "local_entities": "; ".join(sorted(row["local_entities"])),
                "local_is_cex": str(local_is_cex).lower(),
                "high_impact_transfer_count": row["high_impact_transfer_count"],
                "arkham_status": status,
                "arkham_entity": entity,
                "arkham_label": label,
                "arkham_is_cex": arkham_is_cex,
                "arkham_reviewed_at": reviewed_at if local_is_cex else "",
                "minimum_cex_hops": "0" if local_is_cex else "",
                "cex_destination": label if local_is_cex else "",
                "evidence": evidence,
                "notes": "",
            }
        reviewed = existing_reviews.get((chain, address.lower()))
        if reviewed:
            for field in (
                "arkham_status",
                "arkham_entity",
                "arkham_label",
                "arkham_is_cex",
                "arkham_reviewed_at",
                "minimum_cex_hops",
                "cex_destination",
                "evidence",
                "notes",
            ):
                output_row[field] = reviewed.get(field, "")
        output.append(output_row)
    output.sort(
        key=lambda row: (
            row["local_is_cex"] == "true",
            row["path_count"] == 0,
            -float(row["max_cluster_share_pct"]),
            -int(row["high_impact_transfer_count"]),
            row["chain"],
            row["address"],
        )
    )
    write_rows(queue_path, output)
    print(
        f"{len(output)} queued/audited endpoint addresses; "
        f"{sum(int(row['path_count']) > 0 for row in output)} high-impact endpoints; "
        f"{sum(int(row['path_count']) == 0 for row in output)} retained reviewed low-impact endpoints; "
        f"{sum(row['local_is_cex'] == 'true' for row in output)} already CEX-labeled; "
        f"{sum(row['arkham_status'] == 'pending_web_review' for row in output)} pending Arkham review"
    )


if __name__ == "__main__":
    main()
