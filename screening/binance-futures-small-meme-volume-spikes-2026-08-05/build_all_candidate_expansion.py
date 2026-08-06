#!/usr/bin/env python3
"""Build the full candidate registry and audit PG/SSH source coverage.

The screen result remains the selection source of truth.  This utility only
normalizes chain identities, checks the already-configured PostgreSQL snapshot,
and checks a copied Binance Vision daily manifest.  It never calls Bubblemaps or
Binance APIs and never writes to PostgreSQL.
"""

from __future__ import annotations

import argparse
import csv
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
from typing import Any


ROOT = Path(__file__).resolve().parent
PROJECT_ROOT = ROOT.parents[1]

CHAIN_MAP = {
    "1": "eth",
    "10": "optimism",
    "56": "bsc",
    "137": "polygon",
    "169": "manta",
    "324": "zksync",
    "4663": "robinhood",
    "5000": "mantle",
    "8453": "base",
    "9745": "plasma",
    "42161": "arbitrum",
    "42220": "celo",
    "43114": "avalanche",
    "59144": "linea",
    "BN_2020": "ronin",
    "CT_501": "solana",
    "CT_607": "ton",
    "CT_784": "sui",
    "CT_9004": "starknet",
}


def canonical_token_address(value: object) -> str:
    """Lower-case hexadecimal addresses; preserve case-sensitive identities."""
    address = str(value).strip()
    return address.lower() if address.lower().startswith("0x") else address


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--screen-results", type=Path, default=ROOT / "screen-results.json")
    parser.add_argument(
        "--server-manifest",
        type=Path,
        default=PROJECT_ROOT
        / "analysis/binance-bubblemaps-expanded-universe-2026-08-03/"
        "server-binance-vision-daily-20260804T140501Z.csv",
    )
    parser.add_argument(
        "--factor-factory-root",
        type=Path,
        default=PROJECT_ROOT.parent / "Factor_Factory",
    )
    parser.add_argument("--skip-pg", action="store_true")
    parser.add_argument(
        "--output-json", type=Path, default=ROOT / "all-233-expansion-registry.json"
    )
    parser.add_argument(
        "--output-csv", type=Path, default=ROOT / "all-233-expansion-registry.csv"
    )
    return parser.parse_args()


def load_candidates(path: Path) -> list[dict[str, Any]]:
    document = json.loads(path.read_text(encoding="utf-8"))
    candidates = document.get("eligible_additional")
    if not isinstance(candidates, list) or not candidates:
        raise ValueError("screen result has no eligible_additional candidates")
    symbols = [str(row["symbol"]).upper() for row in candidates]
    if len(symbols) != len(set(symbols)):
        raise ValueError("candidate symbols are not unique")
    keys = [
        (CHAIN_MAP.get(str(row["chain_id"])), canonical_token_address(row["contract_address"]))
        for row in candidates
    ]
    if None in {chain for chain, _ in keys}:
        missing = sorted(
            {str(row["chain_id"]) for row in candidates if str(row["chain_id"]) not in CHAIN_MAP}
        )
        raise ValueError("unmapped chain ids: " + ",".join(missing))
    if len(keys) != len(set(keys)):
        raise ValueError("candidate chain + contract keys are not unique")
    return candidates


def load_server_kline_coverage(path: Path, symbols: set[str]) -> dict[str, dict[str, Any]]:
    if not path.is_file():
        return {}
    grouped: dict[str, list[dict[str, str]]] = {}
    with path.open(newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            if (
                row.get("market") == "futures-um"
                and row.get("data_type") == "klines"
                and row.get("interval") == "1d"
                and row.get("symbol") in symbols
            ):
                grouped.setdefault(row["symbol"], []).append(row)
    output = {}
    for symbol, rows in grouped.items():
        usable = [row for row in rows if row.get("status") in {"downloaded", "skipped"}]
        output[symbol] = {
            "available": bool(usable),
            "date": max((row.get("date") or "" for row in usable), default=None),
            "paths": sorted({row["path"] for row in usable if row.get("path")}),
            "manifest": str(path),
        }
    return output


def query_pg(factor_factory_root: Path, targets: list[tuple[str, str]]) -> tuple[dict, set[str]]:
    sys.path.insert(0, str(factor_factory_root))
    from factor_factory.adapters.sql import build_postgresql_engine
    from sqlalchemy import text

    engine = build_postgresql_engine()
    try:
        with engine.connect() as connection:
            present_chains = {
                str(row[0])
                for row in connection.execute(
                    text("SELECT DISTINCT chain FROM public.bubblemaps_membership_snapshot")
                )
            }
            query = text(
                """
                WITH requested(chain, token_address) AS (
                    SELECT * FROM unnest(
                        CAST(:chains AS text[]), CAST(:addresses AS text[])
                    )
                ),
                membership AS (
                    SELECT m.*,
                           row_number() OVER (
                               PARTITION BY m.chain, lower(m.token_address)
                               ORDER BY m.completed_at DESC NULLS LAST, m.created_at DESC
                           ) AS rn
                    FROM public.bubblemaps_membership_snapshot AS m
                    JOIN requested AS r
                      ON r.chain = m.chain
                     AND r.token_address = lower(m.token_address)
                ),
                transfers AS (
                    SELECT v.chain, lower(v.token_address) AS token_address,
                           count(DISTINCT v.member_address) AS member_count,
                           count(DISTINCT v.event_id) AS event_count,
                           max(v.captured_at) AS last_captured_at
                    FROM public.bubblemaps_transfer_member_view AS v
                    JOIN requested AS r
                      ON r.chain = v.chain
                     AND r.token_address = lower(v.token_address)
                    GROUP BY v.chain, lower(v.token_address)
                )
                SELECT m.chain, lower(m.token_address) AS token_address,
                       m.batch_id, m.token_symbol, m.status,
                       m.holder_count, m.cluster_count, m.relationship_count,
                       m.completed_at, m.error_type, m.error_message,
                       coalesce(t.member_count, 0) AS transfer_member_count,
                       coalesce(t.event_count, 0) AS transfer_event_count,
                       t.last_captured_at
                FROM membership AS m
                LEFT JOIN transfers AS t
                  ON t.chain = m.chain AND t.token_address = lower(m.token_address)
                WHERE m.rn = 1
                """
            )
            chains = [chain for chain, _ in targets]
            addresses = [address for _, address in targets]
            rows = connection.execute(
                query, {"chains": chains, "addresses": addresses}
            ).mappings()
            payload = {
                (str(row["chain"]), str(row["token_address"]).lower()): {
                    key: (str(value) if key in {"batch_id", "completed_at", "last_captured_at"} and value is not None else value)
                    for key, value in dict(row).items()
                }
                for row in rows
            }
    finally:
        engine.dispose()
    return payload, present_chains


def pg_state(chain: str, row: dict[str, Any] | None, present_chains: set[str]) -> str:
    if row is None:
        return "pg_missing" if chain in present_chains else "pg_chain_not_present"
    if row.get("status") != "success":
        return "pg_latest_failed"
    if int(row.get("transfer_member_count") or 0) == 0:
        return "pg_structure_ready_transfer_missing"
    return "pg_ready"


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    fields = [
        "rank", "symbol", "base_asset", "project_name", "chain", "external_chain_id",
        "contract_address", "candidate_score", "market_cap_usd", "is_meme",
        "event_count", "max_volume_ratio", "server_kline_available", "server_kline_date",
        "pg_state", "pg_membership_status", "pg_holder_count", "pg_relationship_count",
        "pg_transfer_member_count", "pg_transfer_event_count",
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows([{key: row.get(key) for key in fields} for row in rows])
    temporary.replace(path)


def main() -> None:
    args = parse_args()
    candidates = load_candidates(args.screen_results)
    symbols = {str(row["symbol"]).upper() for row in candidates}
    kline = load_server_kline_coverage(args.server_manifest, symbols)
    targets = [
        (CHAIN_MAP[str(row["chain_id"])], str(row["contract_address"]).lower())
        for row in candidates
    ]
    pg_rows: dict[tuple[str, str], dict[str, Any]] = {}
    present_chains: set[str] = set()
    pg_error = None
    if not args.skip_pg:
        try:
            pg_rows, present_chains = query_pg(args.factor_factory_root, targets)
        except Exception as error:  # keep the registry buildable during a DB outage
            message = str(error).strip().splitlines()[0] if str(error).strip() else "unknown error"
            pg_error = f"{type(error).__name__}: {message}"

    output_rows = []
    symbols_config: dict[str, Any] = {}
    for rank, row in enumerate(candidates, 1):
        symbol = str(row["symbol"]).upper()
        chain = CHAIN_MAP[str(row["chain_id"])]
        address = canonical_token_address(row["contract_address"])
        pg = pg_rows.get((chain, address.lower()))
        state = "pg_not_checked" if args.skip_pg or pg_error else pg_state(chain, pg, present_chains)
        server = kline.get(symbol) or {"available": False, "date": None, "paths": []}
        item = {
            "rank": rank,
            "symbol": symbol,
            "base_asset": row["base_asset"],
            "project_name": row.get("project_name"),
            "chain": chain,
            "external_chain_id": str(row["chain_id"]),
            "contract_address": address,
            "candidate_score": row.get("candidate_score"),
            "market_cap_usd": row.get("market_cap_usd"),
            "is_meme": bool(row.get("is_meme")),
            "event_count": row.get("event_count"),
            "max_volume_ratio": row.get("max_volume_ratio"),
            "server_kline_available": bool(server["available"]),
            "server_kline_date": server["date"],
            "pg_state": state,
            "pg_membership_status": pg.get("status") if pg else None,
            "pg_holder_count": pg.get("holder_count") if pg else None,
            "pg_relationship_count": pg.get("relationship_count") if pg else None,
            "pg_transfer_member_count": pg.get("transfer_member_count") if pg else None,
            "pg_transfer_event_count": pg.get("transfer_event_count") if pg else None,
        }
        output_rows.append(item)
        symbols_config[row["base_asset"]] = {
            "futures_symbol": symbol,
            "project_name": row.get("project_name"),
            "targets": {chain: [address]},
            "external_chain_id": str(row["chain_id"]),
            "selection": {
                "rank": rank,
                "candidate_score": row.get("candidate_score"),
                "market_cap_usd": row.get("market_cap_usd"),
                "is_meme": bool(row.get("is_meme")),
                "event_count": row.get("event_count"),
                "max_volume_ratio": row.get("max_volume_ratio"),
                "peak_event_date": row.get("peak_event_date"),
            },
            "source_readiness": {
                "server_kline": server,
                "postgresql": {"state": state, "latest": pg},
            },
        }

    counts: dict[str, int] = {}
    for row in output_rows:
        counts[row["pg_state"]] = counts.get(row["pg_state"], 0) + 1
    document = {
        "schema_version": "expanded-universe-registry-v2.1",
        "generated_at": utc_now(),
        "selection_source": str(args.screen_results),
        "candidate_count": len(output_rows),
        "all_candidates_enabled": True,
        "research_window": {
            "requested_start_utc": "2025-01-01",
            "end_utc": "2026-08-03",
            "symbol_start_policy": "max(requested_start_utc, actual_binance_futures_first_bar)",
            "pre_listing_policy": "no_backfill_no_forward_fill",
        },
        "source_priority": {
            "bubblemaps": ["postgresql", "existing_local_snapshot", "bubblemaps_api"],
            "klines": ["ssh_binance_vision", "existing_local_cache", "binance_public_api"],
        },
        "ssh_kline_source": {
            "host": "rayer@192.168.32.153",
            "canonical_root": "/data2/shares/raw/binance/vision",
            "staging_root": "/data2/shares/raw_tmp/binance/vision",
            "legacy_root": "/data/shares/raw/binance/vision",
            "latest_manifest": str(args.server_manifest),
            "candidate_symbols_available": sum(bool(row["server_kline_available"]) for row in output_rows),
        },
        "postgresql_audit": {
            "checked": not args.skip_pg and pg_error is None,
            "error": pg_error,
            "states": counts,
            "chains_present": sorted(present_chains),
        },
        "symbols": symbols_config,
    }
    write_json(args.output_json, document)
    write_csv(args.output_csv, output_rows)
    print(json.dumps({
        "candidate_count": len(output_rows),
        "server_kline_available": sum(bool(row["server_kline_available"]) for row in output_rows),
        "pg_states": counts,
        "output_json": str(args.output_json),
        "output_csv": str(args.output_csv),
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
