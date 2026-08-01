# Polymarket Tag-Filtered Three-Ranking Design

## Status

This design supersedes the Polymarket category, ranking, final-output, and CLI
selection behavior described in these earlier specifications:

- `2026-07-28-polymarket-ranked-market-collection-design.md`
- `2026-07-29-polymarket-category-top20-selection-design.md`
- `2026-07-30-polymarket-api-db-aligned-final-design.md`
- `2026-07-31-polymarket-per-category-cli-design.md`

The earlier implementation plans remain historical records and must not be
reused as normative requirements for this change.

The current database contract was verified with a read-only query on
2026-08-01. For rows whose `created_at` falls on the Asia/Shanghai calendar day
2026-07-31, the database contained 180 records: ten records for each of three
ranking metrics in each of six categories. Those rows contain
`ranking_metric`, `ranking_priority`, and ranking-local `rank` in both `content`
and `extra_data`. The checked-in 60-record export for the same date predates the
current database contents and is not the contract source for this design.

## Goal

Collect all active, open markets in six configured Polymarket category Tag
streams, filter only the crypto stream by official market Tag slugs, and produce
three priority-ordered Top-10 rankings per category.

The collector must remain independent of PostgreSQL. Database access was used
only to verify the output contract during design. Runtime collection, filtering,
ranking, and conversion use Polymarket API data only.

## Chosen Approach

The collector will fully paginate every configured category stream. Each page
is persisted immediately and then validated, compacted, and filtered. Ranking
runs locally only after all streams complete.

This approach was selected over two alternatives:

1. Server-side metric queries would reduce downloaded data, but they do not
   provide one verified ordering contract for the locally derived
   `dominant_probability`. Multiple independently changing queries would also
   make priority deduplication and refill harder to reproduce.
2. Streaming Top-K state would still require reading every page, would add
   complexity around repeated market IDs and later category membership, and
   would conflict with retaining a complete `clean.json` candidate artifact.

Raw API responses are never accumulated in memory. Only compact, validated
candidate data required by filtering, output conversion, and inspection is
retained until ranking completes.

## Category Collection

Use these six category streams in this fixed order:

| Category | Polymarket Tag ID |
| --- | ---: |
| `politics` | 2 |
| `geopolitics` | 100265 |
| `economy` | 100328 |
| `finance` | 120 |
| `technology` | 1401 |
| `crypto` | 21 |

Technology uses Tag 1401 only. The previous additional technology Tag IDs
105582 and 22 are removed from the configured contract.

Every keyset request includes:

```text
active=true
closed=false
include_tag=true
limit=<page-limit>
tag_id=<configured-category-tag>
```

The official keyset documentation states that the market Tags relation is
included only when `include_tag=true`:
https://docs.polymarket.com/api-reference/markets/list-markets-keyset-pagination

Category membership comes only from the official Tag stream that returned the
market. The collector must not reinterpret category membership from question,
title, description, event text, or market meaning.

Category pools are logically independent. A market returned by multiple
configured category streams is eligible in each corresponding category. It may
therefore appear once in each such category in `final.json`. Within one category,
the candidate is unique by non-empty string market ID.

## Market Tag Validation

The `tags` field requested with `include_tag=true` is part of the response
contract. For every returned market:

- `tags` must exist and be a list;
- every member must be an object; and
- every member must contain a non-empty string `slug`.

A missing or malformed Tags relation fails the run as a processing-contract
error. The collector must not fall back to description or question matching.

An empty but structurally valid Tag list, or a valid Tag list with no configured
crypto slug, is not a response-shape error. It simply cannot satisfy any crypto
topic rule.

## Crypto Filtering

Politics, geopolitics, economy, finance, and technology accept every valid
active/open market returned by their category stream. Only the crypto stream
has an additional topic filter.

Normalize official `market.tags[].slug` values by exact string comparison to the
configured lowercase slugs. Do not perform substring, label, question, title,
event, or description matching.

Evaluate these rules:

### Regulation

Canonical topic value: `regulation`

Match any of:

```text
crypto-policy
crypto-legal
regulation
regulations
sec
cftc
legal
legal-proceedings
ban
```

### ETF

Canonical topic value: `etf`

Match any of:

```text
etf
etfs
etf-approval
```

### Exchange Risk

Canonical topic value: `exchange_risk`

Require at least one slug from both groups:

```text
exchange | exchanges
```

and:

```text
bankruptcy | insolvency | hack | hacking | exploit | exploits |
cybersecurity | data-breach
```

An exchange slug alone or a risk slug alone does not match exchange risk.

### Stablecoin

Canonical topic value: `stablecoin`

Match any of:

```text
stablecoins
tether
usdt
usdc
depeg
```

### Protocol And Security

Canonical topic value: `protocol_security`

Match any of:

```text
protocol-risk
protocol-upgrade
hack
hacking
hacker
exploit
exploits
cybersecurity
data-breach
bybit-hack
```

A crypto market is eligible if it matches at least one topic. A market may match
multiple topics, including both exchange risk and protocol/security. It remains
one candidate within the crypto category, and all matched canonical topics are
saved in this fixed order in `content.crypto_topics`:

```text
regulation
etf
exchange_risk
stablecoin
protocol_security
```

The current database rows verified during design contain only the observed
`stablecoin` topic value. The other four canonical names are approved design
values rather than values inferred from existing database examples.

`clean.json` also retains the matched topic values and the exact matching Tag
slugs, sorted lexicographically, as filter evidence.

## Candidate Processing

Pages are processed as they arrive:

1. Persist the raw page under its category Tag directory.
2. Validate the response and market Tags relation.
3. Retain only the compact market and event fields needed by filtering, metric
   normalization, final conversion, and inspection.
4. Apply the crypto topic rules when processing the crypto stream.
5. Record category membership without discarding membership already obtained
   from another configured category stream.

`clean.json` remains the complete normalized candidate artifact rather than a
rank-limited output. It includes official market Tag evidence and is not affected
by the fixed Top-10 ranking capacity.

The same market ID returned more than once in one category uses one category
candidate. If repeated responses for the same category and market ID contain
different compact source data, keep the first source deterministically and
record the conflict in the in-memory processing result for tests and diagnosis.

## Metric Normalization

The three metrics, priorities, and output values are:

| Priority | Metric | Source |
| ---: | --- | --- |
| 1 | `liquidity` | market `liquidity` |
| 2 | `dominant_probability` | maximum member of `outcomePrices` |
| 3 | `volume24hr` | market `volume24hr` |

Liquidity and 24-hour volume must parse as finite, nonnegative decimals.
`outcomePrices` may be a JSON array or an already decoded array; it must be
non-empty and every member must parse as a finite decimal in the inclusive range
zero through one. Otherwise dominant probability is invalid for that market.

An invalid metric excludes the market only from that metric ranking. It remains
eligible for the other rankings. A finite zero is valid. Canonical metric
serialization and the existing numeric conversion rules remain deterministic.

## Ranking And Priority Deduplication

Process every category independently. Start each category with an empty set of
selected market IDs, then run these passes in order:

1. Sort candidates with valid liquidity by liquidity descending, with market ID
   ascending as the tie-breaker. Select up to ten and assign rank 1 through N.
2. Exclude every market ID selected by liquidity. Sort all remaining candidates
   with valid dominant probability by that metric descending and the same
   tie-breaker. Select up to ten and assign a new rank sequence 1 through N.
3. Exclude every market ID selected by either earlier pass. Sort all remaining
   candidates with valid 24-hour volume, select up to ten, and assign a new rank
   sequence 1 through N.

Deduplication happens before each lower-priority ranking is truncated. Therefore,
if an original high-ranked probability or volume candidate was already selected,
the ranking continues to later candidates until it selects ten distinct markets
or exhausts eligible candidates. This is intentional refill behavior; it does
not stop at the original raw Top 10.

Each selected record carries:

```json
{
  "ranking_metric": "dominant_probability",
  "ranking_priority": 2,
  "rank": 1
}
```

`rank` is local to the `(category, ranking_metric)` pair and always reflects the
post-exclusion selected list. It is not a continuous rank across all records in
the category.

A category may produce zero through thirty records. The complete result may
produce zero through 180 category records. A short ranking is a successful
result and is never padded with duplicates or invalid-metric candidates.

There is no cross-category selected-ID set. A market selected in multiple
categories produces one final record in each category.

## CLI Contract

Remove `--per-category`. Do not replace it with another ranking-count option.
Every ranking has a fixed capacity of ten.

`--page-limit` remains a separate request-page-size option with its existing
range of 1 through 20. It does not limit total collection or final ranking size.

The remaining timeout, retry, output-root, business-date, and page-size options
retain their existing validation and behavior.

## Final Output Contract

`final.json` retains the database-aligned wrapper:

```json
{"records": []}
```

Each record retains these 14 outer fields in the current database contract:

```text
id
data_type
title
summary
content
from_source
source_url
content_hash
extra_data
published_at
created_at
updated_at
tags
source_updated_at
```

Non-crypto `content` has these 19 fields:

```text
category
dominant_outcome
dominant_probability
event_id
fetched_at
liquidity
market_id
market_question
outcome
probability
rank
ranking_metric
ranking_priority
record_type
snapshot_date
title
volume24hr
window_end
window_start
```

Crypto `content` has the same fields plus `crypto_topics`.

`extra_data` has these 28 fields from the current database contract:

```text
acceptingOrders
active
category
closed
description
dominant_outcome
dominant_probability
end_date
endpoint
event_active
event_closed
event_id
fetched_at
liquidity
market_id
outcome_prices
outcomes
rank
ranking_metric
ranking_priority
resolution_source
snapshot_date
source_tag
start_date
title
volume24hr
window_end
window_start
```

The ranking fields in `content` and `extra_data` must have identical values.
`source_tag` is the integer category Tag ID for the final record. `endpoint` is
`/markets/keyset`. Market and first-event fields populate the corresponding
market, outcome, event, status, date, and resolution fields where available.

Fields that the API cannot reliably supply use JSON `null`. This includes the
database row ID, database content hash, and window fields. Null placeholders are
data differences, not structural differences.

The outer `tags` field remains the synthesized database-style record Tag array;
it is distinct from official Polymarket `market.tags`, which is used for crypto
filtering and retained as evidence only in candidate data.

Records are emitted in deterministic order:

1. configured category order;
2. `ranking_priority` 1, 2, then 3; and
3. ranking-local `rank` ascending.

## Artifacts

Each run keeps its unique directory and artifact names:

```text
getMarket/Polymarket/market/YYYY-MM-DD_HHMMSS_<random>/
|-- raw/tag-*/page-*.json
|-- clean.json
|-- final.json
`-- error.json  # failure only
```

Raw pages are written as they arrive. `clean.json` and `final.json` are written
only after all configured streams complete and processing succeeds. Individual
JSON files continue to use atomic replacement within the unique run directory.

## Failure Handling

Any request, retry exhaustion, HTTP failure, invalid response shape, repeated
pagination cursor, or malformed market Tags relation fails the complete run.
Already written raw pages remain available. The run writes sanitized
`error.json` and does not write `clean.json` or `final.json`.

A valid crypto market that matches no configured topic is a normal rejection.
An invalid or missing metric affects only that metric pass. A ranking or category
with fewer than ten eligible candidates is a successful short result.

Runtime errors must not expose raw exception messages, credentials, response
bodies, or unbounded market content in `error.json`.

## Component Changes

- `polymarket_api.py`: request `include_tag=true` and enforce the Tags response
  contract while retaining current pagination and retry behavior.
- `market_filter.py`: use the six category Tag IDs above, retain official Tag
  evidence, remove description keywords, and implement the five exact crypto
  topic rules including exchange-risk AND matching.
- `market_ranking.py`: give each priority pass an independent capacity of ten,
  exclude prior selections before truncation, refill from later candidates, and
  assign ranking-local ranks.
- `final_contract.py`: align `content` and `extra_data` with the verified current
  database shape and map ranking fields into both objects.
- `export_polymarket_market.py`: remove `--per-category`, preserve full-page
  collection and artifact orchestration, and pass the revised candidates through
  filtering, ranking, and conversion.
- Polymarket and global operator documentation: replace the old Top-20 fallback,
  description filter, multi-Tag technology, configurable category cap, and old
  final-field descriptions.

The database exporter is not changed by this feature.

## Test Contract

Offline tests must verify:

- every API request includes `include_tag=true`;
- missing and malformed Tags relations fail safely;
- a structurally valid nonmatching crypto Tag set is rejected without failing;
- description and question text never affect crypto eligibility;
- every configured crypto slug maps to the correct canonical topic;
- exchange risk requires one exchange slug and one risk slug;
- shared risk slugs may independently match protocol/security;
- multiple matched crypto topics produce one crypto candidate with all topics;
- only Tag 1401 populates the configured technology stream;
- each category has three independent ranking capacities of ten;
- lower-priority rankings exclude earlier winners before truncation and use the
  original 11th and later candidates to refill;
- every ranking has an independent rank sequence starting at one;
- invalid metrics affect only their own ranking;
- metric ties use market ID ascending;
- market IDs are unique within a category and may repeat across categories;
- short rankings succeed with their actual record count;
- the fixed maximum is thirty records per category and 180 total;
- ranking metric, priority, and rank match between `content` and `extra_data`;
- non-crypto, crypto, `extra_data`, and outer field sets match the current
  database contract;
- unavailable values are serialized as `null` without changing field shape;
- `--per-category` is rejected after removal and `--page-limit` remains
  independent; and
- collection or processing failure writes sanitized `error.json` without final
  artifacts.

Run the focused Polymarket tests, the complete offline regression suite, syntax
checks, and the read-only live Polymarket smoke test. The live test must confirm
that `include_tag=true` yields a valid `tags[].slug` relation without asserting a
fixed live market count.

## Non-Goals

- Reading PostgreSQL from the API collector
- Matching crypto topics from description, question, title, or Tag label
- Reinterpreting official category membership from market semantics
- Deduplicating the same market across different categories
- Making the fixed Top-10 ranking size configurable
- Writing API selections into PostgreSQL
- Adding scheduling or changing database export behavior
