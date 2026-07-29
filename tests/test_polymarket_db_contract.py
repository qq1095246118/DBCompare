import json
from copy import deepcopy
from datetime import datetime, timezone

import pytest

from getDB.Polymarket.tool.contract import (
    CATEGORY_ORDER,
    ROW_FIELDS,
    build_db_output,
    normalize_row,
)


def source_row() -> dict:
    content = {
        "category": "politics",
        "market_id": "market-1",
        "rank": 1,
        "market_question": "Question?",
    }
    timestamp = datetime(2026, 7, 29, 1, 2, 3, tzinfo=timezone.utc)
    return {
        "id": 101,
        "data_type": "PREDICTION_MARKET_SELECTION",
        "title": "Title",
        "summary": "Summary",
        "content": json.dumps(content),
        "from_source": "polymarket",
        "source_url": "https://example.com/market-1",
        "content_hash": "hash-1",
        "extra_data": {"selection": "metadata"},
        "published_at": timestamp,
        "created_at": timestamp,
        "updated_at": timestamp,
        "tags": ["politics"],
        "source_updated_at": None,
    }


def test_normalize_row_preserves_contract_and_converts_timestamps() -> None:
    row = source_row()

    result = normalize_row(row)

    assert tuple(result) == ROW_FIELDS
    assert result["id"] == 101
    assert result["content"] == {
        "category": "politics",
        "market_id": "market-1",
        "rank": 1,
        "market_question": "Question?",
    }
    assert result["extra_data"] == {"selection": "metadata"}
    assert result["published_at"] == "2026-07-29T09:02:03+08:00"
    assert result["created_at"] == "2026-07-29T09:02:03+08:00"
    assert result["source_updated_at"] is None


def test_normalize_row_accepts_and_deep_copies_dict_content() -> None:
    row = source_row()
    row["content"] = {
        "category": "politics",
        "market_id": "market-1",
        "rank": 1,
        "nested": {"items": [1, 2]},
    }
    original = deepcopy(row)

    result = normalize_row(row)
    result["content"]["nested"]["items"].append(3)

    assert row == original
    assert row["content"]["nested"]["items"] == [1, 2]


@pytest.mark.parametrize(
    ("content", "message"),
    [
        ([], "content must be a JSON object"),
        ('{"category":"politics","market_id":"market-1","rank":1,"value":NaN}', "standards-compliant JSON"),
        ('{"category":"politics","market_id":"market-1","rank":1,"value":Infinity}', "standards-compliant JSON"),
        ('{"category":"politics","market_id":"market-1","rank":1,"value":-Infinity}', "standards-compliant JSON"),
        (
            {
                "category": "politics",
                "market_id": "market-1",
                "rank": 1,
                "value": float("nan"),
            },
            "standards-compliant JSON",
        ),
    ],
)
def test_normalize_row_rejects_nonstandard_or_nonobject_content(
    content: object,
    message: str,
) -> None:
    row = source_row()
    row["content"] = content

    with pytest.raises(ValueError, match=message):
        normalize_row(row)


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("category", "   ", "content.category"),
        ("category", None, "content.category"),
        ("market_id", "   ", "content.market_id"),
        ("market_id", None, "content.market_id"),
        ("rank", True, "content.rank"),
        ("rank", 0, "content.rank"),
        ("rank", None, "content.rank"),
    ],
)
def test_normalize_row_rejects_invalid_required_content_fields(
    field: str,
    value: object,
    message: str,
) -> None:
    row = source_row()
    content = json.loads(row["content"])
    if value is None:
        content.pop(field)
    else:
        content[field] = value
    row["content"] = content

    with pytest.raises(ValueError, match=message):
        normalize_row(row)


@pytest.mark.parametrize(
    "created_at",
    [None, "2026-07-29T01:02:03+00:00", datetime(2026, 7, 29, 1, 2, 3)],
)
def test_normalize_row_rejects_invalid_created_at(created_at: object) -> None:
    row = source_row()
    row["created_at"] = created_at

    with pytest.raises(ValueError, match="created_at"):
        normalize_row(row)


def test_category_order_matches_database_export_contract() -> None:
    assert CATEGORY_ORDER == (
        "politics",
        "geopolitics",
        "economy",
        "finance",
        "technology",
        "crypto",
    )


def test_build_db_output_preserves_and_sorts_all_valid_rows() -> None:
    early = datetime(2026, 7, 29, 1, 0, tzinfo=timezone.utc)
    late = datetime(2026, 7, 29, 2, 0, tzinfo=timezone.utc)

    def row(record_id: int, category: str, rank: int, created_at: datetime) -> dict:
        result = source_row()
        result["id"] = record_id
        result["created_at"] = created_at
        result["content_hash"] = f"hash-{record_id}"
        result["content"] = {
            "category": category,
            "market_id": "repeated-market",
            "rank": rank,
        }
        return result

    rows = [
        row(9, "sports", 1, early),
        row(7, "crypto", 1, early),
        row(6, "politics", 2, early),
        row(5, "politics", 1, late),
        row(4, "politics", 1, early),
        row(3, "politics", 1, early),
    ]

    payload, errors, category_counts = build_db_output(rows)

    assert [record["id"] for record in payload["records"]] == [3, 4, 5, 6, 7, 9]
    assert errors == []
    assert category_counts == {"politics": 4, "crypto": 1, "sports": 1}


def test_build_db_output_isolates_malformed_rows_without_leaking_content() -> None:
    valid = source_row()
    invalid_mapping = source_row()
    invalid_mapping["id"] = 202
    invalid_mapping["content_hash"] = "bad-hash"
    invalid_mapping["content"] = "secret text that must not leak"
    invalid_non_mapping = ["another secret"]

    payload, errors, category_counts = build_db_output(
        [invalid_mapping, valid, invalid_non_mapping]
    )

    assert [record["id"] for record in payload["records"]] == [101]
    assert category_counts == {"politics": 1}
    assert len(errors) == 2
    assert errors[0].keys() == {"id", "content_hash", "stage", "type", "message"}
    assert errors[0]["id"] == 202
    assert errors[0]["content_hash"] == "bad-hash"
    assert errors[1]["id"] is None
    assert errors[1]["content_hash"] is None
    assert [error["stage"] for error in errors] == [
        "row_validation",
        "row_validation",
    ]
    serialized_errors = json.dumps(errors)
    assert "secret text that must not leak" not in serialized_errors
    assert "another secret" not in serialized_errors


def test_build_db_output_keeps_duplicate_market_rows_across_categories() -> None:
    rows = []
    for record_id, category in (
        (301, "technology"),
        (302, "technology"),
        (303, "finance"),
    ):
        row = source_row()
        row["id"] = record_id
        row["content"] = {
            "category": category,
            "market_id": "same-market",
            "rank": 1,
        }
        rows.append(row)

    payload, errors, category_counts = build_db_output(rows)

    assert [record["id"] for record in payload["records"]] == [303, 301, 302]
    assert errors == []
    assert category_counts == {"finance": 1, "technology": 2}
