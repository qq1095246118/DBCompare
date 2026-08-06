#!/usr/bin/env python3
"""Rebuild a snapshot manifest from validated local structure/transfer files."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
from decimal import Decimal
import importlib.util
import json
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parent
PROJECT_ROOT = ROOT.parents[1]
CAPTURE_PATH = (
    PROJECT_ROOT
    / "analysis/binance-bubblemaps-out-of-sample-2026-07-30/capture_bubblemaps.py"
)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def canonical(value: object) -> str:
    text = str(value).strip()
    return text.lower() if text.lower().startswith("0x") else text


def load_capture():
    spec = importlib.util.spec_from_file_location("capture_bubblemaps", CAPTURE_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError("capture module unavailable")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def valid_list(path: Path) -> list[dict[str, Any]] | None:
    if not path.is_file():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    return value if isinstance(value, list) else None


def transfer_identity(row: dict[str, Any]) -> tuple:
    data = row["data"]
    return (
        canonical(data["tx_hash"]),
        canonical(row["from_address"]),
        canonical(row["to_address"]),
        Decimal(str(data["value"])),
        int(data["date"]),
    )


def symbol_from_entry(entry: dict[str, Any]) -> str:
    token_file = str(entry.get("token_file") or "")
    parts = Path(token_file).parts
    return parts[1] if len(parts) >= 3 and parts[0] == "data" else ""


def build_entry(capture, snapshot_root: Path, symbol: str, spec: dict, captured_at: str):
    pairs = [
        (str(chain), str(address))
        for chain, addresses in spec["targets"].items()
        for address in addresses
    ]
    if len(pairs) != 1:
        raise ValueError(f"{symbol} must have exactly one target")
    chain, address = pairs[0]
    clean_root = snapshot_root / "clean" / symbol
    holders = valid_list(clean_root / "holders.json")
    relationships = valid_list(clean_root / "relationships.json")
    if holders is None or relationships is None:
        return None
    target = capture.make_target(chain, address)
    snapshot = capture.build_snapshot(target, holders, relationships)
    members = set(capture.ordinary_cluster_members(snapshot))
    available = set()
    unique = set()
    invalid = []
    for member in sorted(members):
        path = clean_root / "transfers" / f"{member}.json"
        if not path.is_file():
            continue
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
            rows = document.get("transfers")
            if document.get("member_address") != member or not isinstance(rows, list):
                raise ValueError("invalid member transfer document")
            for row in rows:
                unique.add(transfer_identity(row))
        except (OSError, UnicodeError, json.JSONDecodeError, KeyError, ValueError) as exc:
            invalid.append({"member_address": member, "type": type(exc).__name__})
            continue
        available.add(member)
    complete = len(available) == len(members) and not invalid
    return {
        "requested_chain": target.requested_chain,
        "requested_token_address": target.requested_token_address,
        "canonical_chain": target.chain,
        "canonical_token_address": target.token_address,
        "captured_at": captured_at,
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
        "available_member_count": len(available),
        "transfer_error_count": len(invalid),
        "errors": invalid[-100:],
        "status": "success" if complete else "partial_success",
        "sources": ["postgresql", "local_snapshot"],
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--snapshot-root", type=Path, required=True)
    parser.add_argument(
        "--preserve-unconfigured",
        action="store_true",
        help="Retain existing manifest entries whose symbols are absent from config.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    capture = load_capture()
    config = json.loads(args.config.read_text(encoding="utf-8"))
    captured_at = utc_now()
    entries = []
    missing = []
    for symbol, spec in config["symbols"].items():
        entry = build_entry(capture, args.snapshot_root, symbol, spec, captured_at)
        if entry is None:
            missing.append(symbol)
        else:
            entries.append(entry)
    if args.preserve_unconfigured:
        manifest_path = args.snapshot_root / "manifest.json"
        if manifest_path.is_file():
            old = json.loads(manifest_path.read_text(encoding="utf-8"))
            configured = set(config["symbols"])
            entries.extend(
                row
                for row in old.get("tokens", [])
                if symbol_from_entry(row) not in configured
            )
    entries.sort(key=lambda row: symbol_from_entry(row))
    manifest = {
        "schema_version": "out-of-sample-v1",
        "source": "postgresql+local_snapshot",
        "status": (
            "success"
            if not missing and all(row["status"] == "success" for row in entries)
            else "partial_success"
        ),
        "captured_at": captured_at,
        "business_date": captured_at[:10],
        "tokens": entries,
        "skipped_tokens": missing,
    }
    capture.write_json(args.snapshot_root / "manifest.json", manifest)
    print(
        json.dumps(
            {
                "manifest_tokens": len(entries),
                "configured_available": len(config["symbols"]) - len(missing),
                "configured_missing": len(missing),
                "ordinary_members": sum(int(row["ordinary_member_count"]) for row in entries),
                "available_members": sum(int(row["available_member_count"]) for row in entries),
                "unique_transfers": sum(int(row["unique_transfer_count"]) for row in entries),
                "status": manifest["status"],
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
