#!/usr/bin/env python3
"""Probe additional Binance candidates for price history and Bubblemaps coverage."""

from __future__ import annotations

import asyncio
import importlib.util
import json
import sys
from datetime import date
from pathlib import Path


ROOT = Path(__file__).resolve().parent
PROJECT_ROOT = ROOT.parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from getMarket.bubblemaps.tool.bubblemaps_api import BubblemapsApiClient
from getMarket.bubblemaps.tool.export_bubblemaps_market import (
    clean_holders,
    clean_relationships,
    ordinary_cluster_members,
)
from getMarket.bubblemaps.tool.market_identity import make_target
from getMarket.bubblemaps.tool.market_transform import (
    SnapshotModel,
    filter_subgraph_edges,
    parse_ranked_holders,
    reconstruct_clusters,
    token_snapshot_fingerprint,
)


CANDIDATES_PATH = (
    PROJECT_ROOT / "screening/binance-small-volatile-2026-07-30/candidates.json"
)
ENGINE_PATH = (
    PROJECT_ROOT
    / "analysis/binance-bubblemaps-daily-events-2026-07-30/"
    "analyze_daily_events.py"
)
OUTPUT_PATH = ROOT / "candidate-probe-results.json"
SHORTLIST = (
    "CYS",
    "BULLA",
    "KGEN",
    "EVAA",
    "GWEI",
    "ZAMA",
    "CLO",
    "SENT",
    "SKYAI",
    "HOLO",
)


def load_engine():
    spec = importlib.util.spec_from_file_location("daily_factor_engine", ENGINE_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError("daily factor engine unavailable")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


async def probe_bubblemaps(client, symbol: str, chain: str, address: str) -> dict:
    target = make_target(chain, address)
    try:
        holders_result = await client.top_holders(target)
        holders_clean = clean_holders(holders_result, target)
        relationships_result = await client.subgraph(
            target, [row["address"] for row in holders_clean]
        )
        relationships_clean = clean_relationships(
            relationships_result, target, holders_clean
        )
        holders = parse_ranked_holders(holders_clean, target=target)
        edges = filter_subgraph_edges(
            relationships_clean,
            target=target,
            holders={holder.address: holder for holder in holders},
        )
        snapshot = SnapshotModel(
            target=target,
            holders=holders,
            edges=edges,
            clusters=reconstruct_clusters(holders, edges),
            fingerprint=token_snapshot_fingerprint(holders, edges),
            captured_at="probe",
        )
        members = ordinary_cluster_members(snapshot)
        return {
            "status": "supported",
            "ranked_holder_count": len(snapshot.holders),
            "edge_count": len(snapshot.edges),
            "cluster_count": len(snapshot.clusters),
            "ordinary_member_count": len(members),
        }
    except Exception as error:
        return {
            "status": "unsupported",
            "error_type": type(error).__name__,
            "message": str(error),
        }


async def main() -> None:
    ROOT.mkdir(parents=True, exist_ok=True)
    engine = load_engine()
    candidates = json.loads(CANDIDATES_PATH.read_text(encoding="utf-8"))[
        "candidates"
    ]
    by_symbol = {row["base_asset"]: row for row in candidates}
    client = BubblemapsApiClient(
        timeout=30,
        max_attempts=4,
        retry_delay=1,
        min_request_interval=0.8,
    )
    output = []
    for symbol in SHORTLIST:
        candidate = by_symbol[symbol]
        bars = engine.fetch_daily_bars(symbol, date(2026, 7, 29))
        episodes = engine.event_episodes(bars)
        chain = "bsc" if candidate["chain_id"] == "56" else "eth"
        coverage = await probe_bubblemaps(
            client, symbol, chain, candidate["contract_address"]
        )
        row = {
            "symbol": symbol,
            "candidate_rank": candidates.index(candidate) + 1,
            "listing_age_days": candidate["listing_age_days"],
            "market_cap_usd": candidate["market_cap_usd"],
            "quote_volume_usd": candidate["quote_volume_usd"],
            "risk_score": candidate["risk_score"],
            "chain": chain,
            "contract_address": candidate["contract_address"],
            "bar_count": len(bars),
            "event_count": len(episodes),
            **coverage,
        }
        output.append(row)
        print(
            f"{symbol}: bars={len(bars)} events={len(episodes)} "
            f"bubblemaps={coverage['status']} "
            f"members={coverage.get('ordinary_member_count', 0)}",
            flush=True,
        )
    OUTPUT_PATH.write_text(
        json.dumps(output, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(f"wrote {OUTPUT_PATH}")


if __name__ == "__main__":
    asyncio.run(main())
