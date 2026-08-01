"""Convert ranked Polymarket selections into the API final-record contract."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from copy import deepcopy
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
import json
from zoneinfo import ZoneInfo

from getMarket.Polymarket.tool.market_filter import (
    CATEGORY_ORDER,
    CATEGORY_TAG_IDS,
    CRYPTO_TOPIC_ORDER,
)
from getMarket.Polymarket.tool.market_ranking import METRIC_PRIORITIES, RANKING_LIMIT


OUTER_FIELDS = (
    "id", "data_type", "title", "summary", "content", "from_source",
    "source_url", "content_hash", "extra_data", "published_at", "created_at",
    "updated_at", "tags", "source_updated_at",
)

CONTENT_FIELDS = (
    "category", "dominant_outcome", "dominant_probability", "event_id",
    "fetched_at", "liquidity", "market_id", "market_question", "outcome",
    "probability", "rank", "ranking_metric", "ranking_priority", "record_type",
    "snapshot_date", "title", "volume24hr", "window_end", "window_start",
)

CRYPTO_CONTENT_FIELDS = (
    "category", "crypto_topics", "dominant_outcome", "dominant_probability",
    "event_id", "fetched_at", "liquidity", "market_id", "market_question",
    "outcome", "probability", "rank", "ranking_metric", "ranking_priority",
    "record_type", "snapshot_date", "title", "volume24hr", "window_end",
    "window_start",
)

EXTRA_DATA_FIELDS = (
    "acceptingOrders", "active", "category", "closed", "description",
    "dominant_outcome", "dominant_probability", "end_date", "endpoint",
    "event_active", "event_closed", "event_id", "fetched_at", "liquidity",
    "market_id", "outcome_prices", "outcomes", "rank", "ranking_metric",
    "ranking_priority", "resolution_source", "snapshot_date", "source_tag",
    "start_date", "title", "volume24hr", "window_end", "window_start",
)


def _json_number(value: object, *, maximum: Decimal | None = None) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
        return None
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    if not number.is_finite() or number < 0 or (maximum is not None and number > maximum):
        return None
    result = float(number)
    return result if result != float("inf") else None


def _string(value: object) -> str | None:
    return value if isinstance(value, str) else None


def _boolean(value: object) -> bool | None:
    return value if type(value) is bool else None


def _array(value: object) -> list[object] | None:
    if isinstance(value, list):
        decoded = value
    elif isinstance(value, str):
        try:
            decoded = json.loads(value)
        except (TypeError, json.JSONDecodeError):
            return None
    else:
        return None
    if not isinstance(decoded, list):
        return None
    try:
        json.dumps(decoded, allow_nan=False)
    except (TypeError, ValueError):
        return None
    return deepcopy(decoded)


def _first_event(
    source: Mapping[object, object],
) -> Mapping[object, object] | None:
    events = source.get("events")
    if type(events) is list and events and isinstance(events[0], Mapping):
        return events[0]
    return None


def _event_url(event: Mapping[object, object] | None) -> str | None:
    slug = _string(event.get("slug")) if event is not None else None
    return f"https://polymarket.com/event/{slug}" if slug and slug.strip() else None


def _dominant_outcome(source: Mapping[object, object]) -> str | None:
    outcomes = _array(source.get("outcomes"))
    prices = _array(source.get("outcomePrices"))
    if not outcomes or not prices or len(outcomes) != len(prices):
        return None

    selected_outcome: str | None = None
    selected_price: float | None = None
    for outcome, price in zip(outcomes, prices):
        if not isinstance(outcome, str):
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
    if (
        not isinstance(captured_at, datetime)
        or captured_at.tzinfo is None
        or captured_at.utcoffset() is None
    ):
        raise TypeError("captured_at must be timezone-aware")


def _validate_item(
    item: object,
) -> tuple[
    str,
    str,
    str,
    int,
    int,
    list[str],
    Mapping[object, object],
    Mapping[object, object] | None,
]:
    if not isinstance(item, Mapping):
        raise ValueError("selected item must be a mapping")
    market_id = item.get("market_id")
    if not isinstance(market_id, str) or not market_id.strip():
        raise ValueError("market_id must be a non-whitespace string")
    category = item.get("selected_category")
    if not isinstance(category, str) or category not in CATEGORY_ORDER:
        raise ValueError("selected_category must be configured")
    metric = item.get("ranking_metric")
    priority = item.get("ranking_priority")
    if (
        not isinstance(metric, str)
        or metric not in METRIC_PRIORITIES
        or type(priority) is not int
        or priority != METRIC_PRIORITIES.index(metric) + 1
    ):
        raise ValueError("ranking metadata is invalid")
    rank = item.get("rank")
    if type(rank) is not int or not 1 <= rank <= RANKING_LIMIT:
        raise ValueError("rank must be between 1 and 10")
    topics = item.get("crypto_topics")
    if type(topics) is not list or any(
        not isinstance(topic, str) or topic not in CRYPTO_TOPIC_ORDER
        for topic in topics
    ):
        raise ValueError("crypto_topics must be a configured topic list")
    canonical_topics = [topic for topic in CRYPTO_TOPIC_ORDER if topic in topics]
    if topics != canonical_topics:
        raise ValueError("crypto_topics must use canonical order without duplicates")
    if category == "crypto" and not topics:
        raise ValueError("crypto selection must contain at least one topic")
    source = item.get("source")
    if not isinstance(source, Mapping):
        raise ValueError("source must be a mapping")
    metrics = item.get("normalized_metrics")
    return (
        market_id,
        category,
        metric,
        priority,
        rank,
        canonical_topics,
        source,
        metrics if isinstance(metrics, Mapping) else None,
    )


def _validate_shape(record: dict[str, object]) -> None:
    if set(record) != set(OUTER_FIELDS):
        raise ValueError("outer record fields do not match final contract")
    content = record.get("content")
    extra_data = record.get("extra_data")
    if not isinstance(content, dict):
        raise ValueError("content fields do not match final contract")
    expected_content = (
        CRYPTO_CONTENT_FIELDS
        if content.get("category") == "crypto"
        else CONTENT_FIELDS
    )
    if set(content) != set(expected_content):
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
        (
            market_id,
            category,
            ranking_metric,
            ranking_priority,
            rank,
            crypto_topics,
            source,
            metrics,
        ) = _validate_item(item)
        question = _string(source.get("question"))
        event = _first_event(source)
        event_id = _string(event.get("id")) if event is not None else None
        event_title = _string(event.get("title")) if event is not None else None
        title = event_title if event_title is not None else question
        liquidity = (
            _json_number(metrics.get("liquidity")) if metrics is not None else None
        )
        volume = (
            _json_number(metrics.get("volume24hr")) if metrics is not None else None
        )
        probability = (
            _json_number(
                metrics.get("dominant_probability"), maximum=Decimal("1")
            )
            if metrics is not None
            else None
        )
        outcome = _dominant_outcome(source)
        content = {
            "category": category,
            "dominant_outcome": outcome,
            "dominant_probability": probability,
            "event_id": event_id,
            "fetched_at": content_fetched_at,
            "liquidity": liquidity,
            "market_id": market_id,
            "market_question": question,
            "outcome": outcome,
            "probability": probability,
            "rank": rank,
            "ranking_metric": ranking_metric,
            "ranking_priority": ranking_priority,
            "record_type": "prediction_market_selection",
            "snapshot_date": snapshot_date,
            "title": title,
            "volume24hr": volume,
            "window_end": None,
            "window_start": None,
        }
        if category == "crypto":
            content = {
                "category": category,
                "crypto_topics": crypto_topics,
                **{key: value for key, value in content.items() if key != "category"},
            }
        extra_data = {
            "acceptingOrders": _boolean(source.get("acceptingOrders")),
            "active": _boolean(source.get("active")),
            "category": category,
            "closed": _boolean(source.get("closed")),
            "description": _string(source.get("description")),
            "dominant_outcome": outcome,
            "dominant_probability": probability,
            "end_date": _string(source.get("endDate")),
            "endpoint": "/markets/keyset",
            "event_active": (
                _boolean(event.get("active")) if event is not None else None
            ),
            "event_closed": (
                _boolean(event.get("closed")) if event is not None else None
            ),
            "event_id": event_id,
            "fetched_at": extra_fetched_at,
            "liquidity": liquidity,
            "market_id": market_id,
            "outcome_prices": _array(source.get("outcomePrices")),
            "outcomes": _array(source.get("outcomes")),
            "rank": rank,
            "ranking_metric": ranking_metric,
            "ranking_priority": ranking_priority,
            "resolution_source": _string(source.get("resolutionSource")),
            "snapshot_date": snapshot_date,
            "source_tag": CATEGORY_TAG_IDS[category],
            "start_date": _string(source.get("startDate")),
            "title": title,
            "volume24hr": volume,
            "window_end": None,
            "window_start": None,
        }
        record = {
            "id": None,
            "data_type": "PREDICTION_MARKET_SELECTION",
            "title": title,
            "summary": question,
            "content": content,
            "from_source": "polymarket",
            "source_url": _event_url(event),
            "content_hash": None,
            "extra_data": extra_data,
            "published_at": None,
            "created_at": local_captured_at,
            "updated_at": local_captured_at,
            "tags": [
                "active", f"category:{category}", "prediction", "selected-market",
            ],
            "source_updated_at": None,
        }
        _validate_shape(record)
        records.append(record)

    records.sort(key=lambda record: (
        CATEGORY_ORDER.index(record["content"]["category"]),
        record["content"]["ranking_priority"],
        record["content"]["rank"],
    ))
    result = {"records": records}
    if set(result) != {"records"}:
        raise ValueError("final contract must contain records only")
    json.dumps(result, allow_nan=False)
    return result
