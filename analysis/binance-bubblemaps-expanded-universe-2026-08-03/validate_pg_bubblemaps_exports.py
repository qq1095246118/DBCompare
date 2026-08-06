#!/usr/bin/env python3
"""Validate PG-exported Bubblemaps structures without touching source data."""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Any


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--registry", type=Path, required=True)
    parser.add_argument("--export-report", type=Path, required=True)
    parser.add_argument("--snapshot-root", type=Path, required=True)
    parser.add_argument("--min-relationships", type=int, default=50)
    parser.add_argument("--output", type=Path, required=True)
    return parser.parse_args()


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def target_identity(spec: dict[str, Any]) -> tuple[str, str]:
    pairs = [
        (str(chain).lower(), str(address).strip())
        for chain, addresses in spec["targets"].items()
        for address in addresses
    ]
    if len(pairs) != 1:
        raise ValueError("each registry symbol must have exactly one target")
    return pairs[0]


def main() -> None:
    args = parse_args()
    registry = load_json(args.registry)
    export_report = load_json(args.export_report)
    report_by_symbol = {row["symbol"]: row for row in export_report["tokens"]}
    errors: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    validated: list[dict[str, Any]] = []
    not_exported: list[dict[str, Any]] = []

    registry_states = Counter()
    registry_chains = defaultdict(Counter)
    for symbol, spec in registry["symbols"].items():
        chain, address = target_identity(spec)
        state = spec["source_readiness"]["postgresql"]["state"]
        registry_states[state] += 1
        registry_chains[chain][state] += 1
        row = report_by_symbol.get(symbol)
        if row is None:
            errors.append({"symbol": symbol, "error": "missing_export_report_row"})
            continue
        if row["chain"] != chain or row["token_address"] != address:
            errors.append(
                {
                    "symbol": symbol,
                    "error": "target_identity_mismatch",
                    "registry": [chain, address],
                    "report": [row["chain"], row["token_address"]],
                }
            )
            continue
        if row["status"] != "exported_from_pg":
            membership = row.get("membership") or {}
            not_exported.append(
                {
                    "symbol": symbol,
                    "chain": chain,
                    "token_address": address,
                    "registry_pg_state": state,
                    "export_status": row["status"],
                    "latest_membership_status": membership.get("status"),
                    "latest_error_type": membership.get("error_type"),
                }
            )
            continue

        holder_path = args.snapshot_root / "clean" / symbol / "holders.json"
        relationship_path = args.snapshot_root / "clean" / symbol / "relationships.json"
        token_path = args.snapshot_root / "data" / symbol / "token.json"
        try:
            holders = load_json(holder_path)
            relationships = load_json(relationship_path)
            token = load_json(token_path)
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            errors.append({"symbol": symbol, "error": "unreadable_json", "detail": str(error)})
            continue
        if not isinstance(holders, list) or not isinstance(relationships, list) or not isinstance(token, dict):
            errors.append({"symbol": symbol, "error": "invalid_json_shape"})
            continue
        if len(holders) != int(row["holder_count"]) or len(relationships) != int(row["relationship_count"]):
            errors.append(
                {
                    "symbol": symbol,
                    "error": "count_mismatch",
                    "files": [len(holders), len(relationships)],
                    "report": [row["holder_count"], row["relationship_count"]],
                }
            )
            continue
        if token.get("canonical_chain") != chain or token.get("canonical_token_address") != address:
            errors.append(
                {
                    "symbol": symbol,
                    "error": "token_identity_mismatch",
                    "token": [token.get("canonical_chain"), token.get("canonical_token_address")],
                }
            )
            continue
        if int(row.get("ordinary_member_count") or 0) <= 0:
            errors.append({"symbol": symbol, "error": "no_ordinary_cluster_members"})
            continue
        if len(relationships) < args.min_relationships:
            warnings.append(
                {
                    "symbol": symbol,
                    "chain": chain,
                    "warning": "relationship_count_below_quality_threshold",
                    "relationship_count": len(relationships),
                    "threshold": args.min_relationships,
                }
            )
        validated.append(
            {
                "symbol": symbol,
                "chain": chain,
                "holder_count": len(holders),
                "relationship_count": len(relationships),
                "ordinary_member_count": int(row["ordinary_member_count"]),
            }
        )

    export_states = Counter(row["status"] for row in export_report["tokens"])
    expected_exported = registry_states["pg_ready"] + registry_states["pg_structure_ready_transfer_missing"]
    if export_states["exported_from_pg"] != expected_exported:
        errors.append(
            {
                "error": "exported_count_disagrees_with_registry",
                "expected": expected_exported,
                "actual": export_states["exported_from_pg"],
            }
        )
    if export_states["latest_pg_snapshot_failed"] != registry_states["pg_latest_failed"]:
        errors.append({"error": "latest_failed_count_disagrees_with_registry"})
    expected_missing = registry_states["pg_missing"] + registry_states["pg_chain_not_present"]
    if export_states["missing_in_pg"] != expected_missing:
        errors.append(
            {
                "error": "missing_count_disagrees_with_registry",
                "expected": expected_missing,
                "actual": export_states["missing_in_pg"],
            }
        )

    document = {
        "generated_at": utc_now(),
        "registry": str(args.registry),
        "export_report": str(args.export_report),
        "snapshot_root": str(args.snapshot_root),
        "status": "pass" if not errors else "fail",
        "summary": {
            "registry_targets": len(registry["symbols"]),
            "validated_pg_exports": len(validated),
            "validation_errors": len(errors),
            "quality_warnings": len(warnings),
            "registry_pg_states": dict(sorted(registry_states.items())),
            "export_states": dict(sorted(export_states.items())),
        },
        "pg_states_by_chain": {
            chain: dict(sorted(counts.items())) for chain, counts in sorted(registry_chains.items())
        },
        "quality_warnings": warnings,
        "not_exported": not_exported,
        "errors": errors,
        "validated": validated,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    temporary = args.output.with_suffix(args.output.suffix + ".tmp")
    temporary.write_text(json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(args.output)
    print(json.dumps(document["summary"], ensure_ascii=False, indent=2))
    if errors:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
