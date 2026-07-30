"""Convert ranked Polymarket selections into the API final-record contract."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
import json
from zoneinfo import ZoneInfo

from getMarket.Polymarket.tool.market_filter import CATEGORY_ORDER


OUTER_FIELDS = (
    "id", "data_type", "title", "summary", "content", "from_source",
    "source_url", "content_hash", "extra_data", "published_at", "created_at",
    "updated_at", "tags", "source_updated_at",
)

CONTENT_FIELDS = (
    "category", "dominant_outcome", "dominant_probability", "event_id",
    "fetched_at", "liquidity", "market_id", "market_question", "outcome",
    "probability", "rank", "record_type", "snapshot_date", "title",
    "volume24hr", "window_end", "window_start",
)

EXTRA_DATA_FIELDS = (
    "endpoint", "event_id", "fetched_at", "market_id", "rank",
    "snapshot_date", "window_end", "window_start",
)


def _json_number(value: object, *, maximum: Decimal | None = None) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    if not number.is_finite() or number < 0 or (maximum is not None and number > maximum):
        return None
    result = float(number)
    return result if result != float("inf") else None


def _array(value: object) -> list[object] | None:
    if isinstance(value, list):
        return value
    if not isinstance(value, str):
        return None
    try:
        decoded = json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return None
    return decoded if isinstance(decoded, list) else None


def _dominant_outcome(source: Mapping[object, object]) -> str | None:
    outcomes = _array(source.get("outcomes"))
    prices = _array(source.get("outcomePrices"))
    if not outcomes or not prices or len(outcomes) != len(prices):
        return None

    selected_outcome: str | None = None
    selected_price: float | None = None
    for outcome, price in zip(outcomes, prices):
        if not isinstance(outcome, str) or not outcome.strip():
            return None
        probability = _json_number(price, maximum=Decimal("1"))
        if probability is None:
            return None
        if selected_price is None or probability > selected_price:
            selected_outcome = outcome
            selected_price = probability
    return selected_outcome


def _validate_inputs(business_date: date, captured_at: datetime) -> None:
    if type(business_date) is not date:
        raise TypeError("business_date must be a date")
    if not isinstance(captured_at, datetime) or captured_at.tzinfo is None or captured_at.utcoffset() is None:
        raise TypeError("captured_at must be timezone-aware")


def _validate_item(item: object) -> tuple[str, str, int, Mapping[object, object], Mapping[object, object] | None]:
    if not isinstance(item, Mapping):
        raise ValueError("selected item must be a mapping")
    market_id = item.get("market_id")
    if not isinstance(market_id, str) or not market_id.strip():
        raise ValueError("market_id must be a non-whitespace string")
    category = item.get("selected_category")
    if not isinstance(category, str) or not category.strip() or category not in CATEGORY_ORDER:
        raise ValueError("selected_category must be configured")
    rank = item.get("rank_in_category")
    if isinstance(rank, bool) or not isinstance(rank, int) or rank <= 0:
        raise ValueError("rank_in_category must be a positive integer")
    source = item.get("source")
    if not isinstance(source, Mapping):
        raise ValueError("source must be a mapping")
    metrics = item.get("normalized_metrics")
    return market_id, category, rank, source, metrics if isinstance(metrics, Mapping) else None


def _validate_shape(record: dict[str, object]) -> None:
    if set(record) != set(OUTER_FIELDS):
        raise ValueError("outer record fields do not match final contract")
    content = record.get("content")
    extra_data = record.get("extra_data")
    if not isinstance(content, dict) or set(content) != set(CONTENT_FIELDS):
        raise ValueError("content fields do not match final contract")
    if not isinstance(extra_data, dict) or set(extra_data) != set(EXTRA_DATA_FIELDS):
        raise ValueError("extra_data fields do not match final contract")


def build_db_aligned_final(
    selected: Iterable[object], *, business_date: date, captured_at: datetime,
) -> dict[str, list[dict[str, object]]]:
    """Build API records without changing the ranked selection input."""
    _validate_inputs(business_date, captured_at)
    captured_utc = captured_at.astimezone(timezone.utc)
    snapshot_date = business_date.isoformat()
    content_fetched_at = captured_utc.isoformat().replace("+00:00", "Z")
    extra_fetched_at = captured_utc.isoformat()
    local_captured_at = captured_at.astimezone(ZoneInfo("Asia/Shanghai")).isoformat()
    records: list[dict[str, object]] = []

    for item in selected:
        market_id, category, rank, source, metrics = _validate_item(item)
        question = source.get("question")
        question = question if isinstance(question, str) else None
        liquidity = _json_number(metrics.get("liquidity")) if metrics is not None else None
        volume = _json_number(metrics.get("volume24hr")) if metrics is not None else None
        probability = (
            _json_number(metrics.get("dominant_probability"), maximum=Decimal("1"))
            if metrics is not None else None
        )
        outcome = _dominant_outcome(source)
        content = {
            "category": category, "dominant_outcome": outcome,
            "dominant_probability": probability, "event_id": None,
            "fetched_at": content_fetched_at, "liquidity": liquidity,
            "market_id": market_id, "market_question": question, "outcome": outcome,
            "probability": probability, "rank": rank,
            "record_type": "prediction_market_selection", "snapshot_date": snapshot_date,
            "title": question, "volume24hr": volume, "window_end": None,
            "window_start": None,
        }
        extra_data = {
            "endpoint": None, "event_id": None, "fetched_at": extra_fetched_at,
            "market_id": market_id, "rank": rank, "snapshot_date": snapshot_date,
            "window_end": None, "window_start": None,
        }
        record = {
            "id": None, "data_type": "PREDICTION_MARKET_SELECTION", "title": question,
            "summary": question, "content": content, "from_source": "polymarket",
            "source_url": None, "content_hash": None, "extra_data": extra_data,
            "published_at": None, "created_at": local_captured_at,
            "updated_at": local_captured_at,
            "tags": ["active", f"category:{category}", "prediction", "selected-market"],
            "source_updated_at": None,
        }
        _validate_shape(record)
        records.append(record)

    result = {"records": records}
    if set(result) != {"records"}:
        raise ValueError("final contract must contain records only")
    json.dumps(result, allow_nan=False)
    return result
