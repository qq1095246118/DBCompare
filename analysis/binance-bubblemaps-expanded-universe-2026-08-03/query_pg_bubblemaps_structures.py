#!/usr/bin/env python3
"""Python 3.9-compatible, read-only PG structure dump helper."""

import argparse
import json
from pathlib import Path
import sys


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--factor-factory-root", type=Path, required=True)
    args = parser.parse_args()
    sys.path.insert(0, str(args.factor_factory_root))
    from factor_factory.adapters.sql import build_postgresql_engine
    from sqlalchemy import text

    request = json.load(sys.stdin)
    targets = request["targets"]
    chains = [row["chain"] for row in targets]
    addresses = [row.get("pg_lookup_address", row["token_address"].lower()) for row in targets]
    engine = build_postgresql_engine()
    try:
        with engine.connect() as connection:
            latest_query = text(
                """
                WITH requested(chain, token_address) AS (
                    SELECT * FROM unnest(
                        CAST(:chains AS text[]), CAST(:addresses AS text[])
                    )
                ), ranked AS (
                    SELECT m.*,
                           row_number() OVER (
                               PARTITION BY m.chain, lower(m.token_address)
                               ORDER BY m.completed_at DESC NULLS LAST, m.created_at DESC
                           ) AS rn
                    FROM public.bubblemaps_membership_snapshot AS m
                    JOIN requested AS r
                      ON r.chain = m.chain
                     AND r.token_address = lower(m.token_address)
                )
                SELECT chain, lower(token_address) AS token_address, batch_id,
                       token_symbol, status, holder_count, cluster_count,
                       relationship_count, completed_at, error_type, error_message
                FROM ranked WHERE rn = 1
                """
            )
            latest = [
                dict(row)
                for row in connection.execute(
                    latest_query, {"chains": chains, "addresses": addresses}
                ).mappings()
            ]
            batch_ids = [row["batch_id"] for row in latest if row["status"] == "success"]
            holders = []
            relationships = []
            if batch_ids:
                holders = [
                    dict(row)
                    for row in connection.execute(
                        text(
                            "SELECT batch_id, raw_data FROM public.bubblemaps_token_holder "
                            "WHERE batch_id = ANY(:batch_ids) ORDER BY batch_id, rank, id"
                        ),
                        {"batch_ids": batch_ids},
                    ).mappings()
                ]
                relationships = [
                    dict(row)
                    for row in connection.execute(
                        text(
                            "SELECT batch_id, raw_data "
                            "FROM public.bubblemaps_holder_relationship_snapshot "
                            "WHERE batch_id = ANY(:batch_ids) ORDER BY batch_id, id"
                        ),
                        {"batch_ids": batch_ids},
                    ).mappings()
                ]
    finally:
        engine.dispose()
    json.dump(
        {"latest": latest, "holders": holders, "relationships": relationships},
        sys.stdout,
        default=str,
        separators=(",", ":"),
    )


if __name__ == "__main__":
    main()
