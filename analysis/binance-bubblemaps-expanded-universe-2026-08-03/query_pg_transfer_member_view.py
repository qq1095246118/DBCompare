#!/usr/bin/env python3
"""Python 3.9-compatible read-only PG query helper for Factor_Factory."""

import argparse
import json
from pathlib import Path
import sys


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("--factor-factory-root", type=Path, required=True)
    return parser.parse_args()


def main():
    args = parse_args()
    sys.path.insert(0, str(args.factor_factory_root))
    from factor_factory.adapters.sql import build_postgresql_engine
    from sqlalchemy import text

    request = json.load(sys.stdin)
    query = text(
        """
        SELECT DISTINCT ON (v.member_address, e.event_id)
               v.member_address, e.event_id, e.tx_hash,
               e.from_address, e.to_address, e.amount,
               e.event_timestamp_ms, e.event_at
        FROM public.bubblemaps_transfer_member_view AS v
        JOIN public.token_transfer_event AS e ON e.event_id = v.event_id
        WHERE v.chain = :chain
          AND lower(v.token_address) = :token_address_lookup
          AND v.member_address = ANY(:members)
          AND (CAST(:start_at AS timestamptz) IS NULL
               OR e.event_at >= CAST(:start_at AS timestamptz))
          AND (CAST(:end_exclusive AS timestamptz) IS NULL
               OR e.event_at < CAST(:end_exclusive AS timestamptz))
        ORDER BY v.member_address, e.event_id, v.captured_at DESC
        """
    )
    engine = build_postgresql_engine()
    try:
        with engine.connect() as connection:
            rows = connection.execute(query, request).mappings()
            payload = [dict(row) for row in rows]
    finally:
        engine.dispose()
    json.dump(payload, sys.stdout, default=str, separators=(",", ":"))


if __name__ == "__main__":
    main()
