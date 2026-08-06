#!/usr/bin/env python3
"""Export latest successful Bubblemaps structures from PostgreSQL.

This is the PG-first structure stage.  It reads only the latest membership
snapshot for each exact ``chain + token_address`` target, writes the existing
clean holders/relationships schema, and derives the normal token/cluster
document with the same code used by the API collector.  Missing/failed targets
remain untouched for the API fallback stage.
"""

from __future__ import annotations

import argparse
from collections import defaultdict
from datetime import datetime, timezone
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
from typing import Any


ROOT = Path(__file__).resolve().parent
PROJECT_ROOT = ROOT.parents[1]
CAPTURE_PATH = (
    PROJECT_ROOT
    / "analysis/binance-bubblemaps-out-of-sample-2026-07-30/capture_bubblemaps.py"
)
QUERY_HELPER = ROOT / "query_pg_bubblemaps_structures.py"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def load_capture():
    spec = importlib.util.spec_from_file_location("capture_bubblemaps", CAPTURE_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError("capture module unavailable")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--snapshot-root", type=Path, required=True)
    parser.add_argument(
        "--factor-factory-root",
        type=Path,
        default=PROJECT_ROOT.parent / "Factor_Factory",
    )
    parser.add_argument("--symbols", help="Optional comma-separated base assets")
    parser.add_argument("--overwrite", action="store_true")
    parser.add_argument("--report", type=Path, default=ROOT / "pg-structure-export-report.json")
    return parser.parse_args()


def target_rows(config: dict[str, Any], selected: set[str] | None) -> list[dict[str, str]]:
    rows = []
    for symbol, spec in config["symbols"].items():
        if selected is not None and symbol not in selected:
            continue
        pairs = [
            (str(chain).lower(), str(address).strip())
            for chain, addresses in spec["targets"].items()
            for address in addresses
        ]
        if len(pairs) != 1:
            raise ValueError(f"{symbol} must have exactly one target")
        chain, address = pairs[0]
        rows.append(
            {
                "symbol": symbol,
                "chain": chain,
                "token_address": address,
                "pg_lookup_address": address.lower(),
            }
        )
    return rows


def query_pg(factor_factory_root: Path, targets: list[dict[str, str]]) -> tuple[dict, dict, dict]:
    python = factor_factory_root / ".venv/bin/python"
    result = subprocess.run(
        [
            str(python),
            str(QUERY_HELPER),
            "--factor-factory-root",
            str(factor_factory_root),
        ],
        input=json.dumps({"targets": targets}),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if result.returncode:
        detail = result.stderr.strip().splitlines()[-1] if result.stderr.strip() else "unknown error"
        raise RuntimeError("PG structure query failed: " + detail)
    payload = json.loads(result.stdout)
    latest = {
        (str(row["chain"]), str(row["token_address"]).lower()): row
        for row in payload["latest"]
    }
    holders: dict[str, list[dict[str, Any]]] = defaultdict(list)
    relationships: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in payload["holders"]:
        holders[str(row["batch_id"])].append(row["raw_data"])
    for row in payload["relationships"]:
        relationships[str(row["batch_id"])].append(row["raw_data"])
    return latest, holders, relationships


def valid_list(path: Path) -> bool:
    if not path.is_file():
        return False
    try:
        return isinstance(json.loads(path.read_text(encoding="utf-8")), list)
    except (OSError, UnicodeError, json.JSONDecodeError):
        return False


def json_safe(row: dict[str, Any] | None) -> dict[str, Any] | None:
    if row is None:
        return None
    return {key: (str(value) if value is not None else None) for key, value in row.items()}


def main() -> None:
    args = parse_args()
    config = json.loads(args.config.read_text(encoding="utf-8"))
    selected = None
    if args.symbols:
        selected = {item.strip().upper() for item in args.symbols.split(",") if item.strip()}
    targets = target_rows(config, selected)
    latest, holders_by_batch, relationships_by_batch = query_pg(
        args.factor_factory_root, targets
    )
    capture = load_capture()
    report_rows = []
    for target_row in targets:
        symbol = target_row["symbol"]
        chain = target_row["chain"]
        address = target_row["token_address"]
        membership = latest.get((chain, target_row["pg_lookup_address"]))
        result = {
            "symbol": symbol,
            "chain": chain,
            "token_address": address,
            "status": "missing_in_pg",
            "membership": json_safe(membership),
        }
        if membership is None or membership.get("status") != "success":
            if membership is not None:
                result["status"] = "latest_pg_snapshot_failed"
            report_rows.append(result)
            continue
        batch_id = str(membership["batch_id"])
        holders = holders_by_batch.get(batch_id, [])
        relationships = relationships_by_batch.get(batch_id, [])
        if not holders or not relationships:
            result["status"] = "pg_snapshot_incomplete"
            report_rows.append(result)
            continue
        clean_root = args.snapshot_root / "clean" / symbol
        holder_path = clean_root / "holders.json"
        relationship_path = clean_root / "relationships.json"
        if not args.overwrite and valid_list(holder_path) and valid_list(relationship_path):
            result["status"] = "existing_local_reused"
            report_rows.append(result)
            continue
        capture.write_json(holder_path, holders)
        capture.write_json(relationship_path, relationships)
        target = capture.make_target(chain, address)
        snapshot = capture.build_snapshot(target, holders, relationships)
        capture.write_json(
            args.snapshot_root / "data" / symbol / "token.json",
            capture.cluster_document(snapshot, symbol),
        )
        result.update(
            {
                "status": "exported_from_pg",
                "holder_count": len(holders),
                "relationship_count": len(relationships),
                "ordinary_member_count": len(capture.ordinary_cluster_members(snapshot)),
            }
        )
        report_rows.append(result)
        print(
            f"{symbol}: PG {len(holders)} holders, {len(relationships)} relationships",
            flush=True,
        )
    summary: dict[str, int] = {}
    for row in report_rows:
        summary[row["status"]] = summary.get(row["status"], 0) + 1
    document = {
        "generated_at": utc_now(),
        "config": str(args.config),
        "snapshot_root": str(args.snapshot_root),
        "source": "postgresql",
        "summary": summary,
        "tokens": report_rows,
    }
    capture.write_json(args.report, document)
    print(json.dumps(summary, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
