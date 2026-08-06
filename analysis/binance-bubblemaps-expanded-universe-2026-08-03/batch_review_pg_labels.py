#!/usr/bin/env python3
"""Batch-review all PG-unchecked high-impact transfer endpoints.

The queue remains conservative: a PostgreSQL row is CEX only when the stored
flag is true or the label has explicit exchange-boundary semantics such as a
deposit address, hot/cold wallet, or Prime custody.  Merely being associated
with an exchange entity (for example an airdrop distribution wallet) is not
enough to enter the CEX-flow calculation.
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parent
QUEUE = ROOT / "arkham-review/arkham-label-queue.csv"
DEFAULT_STATE = Path("/private/tmp/pg_checked_addresses.json")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=0, help="0 means all unchecked rows")
    parser.add_argument("--state", type=Path, default=DEFAULT_STATE)
    parser.add_argument(
        "--recheck-pending",
        action="store_true",
        help="Re-query every pending row even if a previous PG lookup had no hit.",
    )
    parser.add_argument(
        "--include-api-unlabeled",
        action="store_true",
        help="Re-query high-impact rows previously left unknown by Arkham API.",
    )
    return parser.parse_args()


def read_queue() -> tuple[list[dict[str, str]], list[str]]:
    with QUEUE.open(encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        rows = list(reader)
        return rows, list(reader.fieldnames or [])


def write_queue(rows: list[dict[str, str]], fieldnames: list[str]) -> None:
    temporary = QUEUE.with_suffix(".csv.tmp")
    with temporary.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
    temporary.replace(QUEUE)


def load_checked(path: Path) -> set[str]:
    if not path.is_file():
        return set()
    return set(json.loads(path.read_text(encoding="utf-8")))


def write_checked(path: Path, checked: set[str]) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(sorted(checked), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def fetch_labels(addresses: list[dict[str, str]]) -> list[dict]:
    sys.path.insert(0, "/Users/rayer/Documents/Factor_Factory")
    from factor_factory.adapters.sql import build_postgresql_engine
    from sqlalchemy import text

    by_chain: dict[str, list[str]] = {}
    for item in addresses:
        by_chain.setdefault(item["chain"], []).append(item["address"])
    engine = build_postgresql_engine()
    rows: list[dict] = []
    try:
        with engine.connect() as connection:
            for chain, chain_addresses in by_chain.items():
                result = connection.execute(
                    text(
                        "SELECT DISTINCT ON (lower(address)) * "
                        "FROM public.address_entity_label_snapshot "
                        "WHERE lower(chain)=:chain AND lower(address)=ANY(:addresses) "
                        "AND (entity_name IS NOT NULL OR address_label IS NOT NULL "
                        " OR entity_type NOT IN ('unknown','cluster_member') "
                        " OR is_cex IS TRUE OR is_dex IS TRUE OR is_bridge IS TRUE) "
                        "ORDER BY lower(address), observed_at DESC NULLS LAST"
                    ),
                    {"chain": chain, "addresses": chain_addresses},
                ).mappings()
                rows.extend(dict(row) for row in result)
    finally:
        engine.dispose()
    return rows


def explicit_cex_semantics(row: dict) -> bool:
    text = " ".join(
        str(row.get(field) or "")
        for field in ("entity_name", "address_label", "entity_type")
    ).lower()
    boundary_terms = (
        " deposit",
        "deposit ",
        "hot wallet",
        "cold wallet",
        "prime custody",
        "exchange wallet",
        "cex wallet",
    )
    return any(term in f" {text} " for term in boundary_terms)


def classify(row: dict) -> tuple[str, str, bool, str]:
    entity = str(row.get("entity_name") or "").strip()
    label = str(row.get("address_label") or "").strip()
    entity_type = str(row.get("entity_type") or "").strip().lower()
    if not entity:
        entity = "Contract" if entity_type == "contract" else entity_type or "Unknown"
    if not label:
        label = "Contract" if entity_type == "contract" else entity
    stored_cex = bool(row.get("is_cex"))
    semantic_cex = explicit_cex_semantics(row)
    is_cex = stored_cex or semantic_cex
    reason = "stored is_cex=true" if stored_cex else (
        "explicit CEX-boundary label override" if semantic_cex else "non-CEX label retained"
    )
    return entity, label, is_cex, reason


def main() -> None:
    args = parse_args()
    queue, fieldnames = read_queue()
    checked = load_checked(args.state)
    statuses = {"pending_web_review"}
    if args.include_api_unlabeled:
        statuses.add("reviewed_arkham_api_unlabeled")
    candidates = [
        row
        for row in queue
        if row.get("arkham_status") in statuses
        and int(float(row.get("high_impact_transfer_count") or 0)) > 0
        and (
            row.get("arkham_status") == "reviewed_arkham_api_unlabeled"
            or args.recheck_pending
            or f"{row['chain'].lower()}:{row['address'].lower()}" not in checked
        )
    ]
    if args.limit > 0:
        candidates = candidates[: args.limit]
    requests = [
        {"chain": row["chain"].lower(), "address": row["address"].lower()}
        for row in candidates
    ]
    labels = fetch_labels(requests) if requests else []
    by_key = {
        (str(row["chain"]).lower(), str(row["address"]).lower()): row for row in labels
    }
    reviewed_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    hits: list[dict] = []
    for queue_row in candidates:
        key = (queue_row["chain"].lower(), queue_row["address"].lower())
        checked.add(f"{key[0]}:{key[1]}")
        label_row = by_key.get(key)
        if not label_row:
            continue
        entity, label, is_cex, reason = classify(label_row)
        observed = str(label_row.get("observed_at") or "")
        source = str(label_row.get("source") or "")
        evidence = (
            f"PostgreSQL address_entity_label_snapshot id={label_row.get('id')}; "
            f"source={source}; observed_at={observed}"
        )
        queue_row.update(
            {
                "arkham_status": "reviewed_pg_snapshot",
                "arkham_entity": entity,
                "arkham_label": label,
                "arkham_is_cex": str(is_cex).lower(),
                "arkham_reviewed_at": reviewed_at,
                "minimum_cex_hops": "0" if is_cex else "",
                "cex_destination": label if is_cex else "",
                "evidence": evidence,
                "notes": (
                    f"entity_type={label_row.get('entity_type')}; "
                    f"confidence={label_row.get('confidence')}; {reason}."
                ),
            }
        )
        hits.append(
            {
                "chain": key[0],
                "address": key[1],
                "symbols": queue_row.get("symbols", ""),
                "entity": entity,
                "label": label,
                "is_cex": is_cex,
                "reason": reason,
            }
        )
    if candidates:
        write_checked(args.state, checked)
    backup = ""
    if hits:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        backup_path = QUEUE.with_name(f"{QUEUE.stem}.{stamp}.bak{QUEUE.suffix}")
        backup_path.write_bytes(QUEUE.read_bytes())
        backup = str(backup_path)
        write_queue(queue, fieldnames)
    status_counts = Counter(row.get("arkham_status", "") for row in queue)
    confirmed_cex = sum(row.get("arkham_is_cex", "").lower() == "true" for row in queue)
    print(
        json.dumps(
            {
                "processed": len(candidates),
                "pg_hits": len(hits),
                "cex_hits": [hit for hit in hits if hit["is_cex"]],
                "non_cex_hits": len([hit for hit in hits if not hit["is_cex"]]),
                "pending": status_counts["pending_web_review"],
                "reviewed_pg_snapshot": status_counts["reviewed_pg_snapshot"],
                "confirmed_cex": confirmed_cex,
                "backup": backup,
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
