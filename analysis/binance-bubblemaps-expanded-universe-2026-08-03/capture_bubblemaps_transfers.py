#!/usr/bin/env python3
"""Resume Bubblemaps transfer capture with value-prioritized round-robin scheduling."""

from __future__ import annotations

import argparse
import asyncio
import importlib.util
import json
from collections import defaultdict
from pathlib import Path


ROOT = Path(__file__).resolve().parent
PROJECT_ROOT = ROOT.parents[1]
CAPTURE_PATH = (
    PROJECT_ROOT
    / "analysis/binance-bubblemaps-out-of-sample-2026-07-30/capture_bubblemaps.py"
)


def load_capture():
    spec = importlib.util.spec_from_file_location("capture_bubblemaps", CAPTURE_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError("capture module unavailable")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def load_valid_transfer(path: Path, member: str):
    if not path.is_file():
        return None
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    if document.get("member_address") != member:
        return None
    rows = document.get("transfers")
    return rows if isinstance(rows, list) else None


def transfer_identity(row: dict) -> tuple:
    data = row["data"]
    return (
        str(data["tx_hash"]).lower(),
        str(row["from_address"]).lower(),
        str(row["to_address"]).lower(),
        str(data["value"]),
        int(data["date"]),
    )


def checkpoint(capture, root: Path, states: dict, captured_at: str) -> None:
    entries = []
    for symbol, state in states.items():
        available = len(state["available"])
        total = len(state["members"])
        errors = state["errors"]
        entries.append(
            {
                "requested_chain": state["target"].requested_chain,
                "requested_token_address": state["target"].requested_token_address,
                "canonical_chain": state["target"].chain,
                "canonical_token_address": state["target"].token_address,
                "captured_at": captured_at,
                "token_file": f"data/{symbol}/token.json",
                "ranked_holder_count": len(state["snapshot"].holders),
                "cluster_count": len(state["snapshot"].clusters),
                "ordinary_member_count": total,
                "supernode_count": sum(
                    holder.is_supernode
                    for cluster in state["snapshot"].clusters
                    for holder in cluster.members
                ),
                "unique_transfer_count": len(state["unique"]),
                "available_member_count": available,
                "transfer_error_count": len(errors),
                "errors": errors[-100:],
                "status": "success" if available == total else "partial_success",
            }
        )
    status = "success" if all(row["status"] == "success" for row in entries) else "partial_success"
    capture.write_json(
        root / "manifest.json",
        {
            "schema_version": "out-of-sample-v1",
            "source": "bubblemaps_api",
            "status": status,
            "captured_at": captured_at,
            "business_date": captured_at[:10],
            "tokens": entries,
            "skipped_tokens": [],
        },
    )


async def run(args: argparse.Namespace) -> None:
    capture = load_capture()
    config = json.loads(args.config.read_text(encoding="utf-8"))
    selected = {item.upper() for item in args.symbols.split(",") if item.strip()}
    if not selected:
        selected = set(config["symbols"])
    unknown = selected - set(config["symbols"])
    if unknown:
        raise ValueError(f"unknown symbols: {sorted(unknown)}")

    states = {}
    for symbol, spec in config["symbols"].items():
        if symbol not in selected:
            continue
        [(chain, addresses)] = spec["targets"].items()
        [address] = addresses
        target = capture.make_target(chain, address)
        clean_root = args.snapshot_root / "clean" / symbol
        holders = json.loads((clean_root / "holders.json").read_text(encoding="utf-8"))
        relationships = json.loads(
            (clean_root / "relationships.json").read_text(encoding="utf-8")
        )
        snapshot = capture.build_snapshot(target, holders, relationships)
        member_amount = {
            holder.address: float(holder.amount)
            for cluster in snapshot.clusters
            for holder in cluster.members
            if not holder.is_supernode
        }
        members = sorted(member_amount, key=lambda key: (-member_amount[key], key))
        available = set()
        unique = set()
        for member in members:
            rows = load_valid_transfer(clean_root / "transfers" / f"{member}.json", member)
            if rows is None:
                continue
            available.add(member)
            unique.update(transfer_identity(row) for row in rows)
        states[symbol] = {
            "target": target,
            "snapshot": snapshot,
            "members": members,
            "available": available,
            "unique": unique,
            "errors": [],
        }

    tasks = []
    max_members = max(len(state["members"]) for state in states.values())
    for rank in range(max_members):
        for symbol, state in states.items():
            if rank >= len(state["members"]):
                continue
            member = state["members"][rank]
            if member not in state["available"]:
                tasks.append((rank + 1, symbol, member))
    if args.max_members > 0:
        tasks = tasks[: args.max_members]

    captured_at = capture.utc_now()
    checkpoint(capture, args.snapshot_root, states, captured_at)
    print(
        f"queued {len(tasks)} missing member histories across {len(states)} symbols; "
        f"already available {sum(len(s['available']) for s in states.values())}",
        flush=True,
    )
    client = capture.BubblemapsApiClient(
        timeout=args.timeout,
        max_attempts=args.max_attempts,
        retry_delay=args.retry_delay,
        min_request_interval=args.min_interval,
    )
    completed = 0
    for rank, symbol, member in tasks:
        state = states[symbol]
        path = args.snapshot_root / "clean" / symbol / "transfers" / f"{member}.json"
        try:
            result = await client.transfers(state["target"], member)
            rows = capture.clean_transfers(
                result, state["target"], member, state["snapshot"]
            )
            capture.write_json(
                path,
                {
                    "schema_version": "out-of-sample-v1",
                    "chain": state["target"].requested_chain,
                    "token_address": state["target"].requested_token_address,
                    "canonical_chain": state["target"].chain,
                    "canonical_token_address": state["target"].token_address,
                    "member_address": member,
                    "transfers": rows,
                    "transfer_count": len(rows),
                },
            )
            state["available"].add(member)
            state["unique"].update(transfer_identity(row) for row in rows)
        except Exception as error:
            state["errors"].append(
                {
                    "member_address": member,
                    "type": type(error).__name__,
                    "message": "capture failed; rerun will retry",
                }
            )
        completed += 1
        if completed % args.checkpoint_every == 0 or completed == len(tasks):
            checkpoint(capture, args.snapshot_root, states, captured_at)
            available = sum(len(s["available"]) for s in states.values())
            total = sum(len(s["members"]) for s in states.values())
            errors = sum(len(s["errors"]) for s in states.values())
            print(
                f"{completed}/{len(tasks)} queue attempts; {available}/{total} available; "
                f"{errors} current-run errors; latest {symbol} rank {rank}",
                flush=True,
            )
    checkpoint(capture, args.snapshot_root, states, captured_at)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, default=ROOT / "expanded_universe_config.json")
    parser.add_argument("--snapshot-root", type=Path, default=ROOT / "bubblemaps-snapshot")
    parser.add_argument("--symbols", default="")
    parser.add_argument("--timeout", type=float, default=20)
    parser.add_argument("--max-attempts", type=int, default=100)
    parser.add_argument("--retry-delay", type=float, default=3)
    parser.add_argument("--min-interval", type=float, default=1)
    parser.add_argument("--max-members", type=int, default=0)
    parser.add_argument("--checkpoint-every", type=int, default=20)
    return parser.parse_args()


if __name__ == "__main__":
    asyncio.run(run(parse_args()))
