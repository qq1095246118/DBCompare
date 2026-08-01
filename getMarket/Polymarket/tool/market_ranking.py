"""Strict metric normalization and ordered per-category market ranking."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from copy import deepcopy
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
import json

from getMarket.Polymarket.tool.market_filter import CATEGORY_ORDER


METRIC_PRIORITIES = ("liquidity", "dominant_probability", "volume24hr")
RANKING_LIMIT = 10


@dataclass(frozen=True)
class RankingResult:
    candidates: list[dict[str, object]]
    selected: list[dict[str, object]]
    rankings: dict[str, object]


def _decimal(value: object, *, minimum: Decimal, maximum: Decimal | None = None) -> Decimal | None:
    if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
        return None
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    if not parsed.is_finite() or parsed < minimum:
        return None
    if maximum is not None and parsed > maximum:
        return None
    return parsed


def _canonical(value: Decimal) -> str:
    rendered = format(value, "f")
    if "." in rendered:
        rendered = rendered.rstrip("0").rstrip(".")
    return "0" if rendered in ("", "-0") else rendered


def _probability(source: Mapping[str, object]) -> Decimal | None:
    prices = source.get("outcomePrices")
    if isinstance(prices, str):
        try:
            prices = json.loads(prices)
        except json.JSONDecodeError:
            return None
    if type(prices) is not list or not prices:
        return None
    parsed = [
        _decimal(value, minimum=Decimal(0), maximum=Decimal(1))
        for value in prices
    ]
    if any(value is None for value in parsed):
        return None
    return max(parsed)


def normalize_metrics(source: Mapping[str, object]) -> dict[str, str | None]:
    if not isinstance(source, Mapping):
        raise TypeError("market source must be a mapping")
    liquidity = _decimal(source.get("liquidity"), minimum=Decimal(0))
    volume = _decimal(source.get("volume24hr"), minimum=Decimal(0))
    probability = _probability(source)
    return {
        "liquidity": None if liquidity is None else _canonical(liquidity),
        "dominant_probability": None if probability is None else _canonical(probability),
        "volume24hr": None if volume is None else _canonical(volume),
    }


def _rank(
    candidates: list[dict[str, object]], metric: str, excluded: set[str]
) -> tuple[list[dict[str, object]], list[str]]:
    eligible = [
        row for row in candidates
        if row["market_id"] not in excluded
        and row["normalized_metrics"][metric] is not None
    ]
    eligible.sort(key=lambda row: row["market_id"])
    eligible.sort(
        key=lambda row: Decimal(row["normalized_metrics"][metric]), reverse=True
    )
    excluded_by_priority = [
        row["market_id"] for row in candidates
        if row["market_id"] in excluded
        and row["normalized_metrics"][metric] is not None
    ]
    return eligible, sorted(excluded_by_priority)


def select_ranked_markets(
    markets: Iterable[Mapping[str, object]],
) -> RankingResult:
    candidates: list[dict[str, object]] = []
    seen: set[tuple[str, str]] = set()
    for market in markets:
        if not isinstance(market, Mapping):
            raise TypeError("ranked markets must be mappings")
        market_id = market.get("market_id")
        source = market.get("source")
        if not isinstance(market_id, str) or not market_id.strip():
            raise ValueError("ranked market ID must be a non-whitespace string")
        if not isinstance(source, Mapping):
            raise ValueError("ranked market source must be a mapping")
        categories = market.get("categories")
        if (
            type(categories) is not list
            or len(categories) != 1
            or type(categories[0]) is not str
            or categories[0] not in CATEGORY_ORDER
        ):
            raise ValueError("ranked market must have exactly one configured category")
        identity = (categories[0], market_id)
        if identity in seen:
            raise ValueError("category and market ID pairs must be unique")
        seen.add(identity)
        candidate = dict(market)
        candidate["source"] = dict(source)
        normalized = deepcopy(candidate)
        normalized["normalized_metrics"] = normalize_metrics(source)
        candidates.append(normalized)
    category_positions = {
        category: position for position, category in enumerate(CATEGORY_ORDER)
    }
    candidates.sort(key=lambda row: (
        category_positions[row["categories"][0]], row["market_id"]
    ))

    selected: list[dict[str, object]] = []
    rankings: dict[str, object] = {}
    for category in CATEGORY_ORDER:
        category_candidates = [
            row for row in candidates if row["categories"][0] == category
        ]
        selected_ids: set[str] = set()
        rankings[category] = {}
        for priority, metric in enumerate(METRIC_PRIORITIES, start=1):
            eligible, excluded = _rank(category_candidates, metric, selected_ids)
            winners = eligible[:RANKING_LIMIT]
            winner_ids = [row["market_id"] for row in winners]
            rankings[category][metric] = {
                "priority": priority,
                "selected_market_ids": winner_ids,
                "selected_metrics": [
                    row["normalized_metrics"][metric] for row in winners
                ],
                "excluded_by_priorities": excluded,
            }
            for rank, row in enumerate(winners, start=1):
                final = deepcopy(row)
                for legacy_key in (
                    "selected_by", "priority", "rank_in_category", "rank_in_priority",
                ):
                    final.pop(legacy_key, None)
                final.update({
                    "selected_category": category,
                    "ranking_metric": metric,
                    "ranking_priority": priority,
                    "rank": rank,
                })
                selected.append(final)
                selected_ids.add(row["market_id"])
    return RankingResult(candidates, selected, rankings)
