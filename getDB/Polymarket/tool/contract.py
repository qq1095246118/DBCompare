"""Validation and deterministic transformation for Polymarket database rows."""

import json
from collections.abc import Mapping
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
