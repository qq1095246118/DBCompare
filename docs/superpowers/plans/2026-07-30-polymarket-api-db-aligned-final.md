# Polymarket API DB-Aligned Final Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Change only the Polymarket API collector's successful `final.json` into the same 14/17/8-field JSON structure as the processed DB export, using `null` for unavailable API values while preserving API ranking order and the existing `clean.json` contract.

**Architecture:** Add a pure, non-mutating final-artifact converter at `getMarket/Polymarket/tool/final_contract.py`. The collector will continue to fetch, merge, and rank exactly as it does now, then convert `ranked.selected` immediately before atomically writing successful artifacts; conversion errors remain inside the existing sanitized failure boundary.

**Tech Stack:** Python 3 standard library (`datetime`, `decimal`, `json`, `zoneinfo`), existing Polymarket collector modules, `pytest`, `pytest-asyncio`.

---

## Reference And Boundaries

The approved design is:
`docs/superpowers/specs/2026-07-30-polymarket-api-db-aligned-final-design.md`.

This plan changes only the API collector's successful `final.json`. It does not
change Gamma requests, Tag/category rules, ranking, `raw/`, `clean.json`, the DB
exporter, or database validation. In particular, the API artifact deliberately
contains `id: null`, so it must not be passed to
`getDB.Polymarket.tool.contract.validate_serialized_record`.

## File Map

- Create `getMarket/Polymarket/tool/final_contract.py`: validate selected API
  records and convert them to the DB-aligned 14/17/8-field final payload.
- Create `tests/test_polymarket_final_contract.py`: focused unit coverage for
  exact structure, values, null behavior, ordering, duplicate category records,
  time zones, validation, and input immutability.
- Modify `getMarket/Polymarket/tool/export_polymarket_market.py`: call the pure
  converter after ranking and before either successful artifact is written.
- Modify `tests/test_polymarket_cli.py`: prove the new `final.json` shape,
  unchanged `clean.json`, multi-category preservation, and failure atomicity.
- Modify `getMarket/Polymarket/README.md`: document the new API final contract
  and the API-only details that remain in `clean.json`.
- Modify `命令使用指南.md`: update the operator-facing artifact description.

### Task 1: Build The Pure Final Contract Converter

**Files:**
- Create: `getMarket/Polymarket/tool/final_contract.py`
- Create: `tests/test_polymarket_final_contract.py`

- [ ] **Step 1: Write the complete failing converter tests**

Create `tests/test_polymarket_final_contract.py` with exactly this content:

```python
from copy import deepcopy
from datetime import date, datetime, timezone
import json

import pytest

from getMarket.Polymarket.tool.final_contract import (
    CONTENT_FIELDS,
    EXTRA_DATA_FIELDS,
    OUTER_FIELDS,
    build_db_aligned_final,
)


DAY = date(2026, 7, 28)
CAPTURED_AT = datetime(2026, 7, 28, 0, 0, 0, tzinfo=timezone.utc)

EXPECTED_OUTER_FIELDS = (
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
EXPECTED_CONTENT_FIELDS = (
    "category",
    "dominant_outcome",
    "dominant_probability",
    "event_id",
    "fetched_at",
    "liquidity",
    "market_id",
    "market_question",
    "outcome",
    "probability",
    "rank",
    "record_type",
    "snapshot_date",
    "title",
    "volume24hr",
    "window_end",
    "window_start",
)
EXPECTED_EXTRA_DATA_FIELDS = (
    "endpoint",
    "event_id",
    "fetched_at",
    "market_id",
    "rank",
    "snapshot_date",
    "window_end",
    "window_start",
)


def selected_record(
    *,
    market_id: str = "market-1",
    category: str = "politics",
    rank: int = 1,
) -> dict[str, object]:
    return {
        "market_id": market_id,
        "categories": [category],
        "matched_tag_ids": ["2"],
        "matched_crypto_keywords": [],
        "normalized_metrics": {
            "liquidity": "1200.5",
            "dominant_probability": "0.6",
            "volume24hr": "800",
        },
        "source": {
            "id": market_id,
            "question": "Will the bill pass?",
            "outcomes": ["Yes", "No"],
            "outcomePrices": ["0.6", "0.4"],
        },
        "selected_category": category,
        "selected_by": "liquidity",
        "priority": 1,
        "rank_in_category": rank,
    }


def test_build_db_aligned_final_maps_exact_contract_without_mutation() -> None:
    selected = [selected_record()]
    before = deepcopy(selected)

    payload = build_db_aligned_final(
        selected,
        business_date=DAY,
        captured_at=CAPTURED_AT,
    )

    assert OUTER_FIELDS == EXPECTED_OUTER_FIELDS
    assert CONTENT_FIELDS == EXPECTED_CONTENT_FIELDS
    assert EXTRA_DATA_FIELDS == EXPECTED_EXTRA_DATA_FIELDS
    assert list(payload) == ["records"]
    assert len(payload["records"]) == 1
    record = payload["records"][0]
    assert set(record) == set(EXPECTED_OUTER_FIELDS)
    assert set(record["content"]) == set(EXPECTED_CONTENT_FIELDS)
    assert set(record["extra_data"]) == set(EXPECTED_EXTRA_DATA_FIELDS)
    assert record == {
        "id": None,
        "data_type": "PREDICTION_MARKET_SELECTION",
        "title": "Will the bill pass?",
        "summary": "Will the bill pass?",
        "content": {
            "category": "politics",
            "dominant_outcome": "Yes",
            "dominant_probability": 0.6,
            "event_id": None,
            "fetched_at": "2026-07-28T00:00:00Z",
            "liquidity": 1200.5,
            "market_id": "market-1",
            "market_question": "Will the bill pass?",
            "outcome": "Yes",
            "probability": 0.6,
            "rank": 1,
            "record_type": "prediction_market_selection",
            "snapshot_date": "2026-07-28",
            "title": "Will the bill pass?",
            "volume24hr": 800.0,
            "window_end": None,
            "window_start": None,
        },
        "from_source": "polymarket",
        "source_url": None,
        "content_hash": None,
        "extra_data": {
            "endpoint": None,
            "event_id": None,
            "fetched_at": "2026-07-28T00:00:00+00:00",
            "market_id": "market-1",
            "rank": 1,
            "snapshot_date": "2026-07-28",
            "window_end": None,
            "window_start": None,
        },
        "published_at": None,
        "created_at": "2026-07-28T08:00:00+08:00",
        "updated_at": "2026-07-28T08:00:00+08:00",
        "tags": [
            "active",
            "category:politics",
            "prediction",
            "selected-market",
        ],
        "source_updated_at": None,
    }
    assert json.loads(json.dumps(payload, allow_nan=False)) == payload
    assert selected == before


@pytest.mark.parametrize(
    ("outcomes", "prices", "expected"),
    [
        (["Yes", "No"], ["0.4", "0.6"], "No"),
        ('["First", "Second"]', '["0.5", "0.5"]', "First"),
    ],
)
def test_build_db_aligned_final_decodes_outcomes_and_breaks_ties_by_position(
    outcomes: object,
    prices: object,
    expected: str,
) -> None:
    row = selected_record()
    row["source"]["outcomes"] = outcomes
    row["source"]["outcomePrices"] = prices

    content = build_db_aligned_final(
        [row], business_date=DAY, captured_at=CAPTURED_AT
    )["records"][0]["content"]

    assert content["dominant_outcome"] == expected
    assert content["outcome"] == expected


def test_build_db_aligned_final_turns_malformed_optional_values_into_null() -> None:
    row = selected_record()
    row["normalized_metrics"] = {
        "liquidity": "NaN",
        "dominant_probability": "1.1",
        "volume24hr": "not-a-number",
    }
    row["source"]["outcomes"] = ["Yes"]
    row["source"]["outcomePrices"] = ["0.4", "0.6"]
    row["source"]["question"] = None

    record = build_db_aligned_final(
        [row], business_date=DAY, captured_at=CAPTURED_AT
    )["records"][0]

    assert record["title"] is None
    assert record["summary"] is None
    assert record["content"]["market_question"] is None
    assert record["content"]["title"] is None
    assert record["content"]["liquidity"] is None
    assert record["content"]["dominant_probability"] is None
    assert record["content"]["probability"] is None
    assert record["content"]["volume24hr"] is None
    assert record["content"]["dominant_outcome"] is None
    assert record["content"]["outcome"] is None


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("market_id", " ", "market_id must be a non-whitespace string"),
        ("selected_category", "sports", "selected_category must be configured"),
        ("rank_in_category", 0, "rank_in_category must be a positive integer"),
        ("rank_in_category", True, "rank_in_category must be a positive integer"),
        ("source", None, "source must be a mapping"),
    ],
)
def test_build_db_aligned_final_rejects_invalid_required_selection_fields(
    field: str,
    value: object,
    message: str,
) -> None:
    row = selected_record()
    row[field] = value

    with pytest.raises(ValueError, match=message):
        build_db_aligned_final(
            [row], business_date=DAY, captured_at=CAPTURED_AT
        )


def test_build_db_aligned_final_rejects_non_mapping_selected_item() -> None:
    with pytest.raises(ValueError, match="selected item must be a mapping"):
        build_db_aligned_final(
            [None], business_date=DAY, captured_at=CAPTURED_AT
        )


def test_build_db_aligned_final_validates_date_and_aware_capture_time() -> None:
    row = selected_record()
    with pytest.raises(TypeError, match="business_date must be a date"):
        build_db_aligned_final(
            [row], business_date="2026-07-28", captured_at=CAPTURED_AT
        )
    with pytest.raises(TypeError, match="captured_at must be timezone-aware"):
        build_db_aligned_final(
            [row], business_date=DAY, captured_at=datetime(2026, 7, 28)
        )


def test_build_db_aligned_final_preserves_order_and_cross_category_duplicates() -> None:
    selected = [
        selected_record(market_id="same-market", category="finance", rank=2),
        selected_record(market_id="same-market", category="politics", rank=1),
    ]

    records = build_db_aligned_final(
        selected, business_date=DAY, captured_at=CAPTURED_AT
    )["records"]

    assert [record["content"]["category"] for record in records] == [
        "finance",
        "politics",
    ]
    assert [record["content"]["rank"] for record in records] == [2, 1]
    assert [record["content"]["market_id"] for record in records] == [
        "same-market",
        "same-market",
    ]
```

- [ ] **Step 2: Run the tests and verify the RED state**

Run:

```bash
.venv/bin/python -m pytest tests/test_polymarket_final_contract.py -q
```

Expected: collection fails with `ModuleNotFoundError` for
`getMarket.Polymarket.tool.final_contract`.

- [ ] **Step 3: Implement the pure converter**

Create `getMarket/Polymarket/tool/final_contract.py` with exactly this content:

```python
"""Build the DB-aligned final artifact for selected Polymarket API rows."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
import json
from zoneinfo import ZoneInfo

from getMarket.Polymarket.tool.market_filter import CATEGORY_ORDER


OUTER_FIELDS = (
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
CONTENT_FIELDS = (
    "category",
    "dominant_outcome",
    "dominant_probability",
    "event_id",
    "fetched_at",
    "liquidity",
    "market_id",
    "market_question",
    "outcome",
    "probability",
    "rank",
    "record_type",
    "snapshot_date",
    "title",
    "volume24hr",
    "window_end",
    "window_start",
)
EXTRA_DATA_FIELDS = (
    "endpoint",
    "event_id",
    "fetched_at",
    "market_id",
    "rank",
    "snapshot_date",
    "window_end",
    "window_start",
)

_SHANGHAI = ZoneInfo("Asia/Shanghai")


def _optional_number(
    value: object,
    *,
    minimum: Decimal,
    maximum: Decimal | None = None,
) -> float | None:
    if isinstance(value, bool) or not isinstance(
        value, (str, int, float, Decimal)
    ):
        return None
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    if not parsed.is_finite() or parsed < minimum:
        return None
    if maximum is not None and parsed > maximum:
        return None
    return float(parsed)


def _decoded_array(value: object) -> list[object] | None:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            return None
    return value if type(value) is list else None


def _dominant_outcome(source: Mapping[str, object]) -> str | None:
    outcomes = _decoded_array(source.get("outcomes"))
    prices = _decoded_array(source.get("outcomePrices"))
    if (
        not outcomes
        or not prices
        or len(outcomes) != len(prices)
        or any(not isinstance(outcome, str) for outcome in outcomes)
    ):
        return None
    parsed_prices = [
        _optional_number(
            value,
            minimum=Decimal(0),
            maximum=Decimal(1),
        )
        for value in prices
    ]
    if any(value is None for value in parsed_prices):
        return None
    best_index = max(
        range(len(parsed_prices)),
        key=lambda index: parsed_prices[index],
    )
    return outcomes[best_index]


def _required_text(row: Mapping[str, object], field: str) -> str:
    value = row.get(field)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a non-whitespace string")
    return value


def _build_record(
    selected: object,
    *,
    snapshot_date: str,
    fetched_at: str,
    fetched_at_offset: str,
    shanghai_timestamp: str,
) -> dict[str, object]:
    if not isinstance(selected, Mapping):
        raise ValueError("selected item must be a mapping")
    market_id = _required_text(selected, "market_id")
    category = _required_text(selected, "selected_category")
    if category not in CATEGORY_ORDER:
        raise ValueError("selected_category must be configured")
    rank = selected.get("rank_in_category")
    if type(rank) is not int or rank < 1:
        raise ValueError("rank_in_category must be a positive integer")
    source = selected.get("source")
    if not isinstance(source, Mapping):
        raise ValueError("source must be a mapping")

    question_value = source.get("question")
    question = question_value if isinstance(question_value, str) else None
    metrics_value = selected.get("normalized_metrics")
    metrics = metrics_value if isinstance(metrics_value, Mapping) else {}
    liquidity = _optional_number(
        metrics.get("liquidity"), minimum=Decimal(0)
    )
    probability = _optional_number(
        metrics.get("dominant_probability"),
        minimum=Decimal(0),
        maximum=Decimal(1),
    )
    volume24hr = _optional_number(
        metrics.get("volume24hr"), minimum=Decimal(0)
    )
    outcome = _dominant_outcome(source)

    content = {
        "category": category,
        "dominant_outcome": outcome,
        "dominant_probability": probability,
        "event_id": None,
        "fetched_at": fetched_at,
        "liquidity": liquidity,
        "market_id": market_id,
        "market_question": question,
        "outcome": outcome,
        "probability": probability,
        "rank": rank,
        "record_type": "prediction_market_selection",
        "snapshot_date": snapshot_date,
        "title": question,
        "volume24hr": volume24hr,
        "window_end": None,
        "window_start": None,
    }
    extra_data = {
        "endpoint": None,
        "event_id": None,
        "fetched_at": fetched_at_offset,
        "market_id": market_id,
        "rank": rank,
        "snapshot_date": snapshot_date,
        "window_end": None,
        "window_start": None,
    }
    return {
        "id": None,
        "data_type": "PREDICTION_MARKET_SELECTION",
        "title": question,
        "summary": question,
        "content": content,
        "from_source": "polymarket",
        "source_url": None,
        "content_hash": None,
        "extra_data": extra_data,
        "published_at": None,
        "created_at": shanghai_timestamp,
        "updated_at": shanghai_timestamp,
        "tags": [
            "active",
            f"category:{category}",
            "prediction",
            "selected-market",
        ],
        "source_updated_at": None,
    }


def _validate_shape(payload: object) -> None:
    if type(payload) is not dict or set(payload) != {"records"}:
        raise ValueError("final payload fields do not match the contract")
    records = payload["records"]
    if type(records) is not list:
        raise ValueError("final records must be an array")
    for record in records:
        if type(record) is not dict or set(record) != set(OUTER_FIELDS):
            raise ValueError("final record fields do not match the contract")
        content = record["content"]
        if type(content) is not dict or set(content) != set(CONTENT_FIELDS):
            raise ValueError("final content fields do not match the contract")
        extra_data = record["extra_data"]
        if (
            type(extra_data) is not dict
            or set(extra_data) != set(EXTRA_DATA_FIELDS)
        ):
            raise ValueError("final extra_data fields do not match the contract")


def build_db_aligned_final(
    selected: Iterable[object],
    *,
    business_date: date,
    captured_at: datetime,
) -> dict[str, list[dict[str, object]]]:
    """Convert ranked API selections without sorting or deduplicating them."""
    if type(business_date) is not date:
        raise TypeError("business_date must be a date")
    if (
        not isinstance(captured_at, datetime)
        or captured_at.utcoffset() is None
    ):
        raise TypeError("captured_at must be timezone-aware")

    captured_utc = captured_at.astimezone(timezone.utc)
    fetched_at_offset = captured_utc.isoformat()
    fetched_at = fetched_at_offset.replace("+00:00", "Z")
    shanghai_timestamp = captured_at.astimezone(_SHANGHAI).isoformat()
    snapshot_date = business_date.isoformat()
    payload = {
        "records": [
            _build_record(
                row,
                snapshot_date=snapshot_date,
                fetched_at=fetched_at,
                fetched_at_offset=fetched_at_offset,
                shanghai_timestamp=shanghai_timestamp,
            )
            for row in selected
        ]
    }
    _validate_shape(payload)
    return payload
```

- [ ] **Step 4: Run the converter tests and verify GREEN**

Run:

```bash
.venv/bin/python -m pytest tests/test_polymarket_final_contract.py -q
```

Expected: `12 passed` with exit code `0`.

- [ ] **Step 5: Run adjacent filter and ranking regressions**

Run:

```bash
.venv/bin/python -m pytest \
  tests/test_polymarket_filter.py \
  tests/test_polymarket_ranking.py \
  tests/test_polymarket_final_contract.py -q
```

Expected: all selected tests pass with exit code `0`.

- [ ] **Step 6: Commit the converter and tests**

```bash
git add \
  getMarket/Polymarket/tool/final_contract.py \
  tests/test_polymarket_final_contract.py
git commit -m "feat: add DB-aligned Polymarket API final contract"
```

### Task 2: Integrate Conversion At The CLI Write Boundary

**Files:**
- Modify: `getMarket/Polymarket/tool/export_polymarket_market.py`
- Modify: `tests/test_polymarket_cli.py`

- [ ] **Step 1: Extend the CLI fixture with fields required to verify mapping**

In `tests/test_polymarket_cli.py`, replace `source_market` with:

```python
def source_market(market_id, liquidity):
    return {
        "id": market_id,
        "question": f"Question {market_id}?",
        "active": True,
        "closed": False,
        "description": "ETF regulation update.",
        "liquidity": str(liquidity),
        "outcomes": ["Yes", "No"],
        "outcomePrices": ["0.6", "0.4"],
        "volume24hr": str(1000 - liquidity),
    }
```

- [ ] **Step 2: Rewrite the successful-generation assertions for the new final contract**

Add this import beside the other Polymarket imports:

```python
from getMarket.Polymarket.tool.final_contract import (
    CONTENT_FIELDS,
    EXTRA_DATA_FIELDS,
    OUTER_FIELDS,
)
```

In `test_run_collects_all_tags_and_publishes_ranked_generation`, replace the
block beginning with `final = json.loads(...)` and ending immediately before
`raw_files = ...` with:

```python
    final_payload = json.loads((run_dir / "final.json").read_text())
    assert set(final_payload) == {"records"}
    final = final_payload["records"]
    assert len(final) == 120
    assert Counter(row["content"]["category"] for row in final) == Counter({
        category: 20 for category in CATEGORY_ORDER
    })
    assert [row["content"]["category"] for row in final] == [
        category for category in CATEGORY_ORDER for _ in range(20)
    ]
    assert [row["content"]["rank"] for row in final] == (
        list(range(1, 21)) * len(CATEGORY_ORDER)
    )
    assert [
        row["content"]["category"]
        for row in final
        if row["content"]["market_id"] == "shared"
    ] == ["politics", "finance"]

    representative = final[0]
    assert set(representative) == set(OUTER_FIELDS)
    assert set(representative["content"]) == set(CONTENT_FIELDS)
    assert set(representative["extra_data"]) == set(EXTRA_DATA_FIELDS)
    assert representative["id"] is None
    assert representative["data_type"] == "PREDICTION_MARKET_SELECTION"
    assert representative["from_source"] == "polymarket"
    assert representative["title"] == "Question shared?"
    assert representative["created_at"] == "2026-07-28T08:00:00+08:00"
    assert representative["updated_at"] == "2026-07-28T08:00:00+08:00"
    assert representative["content"]["dominant_outcome"] == "Yes"
    assert representative["content"]["dominant_probability"] == 0.6
    assert representative["content"]["fetched_at"] == CAPTURED_AT
    assert representative["content"]["snapshot_date"] == DAY.isoformat()
    assert representative["extra_data"]["fetched_at"] == (
        "2026-07-28T00:00:00+00:00"
    )
    assert representative["extra_data"]["snapshot_date"] == DAY.isoformat()
    assert "selected_category" not in representative
    assert "rank_in_category" not in representative
    assert "selected_by" not in representative
    assert "priority" not in representative

    clean = json.loads((run_dir / "clean.json").read_text())
    assert len(clean) == 125
    assert len({row["market_id"] for row in clean}) == 125
    shared_clean = next(row for row in clean if row["market_id"] == "shared")
    assert shared_clean["categories"] == ["finance", "politics"]
    assert shared_clean["normalized_metrics"]["liquidity"] == "100"
    assert shared_clean["source"]["question"] == "Question shared?"
    assert "selected_category" not in shared_clean
    assert "selected_by" not in shared_clean
    assert "priority" not in shared_clean
```

- [ ] **Step 3: Add a conversion-failure atomicity test**

Append this test to `tests/test_polymarket_cli.py`:

```python
@pytest.mark.asyncio
async def test_run_treats_final_conversion_failure_as_processing_error(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(cli, "_business_today", lambda: DAY)
    monkeypatch.setattr(cli, "_utc_now", lambda: CAPTURED_AT)
    monkeypatch.setattr(cli, "_run_name", lambda _day: "failed-conversion")

    def fail_conversion(*_args, **_kwargs):
        raise ValueError("private conversion detail")

    monkeypatch.setattr(cli, "build_db_aligned_final", fail_conversion)

    exit_code = await cli.run_async(
        cli.parse_args(["--output-root", str(tmp_path)]),
        client=FakeClient(complete_pages()),
    )

    assert exit_code == 1
    run_dir = tmp_path / "failed-conversion"
    error = json.loads((run_dir / "error.json").read_text())
    assert error["stage"] == "processing"
    assert error["type"] == "ValueError"
    assert error["message"] == "processing failed"
    assert "private conversion detail" not in (run_dir / "error.json").read_text()
    assert not (run_dir / "clean.json").exists()
    assert not (run_dir / "final.json").exists()
    assert len(list((run_dir / "raw").glob("tag-*/page-*.json"))) == len(
        TAG_CATEGORIES
    )
```

- [ ] **Step 4: Run the changed CLI tests and verify RED**

Run:

```bash
.venv/bin/python -m pytest \
  tests/test_polymarket_cli.py::test_run_collects_all_tags_and_publishes_ranked_generation \
  tests/test_polymarket_cli.py::test_run_treats_final_conversion_failure_as_processing_error \
  -q
```

Expected: failures because the collector still writes `ranked.selected`
directly and does not expose `build_db_aligned_final` at the CLI module boundary.

- [ ] **Step 5: Import and call the converter before successful writes**

In `getMarket/Polymarket/tool/export_polymarket_market.py`, add this import after
the `common.artifacts` import:

```python
from getMarket.Polymarket.tool.final_contract import build_db_aligned_final
```

Then replace the final four lines inside the successful `try` block:

```python
        ranked = select_ranked_markets(merged.markets)
        write_json_atomic(run_directory / "clean.json", ranked.candidates)
        write_json_atomic(run_directory / "final.json", ranked.selected)
        return 0
```

with:

```python
        ranked = select_ranked_markets(merged.markets)
        final_payload = build_db_aligned_final(
            ranked.selected,
            business_date=business_date,
            captured_at=datetime.fromisoformat(
                captured_at.replace("Z", "+00:00")
            ),
        )
        write_json_atomic(run_directory / "clean.json", ranked.candidates)
        write_json_atomic(run_directory / "final.json", final_payload)
        return 0
```

This preserves `_utc_now()` and `_safe_error()` as string-based APIs while
honoring the converter's required timezone-aware `datetime` input. Building the
final payload before both writes ensures a conversion failure cannot publish a
successful `clean.json` without its matching `final.json`.

- [ ] **Step 6: Run all CLI tests and verify GREEN**

Run:

```bash
.venv/bin/python -m pytest tests/test_polymarket_cli.py -q
```

Expected: all CLI tests pass with exit code `0`.

- [ ] **Step 7: Run the complete API-side Polymarket suite**

Run:

```bash
.venv/bin/python -m pytest \
  tests/test_polymarket_api.py \
  tests/test_polymarket_filter.py \
  tests/test_polymarket_ranking.py \
  tests/test_polymarket_final_contract.py \
  tests/test_polymarket_cli.py -q
```

Expected: all selected tests pass with exit code `0`.

- [ ] **Step 8: Commit the CLI integration**

```bash
git add \
  getMarket/Polymarket/tool/export_polymarket_market.py \
  tests/test_polymarket_cli.py
git commit -m "feat: publish DB-aligned Polymarket API final"
```

### Task 3: Document The Breaking Final Format And Verify The Repository

**Files:**
- Modify: `getMarket/Polymarket/README.md`
- Modify: `命令使用指南.md`

- [ ] **Step 1: Confirm the two stale artifact descriptions exist**

Run:

```bash
rg -n '增加 `selected_category`|final.json.*按大类展开并包含大类内排名' \
  getMarket/Polymarket/README.md 命令使用指南.md
```

Expected: both old descriptions are reported, proving the docs still describe
the pre-change top-level array.

- [ ] **Step 2: Replace the README artifact description**

In `getMarket/Polymarket/README.md`, replace the four artifact bullets under
`## 产物` with:

```markdown
- `raw/tag-*/page-*.json`：收到一页就立即写入的原始 API 响应；
- `clean.json`：按 `market_id` 全局唯一的候选市场，保留全部分类归属、匹配
  Tag、规范化指标和压缩后的 API `source`；
- `final.json`：顶层为 `{"records": [...]}`，每条记录使用与 DB 处理结果
  相同的 14 个外层字段、17 个 `content` 字段和 8 个 `extra_data` 字段；
- `error.json`：本次运行失败时的脱敏错误信息。
```

Immediately after those bullets, insert:

```markdown
`final.json` 只保证结构与 DB 处理结果对齐，不保证字段值相同，也不是有效的 DB
generation。API 无法可靠提供的字段使用 `null`，包括数据库 `id`、
`content_hash` 和窗口字段，因此不能交给 DB 侧严格记录校验器。
`selected_category` 和 `rank_in_category` 分别映射为 `content.category` 和
`content.rank`；`selected_by` 与 `priority` 不再写入最终产物。需要 API 候选细节时
读取结构保持不变的 `clean.json`。
```

- [ ] **Step 3: Replace the command guide's final artifact paragraph**

In `命令使用指南.md`, replace:

```markdown
`raw` 页面会边采集边写入；`clean.json` 保持全局 `market_id` 唯一，
`final.json` 按大类展开并包含大类内排名。`final.json` 不存在表示本次没有完整成功。
```

with:

```markdown
`raw` 页面会边采集边写入；`clean.json` 保持全局 `market_id` 唯一并保留 API
候选细节。`final.json` 顶层为 `{"records": [...]}`，记录使用与 DB 处理结果
相同的 14/17/8 字段结构，API 无法提供的 DB 字段为 `null`。它只做结构对齐，
不是有效的 DB generation；`selected_by` 和 `priority` 不再写入最终产物。
`final.json` 不存在表示本次没有完整成功。
```

- [ ] **Step 4: Verify the documentation states the new boundary**

Run:

```bash
rg -n '"records"|14/17/8|selected_by|不是有效的 DB' \
  getMarket/Polymarket/README.md 命令使用指南.md
```

Expected: both documents mention `{"records": [...]}`, structural alignment,
nullable/unavailable DB fields, and the loss of `selected_by`/`priority` from
the final artifact.

- [ ] **Step 5: Run all Polymarket tests, including DB export regressions**

Run:

```bash
.venv/bin/python -m pytest \
  tests/test_polymarket_*.py \
  -m "not live_polymarket" -q
```

Expected: all offline Polymarket API and DB tests pass; the live smoke tests are
deselected.

- [ ] **Step 6: Run the complete offline regression suite**

Run:

```bash
.venv/bin/python -m pytest \
  -m "not live_bubblemaps and not live_polymarket" -q
```

Expected: the complete offline suite passes with exit code `0`.

- [ ] **Step 7: Run compilation and whitespace checks**

Run:

```bash
.venv/bin/python -m compileall -q common getDB getMarket
git diff --check
```

Expected: both commands exit `0` with no output.

- [ ] **Step 8: Review the final diff against the approved scope**

Run:

```bash
git diff --stat HEAD~2
git diff HEAD~2 -- \
  getMarket/Polymarket/tool/final_contract.py \
  getMarket/Polymarket/tool/export_polymarket_market.py \
  tests/test_polymarket_final_contract.py \
  tests/test_polymarket_cli.py \
  getMarket/Polymarket/README.md \
  命令使用指南.md
```

Expected: only the six listed files are part of the feature; there are no
changes to ranking, category filters, DB export code, generated market data, or
the `clean.json` candidate construction.

- [ ] **Step 9: Commit documentation and final verification state**

```bash
git add getMarket/Polymarket/README.md 命令使用指南.md
git commit -m "docs: explain DB-aligned Polymarket API final"
```

- [ ] **Step 10: Run the final clean-tree verification**

Run:

```bash
git status --short
```

Expected: no tracked modifications remain. Existing untracked generated
artifact directories may still appear and must not be added, modified, or
deleted.
