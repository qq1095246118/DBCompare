# Polymarket Database Export Design

## Goal

Add a read-only PostgreSQL exporter under `getDB/Polymarket` that exports the
Polymarket selection rows already stored by the database pipeline. The exporter
must preserve the database result set faithfully: it does not call Polymarket,
reclassify markets, rerun Top-20 selection, or deduplicate rows.

The export date is the `Asia/Shanghai` calendar date of `created_at`. A run
produces one validated artifact generation for that date and returns a
non-zero exit code whenever the generation is incomplete or unusable.

## Confirmed Source Contract

The source is `public.information`, restricted by both of these predicates:

```sql
from_source = 'polymarket'
AND data_type = 'PREDICTION_MARKET_SELECTION'
```

Relevant table columns are:

```text
id, data_type, title, summary, content, from_source, source_url,
content_hash, extra_data, published_at, created_at, updated_at,
tags, source_updated_at
```

`id` is a PostgreSQL `bigint`; `published_at`, `created_at`, `updated_at`, and
`source_updated_at` are `timestamp with time zone`; `content` is text containing
a JSON object; `extra_data` is `jsonb`; and `tags` is `text[]`. Observed data can
contain more than one ingestion batch on one Shanghai date, so the exporter
must not select only the latest `snapshot_date` or batch.

## Non-Goals

- Calling the Polymarket Gamma API
- Reconstructing API-side `raw`, `clean.json`, or `final.json` artifacts
- Reapplying Tag filters, crypto keyword rules, or Top-20 ranking
- Inferring category membership from a market question, title, or description
- Deduplicating by `content_hash`, `market_id`, event, category, or snapshot
- Adding a scheduler or changing the database writer
- Refactoring the existing Bubblemaps exporter into a generic framework

## Architecture

Create this feature-owned package:

```text
getDB/Polymarket/
|-- __init__.py
|-- README.md
`-- tool/
    |-- __init__.py
    |-- contract.py
    |-- db_source.py
    `-- export_polymarket_db.py
```

`db_source.py` owns PostgreSQL settings, the parameterized query, and the
read-only transaction. `contract.py` converts and validates individual source
rows, isolates row errors, and applies output ordering. The CLI module owns date
parsing, orchestration, locking, artifact commits, manifest validation, and exit
codes.

Reuse `common.time_window.china_day_bounds`,
`common.artifacts.output_directory`, and
`common.artifacts.write_json_atomic`. Do not import Bubblemaps business modules
or change their behavior. The small amount of exporter-specific locking and
manifest orchestration remains local so this work does not expand into a shared
framework refactor.

Load the repository `.env` with `python-dotenv` and require non-empty `PGHOST`,
`PGPORT`, `PGDATABASE`, `PGUSER`, and `PGPASSWORD` values. `PGPORT` must parse as
an integer from 1 through 65535. Missing or invalid settings fail before a
connection is attempted and pass through the same sanitized source-error
boundary as connection and query failures.

## CLI And Date Window

The entry point is:

```bash
.venv/bin/python -m getDB.Polymarket.tool.export_polymarket_db \
  --date YYYY-MM-DD
```

`--date` defaults to the current `Asia/Shanghai` date. `--output-root` defaults
to `getDB/Polymarket/db` and is available for tests and controlled alternate
destinations.

For business date `D`, calculate `[lower, upper)` with
`china_day_bounds(D)`. For example, `2026-07-29` in Shanghai maps to
`2026-07-28T16:00:00+00:00 <= created_at < 2026-07-29T16:00:00+00:00`.
The SQL must use bound parameters:

```sql
SELECT id, data_type, title, summary, content, from_source, source_url,
       content_hash, extra_data, published_at, created_at, updated_at,
       tags, source_updated_at
FROM public.information
WHERE from_source = %s
  AND data_type = %s
  AND created_at >= %s
  AND created_at < %s
ORDER BY created_at, id;
```

Run the query in a `REPEATABLE READ READ ONLY` transaction. Commit only after
the complete result set has been fetched, and roll back on any query or fetch
failure. Row conversion happens after the source transaction has closed so one
malformed row cannot invalidate the database read.

## Record Contract

The primary artifact is an object with a single `records` array:

```json
{
  "records": [
    {
      "id": 123,
      "data_type": "PREDICTION_MARKET_SELECTION",
      "title": "...",
      "summary": "...",
      "content": {
        "category": "politics",
        "market_id": "...",
        "rank": 1
      },
      "from_source": "polymarket",
      "source_url": "...",
      "content_hash": "...",
      "extra_data": {},
      "published_at": "2026-07-29T08:00:00+08:00",
      "created_at": "2026-07-29T09:00:00+08:00",
      "updated_at": "2026-07-29T09:00:00+08:00",
      "tags": [],
      "source_updated_at": "2026-07-29T08:00:00+08:00"
    }
  ]
}
```

Preserve every selected table column. Parse `content` exactly once and retain
the resulting object under `content`; do not flatten it into the database
metadata. Preserve `extra_data` as the decoded JSON value returned by psycopg.
Serialize non-null timestamp columns as ISO-8601 strings normalized to
`Asia/Shanghai`, always with the explicit `+08:00` offset. Preserve nullable
columns as JSON `null`.

A valid business row requires:

- `content` is either an already-decoded object or text that parses as
  standards-compliant JSON to an object; non-standard `NaN`, `Infinity`, and
  `-Infinity` values are rejected in either representation;
- `content.category` is a string containing at least one non-whitespace
  character;
- `content.market_id` is a string containing at least one non-whitespace
  character; and
- `content.rank` is a positive integer, excluding booleans.

Copy an already-decoded object before normalization so output construction does
not mutate the source row. Validation does not trim or otherwise rewrite valid
category and market ID values. Other content keys are preserved without
business reinterpretation. A structurally valid category outside the six
configured categories is not a row error: the database category remains
authoritative.

Every non-null timestamp must arrive from psycopg as a timezone-aware
`datetime`. A non-datetime or naive value is a row-validation error; never infer
its timezone from the host. Valid values are converted to `Asia/Shanghai` for
serialization.

## Deterministic Ordering And Duplicates

Sort records by:

1. category order: `politics`, `geopolitics`, `economy`, `finance`,
   `technology`, `crypto`;
2. positive `content.rank`, ascending;
3. `created_at` instant, ascending; and
4. numeric database `id`, ascending.

Unknown but valid categories follow the six configured categories and are
ordered by `(category.casefold(), category)` before applying the remaining
keys.

Never collapse records. If the same market is stored in multiple categories,
snapshots, ingestion batches, or rows, each valid database row remains one
output record.

## Artifacts And Commit Protocol

One date maps to one stable directory:

```text
getDB/Polymarket/db/YYYY-MM-DD/
|-- .generation.lock
|-- polymarket_db.json
|-- errors.json
`-- manifest.json
```

Use a cooperative exclusive `fcntl.flock` while replacing a generation and a
shared lock while reading one. Write `polymarket_db.json` and `errors.json`
atomically. Write `manifest.json` last as the commit record. Before the final
manifest, its state is `in_progress`; an interrupted or failed commit must not
leave a manifest that readers accept as complete.

Every completed invocation represents the latest attempt for that date. A
committed `partial` or `failed` generation therefore supersedes an older
generation in the same date directory instead of silently serving stale data.
This directory is not a history archive.

The final manifest contains exactly:

```json
{
  "source": "postgresql",
  "dataset": "polymarket",
  "generation_id": "uuid",
  "status": "success",
  "business_date": "2026-07-29",
  "timezone": "Asia/Shanghai",
  "utc_lower_bound": "2026-07-28T16:00:00+00:00",
  "utc_upper_bound": "2026-07-29T16:00:00+00:00",
  "captured_at": "...+00:00",
  "source_row_count": 60,
  "record_count": 60,
  "error_count": 0,
  "category_counts": {
    "politics": 10
  },
  "artifacts": {
    "polymarket_db.json": {"sha256": "..."},
    "errors.json": {"sha256": "..."}
  }
}
```

`category_counts` is computed from valid output records and includes unknown
valid category names if present. It is sparse: a category with zero valid
records is omitted. `source_row_count` is the number fetched when the query
completed, including invalid rows. It is JSON `null` when configuration,
connection, query, or fetch failed before a complete result set was available.
`record_count` and `error_count` are always the exact lengths of `records` and
`errors.json`.

### Validated Reader Contract

A validated reader holds the shared lock across all reads and enforces these
rules:

- `polymarket_db.json` is an object with exactly one `records` key whose value
  is an array of objects; each record contains exactly the selected table
  columns and satisfies the record contract above.
- `errors.json` is an array. Every element is an object with non-empty string
  `stage`, `type`, and `message` fields. Row errors additionally carry their
  database `id` and nullable `content_hash`.
- `manifest.json` is an object with exactly the fields shown above. `source`,
  `dataset`, and `timezone` equal their documented constants; `generation_id`
  is a valid UUID string; `status` is `success`, `partial`, or `failed`;
  `business_date` matches the date directory; UTC bounds equal
  `china_day_bounds(business_date)`; and `captured_at` is an aware UTC
  ISO-8601 timestamp. Booleans are not accepted as integers.
- `record_count` equals the primary array length, `error_count` equals the
  error-array length, and `category_counts` exactly matches a fresh count of
  valid records. Every category-count value is a non-negative integer.
- When `source_row_count` is an integer greater than zero, it equals
  `record_count` plus the number of row-validation errors. A zero source count
  is valid only for the explicit no-record failure. A null source count is valid
  only for a sanitized source failure.
- `artifacts` contains exactly `polymarket_db.json` and `errors.json`. Each
  `sha256` is a lowercase 64-character hexadecimal string matching the file
  bytes.

Integrity validation may return a `partial` or `failed` generation; consumers
must inspect `status` before using its business records.

## Status, Errors, And Exit Codes

Use these final states:

| Condition | Manifest status | Exit code |
| --- | --- | --- |
| At least one valid row and no errors | `success` | 0 |
| At least one valid row and one or more row errors | `partial` | 1 |
| Query/configuration failure | `failed` | 1 |
| Zero source rows | `failed` | 1 |
| Source rows exist but every row is invalid | `failed` | 1 |

For `partial`, omit invalid rows from `polymarket_db.json` and retain all valid
rows. For `failed`, write `{"records": []}` unless an artifact failure prevents
a consistent generation from being committed. Status/count invariants are:

- `success`: `record_count > 0`, `error_count == 0`, and integer
  `source_row_count == record_count`;
- `partial`: `record_count > 0`, `error_count > 0`, and integer
  `source_row_count > record_count`; and
- `failed`: `record_count == 0`, `error_count > 0`, with `source_row_count`
  either `null`, zero, or equal to the row-validation error count.

`errors.json` is always a top-level JSON array. A completed query with zero rows
adds one synthetic error with stage `source_selection`, type `NoRecordsError`,
and message `No Polymarket rows found for requested date`. An all-invalid result
contains one row-validation error per fetched row. A configuration, connection,
query, or fetch failure contains one sanitized source error and uses a null
`source_row_count`.

Each row error in `errors.json` contains only a safe row identity and diagnostic
fields:

```json
{
  "id": 123,
  "content_hash": "...",
  "stage": "row_validation",
  "type": "ValueError",
  "message": "content.rank must be a positive integer"
}
```

Do not copy raw `content` into an error. Source errors use a generic sanitized
message and an allowlisted exception type. They must not expose the PostgreSQL
host, port, database name, user, password, DSN, or server-provided connection
text.

If an artifact write or final manifest write fails, return `1` and leave no
manifest that claims a valid final generation. Best-effort artifact diagnostics
may be written to `errors.json`, following the existing Bubblemaps safety
pattern.

## Tests And Verification

Add focused tests:

- `tests/test_polymarket_db_source.py`: settings validation, parameterized
  source predicates, required environment variables, port bounds, UTC
  half-open bounds, deterministic initial ordering, read-only repeatable-read
  transaction, commit, and rollback.
- `tests/test_polymarket_db_contract.py`: text and already-decoded content
  objects, required fields, rejection of non-standard constants and
  whitespace-only identifiers, positive integer rank, complete column
  preservation, aware timestamp requirements, Shanghai timestamp
  serialization, fixed category order, unknown-category placement, stable
  tie-breakers, duplicate preservation, and per-row error isolation.
- `tests/test_polymarket_db_cli.py`: argument defaults, explicit dates, output
  paths, success/partial/failed manifests, zero-row failure, all-invalid failure,
  exact count semantics, errors-array shape, exit codes, credential redaction,
  atomic replacement, locking, artifact hashes, and every validated-reader
  invariant.

Run the entire non-live suite to detect regressions in the existing Bubblemaps
and Polymarket API collectors. Do not add a network-dependent test.

For local acceptance, run the exporter against an existing date using `.env`.
Use an independent read-only SQL query over the same UTC bounds to compare
source row count, valid row count, category counts, and duplicate preservation.
Do not hard-code the currently observed row totals in automated tests because
the database can receive new rows.

Update `getDB/Polymarket/README.md` and the Polymarket database section of
`命令使用指南.md` with the command, date semantics, artifacts, manifest states,
and exit-code contract.

## Acceptance Criteria

- A date is selected by `created_at` in `Asia/Shanghai`, using the exact UTC
  half-open interval.
- Only the two confirmed Polymarket source predicates are exported.
- Every valid source row becomes exactly one output row, with no re-ranking,
  reclassification, or deduplication.
- Records use the confirmed category/rank/created-at/id order.
- Malformed rows cannot suppress valid rows and always make the run non-zero.
- Empty dates and entirely invalid result sets fail visibly.
- A committed generation is atomically readable and hash-verifiable.
- Database credentials and raw malformed content do not appear in diagnostics.
- Existing offline tests continue to pass.
