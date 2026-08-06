#!/usr/bin/env python3
"""Capture resumable Bubblemaps data for explicit out-of-sample targets."""

from __future__ import annotations

import argparse
import asyncio
import json
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parent
PROJECT_ROOT = ROOT.parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from getMarket.bubblemaps.tool.bubblemaps_api import BubblemapsApiClient
from getMarket.bubblemaps.tool.export_bubblemaps_market import (
    clean_holders,
    clean_relationships,
    clean_transfers,
    ordinary_cluster_members,
)
from getMarket.bubblemaps.tool.market_identity import make_target
from getMarket.bubblemaps.tool.market_transform import (
    SnapshotModel,
    filter_subgraph_edges,
    parse_ranked_holders,
    reconstruct_clusters,
    token_snapshot_fingerprint,
)


CONFIG_PATH = ROOT / "out_of_sample_config.json"
SNAPSHOT_ROOT = ROOT / "bubblemaps-snapshot"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def holder_index(cleaned: list[dict], target) -> dict:
    holders = parse_ranked_holders(cleaned, target=target)
    return {holder.address: holder for holder in holders}


def build_snapshot(
    target,
    holders_clean: list[dict],
    relationships_clean: list[dict],
) -> SnapshotModel:
    holders = parse_ranked_holders(holders_clean, target=target)
    edges = filter_subgraph_edges(
        relationships_clean,
        target=target,
        holders={holder.address: holder for holder in holders},
    )
    clusters = reconstruct_clusters(holders, edges)
    return SnapshotModel(
        target=target,
        holders=holders,
        edges=edges,
        clusters=clusters,
        fingerprint=token_snapshot_fingerprint(holders, edges),
        captured_at=utc_now(),
    )


def cluster_document(snapshot: SnapshotModel, symbol: str) -> dict:
    clusters = []
    for cluster in snapshot.clusters:
        members = []
        for holder in cluster.members:
            member = {
                "address": holder.address,
                "amount": holder.amount,
                "share": holder.share,
                "share_percent": holder.share_percent,
                "source_rank": holder.source_rank,
                "is_supernode": holder.is_supernode,
            }
            if not holder.is_supernode:
                member["transfer_file"] = (
                    f"clean/{symbol}/transfers/{holder.address}.json"
                )
            members.append(member)
        clusters.append(
            {
                "cluster_rank": cluster.cluster_rank,
                "amount": cluster.amount,
                "share": cluster.share,
                "share_percent": cluster.share_percent,
                "member_count": len(members),
                "members": members,
            }
        )
    return {
        "schema_version": "out-of-sample-v1",
        "chain": snapshot.target.requested_chain,
        "token_address": snapshot.target.requested_token_address,
        "canonical_chain": snapshot.target.chain,
        "canonical_token_address": snapshot.target.token_address,
        "captured_at": snapshot.captured_at,
        "clusters": clusters,
    }


async def capture_symbol(
    client: BubblemapsApiClient,
    symbol: str,
    chain: str,
    address: str,
    concurrency: int,
    snapshot_root: Path,
) -> dict:
    target = make_target(chain, address)
    symbol_root = snapshot_root / "clean" / symbol
    holders_path = symbol_root / "holders.json"
    relationships_path = symbol_root / "relationships.json"

    holders_result = await client.top_holders(target)
    holders_clean = clean_holders(holders_result, target)
    write_json(holders_path, holders_clean)
    relationships_result = await client.subgraph(
        target, [row["address"] for row in holders_clean]
    )
    relationships_clean = clean_relationships(
        relationships_result, target, holders_clean
    )
    write_json(relationships_path, relationships_clean)
    snapshot = build_snapshot(target, holders_clean, relationships_clean)
    members = ordinary_cluster_members(snapshot)
    semaphore = asyncio.Semaphore(concurrency)
    progress_lock = asyncio.Lock()
    processed = 0
    completed = 0
    errors: list[dict] = []

    async def capture_member(member: str) -> None:
        nonlocal processed, completed
        transfer_path = symbol_root / "transfers" / f"{member}.json"
        if transfer_path.is_file():
            try:
                existing = json.loads(transfer_path.read_text(encoding="utf-8"))
                if (
                    isinstance(existing, dict)
                    and isinstance(existing.get("transfers"), list)
                    and existing.get("member_address") == member
                ):
                    async with progress_lock:
                        processed += 1
                        completed += 1
                        if processed % 20 == 0 or processed == len(members):
                            print(
                                f"{symbol}: {processed}/{len(members)} members "
                                f"processed; {completed} available; "
                                f"{len(errors)} errors",
                                flush=True,
                            )
                    return
            except (OSError, UnicodeError, json.JSONDecodeError):
                pass
        try:
            async with semaphore:
                result = await client.transfers(target, member)
            cleaned = clean_transfers(result, target, member, snapshot)
            write_json(
                transfer_path,
                {
                    "schema_version": "out-of-sample-v1",
                    "chain": target.requested_chain,
                    "token_address": target.requested_token_address,
                    "canonical_chain": target.chain,
                    "canonical_token_address": target.token_address,
                    "member_address": member,
                    "transfers": cleaned,
                    "transfer_count": len(cleaned),
                },
            )
            success = True
        except Exception as error:
            success = False
            failure = {
                "member_address": member,
                "type": type(error).__name__,
                "message": "capture failed",
            }
        async with progress_lock:
            processed += 1
            if success:
                completed += 1
            else:
                errors.append(failure)
            if processed % 20 == 0 or processed == len(members):
                print(
                    f"{symbol}: {processed}/{len(members)} members processed; "
                    f"{completed} available; {len(errors)} errors",
                    flush=True,
                )

    await asyncio.gather(*(capture_member(member) for member in members))

    token_path = snapshot_root / "data" / symbol / "token.json"
    write_json(token_path, cluster_document(snapshot, symbol))
    unique = set()
    for member in members:
        path = symbol_root / "transfers" / f"{member}.json"
        if not path.is_file():
            continue
        document = json.loads(path.read_text(encoding="utf-8"))
        for row in document.get("transfers", []):
            data = row["data"]
            unique.add(
                (
                    data["tx_hash"],
                    row["from_address"].lower(),
                    row["to_address"].lower(),
                    str(data["value"]),
                    int(data["date"]),
                )
            )
    return {
        "requested_chain": target.requested_chain,
        "requested_token_address": target.requested_token_address,
        "canonical_chain": target.chain,
        "canonical_token_address": target.token_address,
        "captured_at": snapshot.captured_at,
        "token_file": f"data/{symbol}/token.json",
        "ranked_holder_count": len(snapshot.holders),
        "cluster_count": len(snapshot.clusters),
        "ordinary_member_count": len(members),
        "supernode_count": sum(
            holder.is_supernode
            for cluster in snapshot.clusters
            for holder in cluster.members
        ),
        "unique_transfer_count": len(unique),
        "available_member_count": completed,
        "transfer_error_count": len(errors),
        "errors": errors,
        "status": "success" if not errors else "partial_success",
    }


async def run(args: argparse.Namespace) -> None:
    configuration = json.loads(args.config.read_text(encoding="utf-8"))
    client = BubblemapsApiClient(
        timeout=args.timeout,
        max_attempts=args.max_attempts,
        retry_delay=args.retry_delay,
        min_request_interval=args.min_interval,
    )
    entries = []
    for symbol, spec in configuration["symbols"].items():
        targets = [
            (chain, address)
            for chain, addresses in spec["targets"].items()
            for address in addresses
        ]
        if len(targets) != 1:
            raise ValueError("focused sample expects exactly one target per symbol")
        chain, address = targets[0]
        print(f"{symbol}: capture started", flush=True)
        entries.append(
            await capture_symbol(
                client,
                symbol,
                chain,
                address,
                args.concurrency,
                args.snapshot_root,
            )
        )
    captured_at = utc_now()
    manifest = {
        "schema_version": "out-of-sample-v1",
        "source": "bubblemaps_api",
        "status": (
            "success"
            if all(entry["status"] == "success" for entry in entries)
            else "partial_success"
        ),
        "captured_at": captured_at,
        "business_date": captured_at[:10],
        "tokens": entries,
        "skipped_tokens": [],
    }
    write_json(args.snapshot_root / "manifest.json", manifest)
    grouped_targets: dict[str, list[str]] = {}
    for spec in configuration["symbols"].values():
        for chain, addresses in spec["targets"].items():
            grouped_targets.setdefault(chain, []).extend(addresses)
    write_json(
        args.snapshot_root / "targets.json",
        {
            chain: sorted(set(addresses))
            for chain, addresses in sorted(grouped_targets.items())
        },
    )
    print(f"wrote {args.snapshot_root / 'manifest.json'}", flush=True)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, default=CONFIG_PATH)
    parser.add_argument("--snapshot-root", type=Path, default=SNAPSHOT_ROOT)
    parser.add_argument("--timeout", type=float, default=30)
    parser.add_argument("--max-attempts", type=int, default=4)
    parser.add_argument("--retry-delay", type=float, default=1)
    parser.add_argument("--min-interval", type=float, default=0.8)
    parser.add_argument("--concurrency", type=int, default=4)
    return parser.parse_args()


if __name__ == "__main__":
    asyncio.run(run(parse_args()))
