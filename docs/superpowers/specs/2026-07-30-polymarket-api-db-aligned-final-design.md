# Polymarket API Final DB-Aligned Structure Design

## Goal

Change only the Polymarket API collector's successful `final.json` so its JSON
shape matches the processed Polymarket DB export. The values do not need to be
equal to database values. Fields that the API collector cannot obtain reliably
are present with JSON `null` values.

This is a structural compatibility feature. The API artifact does not claim to
be a database artifact and is not required to pass the DB exporter's strict
record validator.

## Current State

The API collector currently writes `ranked.selected` directly as a top-level
array. Each item contains API-specific selection fields such as
`selected_category`, `rank_in_category`, `selected_by`, `normalized_metrics`,
and `source`.

The DB exporter writes an object with one `records` array. Each record has 14
database-envelope fields, a fixed 17-field business object under `content`, and
an 8-field object under `extra_data`.

## Scope

Only `getMarket/Polymarket/.../final.json` changes. The following behavior stays
unchanged:

- Gamma API requests and raw page artifacts;
- Tag-based category membership and crypto keyword filtering;
- candidate merging and per-category Top 20 ranking;
- `clean.json` structure and contents;
- record order from the ranking result; and
- `error.json` safety behavior and process exit codes.

This feature does not change `getDB/Polymarket`, call PostgreSQL, compare API and
DB values, or add a comparison report.

## Architecture

Add `getMarket/Polymarket/tool/final_contract.py` with a pure conversion entry
point:

```python
build_db_aligned_final(
    selected,
    *,
    business_date,
    captured_at,
) -> dict
```

The data flow becomes:

```text
Gamma API
  -> merge_markets()
  -> select_ranked_markets()
  -> ranked.selected
  -> build_db_aligned_final()
  -> final.json
```

`export_polymarket_market.py` builds the converted payload after ranking and
before writing successful artifacts. It continues to write `ranked.candidates`
unchanged to `clean.json`.

The converter must not mutate `ranked.selected` or any nested source objects.

## Final Artifact Contract

`final.json` changes from a top-level array to exactly:

```json
{
  "records": []
}
```

Each entry in `records` contains exactly these 14 outer fields:

```text
id, data_type, title, summary, content, from_source, source_url,
content_hash, extra_data, published_at, created_at, updated_at,
tags, source_updated_at
```

Each `content` object contains exactly:

```text
category, dominant_outcome, dominant_probability, event_id, fetched_at,
liquidity, market_id, market_question, outcome, probability, rank,
record_type, snapshot_date, title, volume24hr, window_end, window_start
```

Each `extra_data` object contains exactly:

```text
endpoint, event_id, fetched_at, market_id, rank, snapshot_date,
window_end, window_start
```

API-specific fields are not copied as extra top-level fields. Candidate-level
details (`categories`, matched Tags, normalized metrics, and `source`) remain
available in `clean.json`. Selection-only `selected_by` and `priority` values
are intentionally no longer persisted. `selected_category` and
`rank_in_category` remain represented as `content.category` and `content.rank`.

## Field Mapping

### Outer Record

| Field | Value |
| --- | --- |
| `id` | `null` |
| `data_type` | `"PREDICTION_MARKET_SELECTION"` |
| `title` | `source.question`, or `null` |
| `summary` | `source.question`, or `null` |
| `content` | The mapped business object below |
| `from_source` | `"polymarket"` |
| `source_url` | `null` because an event URL is not reliably available |
| `content_hash` | `null`; do not synthesize a database content hash |
| `extra_data` | The mapped metadata object below |
| `published_at` | `null` |
| `created_at` | API run capture time normalized to `Asia/Shanghai` |
| `updated_at` | Same value as `created_at` |
| `tags` | `active`, `category:<category>`, `prediction`, `selected-market` |
| `source_updated_at` | `null` |

### Content

| Field | Value |
| --- | --- |
| `category` | `selected_category` |
| `market_id` | selected record `market_id` |
| `rank` | `rank_in_category` |
| `market_question` | `source.question`, or `null` |
| `title` | `source.question`, or `null` |
| `liquidity` | normalized liquidity converted to a JSON number, or `null` |
| `volume24hr` | normalized 24-hour volume converted to a JSON number, or `null` |
| `dominant_probability` | normalized dominant probability as a JSON number, or `null` |
| `probability` | same value as `dominant_probability` |
| `dominant_outcome` | outcome corresponding to the greatest valid price, or `null` |
| `outcome` | same value as `dominant_outcome` |
| `record_type` | `"prediction_market_selection"` |
| `fetched_at` | API run capture time in UTC using a `Z` suffix |
| `snapshot_date` | requested/default API business date in `YYYY-MM-DD` form |
| `event_id` | `null` |
| `window_start` | `null` |
| `window_end` | `null` |

The converter accepts Gamma `outcomes` and `outcomePrices` as either decoded
arrays or JSON array strings. The first greatest price wins a tie. Invalid or
incomplete optional outcome data produces `null`; it does not fail the run.

Normalized metric strings are converted to finite Python floats so the JSON
type is `number`. Missing or invalid optional metrics produce `null`.

### Extra Data

| Field | Value |
| --- | --- |
| `endpoint` | `null` because selected records may originate from multiple Tag requests |
| `event_id` | `null` |
| `fetched_at` | API run capture time in UTC with an explicit `+00:00` offset |
| `market_id` | selected record `market_id` |
| `rank` | `rank_in_category` |
| `snapshot_date` | business date in `YYYY-MM-DD` form |
| `window_start` | `null` |
| `window_end` | `null` |

## Required Inputs And Validation

`business_date` must be a `date`. `captured_at` must be a timezone-aware
`datetime`.

Every selected item must be a mapping with:

- a non-whitespace `market_id` string;
- a configured non-whitespace `selected_category` string;
- a positive integer `rank_in_category`, excluding booleans; and
- a mapping under `source`.

The conversion result is validated before it is returned:

- the top-level object has exactly `records`;
- every outer record has exactly the 14 documented fields;
- every `content` object has exactly the 17 documented fields; and
- every `extra_data` object has exactly the 8 documented fields.

Null placeholders are intentional. The API result must not be passed to
`getDB.Polymarket.tool.contract.validate_serialized_record`, which correctly
requires real DB values such as an integer database `id`.

## Error Handling And Atomicity

Conversion happens after ranking and before successful `clean.json` and
`final.json` writes. A missing required selection field or an internal shape
violation raises a validation error. The existing collector boundary catches
the error, returns exit code `1`, writes a sanitized `error.json`, and does not
write `final.json`.

Optional API data failures use `null` and do not fail conversion. Successful
`clean.json` and `final.json` writes continue to use `write_json_atomic`.

Record ordering is unchanged from `ranked.selected`: fixed category order,
then `rank_in_category`. A market selected in multiple categories remains one
record per category.

## Compatibility

Changing `final.json` from an array to `{"records": [...]}` is an intentional
breaking format change. Consumers must read `payload["records"]`. `clean.json`
remains a top-level array for consumers that require API-specific details.

Because `clean.json` remains unchanged, it does not gain the selection-only
`selected_by` or `priority` fields. Dropping those two fields is an explicitly
accepted consequence of strict final-structure alignment.

The filename remains `final.json`; it is not renamed to
`polymarket_db.json`, and no DB-style manifest or errors artifact is added.

## Tests

Add focused converter tests for:

- exact top-level, outer, `content`, and `extra_data` field sets;
- mappings, constants, time-zone formatting, and intentional nulls;
- metric conversion to JSON numbers;
- decoded and JSON-string outcome arrays, including deterministic ties;
- malformed optional outcome/metric values becoming `null`;
- required-field rejection;
- multiple category records for one market;
- record ordering preservation; and
- source input immutability.

Update CLI tests to prove:

- `clean.json` retains its existing API candidate structure;
- `final.json` uses the new `{"records": [...]}` shape;
- a representative final record has the exact DB-aligned fields; and
- conversion failure returns `1`, writes `error.json`, and leaves no
  `final.json`.

Run all Polymarket API and DB tests plus the complete offline regression suite.

## Documentation

Update the Polymarket API README and global command guide to state that
`final.json` is a DB-aligned structural payload, contains nullable placeholders,
and is not a validated DB generation. Document which candidate-level API fields
remain in `clean.json` and that `selected_by` and `priority` are no longer
persisted.
