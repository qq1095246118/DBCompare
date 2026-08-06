# Polymarket Category Top-20 Selection Design

## Status

This design supersedes the global three-pass selection contract in
`2026-07-28-polymarket-ranked-market-collection-design.md`. Collection,
pagination, retry, raw-page persistence, and run-directory behavior remain
unchanged unless this document says otherwise.

## Goal

Collect active, open Polymarket markets for six configured categories and save
up to 20 selected market records per category. A market may be selected once in
each category it matches, but it may appear only once within a given category.
The final output therefore contains at most 120 category records and may contain
fewer than 120 globally distinct market IDs.

## Category Membership

Use only Polymarket Tag membership to assign the six configured categories:

| Category | Tag IDs |
| --- | --- |
| politics | 2 |
| geopolitics | 100265 |
| economy | 100328 |
| finance | 120 |
| technology | 105582, 1401, 22 |
| crypto | 21 |

Do not reinterpret a market's category from its question, title, or
description. A market returned by multiple configured category Tag streams
belongs to every corresponding category.

Technology is one category. Markets returned by any of its three Tag streams
enter the same technology candidate pool and are deduplicated there by market
ID.

Crypto retains its existing additional rule: the market description must match
at least one configured crypto keyword, case-insensitively. A crypto keyword
miss removes only crypto membership. Membership obtained from another category
remains valid.

## Candidate Model

Keep one normalized candidate object per market ID in memory. The candidate
records all matched categories, Tag IDs, crypto keywords, compact source fields,
and normalized metrics. This representation deduplicates storage without
discarding category membership.

`clean.json` remains a globally unique candidate list. It is not the final
category-expanded result.

## Per-Category Selection

Normalize these metrics using the existing strict decimal rules:

1. `liquidity`
2. `dominant_probability`, defined as the maximum valid `outcomePrices` value
3. `volume24hr`

For each category independently:

1. Start with no selected market IDs and a target of 20 records.
2. Sort candidates with a valid liquidity value by liquidity descending and
   market ID ascending. Select as many as needed, up to the remaining capacity.
3. If fewer than 20 records have been selected, rank the remaining candidates
   with a valid dominant probability by that metric using the same tie-breaker,
   and fill only the remaining capacity.
4. If the category is still below 20, repeat with 24-hour volume.
5. Stop after the volume pass even if the category remains below 20. Do not
   duplicate records or use a lower-priority metric to replace an earlier
   selection.

A finite zero is a valid liquidity or volume value. A metric that is missing,
non-numeric, non-finite, negative, or outside its existing allowed range is
invalid only for that metric pass. The market may still be selected by a later
metric.

The priority metrics are fallbacks used to fill one category Top 20. They are
not three independent Top-20 lists. If liquidity supplies 20 records, dominant
probability and volume select nothing for that category.

The stable category output order is:

1. politics
2. geopolitics
3. economy
4. finance
5. technology
6. crypto

## Final Output

Keep the existing unique run directory and artifact names:

```text
getMarket/Polymarket/market/YYYY-MM-DD_HHMMSS_<random>/
|-- raw/tag-*/page-*.json
|-- clean.json
|-- final.json
`-- error.json  # failure only
```

`final.json` remains a flat list. Expand each category selection into a separate
record, so the same market ID may appear in multiple records with different
`selected_category` values. Order records by the fixed category order above and
then by `rank_in_category`.

Each final record retains the candidate fields and adds:

```json
{
  "selected_category": "technology",
  "selected_by": "liquidity",
  "priority": 1,
  "rank_in_category": 1
}
```

`selected_by` is the metric that admitted the record. `priority` is `1`, `2`,
or `3` for liquidity, dominant probability, or 24-hour volume. The
`rank_in_category` sequence is continuous from 1 through the category's final
record count, including records admitted by fallback metrics.

## Failure Handling

All configured Tag streams must complete. A request or pagination failure in
any Tag fails the run, writes the existing sanitized `error.json`, and prevents
`final.json` from being written. Partial Tag data must not produce category
rankings.

Metric validation failures are candidate-level and metric-specific, not run
failures. A category with fewer than 20 eligible records after all three passes
is a successful, shorter category result.

Raw pages continue to be written as they arrive. `clean.json`, `final.json`, and
`error.json` continue to use atomic single-file replacement inside a unique run
directory.

## Component Changes

- `market_filter.py`: retain Tag-to-category mapping, crypto filtering, and
  market-ID merging with all matched category memberships.
- `market_ranking.py`: replace global three-pass selection with independent
  per-category fallback selection and category-expanded final records.
- `export_polymarket_market.py`: keep collection orchestration and artifact
  behavior; write the revised candidate and final selection results.
- README and the global command guide: replace the global-30 description with
  six category Top-20 selections and explain the fallback semantics.

The API client, pagination limit, retry policy, raw-page layout, and run naming
do not change.

## Test Contract

Offline tests must verify:

- each category selects at most 20 records;
- the total output contains at most 120 category records;
- 20 valid liquidity candidates prevent lower-priority selection;
- dominant probability and then volume fill only remaining capacity;
- a category may finish below 20 without copying records;
- a market is unique within one category but may appear in multiple categories;
- technology deduplicates markets returned by more than one technology Tag;
- crypto description rejection removes only crypto membership;
- invalid metrics affect only their metric pass;
- metric ties use market ID ascending;
- category and rank output order is deterministic;
- a Tag collection failure produces no final result.

The existing read-only live smoke test continues to validate the configured Tag
streams without asserting a fixed market count.

## Non-Goals

- Reproducing the database collector's single-category assignment
- Semantic classification from market text
- A compatibility mode for the previous global-30 selection
- Writing API results into PostgreSQL
- Adding a scheduler or changing the external API contract
