#!/usr/bin/env python3
"""Capture/checkpoint Bubblemaps holders and subgraphs without transfer fan-out."""

from __future__ import annotations

import argparse
import asyncio
import importlib.util
import json
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


def valid_list(path: Path) -> bool:
    if not path.is_file():
        return False
    try:
        return isinstance(json.loads(path.read_text(encoding="utf-8")), list)
    except (OSError, UnicodeError, json.JSONDecodeError):
        return False


async def run(args: argparse.Namespace) -> None:
    capture = load_capture()
    config = json.loads(args.config.read_text(encoding="utf-8"))
    client = capture.BubblemapsApiClient(
        timeout=args.timeout,
        max_attempts=args.max_attempts,
        retry_delay=args.retry_delay,
        min_request_interval=args.min_interval,
    )
    for symbol, spec in config["symbols"].items():
        [(chain, addresses)] = spec["targets"].items()
        [address] = addresses
        target = capture.make_target(chain, address)
        clean_root = args.snapshot_root / "clean" / symbol
        holders_path = clean_root / "holders.json"
        relationships_path = clean_root / "relationships.json"
        if valid_list(holders_path):
            holders_clean = json.loads(holders_path.read_text(encoding="utf-8"))
        else:
            holders_clean = capture.clean_holders(await client.top_holders(target), target)
            capture.write_json(holders_path, holders_clean)
        if valid_list(relationships_path):
            relationships_clean = json.loads(
                relationships_path.read_text(encoding="utf-8")
            )
        else:
            result = await client.subgraph(
                target, [row["address"] for row in holders_clean]
            )
            relationships_clean = capture.clean_relationships(
                result, target, holders_clean
            )
            capture.write_json(relationships_path, relationships_clean)
        snapshot = capture.build_snapshot(target, holders_clean, relationships_clean)
        capture.write_json(
            args.snapshot_root / "data" / symbol / "token.json",
            capture.cluster_document(snapshot, symbol),
        )
        members = capture.ordinary_cluster_members(snapshot)
        print(
            f"{symbol}: {len(holders_clean)} holders, "
            f"{len(relationships_clean)} edges, {len(members)} ordinary members",
            flush=True,
        )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, default=ROOT / "expanded_universe_config.json")
    parser.add_argument("--snapshot-root", type=Path, default=ROOT / "bubblemaps-snapshot")
    parser.add_argument("--timeout", type=float, default=20)
    parser.add_argument("--max-attempts", type=int, default=5)
    parser.add_argument("--retry-delay", type=float, default=3)
    parser.add_argument("--min-interval", type=float, default=2)
    return parser.parse_args()


if __name__ == "__main__":
    asyncio.run(run(parse_args()))
