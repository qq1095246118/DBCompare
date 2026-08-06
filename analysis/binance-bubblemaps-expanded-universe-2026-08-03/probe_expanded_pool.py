#!/usr/bin/env python3
"""Select 20 additional Binance tokens with usable Bubblemaps coverage."""

from __future__ import annotations

import asyncio
import importlib.util
import json
import sys
from datetime import date
from pathlib import Path


HERE = Path(__file__).resolve().parent
PROJECT_ROOT = HERE.parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from getMarket.bubblemaps.tool.bubblemaps_api import BubblemapsApiClient


CANDIDATES_PATH = (
    PROJECT_ROOT / "screening/binance-small-volatile-2026-07-30/candidates.json"
)
ORIGINAL_PROBE_PATH = (
    PROJECT_ROOT
    / "analysis/binance-bubblemaps-additional-out-of-sample-2026-07-30/"
    "candidate-probe-results.json"
)
ORIGINAL_PROBE_SCRIPT = (
    PROJECT_ROOT
    / "analysis/binance-bubblemaps-additional-out-of-sample-2026-07-30/"
    "probe_candidates.py"
)
OUTPUT_PATH = HERE / "expanded-pool-probe-results.json"
TARGET_COUNT = 20
MIN_LISTING_AGE_DAYS = 180
MIN_EVENT_COUNT = 4
MIN_EDGE_COUNT = 50
MIN_ORDINARY_MEMBER_COUNT = 50
EXISTING_SYMBOLS = {
    "SIREN",
    "RAVE",
    "BIRB",
    "VELVET",
    "DEXE",
    "SOON",
    "ESPORTS",
    "KOMA",
    "CYS",
    "BULLA",
    "EVAA",
    "GWEI",
    "CLO",
}
CHAIN_BY_ID = {"1": "eth", "56": "bsc"}


def load_probe_module():
    spec = importlib.util.spec_from_file_location(
        "existing_candidate_probe", ORIGINAL_PROBE_SCRIPT
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("existing Bubblemaps probe unavailable")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


async def main() -> None:
    HERE.mkdir(parents=True, exist_ok=True)
    probe = load_probe_module()
    candidates = json.loads(CANDIDATES_PATH.read_text(encoding="utf-8"))["candidates"]
    cached = {
        row["symbol"]: row
        for row in json.loads(ORIGINAL_PROBE_PATH.read_text(encoding="utf-8"))
        if row.get("status") == "supported"
    }
    if OUTPUT_PATH.exists():
        previous = json.loads(OUTPUT_PATH.read_text(encoding="utf-8"))
        for row in previous.get("attempts", []):
            if row.get("status") == "supported":
                cached[row["symbol"]] = row
    eligible = [
        row
        for row in candidates
        if row["base_asset"] not in EXISTING_SYMBOLS
        and str(row.get("chain_id")) in CHAIN_BY_ID
        and float(row["listing_age_days"]) >= MIN_LISTING_AGE_DAYS
    ]
    client = BubblemapsApiClient(
        timeout=30,
        max_attempts=4,
        retry_delay=1,
        min_request_interval=0.8,
    )
    engine = probe.load_engine()
    selected = []
    attempts = []
    for candidate in eligible:
        symbol = candidate["base_asset"]
        chain = CHAIN_BY_ID[str(candidate["chain_id"])]
        bars = engine.fetch_daily_bars(symbol, date(2026, 8, 2))
        event_count = len(engine.event_episodes(bars))
        if event_count < MIN_EVENT_COUNT:
            attempts.append(
                {
                    "symbol": symbol,
                    "status": "rejected_few_events",
                    "bar_count": len(bars),
                    "event_count": event_count,
                }
            )
            continue
        if symbol in cached:
            coverage = {
                key: value
                for key, value in cached[symbol].items()
                if key
                in {
                    "status",
                    "ranked_holder_count",
                    "edge_count",
                    "cluster_count",
                    "ordinary_member_count",
                }
            }
            coverage["coverage_source"] = "cached_2026-07-30"
        else:
            coverage = await probe.probe_bubblemaps(
                client, symbol, chain, candidate["contract_address"]
            )
            coverage["coverage_source"] = "probed_2026-08-03"
        row = {
            "symbol": symbol,
            "project_name": candidate["project_name"],
            "candidate_rank": candidates.index(candidate) + 1,
            "listing_date_utc": candidate["listing_date_utc"],
            "listing_age_days_at_screen": candidate["listing_age_days"],
            "market_cap_usd_at_screen": candidate["market_cap_usd"],
            "quote_volume_usd_at_screen": candidate["quote_volume_usd"],
            "risk_score": candidate["risk_score"],
            "chain": chain,
            "contract_address": candidate["contract_address"],
            "bar_count": len(bars),
            "event_count": event_count,
            **coverage,
        }
        coverage_ok = (
            coverage["status"] == "supported"
            and int(coverage.get("edge_count", 0)) >= MIN_EDGE_COUNT
            and int(coverage.get("ordinary_member_count", 0))
            >= MIN_ORDINARY_MEMBER_COUNT
        )
        row["selection_status"] = (
            "selected" if coverage_ok else "rejected_weak_bubblemaps_coverage"
        )
        attempts.append(row)
        print(
            f"{symbol}: bars={len(bars)} events={event_count} "
            f"bubblemaps={coverage['status']} "
            f"members={coverage.get('ordinary_member_count', 0)}",
            flush=True,
        )
        if coverage_ok:
            selected.append(row)
        if len(selected) >= TARGET_COUNT:
            break
    if len(selected) < TARGET_COUNT:
        raise RuntimeError(
            f"only {len(selected)} supported candidates found; need {TARGET_COUNT}"
        )
    OUTPUT_PATH.write_text(
        json.dumps(
            {
                "generated_at": "2026-08-03",
                "target_count": TARGET_COUNT,
                "criteria": {
                    "minimum_listing_age_days": MIN_LISTING_AGE_DAYS,
                    "minimum_20pct_event_episodes": MIN_EVENT_COUNT,
                    "minimum_subgraph_edges": MIN_EDGE_COUNT,
                    "minimum_ordinary_cluster_members": MIN_ORDINARY_MEMBER_COUNT,
                    "chains": sorted(set(CHAIN_BY_ID.values())),
                    "requires_bubblemaps_support": True,
                },
                "selected": selected,
                "attempts": attempts,
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    print(f"selected {len(selected)}; wrote {OUTPUT_PATH}")


if __name__ == "__main__":
    asyncio.run(main())
