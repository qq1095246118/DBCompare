import json
from copy import deepcopy
from datetime import datetime, timezone

import pytest

from getDB.Polymarket.tool.contract import ROW_FIELDS, normalize_row


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
