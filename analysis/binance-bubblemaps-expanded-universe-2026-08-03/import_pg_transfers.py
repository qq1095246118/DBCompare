#!/usr/bin/env python3
"""Import PG-backed member transfers into the expanded-universe snapshot.

The importer is deliberately strict about ``chain + token_address``.  A symbol
match alone is never sufficient because the same symbol can exist on multiple
chains with unrelated transfer histories.
"""

from __future__ import annotations

import argparse
from collections import defaultdict
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, InvalidOperation
import importlib.util
import json
from pathlib import Path
import subprocess
from typing import Any, Iterable


ROOT = Path(__file__).resolve().parent
PROJECT_ROOT = ROOT.parents[1]
CAPTURE_PATH = (
    PROJECT_ROOT
    / "analysis/binance-bubblemaps-out-of-sample-2026-07-30/capture_bubblemaps.py"
)
DEFAULT_FACTOR_FACTORY_ROOT = PROJECT_ROOT.parent / "Factor_Factory"
QUERY_HELPER = ROOT / "query_pg_transfer_member_view.py"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def canonical_chain_value(value: object) -> str:
    """Normalize EVM-style hex values without corrupting Base58/Base64 values."""
    text = str(value).strip()
    return text.lower() if text.lower().startswith("0x") else text


def load_capture():
    spec = importlib.util.spec_from_file_location("capture_bubblemaps", CAPTURE_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError("capture module unavailable")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def decimal_text(value: object) -> str:
    try:
        number = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        raise ValueError("transfer amount must be a decimal") from None
    if not number.is_finite():
        raise ValueError("transfer amount must be finite")
    text = format(number, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def event_timestamp_ms(row: dict[str, Any]) -> int:
    value = row.get("event_timestamp_ms")
    if value is not None:
        return int(value)
    event_at = row.get("event_at")
    if isinstance(event_at, str):
        event_at = datetime.fromisoformat(event_at.replace("Z", "+00:00"))
    if not isinstance(event_at, datetime):
        raise ValueError("transfer event has no timestamp")
    if event_at.tzinfo is None:
        event_at = event_at.replace(tzinfo=timezone.utc)
    return int(event_at.timestamp() * 1000)


def clean_pg_transfer(row: dict[str, Any], chain: str, token_address: str) -> dict:
    tx_hash = canonical_chain_value(row.get("tx_hash") or "")
    from_address = canonical_chain_value(row.get("from_address") or "")
    to_address = canonical_chain_value(row.get("to_address") or "")
    if not tx_hash or not from_address or not to_address:
        raise ValueError("transfer event is missing tx/from/to")
    return {
        "from_address": from_address,
        "to_address": to_address,
        "rel_type": "TRANSFER",
        "data": {
            "value": decimal_text(row.get("amount")),
            "date": event_timestamp_ms(row),
            "tx_hash": tx_hash,
            "token_ref": {"chain": chain, "address": token_address},
            "event_id": str(row.get("event_id") or ""),
            "source": "postgresql",
        },
    }


def transfer_identity(row: dict[str, Any]) -> tuple:
    data = row["data"]
    return (
        canonical_chain_value(data["tx_hash"]),
        canonical_chain_value(row["from_address"]),
        canonical_chain_value(row["to_address"]),
        Decimal(str(data["value"])),
        int(data["date"]),
    )


def merge_transfers(
    existing: Iterable[dict[str, Any]], imported: Iterable[dict[str, Any]]
) -> tuple[list[dict[str, Any]], int]:
    merged: dict[tuple, dict[str, Any]] = {}
    for row in existing:
        merged[transfer_identity(row)] = row
    before = len(merged)
    for row in imported:
        merged.setdefault(transfer_identity(row), row)
    output = sorted(
        merged.values(),
        key=lambda row: (
            int(row["data"]["date"]),
            canonical_chain_value(row["data"]["tx_hash"]),
            canonical_chain_value(row["from_address"]),
            canonical_chain_value(row["to_address"]),
            Decimal(str(row["data"]["value"])),
        ),
    )
    return output, len(merged) - before


def target_spec(configuration: dict, symbol: str) -> tuple[str, str]:
    try:
        targets = configuration["symbols"][symbol]["targets"]
    except KeyError:
        raise ValueError(f"unknown symbol: {symbol}") from None
    pairs = [
        (str(chain), canonical_chain_value(address))
        for chain, addresses in targets.items()
        for address in addresses
    ]
    if len(pairs) != 1:
        raise ValueError(f"{symbol} must have exactly one target")
    return pairs[0]


def current_members(
    capture, snapshot_root: Path, symbol: str, chain: str, token_address: str
) -> set[str]:
    clean_root = snapshot_root / "clean" / symbol
    holders = json.loads((clean_root / "holders.json").read_text(encoding="utf-8"))
    relationships = json.loads(
        (clean_root / "relationships.json").read_text(encoding="utf-8")
    )
    target = capture.make_target(chain, token_address)
    snapshot = capture.build_snapshot(target, holders, relationships)
    return set(capture.ordinary_cluster_members(snapshot))


def fetch_pg_rows(
    factor_factory_root: Path,
    chain: str,
    token_address: str,
    members: set[str],
    start_date: date | None,
    end_date: date | None,
) -> list[dict[str, Any]]:
    if not members:
        return []
    python = factor_factory_root / ".venv/bin/python"
    if not python.is_file():
        raise ValueError(f"Factor_Factory Python unavailable: {python}")
    result = subprocess.run(
        [
            str(python),
            str(QUERY_HELPER),
            "--factor-factory-root",
            str(factor_factory_root),
        ],
        input=json.dumps(
            {
                "chain": chain,
                "token_address": token_address,
                "token_address_lookup": token_address.lower(),
                "members": sorted(members),
                "start_at": (
                    datetime.combine(start_date, time.min, tzinfo=timezone.utc).isoformat()
                    if start_date else None
                ),
                "end_exclusive": (
                    datetime.combine(end_date + timedelta(days=1), time.min, tzinfo=timezone.utc).isoformat()
                    if end_date else None
                ),
            }
        ),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if result.returncode:
        raise RuntimeError(
            "PG transfer query failed: " + result.stderr.strip().splitlines()[-1]
        )
    payload = json.loads(result.stdout)
    if not isinstance(payload, list):
        raise ValueError("PG transfer helper returned a non-list payload")
    return payload


def import_symbol(
    capture,
    factor_factory_root: Path,
    snapshot_root: Path,
    configuration: dict,
    symbol: str,
    start_date: date | None,
    end_date: date | None,
) -> dict[str, Any]:
    chain, token_address = target_spec(configuration, symbol)
    members = current_members(
        capture, snapshot_root, symbol, chain, token_address
    )
    rows = fetch_pg_rows(
        factor_factory_root, chain, token_address, members, start_date, end_date
    )
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        member = canonical_chain_value(row["member_address"])
        if member not in members:
            raise ValueError(f"PG returned non-current member for {symbol}: {member}")
        grouped[member].append(clean_pg_transfer(row, chain, token_address))

    created = 0
    merged_existing = 0
    events_added = 0
    for member, imported in sorted(grouped.items()):
        path = snapshot_root / "clean" / symbol / "transfers" / f"{member}.json"
        existing_document: dict[str, Any] = {}
        existing_rows: list[dict[str, Any]] = []
        if path.is_file():
            try:
                candidate = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, json.JSONDecodeError):
                candidate = {}
            if (
                isinstance(candidate, dict)
                and candidate.get("member_address") == member
                and isinstance(candidate.get("transfers"), list)
            ):
                existing_document = candidate
                existing_rows = candidate["transfers"]
                merged_existing += 1
        if not existing_document:
            created += 1
        merged, added = merge_transfers(existing_rows, imported)
        events_added += added
        document = {
            **existing_document,
            "schema_version": "out-of-sample-v1",
            "chain": chain,
            "token_address": token_address,
            "canonical_chain": chain,
            "canonical_token_address": token_address,
            "member_address": member,
            "transfers": merged,
            "transfer_count": len(merged),
            "sources": sorted(
                set(existing_document.get("sources") or [])
                | {"postgresql"}
                | ({"bubblemaps_api"} if existing_rows else set())
            ),
            "pg_imported_at": utc_now(),
        }
        write_json(path, document)

    return {
        "symbol": symbol,
        "chain": chain,
        "token_address": token_address,
        "current_member_count": len(members),
        "pg_member_count": len(grouped),
        "pg_event_count": len(rows),
        "start_date": start_date.isoformat() if start_date else None,
        "end_date": end_date.isoformat() if end_date else None,
        "pg_member_coverage_ratio": (
            round(len(grouped) / len(members), 8) if members else 0
        ),
        "created_member_files": created,
        "merged_existing_member_files": merged_existing,
        "events_added": events_added,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--config", type=Path, default=ROOT / "expanded_universe_config.json"
    )
    parser.add_argument(
        "--snapshot-root", type=Path, default=ROOT / "bubblemaps-snapshot"
    )
    parser.add_argument("--symbols", default="BLESS,FF,ZBT")
    parser.add_argument("--start-date", type=date.fromisoformat)
    parser.add_argument("--end-date", type=date.fromisoformat)
    parser.add_argument(
        "--factor-factory-root", type=Path, default=DEFAULT_FACTOR_FACTORY_ROOT
    )
    parser.add_argument(
        "--report", type=Path, default=ROOT / "pg-transfer-import-report.json"
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if args.start_date and args.end_date and args.start_date > args.end_date:
        raise ValueError("start-date must not be after end-date")
    symbols = [item.strip().upper() for item in args.symbols.split(",") if item.strip()]
    configuration = json.loads(args.config.read_text(encoding="utf-8"))
    capture = load_capture()
    results = [
        import_symbol(
            capture,
            args.factor_factory_root,
            args.snapshot_root,
            configuration,
            symbol,
            args.start_date,
            args.end_date,
        )
        for symbol in symbols
    ]
    report = {
        "generated_at": utc_now(),
        "source": "postgresql",
        "identity_key": "chain+token_address",
        "research_window": {
            "start_date": args.start_date.isoformat() if args.start_date else None,
            "end_date": args.end_date.isoformat() if args.end_date else None,
        },
        "symbols": results,
    }
    write_json(args.report, report)
    for row in results:
        print(
            f"{row['symbol']} {row['chain']}: PG members "
            f"{row['pg_member_count']}/{row['current_member_count']}; "
            f"events {row['pg_event_count']}; added {row['events_added']}",
            flush=True,
        )
    print(f"wrote {args.report}", flush=True)


if __name__ == "__main__":
    main()
