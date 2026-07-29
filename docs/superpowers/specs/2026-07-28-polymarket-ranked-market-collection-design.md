# Polymarket Ranked Market Collection Design

## Goal

Collect active, open Polymarket markets for six configured subject areas and
save at most 30 distinct markets selected by three ordered global rankings.

## Collection

Query `GET https://gamma-api.polymarket.com/markets/keyset` for these Tag IDs:

| Category | Tag IDs |
| --- | --- |
| politics | 2 |
| geopolitics | 100265 |
| economy | 100328 |
| finance | 120 |
| technology | 105582, 1401, 22 |
| crypto | 21 |

Every request includes `active=true`, `closed=false`, and keyset pagination.
The page size defaults to 20 and cannot exceed 20 because larger nested market
responses exceeded the safe resource envelope during live verification.

Markets are deduplicated by market ID. Crypto membership additionally requires
a case-insensitive configured keyword match in `description` only. Failure to
match crypto does not remove a category obtained through another Tag.

## Ranking

Normalize numbers with `Decimal`. Define `dominant_probability` as the maximum
valid member of `outcomePrices`. Globally select:

1. Top 10 by liquidity.
2. Excluding selected IDs, top 10 by dominant probability.
3. Excluding selected IDs, top 10 by 24-hour volume.

All metrics sort descending, with market ID ascending as the stable tie-breaker.
Invalid metrics exclude a market only from that metric's pass.

## Output

Each run creates a new directory:

```text
getMarket/Polymarket/market/YYYY-MM-DD_HHMMSS_<random>/
├── raw/tag-*/page-*.json
├── clean.json
├── final.json
└── error.json  # failure only
```

Raw pages are written as soon as they arrive. Only the compact market fields
needed for filtering, ranking, and downstream inspection remain in memory.
`clean.json` contains eligible normalized candidates. `final.json` contains the ordered selections. If collection or
processing fails, the run directory contains `error.json` and no final result.

Runs never overwrite one another, so the collector does not need staging
generations, manifests, locks, backups, publication transactions, or rollback.
Each JSON file is still written through a temporary file and atomically renamed
to avoid partially written individual files.

## Reliability

Retry timeouts, HTTP 429, and HTTP 5xx responses for a bounded number of
attempts. Recognize both direct socket timeouts and timeouts wrapped by
`urllib.error.URLError`. Persist only fixed, sanitized failure fields; never
persist raw exception text or credentials.
