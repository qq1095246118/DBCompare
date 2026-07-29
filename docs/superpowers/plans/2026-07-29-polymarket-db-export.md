# Polymarket Database Export Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a read-only `getDB/Polymarket` CLI that exports every valid Polymarket selection row for one `Asia/Shanghai` `created_at` date into locked, atomic, hash-validated database artifacts.

**Architecture:** Keep Polymarket database behavior in a feature-owned package. `contract.py` converts and orders rows without reclassification, ranking, or deduplication; `db_source.py` owns the parameterized read-only PostgreSQL transaction; `export_polymarket_db.py` owns CLI parsing, status calculation, sanitized errors, generation locking, atomic artifact commits, and strict validated reads. Reuse only `common.time_window` and `common.artifacts`, leaving Bubblemaps behavior unchanged.

**Tech Stack:** Python 3.12, psycopg 3, python-dotenv, pytest, `zoneinfo`, POSIX `fcntl.flock`, existing atomic JSON helpers.

---

## Reference Contract

Implement exactly against
`docs/superpowers/specs/2026-07-29-polymarket-db-export-design.md`.
The database is authoritative: do not call the API, rerun Top-20 selection,
infer categories from text, select only the latest snapshot, or deduplicate any
valid source rows.

## File Map

- Create `getDB/Polymarket/__init__.py`: package marker.
- Create `getDB/Polymarket/tool/__init__.py`: tool package marker.
- Create `getDB/Polymarket/tool/contract.py`: source-row parsing, validation,
  timestamp normalization, row-error isolation, category counts, and stable
  ordering.
- Create `getDB/Polymarket/tool/db_source.py`: `.env` PostgreSQL settings and
  the one read-only `public.information` query.
- Create `getDB/Polymarket/tool/export_polymarket_db.py`: CLI, manifest/status
  construction, sanitized failures, locking, atomic writes, and strict reads.
- Create `tests/test_polymarket_db_contract.py`: record and batch contract.
- Create `tests/test_polymarket_db_source.py`: settings, SQL, transaction, and
  rollback contract.
- Create `tests/test_polymarket_db_cli.py`: CLI states and artifact protocol.
- Create `getDB/Polymarket/README.md`: operator-facing command and artifact
  documentation.
- Modify `命令使用指南.md`: add the Polymarket DB command and test/help entries
  without changing unrelated API collector instructions.
- Do not modify `getDB/bubblemaps`, `getMarket/Polymarket`, `common`, or existing
  test behavior.

### Task 1: Normalize One Polymarket Database Row

**Files:**
- Create: `getDB/Polymarket/__init__.py`
- Create: `getDB/Polymarket/tool/__init__.py`
- Create: `getDB/Polymarket/tool/contract.py`
- Create: `tests/test_polymarket_db_contract.py`

- [ ] **Step 1: Create package markers and write the failing row tests**

Create the two package markers with these exact docstrings:

```python
# getDB/Polymarket/__init__.py
"""Polymarket database export package."""
```

```python
# getDB/Polymarket/tool/__init__.py
"""Polymarket database export tools."""
```

Create `tests/test_polymarket_db_contract.py` with this initial contract:

```python
from copy import deepcopy
from datetime import datetime, timezone
import json

import pytest

from getDB.Polymarket.tool.contract import ROW_FIELDS, normalize_row


UTC_TIME = datetime(2026, 7, 29, 1, 2, 3, tzinfo=timezone.utc)


def source_row(
    *,
    row_id=101,
    category="politics",
    market_id="market-1",
    rank=1,
    content=None,
    created_at=UTC_TIME,
):
    payload = (
        {"category": category, "market_id": market_id, "rank": rank,
         "market_question": "Question?"}
        if content is None
        else content
    )
    return {
        "id": row_id,
        "data_type": "PREDICTION_MARKET_SELECTION",
        "title": "Stored title",
        "summary": None,
        "content": json.dumps(payload) if isinstance(payload, dict) else payload,
        "from_source": "polymarket",
        "source_url": "https://example.invalid/market-1",
        "content_hash": f"hash-{row_id}",
        "extra_data": {"event_id": "event-1"},
        "published_at": UTC_TIME,
        "created_at": created_at,
        "updated_at": UTC_TIME,
        "tags": ["prediction-market"],
        "source_updated_at": None,
    }


def test_normalize_row_preserves_columns_and_parses_business_content():
    result = normalize_row(source_row())

    assert tuple(result) == ROW_FIELDS
    assert result["id"] == 101
    assert result["content"] == {
        "category": "politics",
        "market_id": "market-1",
        "rank": 1,
        "market_question": "Question?",
    }
    assert result["extra_data"] == {"event_id": "event-1"}
    assert result["published_at"] == "2026-07-29T09:02:03+08:00"
    assert result["created_at"] == "2026-07-29T09:02:03+08:00"
    assert result["source_updated_at"] is None


def test_normalize_row_accepts_a_decoded_object_without_mutating_it():
    content = {"category": "technology", "market_id": "market-2", "rank": 2}
    row = source_row(content="unused")
    row["content"] = content
    before = deepcopy(row)

    result = normalize_row(row)

    assert result["content"] == content
    assert row == before
    assert result["content"] is not content


@pytest.mark.parametrize(
    ("content", "message"),
    [
        ("[]", "content must be a JSON object"),
        ('{"category":"politics","market_id":"m","rank":NaN}',
         "standards-compliant JSON"),
        ('{"category":"politics","market_id":"m","rank":Infinity}',
         "standards-compliant JSON"),
        ('{"category":"politics","market_id":"m","rank":-Infinity}',
         "standards-compliant JSON"),
        ({"category": "politics", "market_id": "m", "rank": 1,
          "metric": float("nan")}, "standards-compliant JSON"),
        ({"category": " ", "market_id": "m", "rank": 1},
         "content.category"),
        ({"market_id": "m", "rank": 1}, "content.category"),
        ({"category": "politics", "market_id": "\t", "rank": 1},
         "content.market_id"),
        ({"category": "politics", "rank": 1}, "content.market_id"),
        ({"category": "politics", "market_id": "m", "rank": True},
         "content.rank"),
        ({"category": "politics", "market_id": "m", "rank": 0},
         "content.rank"),
        ({"category": "politics", "market_id": "m"}, "content.rank"),
    ],
)
def test_normalize_row_rejects_malformed_business_content(content, message):
    row = source_row(content="unused")
    row["content"] = content

    with pytest.raises((TypeError, ValueError), match=message):
        normalize_row(row)


@pytest.mark.parametrize("created_at", [None, "2026-07-29T01:02:03Z",
                                         datetime(2026, 7, 29, 1, 2, 3)])
def test_normalize_row_requires_an_aware_created_at(created_at):
    with pytest.raises((TypeError, ValueError), match="created_at"):
        normalize_row(source_row(created_at=created_at))
```

- [ ] **Step 2: Run the row tests and verify the missing module failure**

Run:

```bash
.venv/bin/python -m pytest tests/test_polymarket_db_contract.py -q
```

Expected: FAIL during collection with
`ModuleNotFoundError: No module named 'getDB.Polymarket.tool.contract'`.

- [ ] **Step 3: Implement strict, non-mutating row normalization**

Create `getDB/Polymarket/tool/contract.py` with:

```python
"""Validation and deterministic transformation for Polymarket database rows."""

from collections.abc import Mapping
from copy import deepcopy
from datetime import datetime
import json
from zoneinfo import ZoneInfo


CATEGORY_ORDER = (
    "politics",
    "geopolitics",
    "economy",
    "finance",
    "technology",
    "crypto",
)
ROW_FIELDS = (
    "id",
    "data_type",
    "title",
    "summary",
    "content",
    "from_source",
    "source_url",
    "content_hash",
    "extra_data",
    "published_at",
    "created_at",
    "updated_at",
    "tags",
    "source_updated_at",
)
TIMESTAMP_FIELDS = (
    "published_at",
    "created_at",
    "updated_at",
    "source_updated_at",
)

_SHANGHAI = ZoneInfo("Asia/Shanghai")


def _reject_json_constant(_value: str) -> None:
    raise ValueError("content must use standards-compliant JSON")


def _content_object(value: object) -> dict:
    if type(value) is dict:
        result = deepcopy(value)
    elif isinstance(value, str):
        try:
            result = json.loads(value, parse_constant=_reject_json_constant)
        except json.JSONDecodeError as error:
            raise ValueError("content must use standards-compliant JSON") from error
    else:
        raise TypeError("content must be JSON text or an object")
    if type(result) is not dict:
        raise ValueError("content must be a JSON object")
    try:
        json.dumps(result, allow_nan=False)
    except (TypeError, ValueError):
        raise ValueError("content must use standards-compliant JSON") from None
    return result


def _required_text(content: dict, field: str) -> str:
    value = content.get(field)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"content.{field} must be a non-whitespace string")
    return value


def _validate_content(content: dict) -> None:
    _required_text(content, "category")
    _required_text(content, "market_id")
    rank = content.get("rank")
    if type(rank) is not int or rank <= 0:
        raise ValueError("content.rank must be a positive integer")


def _timestamp_text(value: object, field: str) -> str | None:
    if value is None:
        if field == "created_at":
            raise ValueError("created_at is required")
        return None
    if not isinstance(value, datetime):
        raise TypeError(f"{field} must be a timezone-aware datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field} must be a timezone-aware datetime")
    return value.astimezone(_SHANGHAI).isoformat()


def normalize_row(row: Mapping[str, object]) -> dict:
    if not isinstance(row, Mapping):
        raise TypeError("database row must be a mapping")
    missing = [field for field in ROW_FIELDS if field not in row]
    if missing:
        raise ValueError(f"database row is missing field {missing[0]}")
    if type(row["id"]) is not int:
        raise ValueError("id must be an integer")

    content = _content_object(row["content"])
    _validate_content(content)
    result = {field: deepcopy(row[field]) for field in ROW_FIELDS}
    result["content"] = content
    for field in TIMESTAMP_FIELDS:
        result[field] = _timestamp_text(row[field], field)
    try:
        json.dumps(result, allow_nan=False)
    except (TypeError, ValueError):
        raise ValueError("database row must contain JSON-compatible values") from None
    return result
```

- [ ] **Step 4: Run the row tests and confirm they pass**

Run:

```bash
.venv/bin/python -m pytest tests/test_polymarket_db_contract.py -q
```

Expected: PASS.

- [ ] **Step 5: Commit the isolated record contract**

```bash
git add getDB/Polymarket/__init__.py getDB/Polymarket/tool/__init__.py \
  getDB/Polymarket/tool/contract.py tests/test_polymarket_db_contract.py
git commit -m "feat: normalize Polymarket database rows"
```

### Task 2: Preserve, Isolate, Count, And Sort All Rows

**Files:**
- Modify: `getDB/Polymarket/tool/contract.py`
- Modify: `tests/test_polymarket_db_contract.py`

- [ ] **Step 1: Add failing batch-order and row-isolation tests**

Extend the import in `tests/test_polymarket_db_contract.py` and append these
tests:

```python
from getDB.Polymarket.tool.contract import (
    CATEGORY_ORDER,
    ROW_FIELDS,
    build_db_output,
    normalize_row,
)


def test_category_order_matches_the_confirmed_database_export_order():
    assert CATEGORY_ORDER == (
        "politics",
        "geopolitics",
        "economy",
        "finance",
        "technology",
        "crypto",
    )


def test_build_db_output_sorts_categories_rank_time_and_id_without_deduplication():
    early = datetime(2026, 7, 29, 0, 0, tzinfo=timezone.utc)
    late = datetime(2026, 7, 29, 1, 0, tzinfo=timezone.utc)
    rows = [
        source_row(row_id=9, category="sports", market_id="unknown", rank=1),
        source_row(row_id=7, category="crypto", market_id="crypto", rank=1),
        source_row(row_id=6, category="politics", market_id="shared", rank=2),
        source_row(row_id=5, category="politics", market_id="shared", rank=1,
                   created_at=late),
        source_row(row_id=4, category="politics", market_id="shared", rank=1,
                   created_at=early),
        source_row(row_id=3, category="politics", market_id="shared", rank=1,
                   created_at=early),
    ]

    primary, errors, counts = build_db_output(rows)

    assert errors == []
    assert [row["id"] for row in primary["records"]] == [3, 4, 5, 6, 7, 9]
    assert [row["content"]["market_id"] for row in primary["records"][:4]] == [
        "shared", "shared", "shared", "shared"
    ]
    assert counts == {"politics": 4, "crypto": 1, "sports": 1}


def test_build_db_output_omits_bad_rows_without_copying_raw_content_into_errors():
    secret = "raw-content-secret-sentinel"
    bad_json = source_row(row_id=201, content=secret)
    bad_rank = source_row(row_id=202, rank=0)
    good = source_row(row_id=203, category="finance", market_id="valid", rank=1)

    primary, errors, counts = build_db_output([bad_json, good, bad_rank])

    assert [row["id"] for row in primary["records"]] == [203]
    assert counts == {"finance": 1}
    assert [error["id"] for error in errors] == [201, 202]
    assert all(error["stage"] == "row_validation" for error in errors)
    assert all(set(error) == {"id", "content_hash", "stage", "type", "message"}
               for error in errors)
    assert secret not in json.dumps(errors)


def test_build_db_output_retains_each_valid_row_from_multiple_snapshots():
    rows = [
        source_row(row_id=301, category="technology", market_id="same", rank=1),
        source_row(row_id=302, category="technology", market_id="same", rank=1),
        source_row(row_id=303, category="finance", market_id="same", rank=1),
    ]

    primary, errors, counts = build_db_output(rows)

    assert errors == []
    assert len(primary["records"]) == 3
    assert {row["id"] for row in primary["records"]} == {301, 302, 303}
    assert counts == {"finance": 1, "technology": 2}
```

- [ ] **Step 2: Run the batch tests and verify the missing function failure**

Run:

```bash
.venv/bin/python -m pytest tests/test_polymarket_db_contract.py -q
```

Expected: FAIL during collection because `build_db_output` is not defined.

- [ ] **Step 3: Implement isolated conversion and the confirmed sort order**

Add these imports and functions to `contract.py`:

```python
from collections import Counter
from collections.abc import Iterable


_CATEGORY_INDEX = {category: index for index, category in enumerate(CATEGORY_ORDER)}


def _category_sort_key(category: str) -> tuple[int, str, str]:
    index = _CATEGORY_INDEX.get(category)
    if index is not None:
        return index, "", ""
    return len(CATEGORY_ORDER), category.casefold(), category


def _record_sort_key(record: dict) -> tuple:
    content = record["content"]
    return (
        *_category_sort_key(content["category"]),
        content["rank"],
        datetime.fromisoformat(record["created_at"]),
        record["id"],
    )


def _row_error(row: object, error: Exception) -> dict:
    row_id = row.get("id") if isinstance(row, Mapping) else None
    content_hash = row.get("content_hash") if isinstance(row, Mapping) else None
    return {
        "id": row_id if type(row_id) is int else None,
        "content_hash": content_hash if isinstance(content_hash, str) else None,
        "stage": "row_validation",
        "type": type(error).__name__,
        "message": str(error),
    }


def build_db_output(rows: Iterable[Mapping[str, object]]) -> tuple[dict, list[dict], dict]:
    records = []
    errors = []
    for row in rows:
        try:
            records.append(normalize_row(row))
        except Exception as error:
            errors.append(_row_error(row, error))

    records.sort(key=_record_sort_key)
    counts = Counter(record["content"]["category"] for record in records)
    category_counts = {
        category: counts[category]
        for category in sorted(counts, key=_category_sort_key)
    }
    return {"records": records}, errors, category_counts
```

Keep the unknown-category branch: a new but structurally valid database
category is preserved after the six known categories, as explicitly approved.

- [ ] **Step 4: Run the complete contract tests**

Run:

```bash
.venv/bin/python -m pytest tests/test_polymarket_db_contract.py -q
```

Expected: PASS, including four independent rows for the repeated `market_id`.

- [ ] **Step 5: Commit batch preservation and ordering**

```bash
git add getDB/Polymarket/tool/contract.py tests/test_polymarket_db_contract.py
git commit -m "feat: order Polymarket database selections"
```

### Task 3: Query The Confirmed Database Source

**Files:**
- Create: `getDB/Polymarket/tool/db_source.py`
- Create: `tests/test_polymarket_db_source.py`

- [ ] **Step 1: Write failing settings and transaction tests**

Create `tests/test_polymarket_db_source.py`:

```python
from datetime import datetime, timezone

import pytest

from getDB.Polymarket.tool import db_source
from getDB.Polymarket.tool.db_source import (
    PgSettings,
    fetch_day_rows,
    load_pg_settings,
)


PG_NAMES = ("PGHOST", "PGPORT", "PGDATABASE", "PGUSER", "PGPASSWORD")


def clear_pg(monkeypatch):
    for name in PG_NAMES:
        monkeypatch.delenv(name, raising=False)


def set_pg(monkeypatch):
    values = {
        "PGHOST": "db.example.invalid",
        "PGPORT": "15432",
        "PGDATABASE": "analytics",
        "PGUSER": "reader",
        "PGPASSWORD": "password-sentinel",
    }
    for name, value in values.items():
        monkeypatch.setenv(name, value)
    return values


def test_load_pg_settings_loads_dotenv_and_hides_password(monkeypatch):
    loaded = []
    monkeypatch.setattr(db_source.dotenv, "load_dotenv", lambda: loaded.append(True))
    clear_pg(monkeypatch)
    set_pg(monkeypatch)

    settings = load_pg_settings()

    assert loaded == [True]
    assert settings == PgSettings("db.example.invalid", 15432, "analytics",
                                  "reader", "password-sentinel")
    assert "password-sentinel" not in repr(settings)


@pytest.mark.parametrize("missing_name", PG_NAMES)
def test_load_pg_settings_names_each_missing_variable_without_values(
    monkeypatch, missing_name
):
    monkeypatch.setattr(db_source.dotenv, "load_dotenv", lambda: None)
    clear_pg(monkeypatch)
    values = set_pg(monkeypatch)
    monkeypatch.delenv(missing_name)

    with pytest.raises(ValueError) as raised:
        load_pg_settings()

    assert missing_name in str(raised.value)
    assert all(value not in str(raised.value) for value in values.values())


@pytest.mark.parametrize("port", ["not-an-int", "0", "65536"])
def test_load_pg_settings_rejects_invalid_port_without_echoing_value(monkeypatch, port):
    monkeypatch.setattr(db_source.dotenv, "load_dotenv", lambda: None)
    clear_pg(monkeypatch)
    values = set_pg(monkeypatch)
    monkeypatch.setenv("PGPORT", port)

    with pytest.raises(ValueError) as raised:
        load_pg_settings()

    rendered = repr(raised.value)
    assert "PGPORT" in rendered
    assert port not in rendered
    assert values["PGPASSWORD"] not in rendered


class FakeCursor:
    def __init__(
        self, rows, *, query_error=None, fetch_error=None, rollback_error=None
    ):
        self.rows = rows
        self.query_error = query_error
        self.fetch_error = fetch_error
        self.rollback_error = rollback_error
        self.executions = []

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def execute(self, statement, parameters=None):
        self.executions.append((" ".join(statement.split()), parameters))
        if statement.strip() == "ROLLBACK;" and self.rollback_error:
            raise self.rollback_error
        if "FROM public.information" in statement and self.query_error:
            raise self.query_error

    def fetchall(self):
        if self.fetch_error:
            raise self.fetch_error
        return self.rows


class FakeConnection:
    def __init__(self, cursor):
        self._cursor = cursor

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def cursor(self):
        return self._cursor


def test_fetch_day_rows_uses_exact_filters_bounds_and_read_only_transaction(monkeypatch):
    rows = [{"id": 1}]
    cursor = FakeCursor(rows)
    captured = {}
    monkeypatch.setattr(
        db_source.psycopg,
        "connect",
        lambda **kwargs: captured.update(kwargs) or FakeConnection(cursor),
    )
    settings = PgSettings("host", 5432, "db", "user", "password")
    lower = datetime(2026, 7, 28, 16, tzinfo=timezone.utc)
    upper = datetime(2026, 7, 29, 16, tzinfo=timezone.utc)

    result = fetch_day_rows(settings, lower, upper)

    assert result == rows
    assert captured["autocommit"] is True
    assert captured["row_factory"] is db_source.dict_row
    assert cursor.executions[0] == (
        "BEGIN TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY;", None
    )
    query, parameters = cursor.executions[1]
    assert "FROM public.information" in query
    assert "from_source = %s" in query
    assert "data_type = %s" in query
    assert "created_at >= %s" in query and "created_at < %s" in query
    assert "ORDER BY created_at, id" in query
    assert parameters == (
        "polymarket", "PREDICTION_MARKET_SELECTION", lower, upper
    )
    assert cursor.executions[-1] == ("COMMIT;", None)


def test_fetch_day_rows_rolls_back_without_masking_query_error(monkeypatch):
    query_error = RuntimeError("query-sentinel")
    cursor = FakeCursor([], query_error=query_error,
                        rollback_error=RuntimeError("rollback-sentinel"))
    monkeypatch.setattr(
        db_source.psycopg, "connect", lambda **_kwargs: FakeConnection(cursor)
    )
    settings = PgSettings("host", 5432, "db", "user", "password")
    lower = datetime(2026, 7, 28, 16, tzinfo=timezone.utc)
    upper = datetime(2026, 7, 29, 16, tzinfo=timezone.utc)

    with pytest.raises(RuntimeError, match="query-sentinel"):
        fetch_day_rows(settings, lower, upper)

    assert cursor.executions[-1] == ("ROLLBACK;", None)
    assert all(statement != "COMMIT;" for statement, _ in cursor.executions)


def test_fetch_day_rows_rolls_back_on_fetch_failure(monkeypatch):
    cursor = FakeCursor([], fetch_error=RuntimeError("fetch-sentinel"))
    monkeypatch.setattr(
        db_source.psycopg, "connect", lambda **_kwargs: FakeConnection(cursor)
    )
    settings = PgSettings("host", 5432, "db", "user", "password")
    lower = datetime(2026, 7, 28, 16, tzinfo=timezone.utc)
    upper = datetime(2026, 7, 29, 16, tzinfo=timezone.utc)

    with pytest.raises(RuntimeError, match="fetch-sentinel"):
        fetch_day_rows(settings, lower, upper)

    assert cursor.executions[-1] == ("ROLLBACK;", None)
    assert all(statement != "COMMIT;" for statement, _ in cursor.executions)
```

- [ ] **Step 2: Run the source tests and verify the missing module failure**

Run:

```bash
.venv/bin/python -m pytest tests/test_polymarket_db_source.py -q
```

Expected: FAIL during collection because `db_source.py` does not exist.

- [ ] **Step 3: Implement settings and the single parameterized query**

Create `getDB/Polymarket/tool/db_source.py`:

```python
"""Read-only PostgreSQL source for Polymarket selection rows."""

from dataclasses import dataclass, field
from datetime import datetime
import os

import dotenv
import psycopg
from psycopg.rows import dict_row


_REQUIRED_PG_VARIABLES = (
    "PGHOST", "PGPORT", "PGDATABASE", "PGUSER", "PGPASSWORD"
)
_BEGIN_READ_ONLY = (
    "BEGIN TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY;"
)
_INFORMATION_QUERY = """
SELECT id, data_type, title, summary, content, from_source, source_url,
       content_hash, extra_data, published_at, created_at, updated_at,
       tags, source_updated_at
FROM public.information
WHERE from_source = %s
  AND data_type = %s
  AND created_at >= %s
  AND created_at < %s
ORDER BY created_at, id;
"""


@dataclass(frozen=True)
class PgSettings:
    host: str
    port: int
    dbname: str
    user: str
    password: str = field(repr=False)


def load_pg_settings() -> PgSettings:
    dotenv.load_dotenv()
    values = {}
    for name in _REQUIRED_PG_VARIABLES:
        value = os.getenv(name)
        if not value:
            raise ValueError(f"Missing required environment variable: {name}")
        values[name] = value
    try:
        port = int(values["PGPORT"])
    except ValueError:
        raise ValueError("Invalid integer environment variable: PGPORT") from None
    if not 1 <= port <= 65_535:
        raise ValueError("Environment variable PGPORT is outside the valid range")
    return PgSettings(
        host=values["PGHOST"],
        port=port,
        dbname=values["PGDATABASE"],
        user=values["PGUSER"],
        password=values["PGPASSWORD"],
    )


def fetch_day_rows(
    settings: PgSettings, lower: datetime, upper: datetime
) -> list[dict]:
    with psycopg.connect(
        host=settings.host,
        port=settings.port,
        dbname=settings.dbname,
        user=settings.user,
        password=settings.password,
        row_factory=dict_row,
        autocommit=True,
    ) as connection:
        with connection.cursor() as cursor:
            cursor.execute(_BEGIN_READ_ONLY)
            try:
                cursor.execute(
                    _INFORMATION_QUERY,
                    ("polymarket", "PREDICTION_MARKET_SELECTION", lower, upper),
                )
                rows = list(cursor.fetchall())
                cursor.execute("COMMIT;")
            except BaseException:
                try:
                    cursor.execute("ROLLBACK;")
                except BaseException:
                    pass
                raise
    return rows
```

- [ ] **Step 4: Run the source tests**

```bash
.venv/bin/python -m pytest tests/test_polymarket_db_source.py -q
```

Expected: PASS.

- [ ] **Step 5: Run both completed component suites**

```bash
.venv/bin/python -m pytest \
  tests/test_polymarket_db_contract.py \
  tests/test_polymarket_db_source.py -q
```

Expected: PASS.

- [ ] **Step 6: Commit the database source**

```bash
git add getDB/Polymarket/tool/db_source.py tests/test_polymarket_db_source.py
git commit -m "feat: read Polymarket selections from PostgreSQL"
```

### Task 4: Define CLI Dates, Statuses, Errors, And Manifest Fields

**Files:**
- Create: `getDB/Polymarket/tool/export_polymarket_db.py`
- Create: `tests/test_polymarket_db_cli.py`

- [ ] **Step 1: Write failing CLI-helper tests**

Create `tests/test_polymarket_db_cli.py` with:

```python
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

from getDB.Polymarket.tool import export_polymarket_db as cli


DAY = date(2026, 7, 29)
LOWER = datetime(2026, 7, 28, 16, tzinfo=timezone.utc)
UPPER = datetime(2026, 7, 29, 16, tzinfo=timezone.utc)
CAPTURED = datetime(2026, 7, 29, 8, 30, tzinfo=timezone.utc)
GENERATION_ID = "00000000-0000-4000-8000-000000000001"


def test_parse_args_uses_shanghai_today_and_default_output(monkeypatch):
    monkeypatch.setattr(cli, "_china_today", lambda: DAY)

    arguments = cli.parse_args([])

    assert arguments.date == DAY
    assert arguments.output_root == (
        cli._PROJECT_ROOT / "getDB" / "Polymarket" / "db"
    )


@pytest.mark.parametrize("value", ["2026/07/29", "2026-7-29", "2026-02-30"])
def test_parse_args_rejects_non_strict_or_invalid_dates(value):
    with pytest.raises(SystemExit) as raised:
        cli.parse_args(["--date", value])
    assert raised.value.code == 2


def test_manifest_has_the_exact_generation_contract():
    manifest = cli._manifest(
        business_date=DAY,
        lower=LOWER,
        upper=UPPER,
        generation_id=GENERATION_ID,
        captured_at=CAPTURED,
        status="partial",
        source_row_count=3,
        record_count=2,
        error_count=1,
        category_counts={"politics": 2},
    )

    assert manifest == {
        "source": "postgresql",
        "dataset": "polymarket",
        "generation_id": GENERATION_ID,
        "status": "partial",
        "business_date": "2026-07-29",
        "timezone": "Asia/Shanghai",
        "utc_lower_bound": "2026-07-28T16:00:00+00:00",
        "utc_upper_bound": "2026-07-29T16:00:00+00:00",
        "captured_at": "2026-07-29T08:30:00+00:00",
        "source_row_count": 3,
        "record_count": 2,
        "error_count": 1,
        "category_counts": {"politics": 2},
        "artifacts": {},
    }


@pytest.mark.parametrize(
    ("record_count", "error_count", "expected"),
    [(2, 0, "success"), (2, 1, "partial"), (0, 1, "failed")],
)
def test_generation_status_depends_on_usable_records_and_errors(
    record_count, error_count, expected
):
    assert cli._generation_status(record_count, error_count) == expected


def test_source_errors_are_generic_and_allowlist_the_type():
    error = RuntimeError(
        "host=db-secret password=password-secret database=database-secret"
    )

    result = cli._source_error(error)

    assert result == {
        "stage": "source",
        "type": "RuntimeError",
        "message": "Database source operation failed",
    }
    assert "secret" not in repr(result)


def test_source_errors_collapse_unrecognized_exception_types():
    class InternalConnectorFailure(Exception):
        pass

    assert cli._source_error(InternalConnectorFailure("private"))["type"] == (
        "Exception"
    )


def test_no_records_error_has_a_stable_non_secret_shape():
    assert cli._no_records_error() == {
        "stage": "source_selection",
        "type": "NoRecordsError",
        "message": "No Polymarket rows found for requested date",
    }
```

- [ ] **Step 2: Run the CLI tests and verify the missing module failure**

Run:

```bash
.venv/bin/python -m pytest tests/test_polymarket_db_cli.py -q
```

Expected: FAIL during collection because `export_polymarket_db.py` does not
exist.

- [ ] **Step 3: Implement the CLI and manifest foundation**

Create `getDB/Polymarket/tool/export_polymarket_db.py` with:

```python
"""Export Polymarket selection rows from PostgreSQL on POSIX systems."""

import argparse
from datetime import date, datetime, timezone
from pathlib import Path
import re
import sys
import uuid
from zoneinfo import ZoneInfo


_PROJECT_ROOT = Path(__file__).resolve().parents[3]
if __package__ in (None, ""):
    sys.path.insert(0, str(_PROJECT_ROOT))

from common.time_window import china_day_bounds


_SHANGHAI = ZoneInfo("Asia/Shanghai")
_DEFAULT_OUTPUT_ROOT = _PROJECT_ROOT / "getDB" / "Polymarket" / "db"
_STRICT_DATE_PATTERN = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}\Z")
_PRIMARY_FILENAME = "polymarket_db.json"
_ERRORS_FILENAME = "errors.json"
_MANIFEST_FILENAME = "manifest.json"
_LOCK_FILENAME = ".generation.lock"
_HASHED_ARTIFACT_FILENAMES = (_PRIMARY_FILENAME, _ERRORS_FILENAME)
_FINAL_STATUSES = frozenset({"success", "partial", "failed"})
_SOURCE_ERROR_MESSAGE = "Database source operation failed"
_VALIDATION_ERROR_MESSAGE = "Unable to validate Polymarket database export"
_SAFE_SOURCE_ERROR_TYPES = frozenset(
    {
        "AttributeError", "ConnectionError", "DataError", "DatabaseError",
        "Exception", "IndexError", "IntegrityError", "InterfaceError",
        "InternalError", "KeyError", "LookupError", "NotSupportedError",
        "OperationalError", "OSError", "PermissionError", "ProgrammingError",
        "RuntimeError", "TimeoutError", "TypeError", "ValueError",
    }
)


class GenerationLockError(RuntimeError):
    pass


class GenerationValidationError(RuntimeError):
    pass


def _parse_date(value: str) -> date:
    if _STRICT_DATE_PATTERN.fullmatch(value) is None:
        raise argparse.ArgumentTypeError("date must use YYYY-MM-DD")
    try:
        parsed = date.fromisoformat(value)
        china_day_bounds(parsed)
    except (OverflowError, ValueError):
        raise argparse.ArgumentTypeError(
            "date must be a valid supported Asia/Shanghai calendar date"
        ) from None
    return parsed


def _china_today() -> date:
    return datetime.now(_SHANGHAI).date()


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _new_generation_id() -> str:
    return str(uuid.uuid4())


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", type=_parse_date, default=_china_today())
    parser.add_argument("--output-root", type=Path, default=_DEFAULT_OUTPUT_ROOT)
    return parser.parse_args(argv)


def _utc_iso(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("manifest timestamp must be timezone-aware")
    return value.astimezone(timezone.utc).isoformat()


def _generation_status(record_count: int, error_count: int) -> str:
    if record_count > 0:
        return "partial" if error_count else "success"
    return "failed"


def _manifest(
    *,
    business_date: date,
    lower: datetime,
    upper: datetime,
    generation_id: str,
    captured_at: datetime,
    status: str,
    source_row_count: int | None,
    record_count: int,
    error_count: int,
    category_counts: dict[str, int],
) -> dict:
    return {
        "source": "postgresql",
        "dataset": "polymarket",
        "generation_id": generation_id,
        "status": status,
        "business_date": business_date.isoformat(),
        "timezone": "Asia/Shanghai",
        "utc_lower_bound": _utc_iso(lower),
        "utc_upper_bound": _utc_iso(upper),
        "captured_at": _utc_iso(captured_at),
        "source_row_count": source_row_count,
        "record_count": record_count,
        "error_count": error_count,
        "category_counts": category_counts,
        "artifacts": {},
    }


def _safe_source_error_type(error: Exception) -> str:
    name = type(error).__name__
    return name if name in _SAFE_SOURCE_ERROR_TYPES else "Exception"


def _source_error(error: Exception) -> dict:
    return {
        "stage": "source",
        "type": _safe_source_error_type(error),
        "message": _SOURCE_ERROR_MESSAGE,
    }


def _no_records_error() -> dict:
    return {
        "stage": "source_selection",
        "type": "NoRecordsError",
        "message": "No Polymarket rows found for requested date",
    }
```

- [ ] **Step 4: Run the CLI-helper tests**

```bash
.venv/bin/python -m pytest tests/test_polymarket_db_cli.py -q
```

Expected: PASS.

- [ ] **Step 5: Commit the CLI contract foundation**

```bash
git add getDB/Polymarket/tool/export_polymarket_db.py \
  tests/test_polymarket_db_cli.py
git commit -m "feat: define Polymarket database export manifest"
```

### Task 5: Commit And Strictly Validate Artifact Generations

**Files:**
- Modify: `getDB/Polymarket/tool/contract.py`
- Modify: `getDB/Polymarket/tool/export_polymarket_db.py`
- Modify: `tests/test_polymarket_db_contract.py`
- Modify: `tests/test_polymarket_db_cli.py`

- [ ] **Step 1: Add failing serialized-record validation tests**

Import `validate_serialized_record` in the contract tests and append:

```python
from getDB.Polymarket.tool.contract import validate_serialized_record


def test_validate_serialized_record_accepts_only_the_committed_shape():
    record = normalize_row(source_row(category="geopolitics"))

    assert validate_serialized_record(record) == "geopolitics"

    record["unexpected"] = True
    with pytest.raises(ValueError, match="record fields"):
        validate_serialized_record(record)


def test_validate_serialized_record_rejects_non_shanghai_timestamps():
    record = normalize_row(source_row())
    record["created_at"] = "2026-07-29T01:02:03+00:00"

    with pytest.raises(ValueError, match="Asia/Shanghai"):
        validate_serialized_record(record)
```

- [ ] **Step 2: Run the new contract tests and verify the missing validator**

```bash
.venv/bin/python -m pytest tests/test_polymarket_db_contract.py -q
```

Expected: FAIL during collection because `validate_serialized_record` is not
defined.

- [ ] **Step 3: Add serialized-record validation**

Add `timedelta` to the datetime import in `contract.py`, then append:

```python
from datetime import timedelta


def _serialized_timestamp(value: object, field: str) -> None:
    if value is None:
        if field == "created_at":
            raise ValueError("created_at is required")
        return
    if not isinstance(value, str):
        raise ValueError(f"{field} must be an ISO-8601 string")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        raise ValueError(f"{field} must be an ISO-8601 string") from None
    if parsed.tzinfo is None or parsed.utcoffset() != timedelta(hours=8):
        raise ValueError(f"{field} must use the Asia/Shanghai offset")


def validate_serialized_record(record: object) -> str:
    if type(record) is not dict or set(record) != set(ROW_FIELDS):
        raise ValueError("record fields do not match the database contract")
    if type(record["id"]) is not int:
        raise ValueError("id must be an integer")
    if record["data_type"] != "PREDICTION_MARKET_SELECTION":
        raise ValueError("data_type does not match the Polymarket contract")
    if record["from_source"] != "polymarket":
        raise ValueError("from_source does not match the Polymarket contract")
    for field in ("title", "summary", "source_url", "content_hash"):
        if record[field] is not None and not isinstance(record[field], str):
            raise ValueError(f"{field} must be a string or null")
    tags = record["tags"]
    if tags is not None and (
        type(tags) is not list or any(not isinstance(tag, str) for tag in tags)
    ):
        raise ValueError("tags must be an array of strings or null")
    content = _content_object(record["content"])
    _validate_content(content)
    for field in TIMESTAMP_FIELDS:
        _serialized_timestamp(record[field], field)
    try:
        json.dumps(record, allow_nan=False)
    except (TypeError, ValueError):
        raise ValueError("record must contain JSON-compatible values") from None
    return content["category"]
```

- [ ] **Step 4: Run contract tests before adding artifact code**

```bash
.venv/bin/python -m pytest tests/test_polymarket_db_contract.py -q
```

Expected: PASS.

- [ ] **Step 5: Add failing artifact commit, tamper, and count tests**

Add these imports and helpers to `tests/test_polymarket_db_cli.py`:

```python
import hashlib
import json

from common.artifacts import write_json_atomic
from getDB.Polymarket.tool.contract import build_db_output


def db_row(*, row_id=1, category="politics", rank=1, content=None):
    business = (
        {"category": category, "market_id": f"market-{row_id}", "rank": rank}
        if content is None
        else content
    )
    return {
        "id": row_id,
        "data_type": "PREDICTION_MARKET_SELECTION",
        "title": f"Title {row_id}",
        "summary": None,
        "content": json.dumps(business) if isinstance(business, dict) else business,
        "from_source": "polymarket",
        "source_url": None,
        "content_hash": f"hash-{row_id}",
        "extra_data": {"market_id": f"market-{row_id}"},
        "published_at": CAPTURED,
        "created_at": CAPTURED,
        "updated_at": CAPTURED,
        "tags": ["prediction-market"],
        "source_updated_at": None,
    }


def generation_payload():
    primary, errors, counts = build_db_output(
        [db_row(row_id=1), db_row(row_id=2, category="finance")]
    )
    manifest = cli._manifest(
        business_date=DAY,
        lower=LOWER,
        upper=UPPER,
        generation_id=GENERATION_ID,
        captured_at=CAPTURED,
        status="success",
        source_row_count=2,
        record_count=2,
        error_count=0,
        category_counts=counts,
    )
    return primary, errors, manifest
```

Then append:

```python
def test_write_and_read_validated_generation(tmp_path):
    directory = tmp_path / DAY.isoformat()
    primary, errors, manifest = generation_payload()

    assert cli._write_artifacts(directory, primary, manifest, errors) is True

    actual_primary, actual_errors, actual_manifest = (
        cli.read_validated_generation(directory)
    )
    assert actual_primary == primary
    assert actual_errors == []
    assert actual_manifest["status"] == "success"
    assert set(actual_manifest["artifacts"]) == {
        "polymarket_db.json", "errors.json"
    }
    for filename, metadata in actual_manifest["artifacts"].items():
        assert metadata["sha256"] == hashlib.sha256(
            (directory / filename).read_bytes()
        ).hexdigest()


def test_validated_reader_rejects_tampered_primary(tmp_path):
    directory = tmp_path / DAY.isoformat()
    primary, errors, manifest = generation_payload()
    assert cli._write_artifacts(directory, primary, manifest, errors) is True
    write_json_atomic(directory / "polymarket_db.json", {"records": []})

    with pytest.raises(cli.GenerationValidationError):
        cli.read_validated_generation(directory)


def test_validated_reader_rejects_manifest_count_mismatch(tmp_path):
    directory = tmp_path / DAY.isoformat()
    primary, errors, manifest = generation_payload()
    assert cli._write_artifacts(directory, primary, manifest, errors) is True
    committed = json.loads((directory / "manifest.json").read_text())
    committed["record_count"] = 99
    write_json_atomic(directory / "manifest.json", committed)

    with pytest.raises(cli.GenerationValidationError):
        cli.read_validated_generation(directory)


def test_validated_reader_uses_a_shared_lock(tmp_path, monkeypatch):
    directory = tmp_path / DAY.isoformat()
    primary, errors, manifest = generation_payload()
    assert cli._write_artifacts(directory, primary, manifest, errors) is True
    operations = []
    monkeypatch.setattr(cli.fcntl, "flock", lambda _fd, operation: operations.append(operation))

    cli.read_validated_generation(directory)

    assert operations == [cli.fcntl.LOCK_SH, cli.fcntl.LOCK_UN]


def test_artifact_failure_never_leaves_a_valid_final_manifest(tmp_path, monkeypatch):
    directory = tmp_path / DAY.isoformat()
    primary, errors, manifest = generation_payload()
    real_write = cli.write_json_atomic

    def fail_primary(path, payload):
        if path.name == "polymarket_db.json":
            raise OSError("write-sentinel")
        real_write(path, payload)

    monkeypatch.setattr(cli, "write_json_atomic", fail_primary)

    assert cli._write_artifacts(directory, primary, manifest, errors) is False
    assert cli.validate_manifest_artifacts(directory) is False
    if (directory / "manifest.json").exists():
        assert json.loads((directory / "manifest.json").read_text())["status"] == (
            "in_progress"
        )


def test_final_manifest_failure_restores_an_uncommitted_marker(tmp_path, monkeypatch):
    directory = tmp_path / DAY.isoformat()
    primary, errors, manifest = generation_payload()
    real_write = cli.write_json_atomic

    def fail_final_manifest(path, payload):
        if path.name == "manifest.json" and payload.get("status") == "success":
            raise OSError("manifest-sentinel")
        real_write(path, payload)

    monkeypatch.setattr(cli, "write_json_atomic", fail_final_manifest)

    assert cli._write_artifacts(directory, primary, manifest, errors) is False
    assert cli.validate_manifest_artifacts(directory) is False
    assert json.loads((directory / "manifest.json").read_text())["status"] == (
        "in_progress"
    )
```

- [ ] **Step 6: Run the artifact tests and verify missing functions fail**

```bash
.venv/bin/python -m pytest tests/test_polymarket_db_cli.py -q
```

Expected: FAIL because `_write_artifacts` and `read_validated_generation` do
not exist.

- [ ] **Step 7: Implement locking and atomic artifact commits**

Add these imports to `export_polymarket_db.py`:

```python
from contextlib import contextmanager
import fcntl
import hashlib
import json
import os

from common.artifacts import write_json_atomic
```

Then add:

```python
@contextmanager
def _generation_lock(directory: Path, operation: int):
    descriptor = None
    try:
        directory.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(
            directory / _LOCK_FILENAME, os.O_CREAT | os.O_RDWR, 0o600
        )
        os.fchmod(descriptor, 0o600)
        fcntl.flock(descriptor, operation)
    except BaseException as error:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except Exception:
                pass
        if isinstance(error, Exception):
            raise GenerationLockError("Unable to acquire generation lock") from None
        raise
    body_error = None
    try:
        yield
    except BaseException as error:
        body_error = error
        raise
    finally:
        release_error = None
        try:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        except BaseException as error:
            release_error = error
        try:
            os.close(descriptor)
        except BaseException as error:
            release_error = release_error or error
        if release_error is not None and (
            body_error is None or not isinstance(release_error, Exception)
        ):
            if isinstance(release_error, Exception):
                raise GenerationLockError("Unable to release generation lock") from None
            raise release_error


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _artifact_manifest(directory: Path) -> dict:
    return {
        filename: {"sha256": _sha256_file(directory / filename)}
        for filename in _HASHED_ARTIFACT_FILENAMES
    }


def _artifact_error(error: Exception) -> dict:
    return {
        "stage": "artifact",
        "type": type(error).__name__,
        "message": "Unable to write an export artifact",
    }


def _write_artifacts(directory: Path, primary: dict, manifest: dict, errors: list) -> bool:
    try:
        directory.mkdir(parents=True, exist_ok=True)
        with _generation_lock(directory, fcntl.LOCK_EX):
            return _write_artifacts_unlocked(directory, primary, manifest, errors)
    except GenerationLockError:
        return False


def _write_artifacts_unlocked(
    directory: Path, primary: dict, manifest: dict, errors: list
) -> bool:
    primary_path = directory / _PRIMARY_FILENAME
    errors_path = directory / _ERRORS_FILENAME
    manifest_path = directory / _MANIFEST_FILENAME
    in_progress = {**manifest, "status": "in_progress", "artifacts": {}}
    try:
        write_json_atomic(manifest_path, in_progress)
        write_json_atomic(primary_path, primary)
        write_json_atomic(errors_path, errors)
        committed = {**manifest, "artifacts": _artifact_manifest(directory)}
        write_json_atomic(manifest_path, committed)
        return True
    except Exception as error:
        try:
            write_json_atomic(manifest_path, in_progress)
        except Exception:
            try:
                manifest_path.unlink(missing_ok=True)
            except Exception:
                pass
        try:
            write_json_atomic(errors_path, [*errors, _artifact_error(error)])
        except Exception:
            pass
        return False
```

- [ ] **Step 8: Implement strict validated reads**

Add these imports:

```python
from collections import Counter

from getDB.Polymarket.tool.contract import validate_serialized_record
```

Then add the exact manifest set and validators:

```python
_MANIFEST_FIELDS = {
    "source", "dataset", "generation_id", "status", "business_date",
    "timezone", "utc_lower_bound", "utc_upper_bound", "captured_at",
    "source_row_count", "record_count", "error_count", "category_counts",
    "artifacts",
}
_SHA256_PATTERN = re.compile(r"[0-9a-f]{64}\Z")


def _validation_failure() -> None:
    raise GenerationValidationError(_VALIDATION_ERROR_MESSAGE)


def _strict_integer(value: object, *, nullable: bool = False) -> int | None:
    if nullable and value is None:
        return None
    if type(value) is not int or value < 0:
        _validation_failure()
    return value


def _aware_iso(value: object) -> datetime:
    if not isinstance(value, str):
        _validation_failure()
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        _validation_failure()
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        _validation_failure()
    return parsed


def _validate_error(error: object) -> None:
    if type(error) is not dict:
        _validation_failure()
    for field in ("stage", "type", "message"):
        if not isinstance(error.get(field), str) or not error[field]:
            _validation_failure()
    if error["stage"] == "row_validation":
        if set(error) != {"id", "content_hash", "stage", "type", "message"}:
            _validation_failure()
        if error["id"] is not None and type(error["id"]) is not int:
            _validation_failure()
        if error["content_hash"] is not None and not isinstance(
            error["content_hash"], str
        ):
            _validation_failure()


def _validate_generation(directory: Path, primary: object, errors: object,
                         manifest: object) -> None:
    if type(primary) is not dict or set(primary) != {"records"}:
        _validation_failure()
    if type(primary["records"]) is not list or type(errors) is not list:
        _validation_failure()
    if type(manifest) is not dict or set(manifest) != _MANIFEST_FIELDS:
        _validation_failure()
    if manifest["source"] != "postgresql" or manifest["dataset"] != "polymarket":
        _validation_failure()
    if manifest["timezone"] != "Asia/Shanghai" or manifest["status"] not in _FINAL_STATUSES:
        _validation_failure()
    try:
        uuid.UUID(manifest["generation_id"])
        business_date = date.fromisoformat(manifest["business_date"])
    except (AttributeError, TypeError, ValueError):
        _validation_failure()
    if directory.name != business_date.isoformat():
        _validation_failure()
    lower, upper = china_day_bounds(business_date)
    if manifest["utc_lower_bound"] != _utc_iso(lower):
        _validation_failure()
    if manifest["utc_upper_bound"] != _utc_iso(upper):
        _validation_failure()
    if _aware_iso(manifest["captured_at"]).utcoffset() != timezone.utc.utcoffset(None):
        _validation_failure()

    categories = Counter()
    for record in primary["records"]:
        try:
            categories[validate_serialized_record(record)] += 1
        except (TypeError, ValueError):
            _validation_failure()
    for error in errors:
        _validate_error(error)

    record_count = _strict_integer(manifest["record_count"])
    error_count = _strict_integer(manifest["error_count"])
    source_count = _strict_integer(manifest["source_row_count"], nullable=True)
    if record_count != len(primary["records"]) or error_count != len(errors):
        _validation_failure()
    category_counts = manifest["category_counts"]
    if type(category_counts) is not dict or category_counts != dict(categories):
        _validation_failure()
    if any(not isinstance(key, str) or type(value) is not int or value <= 0
           for key, value in category_counts.items()):
        _validation_failure()

    row_error_count = sum(error["stage"] == "row_validation" for error in errors)
    status = manifest["status"]
    success_status = (
        status == "success" and record_count > 0 and error_count == 0
        and source_count == record_count
    )
    partial_status = (
        status == "partial" and record_count > 0 and error_count > 0
        and row_error_count == error_count
        and source_count == record_count + row_error_count
    )
    failed_source = (
        status == "failed" and record_count == 0 and error_count > 0
        and source_count is None and error_count == 1
        and errors[0]["stage"] == "source"
    )
    failed_empty = (
        status == "failed" and record_count == 0 and error_count == 1
        and source_count == 0 and errors[0]["stage"] == "source_selection"
    )
    failed_rows = (
        status == "failed" and record_count == 0 and row_error_count > 0
        and row_error_count == error_count and source_count == row_error_count
    )
    if not any((success_status, partial_status, failed_source, failed_empty,
                failed_rows)):
        _validation_failure()

    artifacts = manifest["artifacts"]
    if type(artifacts) is not dict or set(artifacts) != set(
        _HASHED_ARTIFACT_FILENAMES
    ):
        _validation_failure()
    for metadata in artifacts.values():
        if type(metadata) is not dict or set(metadata) != {"sha256"}:
            _validation_failure()
        if not isinstance(metadata["sha256"], str) or not _SHA256_PATTERN.fullmatch(
            metadata["sha256"]
        ):
            _validation_failure()


def _read_validated_generation_unlocked(directory: Path) -> tuple[dict, list, dict]:
    try:
        payloads = {
            filename: (directory / filename).read_bytes()
            for filename in (*_HASHED_ARTIFACT_FILENAMES, _MANIFEST_FILENAME)
        }
        manifest = json.loads(payloads[_MANIFEST_FILENAME])
        artifacts = manifest.get("artifacts") if isinstance(manifest, dict) else None
        if not isinstance(artifacts, dict):
            _validation_failure()
        for filename in _HASHED_ARTIFACT_FILENAMES:
            metadata = artifacts.get(filename)
            if not isinstance(metadata, dict):
                _validation_failure()
            if hashlib.sha256(payloads[filename]).hexdigest() != metadata.get("sha256"):
                _validation_failure()
        primary = json.loads(payloads[_PRIMARY_FILENAME])
        errors = json.loads(payloads[_ERRORS_FILENAME])
        _validate_generation(directory, primary, errors, manifest)
        return primary, errors, manifest
    except GenerationValidationError:
        raise
    except (AttributeError, json.JSONDecodeError, KeyError, OSError, TypeError,
            UnicodeError, ValueError):
        raise GenerationValidationError(_VALIDATION_ERROR_MESSAGE) from None


def read_validated_generation(directory: Path) -> tuple[dict, list, dict]:
    with _generation_lock(directory, fcntl.LOCK_SH):
        return _read_validated_generation_unlocked(directory)


def validate_manifest_artifacts(directory: Path) -> bool:
    try:
        read_validated_generation(directory)
    except (GenerationLockError, GenerationValidationError):
        return False
    return True
```

- [ ] **Step 9: Run artifact and contract tests**

```bash
.venv/bin/python -m pytest \
  tests/test_polymarket_db_contract.py \
  tests/test_polymarket_db_cli.py -q
```

Expected: PASS.

- [ ] **Step 10: Add mutation coverage for every reader invariant**

Append these concrete helpers and parameterized cases to
`test_polymarket_db_cli.py`:

```python
def committed_directory(tmp_path):
    directory = tmp_path / DAY.isoformat()
    primary, errors, manifest = generation_payload()
    assert cli._write_artifacts(directory, primary, manifest, errors) is True
    return directory


def rewrite_manifest(directory, mutate):
    path = directory / "manifest.json"
    manifest = json.loads(path.read_text(encoding="utf-8"))
    mutate(manifest)
    write_json_atomic(path, manifest)


def rewrite_hashed_json(directory, filename, mutate):
    path = directory / filename
    payload = json.loads(path.read_text(encoding="utf-8"))
    mutate(payload)
    write_json_atomic(path, payload)
    manifest_path = directory / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["artifacts"][filename]["sha256"] = hashlib.sha256(
        path.read_bytes()
    ).hexdigest()
    write_json_atomic(manifest_path, manifest)


def remove_dataset(manifest):
    manifest.pop("dataset")


def add_manifest_field(manifest):
    manifest["unexpected"] = True


def remove_artifact(manifest):
    manifest["artifacts"].pop("errors.json")


def add_artifact(manifest):
    manifest["artifacts"]["extra.json"] = {"sha256": "0" * 64}


@pytest.mark.parametrize(
    "mutate",
    [
        remove_dataset,
        add_manifest_field,
        lambda value: value.__setitem__("generation_id", "not-a-uuid"),
        lambda value: value.__setitem__("business_date", "2026-07-28"),
        lambda value: value.__setitem__("utc_lower_bound", "2026-07-28T15:00:00+00:00"),
        lambda value: value.__setitem__("captured_at", "2026-07-29T16:30:00+08:00"),
        lambda value: value.__setitem__("record_count", True),
        lambda value: value.__setitem__("category_counts", {"politics": 99}),
        lambda value: value.__setitem__("status", "partial"),
        lambda value: value.__setitem__("source_row_count", 3),
        remove_artifact,
        add_artifact,
        lambda value: value["artifacts"]["errors.json"].__setitem__("sha256", "ABC"),
    ],
)
def test_validated_reader_rejects_each_manifest_invariant(tmp_path, mutate):
    directory = committed_directory(tmp_path)
    rewrite_manifest(directory, mutate)

    with pytest.raises(cli.GenerationValidationError):
        cli.read_validated_generation(directory)


@pytest.mark.parametrize(
    ("filename", "mutate"),
    [
        ("polymarket_db.json", lambda value: value.__setitem__("extra", True)),
        ("polymarket_db.json", lambda value: value["records"][0].__setitem__("extra", True)),
        (
            "polymarket_db.json",
            lambda value: value["records"][0].__setitem__(
                "created_at", "2026-07-29T08:30:00+00:00"
            ),
        ),
        (
            "polymarket_db.json",
            lambda value: value["records"][0].__setitem__("tags", "not-an-array"),
        ),
        ("errors.json", lambda value: value.append({"stage": "row_validation"})),
    ],
)
def test_validated_reader_rejects_each_payload_invariant(
    tmp_path, filename, mutate
):
    directory = committed_directory(tmp_path)
    rewrite_hashed_json(directory, filename, mutate)

    with pytest.raises(cli.GenerationValidationError):
        cli.read_validated_generation(directory)


def test_validated_reader_rejects_non_array_errors(tmp_path):
    directory = committed_directory(tmp_path)
    write_json_atomic(directory / "errors.json", {"error": "wrong shape"})
    rewrite_manifest(
        directory,
        lambda value: value["artifacts"]["errors.json"].__setitem__(
            "sha256", hashlib.sha256((directory / "errors.json").read_bytes()).hexdigest()
        ),
    )

    with pytest.raises(cli.GenerationValidationError):
        cli.read_validated_generation(directory)
```

- [ ] **Step 11: Run the full CLI test file again**

```bash
.venv/bin/python -m pytest tests/test_polymarket_db_cli.py -q
```

Expected: PASS.

- [ ] **Step 12: Commit the artifact protocol**

```bash
git add getDB/Polymarket/tool/contract.py \
  getDB/Polymarket/tool/export_polymarket_db.py \
  tests/test_polymarket_db_contract.py tests/test_polymarket_db_cli.py
git commit -m "feat: validate Polymarket database artifacts"
```

### Task 6: Orchestrate Success, Partial, Empty, And Failed Runs

**Files:**
- Modify: `getDB/Polymarket/tool/export_polymarket_db.py`
- Modify: `tests/test_polymarket_db_cli.py`

- [ ] **Step 1: Add failing end-to-end CLI state tests**

Use the `db_row` helper defined in Task 5 and add this source installer:

```python
def install_source(monkeypatch, rows=None, error=None):
    settings = cli.PgSettings(
        "host-secret", 15432, "database-secret", "user-secret", "password-secret"
    )
    monkeypatch.setattr(cli, "load_pg_settings", lambda: settings)
    if error is None:
        monkeypatch.setattr(cli, "fetch_day_rows", lambda *_args: list(rows or []))
    else:
        monkeypatch.setattr(
            cli, "fetch_day_rows", lambda *_args: (_ for _ in ()).throw(error)
        )
    monkeypatch.setattr(cli, "_new_generation_id", lambda: GENERATION_ID)
    monkeypatch.setattr(cli, "_utc_now", lambda: CAPTURED)
```

Append the state tests:

```python
def test_main_commits_successful_generation(tmp_path, monkeypatch):
    install_source(monkeypatch, [db_row(row_id=2), db_row(row_id=1)])

    exit_code = cli.main(["--date", DAY.isoformat(), "--output-root", str(tmp_path)])

    assert exit_code == 0
    primary, errors, manifest = cli.read_validated_generation(tmp_path / DAY.isoformat())
    assert [row["id"] for row in primary["records"]] == [1, 2]
    assert errors == []
    assert manifest["status"] == "success"
    assert manifest["source_row_count"] == 2
    assert manifest["record_count"] == 2


def test_main_queries_the_exact_shanghai_day_bounds(tmp_path, monkeypatch):
    settings = cli.PgSettings("host", 15432, "db", "user", "password")
    captured_bounds = []
    monkeypatch.setattr(cli, "load_pg_settings", lambda: settings)
    monkeypatch.setattr(
        cli,
        "fetch_day_rows",
        lambda _settings, lower, upper: captured_bounds.append((lower, upper)) or [],
    )
    monkeypatch.setattr(cli, "_new_generation_id", lambda: GENERATION_ID)
    monkeypatch.setattr(cli, "_utc_now", lambda: CAPTURED)

    cli.main(["--date", DAY.isoformat(), "--output-root", str(tmp_path)])

    assert captured_bounds == [(LOWER, UPPER)]


def test_main_commits_partial_generation_and_returns_one(tmp_path, monkeypatch):
    install_source(monkeypatch, [db_row(row_id=1), db_row(row_id=2, rank=0)])

    exit_code = cli.main(["--date", DAY.isoformat(), "--output-root", str(tmp_path)])

    assert exit_code == 1
    primary, errors, manifest = cli.read_validated_generation(tmp_path / DAY.isoformat())
    assert [row["id"] for row in primary["records"]] == [1]
    assert [error["id"] for error in errors] == [2]
    assert manifest["status"] == "partial"
    assert manifest["source_row_count"] == 2
    assert manifest["record_count"] == 1
    assert manifest["error_count"] == 1


def test_main_treats_zero_source_rows_as_a_failed_generation(tmp_path, monkeypatch):
    install_source(monkeypatch, [])

    exit_code = cli.main(["--date", DAY.isoformat(), "--output-root", str(tmp_path)])

    assert exit_code == 1
    primary, errors, manifest = cli.read_validated_generation(tmp_path / DAY.isoformat())
    assert primary == {"records": []}
    assert errors == [cli._no_records_error()]
    assert manifest["status"] == "failed"
    assert manifest["source_row_count"] == 0
    assert manifest["category_counts"] == {}


def test_main_treats_all_invalid_rows_as_a_failed_generation(tmp_path, monkeypatch):
    install_source(monkeypatch, [db_row(row_id=1, rank=0), db_row(row_id=2, rank=True)])

    exit_code = cli.main(["--date", DAY.isoformat(), "--output-root", str(tmp_path)])

    assert exit_code == 1
    primary, errors, manifest = cli.read_validated_generation(tmp_path / DAY.isoformat())
    assert primary == {"records": []}
    assert len(errors) == 2
    assert manifest["status"] == "failed"
    assert manifest["source_row_count"] == 2
    assert manifest["record_count"] == 0


def test_main_sanitizes_source_failure_and_uses_unknown_source_count(tmp_path, monkeypatch):
    install_source(
        monkeypatch,
        error=RuntimeError(
            "host-secret user-secret database-secret password-secret query-secret"
        ),
    )

    exit_code = cli.main(["--date", DAY.isoformat(), "--output-root", str(tmp_path)])

    assert exit_code == 1
    directory = tmp_path / DAY.isoformat()
    primary, errors, manifest = cli.read_validated_generation(directory)
    assert primary == {"records": []}
    assert errors == [{
        "stage": "source",
        "type": "RuntimeError",
        "message": "Database source operation failed",
    }]
    assert manifest["source_row_count"] is None
    persisted = "".join(
        (directory / name).read_text(encoding="utf-8")
        for name in ("polymarket_db.json", "errors.json", "manifest.json")
    )
    assert all(secret not in persisted for secret in (
        "host-secret", "user-secret", "database-secret", "password-secret",
        "query-secret",
    ))


def test_failed_rerun_supersedes_an_older_success_for_the_same_date(
    tmp_path, monkeypatch
):
    install_source(monkeypatch, [db_row()])
    arguments = ["--date", DAY.isoformat(), "--output-root", str(tmp_path)]
    assert cli.main(arguments) == 0

    install_source(monkeypatch, [])
    assert cli.main(arguments) == 1

    primary, errors, manifest = cli.read_validated_generation(
        tmp_path / DAY.isoformat()
    )
    assert primary == {"records": []}
    assert errors == [cli._no_records_error()]
    assert manifest["status"] == "failed"


def test_main_returns_one_when_artifacts_cannot_be_committed(tmp_path, monkeypatch):
    install_source(monkeypatch, [db_row()])
    monkeypatch.setattr(cli, "_write_artifacts", lambda *_args: False)

    assert cli.main([
        "--date", DAY.isoformat(), "--output-root", str(tmp_path)
    ]) == 1
```

- [ ] **Step 2: Run CLI tests and verify `main` is missing**

```bash
.venv/bin/python -m pytest tests/test_polymarket_db_cli.py -q
```

Expected: FAIL because `main` is not defined.

- [ ] **Step 3: Import source and contract dependencies**

Add these imports after the project-root bootstrap in
`export_polymarket_db.py`:

```python
from common.artifacts import output_directory, write_json_atomic
from getDB.Polymarket.tool.contract import build_db_output
from getDB.Polymarket.tool.db_source import (
    PgSettings,
    fetch_day_rows,
    load_pg_settings,
)
```

Remove the earlier duplicate `write_json_atomic` import while keeping one
authoritative import line.

- [ ] **Step 4: Implement orchestration and exact exit codes**

Append:

```python
def main(argv: list[str] | None = None) -> int:
    arguments = parse_args(argv)
    lower, upper = china_day_bounds(arguments.date)

    try:
        settings = load_pg_settings()
        rows = fetch_day_rows(settings, lower, upper)
    except Exception as error:
        source_row_count = None
        primary = {"records": []}
        errors = [_source_error(error)]
        category_counts = {}
    else:
        source_row_count = len(rows)
        if rows:
            primary, errors, category_counts = build_db_output(rows)
        else:
            primary = {"records": []}
            errors = [_no_records_error()]
            category_counts = {}

    record_count = len(primary["records"])
    status = _generation_status(record_count, len(errors))
    manifest = _manifest(
        business_date=arguments.date,
        lower=lower,
        upper=upper,
        generation_id=_new_generation_id(),
        captured_at=_utc_now(),
        status=status,
        source_row_count=source_row_count,
        record_count=record_count,
        error_count=len(errors),
        category_counts=category_counts,
    )
    directory = output_directory(arguments.output_root, arguments.date)
    try:
        written = _write_artifacts(directory, primary, manifest, errors)
    except Exception:
        written = False
    return 0 if written and status == "success" else 1


if __name__ == "__main__":
    raise SystemExit(main())
```

Do not catch or reinterpret individual row errors here; `build_db_output`
already isolates them and reports the exact source-row difference.

- [ ] **Step 5: Run all new Polymarket DB tests**

```bash
.venv/bin/python -m pytest \
  tests/test_polymarket_db_contract.py \
  tests/test_polymarket_db_source.py \
  tests/test_polymarket_db_cli.py -q
```

Expected: PASS.

- [ ] **Step 6: Verify the module help without touching PostgreSQL**

```bash
.venv/bin/python -m getDB.Polymarket.tool.export_polymarket_db --help
```

Expected: exit `0` and help containing `--date` and `--output-root`.

- [ ] **Step 7: Commit the completed CLI workflow**

```bash
git add getDB/Polymarket/tool/export_polymarket_db.py \
  tests/test_polymarket_db_cli.py
git commit -m "feat: export Polymarket database generations"
```

### Task 7: Document And Verify The Completed Exporter

**Files:**
- Create: `getDB/Polymarket/README.md`
- Modify: `命令使用指南.md`
- Verify: all production and test files from Tasks 1-6

- [ ] **Step 1: Create the feature README with the exact operator contract**

Create `getDB/Polymarket/README.md`:

````markdown
# Polymarket 数据库导出

该工具只读取 PostgreSQL 中已经保存的 Polymarket 选择结果，不访问 Gamma API，
不重新分类、排名或去重。日期按 `created_at` 的 `Asia/Shanghai` 自然日计算，SQL
使用对应的 UTC 半开区间。

## 运行

```bash
.venv/bin/python -m getDB.Polymarket.tool.export_polymarket_db \
  --date 2026-07-29
```

试运行必须指定独立输出目录：

```bash
.venv/bin/python -m getDB.Polymarket.tool.export_polymarket_db \
  --date 2026-07-29 \
  --output-root /tmp/dbcompare-polymarket-db-smoke
```

`--date` 默认使用上海当天。数据库连接读取 `.env` 中的 `PGHOST`、`PGPORT`、
`PGDATABASE`、`PGUSER` 和 `PGPASSWORD`。

## 产物

```text
getDB/Polymarket/db/YYYY-MM-DD/
|-- .generation.lock
|-- polymarket_db.json
|-- errors.json
`-- manifest.json
```

`polymarket_db.json` 的顶层是 `{"records": [...]}`。每个合法数据库行保留为
一条独立记录；相同市场出现在多个大类、快照或批次时不会合并。排序依次使用
大类、`rank`、`created_at` 和数据库 `id`。

manifest 状态和退出码：

| 状态 | 含义 | 退出码 |
| --- | --- | --- |
| `success` | 至少一条合法记录且无异常 | 0 |
| `partial` | 有合法记录，但部分行被隔离到 `errors.json` | 1 |
| `failed` | 查询失败、零记录或全部记录非法 | 1 |

`manifest.json` 是最终提交标记，并包含两个 JSON 产物的 SHA-256。消费者应使用
`read_validated_generation()` 在共享锁内读取，并在使用业务记录前检查 `status`。

## 测试

```bash
.venv/bin/python -m pytest tests/test_polymarket_db_*.py -q
```
````

- [ ] **Step 2: Add the DB command to the global guide**

In `命令使用指南.md`, retain the current Bubblemaps DB instructions as
`### 1.1 Bubblemaps 数据库快照`, then add this directly afterward:

````markdown
### 1.2 Polymarket 数据库选择结果

该命令忠实导出数据库已有选择，不调用 Polymarket API，也不重新排名或去重。

```bash
.venv/bin/python -m getDB.Polymarket.tool.export_polymarket_db \
  --date 2026-07-29 \
  --output-root /tmp/dbcompare-polymarket-db-smoke
```

日期按 `created_at` 的 `Asia/Shanghai` 自然日筛选。默认目录为
`getDB/Polymarket/db/YYYY-MM-DD/`，包含 `polymarket_db.json`、`errors.json`
和 `manifest.json`。只有 `success` 返回 0；`partial` 和 `failed` 返回 1。
````

Also add this focused test command under section 4:

```bash
.venv/bin/python -m pytest tests/test_polymarket_db_*.py -q
```

And add this help command beside the existing CLI help commands:

```bash
.venv/bin/python -m getDB.Polymarket.tool.export_polymarket_db --help
```

- [ ] **Step 3: Run focused tests**

```bash
.venv/bin/python -m pytest tests/test_polymarket_db_*.py -q
```

Expected: PASS.

- [ ] **Step 4: Run the entire offline regression suite**

```bash
.venv/bin/python -m pytest \
  -m "not live_bubblemaps and not live_polymarket" -q
```

Expected: PASS with no regressions in Bubblemaps or the Polymarket API
collector.

- [ ] **Step 5: Run compilation and whitespace checks**

```bash
.venv/bin/python -m compileall -q common getDB getMarket
git diff --check
```

Expected: both commands exit `0` with no output.

- [ ] **Step 6: Run a real read-only database smoke export**

Resolve the latest available Shanghai date without printing credentials:

```bash
SMOKE_DATE=$(.venv/bin/python -c 'import psycopg; from getDB.Polymarket.tool.db_source import load_pg_settings; s=load_pg_settings(); c=psycopg.connect(host=s.host,port=s.port,dbname=s.dbname,user=s.user,password=s.password); q=c.cursor(); q.execute("BEGIN TRANSACTION READ ONLY;"); q.execute("SELECT MAX((created_at AT TIME ZONE %s)::date)::text FROM public.information WHERE from_source=%s AND data_type=%s", ("Asia/Shanghai", "polymarket", "PREDICTION_MARKET_SELECTION")); print(q.fetchone()[0]); q.execute("ROLLBACK;"); c.close()')
SMOKE_ROOT=$(mktemp -d)
echo "$SMOKE_ROOT"
.venv/bin/python -m getDB.Polymarket.tool.export_polymarket_db \
  --date "$SMOKE_DATE" --output-root "$SMOKE_ROOT"
```

Expected: exit `0` for a date whose rows are all valid. Do not delete the temp
directory; retain its printed path for inspection.

- [ ] **Step 7: Independently compare SQL and artifact counts**

Run this with `SMOKE_DATE` and `SMOKE_ROOT` still set:

```bash
SMOKE_DATE="$SMOKE_DATE" SMOKE_ROOT="$SMOKE_ROOT" .venv/bin/python - <<'PY'
import json
import os
from collections import Counter
from datetime import date
from pathlib import Path

import psycopg

from common.time_window import china_day_bounds
from getDB.Polymarket.tool.db_source import load_pg_settings

day = date.fromisoformat(os.environ["SMOKE_DATE"])
root = Path(os.environ["SMOKE_ROOT"]) / day.isoformat()
lower, upper = china_day_bounds(day)
settings = load_pg_settings()
with psycopg.connect(
    host=settings.host,
    port=settings.port,
    dbname=settings.dbname,
    user=settings.user,
    password=settings.password,
) as connection:
    with connection.cursor() as cursor:
        cursor.execute("BEGIN TRANSACTION READ ONLY;")
        cursor.execute(
            "SELECT COUNT(*) FROM public.information "
            "WHERE from_source=%s AND data_type=%s "
            "AND created_at >= %s AND created_at < %s",
            ("polymarket", "PREDICTION_MARKET_SELECTION", lower, upper),
        )
        sql_count = cursor.fetchone()[0]
        cursor.execute("ROLLBACK;")

primary = json.loads((root / "polymarket_db.json").read_text(encoding="utf-8"))
errors = json.loads((root / "errors.json").read_text(encoding="utf-8"))
manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
categories = Counter(row["content"]["category"] for row in primary["records"])
assert manifest["source_row_count"] == sql_count
assert manifest["record_count"] == len(primary["records"])
assert manifest["error_count"] == len(errors)
assert manifest["category_counts"] == dict(categories)
assert sql_count == len(primary["records"]) + sum(
    error["stage"] == "row_validation" for error in errors
)
print({
    "date": day.isoformat(),
    "source_rows": sql_count,
    "valid_records": len(primary["records"]),
    "errors": len(errors),
    "categories": dict(categories),
})
PY
```

Expected: all assertions pass and the printed counts agree. Do not assert a
fixed count such as 60 because the database can receive new rows.

- [ ] **Step 8: Commit documentation**

```bash
git add getDB/Polymarket/README.md 命令使用指南.md
git commit -m "docs: explain Polymarket database export"
```

- [ ] **Step 9: Review the final change set**

```bash
git status --short
git log --oneline -7
git diff HEAD~7..HEAD --stat
```

Expected: clean worktree; seven focused commits covering row contract, ordering,
database source, CLI foundation, artifacts, orchestration, and documentation.
