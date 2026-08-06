#!/usr/bin/env python3
"""Audit PostgreSQL Bubblemaps transfer coverage for a research window."""

import argparse
from datetime import date, datetime, timedelta, timezone
import json
from pathlib import Path
import sys


def utc_now():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("--registry", type=Path, required=True)
    parser.add_argument("--start-date", type=date.fromisoformat, default=date(2025, 1, 1))
    parser.add_argument("--end-date", type=date.fromisoformat, required=True)
    parser.add_argument("--factor-factory-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    return parser.parse_args()


def target_rows(registry):
    rows = []
    for base_asset, spec in registry["symbols"].items():
        pairs = [
            (str(chain).lower(), str(address).strip())
            for chain, addresses in spec["targets"].items()
            for address in addresses
        ]
        if len(pairs) != 1:
            raise ValueError("each symbol must have exactly one target")
        chain, address = pairs[0]
        rows.append(
            {
                "base_asset": base_asset,
                "futures_symbol": spec["futures_symbol"],
                "chain": chain,
                "token_address": address,
                "token_address_lookup": address.lower(),
                "pg_state": spec["source_readiness"]["postgresql"]["state"],
            }
        )
    return rows


def main():
    args = parse_args()
    if args.start_date > args.end_date:
        raise ValueError("start-date must not be after end-date")
    sys.path.insert(0, str(args.factor_factory_root))
    from factor_factory.adapters.sql import build_postgresql_engine
    from sqlalchemy import text

    registry = json.loads(args.registry.read_text(encoding="utf-8"))
    targets = target_rows(registry)
    query = text(
        """
        WITH requested(chain, token_address, symbol) AS (
            SELECT * FROM unnest(
                CAST(:chains AS text[]),
                CAST(:addresses AS text[]),
                CAST(:symbols AS text[])
            )
        ), event_coverage AS (
            SELECT r.symbol, r.chain, r.token_address,
                   count(DISTINCT v.event_id) AS event_count,
                   min(e.event_at) AS first_event_at,
                   max(e.event_at) AS last_event_at,
                   count(DISTINCT v.member_address) AS member_count,
                   count(DISTINCT v.event_id) FILTER (
                       WHERE e.event_at >= CAST(:start_date AS timestamptz)
                         AND e.event_at < CAST(:end_exclusive AS timestamptz)
                   ) AS events_in_window
            FROM requested AS r
            LEFT JOIN public.bubblemaps_transfer_member_view AS v
              ON v.chain = r.chain
             AND lower(v.token_address) = r.token_address
            LEFT JOIN public.token_transfer_event AS e
              ON e.event_id = v.event_id
            GROUP BY r.symbol, r.chain, r.token_address
        )
        SELECT * FROM event_coverage ORDER BY symbol
        """
    )
    engine = build_postgresql_engine()
    try:
        with engine.connect() as connection:
            result = [
                dict(row)
                for row in connection.execute(
                    query,
                    {
                        "chains": [row["chain"] for row in targets],
                        "addresses": [row["token_address_lookup"] for row in targets],
                        "symbols": [row["base_asset"] for row in targets],
                        "start_date": args.start_date.isoformat() + "T00:00:00Z",
                        "end_exclusive": (args.end_date + timedelta(days=1)).isoformat() + "T00:00:00Z",
                    },
                ).mappings()
            ]
    finally:
        engine.dispose()

    by_symbol = {row["symbol"]: row for row in result}
    output_rows = []
    for target in targets:
        row = by_symbol[target["base_asset"]]
        first = row["first_event_at"]
        last = row["last_event_at"]
        event_count = int(row["event_count"] or 0)
        events_in_window = int(row["events_in_window"] or 0)
        output_rows.append(
            {
                **target,
                "event_count": event_count,
                "events_in_window": events_in_window,
                "member_count": int(row["member_count"] or 0),
                "first_event_at": first.isoformat() if first else None,
                "last_event_at": last.isoformat() if last else None,
                "has_events": event_count > 0,
                "has_events_in_window": events_in_window > 0,
                "history_reaches_window_start": bool(first and first.date() <= args.start_date),
            }
        )

    document = {
        "generated_at": utc_now(),
        "source": "postgresql",
        "identity_key": "chain+token_address",
        "research_start_date": args.start_date.isoformat(),
        "research_end_date": args.end_date.isoformat(),
        "registry": str(args.registry),
        "summary": {
            "targets": len(output_rows),
            "pg_ready_targets": sum(row["pg_state"] == "pg_ready" for row in output_rows),
            "targets_with_events": sum(row["has_events"] for row in output_rows),
            "targets_with_events_in_window": sum(row["has_events_in_window"] for row in output_rows),
            "history_reaches_window_start": sum(row["history_reaches_window_start"] for row in output_rows),
            "targets_without_events": sum(not row["has_events"] for row in output_rows),
            "total_distinct_events": sum(row["event_count"] for row in output_rows),
            "total_distinct_events_in_window": sum(row["events_in_window"] for row in output_rows),
        },
        "tokens": output_rows,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    temporary = args.output.with_suffix(args.output.suffix + ".tmp")
    temporary.write_text(json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(args.output)
    print(json.dumps(document["summary"], ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
