"""Strict metric normalization and ordered global market ranking."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from copy import deepcopy
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
import json


METRIC_PRIORITIES = ("liquidity", "dominant_probability", "volume24hr")


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
    markets: Iterable[Mapping[str, object]], *, per_priority: int = 10
) -> RankingResult:
    if type(per_priority) is not int or per_priority < 1:
        raise ValueError("per-priority limit must be positive")
    candidates: list[dict[str, object]] = []
    seen: set[str] = set()
    for market in markets:
        if not isinstance(market, Mapping):
            raise TypeError("ranked markets must be mappings")
        market_id = market.get("market_id")
        source = market.get("source")
        if not isinstance(market_id, str) or not market_id:
            raise ValueError("ranked market ID must be a non-empty string")
        if market_id in seen:
            raise ValueError("ranked market IDs must be unique")
        if not isinstance(source, Mapping):
            raise ValueError("ranked market source must be a mapping")
        seen.add(market_id)
        normalized = deepcopy(dict(market))
        normalized["normalized_metrics"] = normalize_metrics(source)
        candidates.append(normalized)
    candidates.sort(key=lambda row: row["market_id"])

    selected: list[dict[str, object]] = []
    selected_ids: set[str] = set()
    rankings: dict[str, object] = {}
    for priority, metric in enumerate(METRIC_PRIORITIES, start=1):
        eligible, excluded = _rank(candidates, metric, selected_ids)
        winners = eligible[:per_priority]
        winner_ids = [row["market_id"] for row in winners]
        rankings[metric] = {
            "priority": priority,
            "selected_market_ids": winner_ids,
            "selected_metrics": [row["normalized_metrics"][metric] for row in winners],
            "excluded_by_priorities": excluded,
        }
        for rank, row in enumerate(winners, start=1):
            final = deepcopy(row)
            final.update({
                "selected_by": metric,
                "priority": priority,
                "rank_in_priority": rank,
            })
            selected.append(final)
            selected_ids.add(row["market_id"])
    return RankingResult(candidates, selected, rankings)
