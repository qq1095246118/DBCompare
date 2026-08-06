from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
import json
from pathlib import Path
from types import MappingProxyType

import pytest

from getMarket.Polymarket.tool.final_contract import (
    CONTENT_FIELDS,
    CRYPTO_CONTENT_FIELDS,
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
        "ranking_metric": "liquidity",
        "ranking_priority": 1,
        "rank": 1,
        "crypto_topics": [],
        "source": {
            "question": "Will this pass?",
            "description": "Resolution details.",
            "acceptingOrders": True,
            "active": True,
            "closed": False,
            "startDate": "2026-01-01T00:00:00Z",
            "endDate": "2026-12-31T00:00:00Z",
            "resolutionSource": "https://example.test/rules",
            "outcomes": ["Yes", "No"],
            "outcomePrices": ["0.4", "0.6"],
            "events": [{
                "id": "event-1",
                "title": "Event title",
                "slug": "event-slug",
                "active": True,
                "closed": False,
            }],
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
        "probability", "rank", "ranking_metric", "ranking_priority", "record_type",
        "snapshot_date", "title", "volume24hr", "window_end", "window_start",
    )
    assert CRYPTO_CONTENT_FIELDS == (
        "category", "crypto_topics", "dominant_outcome", "dominant_probability",
        "event_id", "fetched_at", "liquidity", "market_id", "market_question",
        "outcome", "probability", "rank", "ranking_metric", "ranking_priority",
        "record_type", "snapshot_date", "title", "volume24hr", "window_end",
        "window_start",
    )
    assert EXTRA_DATA_FIELDS == (
        "acceptingOrders", "active", "category", "closed", "description",
        "dominant_outcome", "dominant_probability", "end_date", "endpoint",
        "event_active", "event_closed", "event_id", "fetched_at", "liquidity",
        "market_id", "outcome_prices", "outcomes", "rank", "ranking_metric",
        "ranking_priority", "resolution_source", "snapshot_date", "source_tag",
        "start_date", "title", "volume24hr", "window_end", "window_start",
    )


def test_builds_one_complete_current_db_aligned_record():
    record = build([selected()])["records"][0]

    assert tuple(record) == OUTER_FIELDS
    assert tuple(record["content"]) == CONTENT_FIELDS
    assert tuple(record["extra_data"]) == EXTRA_DATA_FIELDS
    assert set(record) == set(OUTER_FIELDS)
    assert set(record["content"]) == set(CONTENT_FIELDS)
    assert set(record["extra_data"]) == set(EXTRA_DATA_FIELDS)
    assert record["id"] is None
    assert record["data_type"] == "PREDICTION_MARKET_SELECTION"
    assert record["title"] == "Event title"
    assert record["summary"] == "Will this pass?"
    assert record["from_source"] == "polymarket"
    assert record["source_url"] == "https://polymarket.com/event/event-slug"
    assert record["content_hash"] is None
    assert record["published_at"] is None
    assert record["created_at"] == "2026-07-28T08:00:00+08:00"
    assert record["updated_at"] == "2026-07-28T08:00:00+08:00"
    assert record["tags"] == [
        "active", "category:politics", "prediction", "selected-market",
    ]
    assert record["source_updated_at"] is None
    assert record["content"] == {
        "category": "politics",
        "dominant_outcome": "No",
        "dominant_probability": 0.6,
        "event_id": "event-1",
        "fetched_at": "2026-07-28T00:00:00Z",
        "liquidity": 1200.5,
        "market_id": "market-1",
        "market_question": "Will this pass?",
        "outcome": "No",
        "probability": 0.6,
        "rank": 1,
        "ranking_metric": "liquidity",
        "ranking_priority": 1,
        "record_type": "prediction_market_selection",
        "snapshot_date": "2026-07-28",
        "title": "Event title",
        "volume24hr": 42.0,
        "window_end": None,
        "window_start": None,
    }
    assert record["extra_data"] == {
        "acceptingOrders": True,
        "active": True,
        "category": "politics",
        "closed": False,
        "description": "Resolution details.",
        "dominant_outcome": "No",
        "dominant_probability": 0.6,
        "end_date": "2026-12-31T00:00:00Z",
        "endpoint": "/markets/keyset",
        "event_active": True,
        "event_closed": False,
        "event_id": "event-1",
        "fetched_at": "2026-07-28T00:00:00+00:00",
        "liquidity": 1200.5,
        "market_id": "market-1",
        "outcome_prices": ["0.4", "0.6"],
        "outcomes": ["Yes", "No"],
        "rank": 1,
        "ranking_metric": "liquidity",
        "ranking_priority": 1,
        "resolution_source": "https://example.test/rules",
        "snapshot_date": "2026-07-28",
        "source_tag": 2,
        "start_date": "2026-01-01T00:00:00Z",
        "title": "Event title",
        "volume24hr": 42.0,
        "window_end": None,
        "window_start": None,
    }


def test_crypto_content_adds_only_canonical_topics_to_the_non_crypto_shape():
    row = selected(
        selected_category="crypto",
        crypto_topics=["regulation", "protocol_security"],
        ranking_metric="dominant_probability",
        ranking_priority=2,
        rank=3,
    )

    record = build([row])["records"][0]

    assert tuple(record["content"]) == CRYPTO_CONTENT_FIELDS
    assert set(record["content"]) - set(CONTENT_FIELDS) == {"crypto_topics"}
    assert record["content"]["crypto_topics"] == [
        "regulation", "protocol_security",
    ]
    assert record["extra_data"]["source_tag"] == 21
    assert (
        record["content"]["ranking_metric"],
        record["content"]["ranking_priority"],
        record["content"]["rank"],
    ) == ("dominant_probability", 2, 3)
    assert (
        record["extra_data"]["ranking_metric"],
        record["extra_data"]["ranking_priority"],
        record["extra_data"]["rank"],
    ) == ("dominant_probability", 2, 3)


def test_non_crypto_content_never_contains_crypto_topics():
    content = build([selected(crypto_topics=["regulation"])])["records"][0][
        "content"
    ]

    assert tuple(content) == CONTENT_FIELDS
    assert "crypto_topics" not in content


def test_normalizes_non_utc_capture_time_for_each_timestamp_contract():
    captured_at = datetime(2026, 7, 27, 20, tzinfo=timezone(-timedelta(hours=4)))

    record = build_db_aligned_final(
        [selected()], business_date=BUSINESS_DATE, captured_at=captured_at
    )["records"][0]

    assert record["content"]["fetched_at"] == "2026-07-28T00:00:00Z"
    assert record["extra_data"]["fetched_at"] == "2026-07-28T00:00:00+00:00"
    assert record["created_at"] == "2026-07-28T08:00:00+08:00"
    assert record["updated_at"] == "2026-07-28T08:00:00+08:00"


def test_does_not_mutate_selected_items_and_copies_source_arrays():
    rows = [selected()]
    before = deepcopy(rows)

    output = build(rows)

    assert rows == before
    extra_data = output["records"][0]["extra_data"]
    assert extra_data["outcomes"] is not rows[0]["source"]["outcomes"]
    assert extra_data["outcome_prices"] is not rows[0]["source"]["outcomePrices"]
    extra_data["outcomes"].append("Maybe")
    assert rows == before
    rows[0]["source"]["outcomePrices"].append("0")
    assert extra_data["outcome_prices"] == ["0.4", "0.6"]


def test_accepts_mapping_selected_source_metrics_and_first_event():
    row = selected()
    source = deepcopy(row["source"])
    source["events"] = [MappingProxyType(source["events"][0])]
    row["source"] = MappingProxyType(source)
    row["normalized_metrics"] = MappingProxyType(row["normalized_metrics"])

    record = build([MappingProxyType(row)])["records"][0]

    assert record["content"]["event_id"] == "event-1"
    assert record["content"]["liquidity"] == 1200.5


def test_selects_dominant_outcome_from_lists():
    output = build([selected()])

    assert output["records"][0]["content"]["dominant_outcome"] == "No"
    assert output["records"][0]["content"]["outcome"] == "No"


def test_selects_first_dominant_outcome_for_tied_json_arrays():
    row = selected(source={
        "question": "Tie?",
        "outcomes": '["First", "Second"]',
        "outcomePrices": "[0.5, 0.5]",
    })

    record = build([row])["records"][0]

    assert record["content"]["dominant_outcome"] == "First"
    assert record["content"]["outcome"] == "First"
    assert record["extra_data"]["outcomes"] == ["First", "Second"]
    assert record["extra_data"]["outcome_prices"] == [0.5, 0.5]


def test_selects_an_empty_string_outcome_label_when_it_has_the_highest_price():
    row = selected(source={
        "question": "Empty label?",
        "outcomes": ["", "No"],
        "outcomePrices": ["0.9", "0.1"],
    })

    content = build([row])["records"][0]["content"]

    assert content["dominant_outcome"] == ""
    assert content["outcome"] == ""


def test_rejects_stringifiable_objects_as_metrics_and_outcome_prices():
    row = selected(
        source={
            "question": "Path values?",
            "outcomes": ["Yes", "No"],
            "outcomePrices": [Path("0.9"), "0.1"],
        },
        normalized_metrics={
            "liquidity": Path("12.5"),
            "volume24hr": "42",
            "dominant_probability": "0.9",
        },
    )

    record = build([row])["records"][0]

    assert record["content"]["liquidity"] is None
    assert record["content"]["dominant_outcome"] is None
    assert record["content"]["outcome"] is None
    assert record["extra_data"]["outcome_prices"] is None


@pytest.mark.parametrize("unsafe", [float("nan"), float("inf"), -float("inf")])
def test_non_json_safe_source_arrays_become_null(unsafe):
    row = selected(source={
        "question": "Unsafe array?",
        "outcomes": ["Yes", "No"],
        "outcomePrices": [unsafe, "0"],
    })

    record = build([row])["records"][0]

    assert record["content"]["dominant_outcome"] is None
    assert record["content"]["outcome"] is None
    assert record["extra_data"]["outcome_prices"] is None


def test_invalid_optional_metrics_outcomes_and_source_values_become_none():
    row = selected(
        source={
            "question": None,
            "description": None,
            "acceptingOrders": None,
            "active": None,
            "closed": None,
            "startDate": None,
            "endDate": None,
            "resolutionSource": None,
            "events": [],
        },
        normalized_metrics={
            "liquidity": float("nan"),
            "volume24hr": "not-a-number",
            "dominant_probability": "1.1",
        },
    )

    record = build([row])["records"][0]

    assert record["title"] is None
    assert record["summary"] is None
    assert record["source_url"] is None
    assert record["content"]["market_question"] is None
    assert record["content"]["event_id"] is None
    assert record["content"]["title"] is None
    assert record["content"]["liquidity"] is None
    assert record["content"]["volume24hr"] is None
    assert record["content"]["dominant_probability"] is None
    assert record["content"]["probability"] is None
    assert record["content"]["dominant_outcome"] is None
    assert record["content"]["outcome"] is None
    assert record["extra_data"]["acceptingOrders"] is None
    assert record["extra_data"]["active"] is None
    assert record["extra_data"]["closed"] is None
    assert record["extra_data"]["description"] is None
    assert record["extra_data"]["start_date"] is None
    assert record["extra_data"]["end_date"] is None
    assert record["extra_data"]["resolution_source"] is None
    assert record["extra_data"]["event_id"] is None
    assert record["extra_data"]["event_active"] is None
    assert record["extra_data"]["event_closed"] is None
    assert record["extra_data"]["title"] is None
    assert record["extra_data"]["outcomes"] is None
    assert record["extra_data"]["outcome_prices"] is None


def test_no_event_falls_back_to_question_for_titles():
    row = selected(source={
        "question": "Question only",
        "outcomes": ["Yes", "No"],
        "outcomePrices": ["0.4", "0.6"],
    })

    record = build([row])["records"][0]

    assert record["title"] == "Question only"
    assert record["summary"] == "Question only"
    assert record["source_url"] is None
    assert record["content"]["event_id"] is None
    assert record["content"]["title"] == "Question only"
    assert record["extra_data"]["event_id"] is None
    assert record["extra_data"]["event_active"] is None
    assert record["extra_data"]["event_closed"] is None
    assert record["extra_data"]["title"] == "Question only"


@pytest.mark.parametrize("events", [None, (), {}, [], [None]])
def test_only_a_nonempty_exact_event_list_with_a_mapping_first_item_is_used(events):
    source = deepcopy(selected()["source"])
    source["events"] = events

    record = build([selected(source=source)])["records"][0]

    assert record["title"] == "Will this pass?"
    assert record["source_url"] is None
    assert record["content"]["event_id"] is None
    assert record["extra_data"]["event_active"] is None
    assert record["extra_data"]["event_closed"] is None


def test_event_title_falls_back_and_nonblank_slug_is_required_for_url():
    source = deepcopy(selected()["source"])
    source["events"][0].update({"title": None, "slug": "  "})

    record = build([selected(source=source)])["records"][0]

    assert record["title"] == "Will this pass?"
    assert record["content"]["title"] == "Will this pass?"
    assert record["extra_data"]["title"] == "Will this pass?"
    assert record["source_url"] is None


def test_optional_strings_and_booleans_require_exact_source_types():
    source = deepcopy(selected()["source"])
    source.update({
        "description": Path("description"),
        "acceptingOrders": 1,
        "active": 1,
        "closed": 0,
        "startDate": Path("start"),
        "endDate": 2026,
        "resolutionSource": Path("rules"),
    })
    source["events"][0].update({"active": 1, "closed": 0})

    extra_data = build([selected(source=source)])["records"][0]["extra_data"]

    assert extra_data["description"] is None
    assert extra_data["acceptingOrders"] is None
    assert extra_data["active"] is None
    assert extra_data["closed"] is None
    assert extra_data["start_date"] is None
    assert extra_data["end_date"] is None
    assert extra_data["resolution_source"] is None
    assert extra_data["event_active"] is None
    assert extra_data["event_closed"] is None


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
    ({"ranking_metric": "volume"}, "ranking metadata is invalid"),
    ({"ranking_priority": 3}, "ranking metadata is invalid"),
    (
        {"selected_category": "crypto", "crypto_topics": []},
        "crypto selection must contain at least one topic",
    ),
    ({"rank": 0}, "rank must be between 1 and 10"),
    ({"rank": 11}, "rank must be between 1 and 10"),
    ({"rank": True}, "rank must be between 1 and 10"),
    ({"source": None}, "source must be a mapping"),
])
def test_rejects_invalid_required_selected_fields(override, message):
    with pytest.raises(ValueError, match=f"^{message}$"):
        build([selected(**override)])


@pytest.mark.parametrize("topics", [None, (), "regulation", ["unknown"], [1]])
def test_rejects_malformed_or_unconfigured_crypto_topics(topics):
    with pytest.raises(
        ValueError, match="^crypto_topics must be a configured topic list$"
    ):
        build([selected(selected_category="crypto", crypto_topics=topics)])


@pytest.mark.parametrize("topics", [
    ["etf", "regulation"],
    ["regulation", "regulation"],
])
def test_rejects_noncanonical_or_duplicate_crypto_topics(topics):
    with pytest.raises(
        ValueError,
        match="^crypto_topics must use canonical order without duplicates$",
    ):
        build([selected(selected_category="crypto", crypto_topics=topics)])


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


def test_sorts_duplicate_market_ids_by_category_priority_and_rank():
    rows = [
        selected(selected_category="finance", rank=2),
        selected(selected_category="politics", rank=1),
    ]

    output = build(rows)["records"]

    assert [
        (
            row["content"]["market_id"],
            row["content"]["category"],
            row["content"]["ranking_priority"],
            row["content"]["rank"],
        )
        for row in output
    ] == [
        ("market-1", "politics", 1, 1),
        ("market-1", "finance", 1, 2),
    ]


def test_sorts_each_category_by_ranking_priority_then_rank():
    rows = [
        selected(
            market_id="volume-rank-2",
            ranking_metric="volume24hr",
            ranking_priority=3,
            rank=2,
        ),
        selected(market_id="liquidity-rank-2", rank=2),
        selected(
            market_id="probability-rank-1",
            ranking_metric="dominant_probability",
            ranking_priority=2,
            rank=1,
        ),
        selected(market_id="liquidity-rank-1", rank=1),
    ]

    records = build(rows)["records"]

    assert [record["content"]["market_id"] for record in records] == [
        "liquidity-rank-1",
        "liquidity-rank-2",
        "probability-rank-1",
        "volume-rank-2",
    ]


def test_result_is_json_safe_and_round_trips():
    output = build([selected()])

    assert set(output) == {"records"}
    assert json.loads(json.dumps(output, allow_nan=False)) == output
