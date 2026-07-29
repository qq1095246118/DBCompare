# Polymarket Ranked Market Collection Implementation Plan

**Goal:** Implement the focused fetch, filter, rank, and save workflow described
in the corresponding design document.

## Components

1. `polymarket_api.py`: validated keyset requests, page-size bounds, timeout and
   HTTP retries.
2. `market_filter.py`: category assignment, crypto description matching, and
   market-ID merging.
3. `market_ranking.py`: Decimal normalization and ordered three-pass selection.
4. `export_polymarket_market.py`: CLI orchestration and atomic JSON output in a
   unique timestamped run directory.

## Verification

- Unit-test API pagination, retries, wrapped timeouts, and response validation.
- Unit-test category merging and crypto description-only matching.
- Unit-test metric isolation, stable ties, and cross-pass deduplication.
- End-to-end test success and sanitized failure directories with a fake client.
- Run the complete offline suite.
- Run the opt-in live Tag contract test and one real collection in a temporary
  output root.

The previous publication manifest, generation lock, backup, and rollback tasks
were removed after scope review because independent timestamped runs never
replace existing results.
