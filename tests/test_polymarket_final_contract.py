from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
import json

import pytest

from getMarket.Polymarket.tool.final_contract import (
    CONTENT_FIELDS,
    EXTRA_DATA_FIELDS,
    OUTER_FIELDS,
    build_db_aligned_final,
)


BUSINESS_DATE = date(2026, 7, 28)
CAPTURED_AT = datetime(2026, 7, 28, tzinfo=timezone.utc)


def selected(**overrides):
    item = {
        "market_id": "market-1",
        "selected_category": "politics",
        "rank_in_category": 1,
        "source": {
            "question": "Will this pass?",
            "outcomes": ["Yes", "No"],
            "outcomePrices": ["0.4", "0.6"],
        },
        "normalized_metrics": {
            "liquidity": "1200.5",
            "volume24hr": "42",
            "dominant_probability": "0.6",
        },
    }
    item.update(overrides)
    return item


def build(rows):
    return build_db_aligned_final(
        rows, business_date=BUSINESS_DATE, captured_at=CAPTURED_AT
    )


def test_constants_have_the_exact_db_aligned_field_order():
    assert OUTER_FIELDS == (
        "id", "data_type", "title", "summary", "content", "from_source",
        "source_url", "content_hash", "extra_data", "published_at", "created_at",
        "updated_at", "tags", "source_updated_at",
    )
    assert CONTENT_FIELDS == (
        "category", "dominant_outcome", "dominant_probability", "event_id",
        "fetched_at", "liquidity", "market_id", "market_question", "outcome",
        "probability", "rank", "record_type", "snapshot_date", "title",
        "volume24hr", "window_end", "window_start",
    )
    assert EXTRA_DATA_FIELDS == (
        "endpoint", "event_id", "fetched_at", "market_id", "rank",
        "snapshot_date", "window_end", "window_start",
    )


def test_builds_one_complete_db_aligned_record():
    assert build([selected()]) == {
        "records": [{
            "id": None,
            "data_type": "PREDICTION_MARKET_SELECTION",
            "title": "Will this pass?",
            "summary": "Will this pass?",
            "content": {
                "category": "politics", "dominant_outcome": "No",
                "dominant_probability": 0.6, "event_id": None,
                "fetched_at": "2026-07-28T00:00:00Z", "liquidity": 1200.5,
                "market_id": "market-1", "market_question": "Will this pass?",
                "outcome": "No", "probability": 0.6, "rank": 1,
                "record_type": "prediction_market_selection",
                "snapshot_date": "2026-07-28", "title": "Will this pass?",
                "volume24hr": 42.0, "window_end": None, "window_start": None,
            },
            "from_source": "polymarket", "source_url": None, "content_hash": None,
            "extra_data": {
                "endpoint": None, "event_id": None,
                "fetched_at": "2026-07-28T00:00:00+00:00",
                "market_id": "market-1", "rank": 1, "snapshot_date": "2026-07-28",
                "window_end": None, "window_start": None,
            },
            "published_at": None, "created_at": "2026-07-28T08:00:00+08:00",
            "updated_at": "2026-07-28T08:00:00+08:00",
            "tags": ["active", "category:politics", "prediction", "selected-market"],
            "source_updated_at": None,
        }],
    }


def test_normalizes_non_utc_capture_time_for_each_timestamp_contract():
    captured_at = datetime(2026, 7, 27, 20, tzinfo=timezone(-timedelta(hours=4)))

    record = build_db_aligned_final(
        [selected()], business_date=BUSINESS_DATE, captured_at=captured_at
    )["records"][0]

    assert record["content"]["fetched_at"] == "2026-07-28T00:00:00Z"
    assert record["extra_data"]["fetched_at"] == "2026-07-28T00:00:00+00:00"
    assert record["created_at"] == "2026-07-28T08:00:00+08:00"
    assert record["updated_at"] == "2026-07-28T08:00:00+08:00"


def test_does_not_mutate_selected_items_or_nested_source():
    rows = [selected()]
    before = deepcopy(rows)

    build(rows)

    assert rows == before


def test_selects_dominant_outcome_from_lists():
    output = build([selected()])

    assert output["records"][0]["content"]["dominant_outcome"] == "No"
    assert output["records"][0]["content"]["outcome"] == "No"


def test_selects_first_dominant_outcome_for_tied_json_arrays():
    row = selected(source={
        "question": "Tie?", "outcomes": '["First", "Second"]',
        "outcomePrices": "[0.5, 0.5]",
    })

    output = build([row])

    assert output["records"][0]["content"]["dominant_outcome"] == "First"
    assert output["records"][0]["content"]["outcome"] == "First"


def test_invalid_optional_metrics_outcomes_and_question_become_none():
    row = selected(
        source={"question": None, "outcomes": ["Yes"], "outcomePrices": ["0.4", "0.6"]},
        normalized_metrics={
            "liquidity": float("nan"), "volume24hr": "not-a-number",
            "dominant_probability": "1.1",
        },
    )

    record = build([row])["records"][0]

    assert record["title"] is None
    assert record["summary"] is None
    assert record["content"]["market_question"] is None
    assert record["content"]["title"] is None
    assert record["content"]["liquidity"] is None
    assert record["content"]["volume24hr"] is None
    assert record["content"]["dominant_probability"] is None
    assert record["content"]["probability"] is None
    assert record["content"]["dominant_outcome"] is None
    assert record["content"]["outcome"] is None


@pytest.mark.parametrize("metrics", [None, "unavailable"])
def test_missing_or_non_mapping_metrics_become_none(metrics):
    row = selected(normalized_metrics=metrics)

    content = build([row])["records"][0]["content"]

    assert content["liquidity"] is None
    assert content["volume24hr"] is None
    assert content["dominant_probability"] is None
    assert content["probability"] is None


@pytest.mark.parametrize(("override", "message"), [
    ({"market_id": "  "}, "market_id must be a non-whitespace string"),
    ({"selected_category": "sports"}, "selected_category must be configured"),
    ({"rank_in_category": 0}, "rank_in_category must be a positive integer"),
    ({"rank_in_category": True}, "rank_in_category must be a positive integer"),
    ({"source": None}, "source must be a mapping"),
])
def test_rejects_invalid_required_selected_fields(override, message):
    with pytest.raises(ValueError, match=f"^{message}$"):
        build([selected(**override)])


def test_rejects_non_mapping_selected_item():
    with pytest.raises(ValueError, match="^selected item must be a mapping$"):
        build([None])


def test_rejects_invalid_business_date_and_naive_capture_time():
    with pytest.raises(TypeError, match="^business_date must be a date$"):
        build_db_aligned_final([], business_date="2026-07-28", captured_at=CAPTURED_AT)
    with pytest.raises(TypeError, match="^captured_at must be timezone-aware$"):
        build_db_aligned_final(
            [], business_date=BUSINESS_DATE,
            captured_at=datetime(2026, 7, 28),
        )


def test_preserves_duplicate_market_ids_across_categories_and_input_order():
    rows = [
        selected(selected_category="finance", rank_in_category=2),
        selected(selected_category="politics", rank_in_category=1),
    ]

    output = build(rows)["records"]

    assert len(output) == 2
    assert [(row["content"]["market_id"], row["content"]["category"], row["content"]["rank"])
            for row in output] == [
        ("market-1", "finance", 2), ("market-1", "politics", 1),
    ]


def test_result_is_json_safe_and_round_trips():
    output = build([selected()])

    assert json.loads(json.dumps(output, allow_nan=False)) == output
