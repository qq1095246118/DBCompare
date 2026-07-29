"""Validation and deterministic transformation for Polymarket database rows."""

import json
from collections import Counter
from collections.abc import Iterable, Mapping
from copy import deepcopy
from datetime import datetime
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
_CATEGORY_INDEX = {
    category: index for index, category in enumerate(CATEGORY_ORDER)
}


def _reject_json_constant(_value: str) -> None:
    raise ValueError("content must contain standards-compliant JSON")


def _normalize_content(value: object) -> dict:
    if type(value) is str:
        try:
            content = json.loads(value, parse_constant=_reject_json_constant)
        except json.JSONDecodeError as exc:
            raise ValueError(
                "content must contain standards-compliant JSON"
            ) from exc
    elif type(value) is dict:
        content = deepcopy(value)
    else:
        raise ValueError("content must be a JSON object")

    if type(content) is not dict:
        raise ValueError("content must be a JSON object")

    try:
        json.dumps(content, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            "content must contain standards-compliant JSON"
        ) from exc

    category = content.get("category")
    if not isinstance(category, str) or not category.strip():
        raise ValueError("content.category must be a non-empty string")

    market_id = content.get("market_id")
    if not isinstance(market_id, str) or not market_id.strip():
        raise ValueError("content.market_id must be a non-empty string")

    rank = content.get("rank")
    if type(rank) is not int or rank <= 0:
        raise ValueError("content.rank must be a positive integer")

    return content


def _normalize_timestamp(field: str, value: object) -> str | None:
    if value is None:
        if field == "created_at":
            raise ValueError("created_at must be a timezone-aware datetime")
        return None
    if not isinstance(value, datetime) or value.utcoffset() is None:
        raise ValueError(f"{field} must be a timezone-aware datetime")
    return value.astimezone(_SHANGHAI).isoformat()


def normalize_row(row: Mapping) -> dict:
    if not isinstance(row, Mapping):
        raise ValueError("database row must be a mapping")

    for field in ROW_FIELDS:
        if field not in row:
            raise ValueError(f"database row is missing field {field}")

    if type(row["id"]) is not int:
        raise ValueError("id must be an integer")

    result = {field: deepcopy(row[field]) for field in ROW_FIELDS}
    result["content"] = _normalize_content(row["content"])
    for field in TIMESTAMP_FIELDS:
        result[field] = _normalize_timestamp(field, row[field])

    try:
        json.dumps(result, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            "database row must contain JSON-compatible values"
        ) from exc
    return result


def _category_sort_key(category: str) -> tuple:
    if category in _CATEGORY_INDEX:
        return (0, _CATEGORY_INDEX[category], "", "")
    return (1, len(CATEGORY_ORDER), category.casefold(), category)


def _record_sort_key(record: Mapping[str, object]) -> tuple:
    content = record["content"]
    assert isinstance(content, Mapping)
    category = content["category"]
    rank = content["rank"]
    created_at = record["created_at"]
    record_id = record["id"]
    assert isinstance(category, str)
    assert isinstance(rank, int)
    assert isinstance(created_at, str)
    assert isinstance(record_id, int)
    return (
        _category_sort_key(category),
        rank,
        datetime.fromisoformat(created_at),
        record_id,
    )


def _row_error(row: object, error: Exception) -> dict:
    row_id = None
    content_hash = None
    if isinstance(row, Mapping):
        if type(row.get("id")) is int:
            row_id = row["id"]
        if isinstance(row.get("content_hash"), str):
            content_hash = row["content_hash"]
    return {
        "id": row_id,
        "content_hash": content_hash,
        "stage": "row_validation",
        "type": type(error).__name__,
        "message": str(error),
    }


def build_db_output(
    rows: Iterable[Mapping[str, object]],
) -> tuple[dict, list[dict], dict[str, int]]:
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
