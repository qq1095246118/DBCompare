"""Category filtering and deterministic merging for Polymarket markets."""

from __future__ import annotations

from collections.abc import Iterable
from copy import deepcopy
from dataclasses import dataclass


TAG_CATEGORIES = {
    "2": "politics",
    "100265": "geopolitics",
    "100328": "economy",
    "120": "finance",
    "105582": "technology",
    "1401": "technology",
    "22": "technology",
    "21": "crypto",
}

CATEGORY_ORDER = tuple(dict.fromkeys(TAG_CATEGORIES.values()))

CRYPTO_KEYWORDS = (
    "regulation", "regulatory", "regulator", "sec", "cftc", "etf",
    "exchange", "binance", "coinbase", "kraken", "stablecoin", "usdt",
    "usdc", "tether", "depeg", "protocol", "upgrade", "fork", "hack",
    "hacked", "exploit", "breach", "attack", "vulnerability",
    "security incident",
)

_COMPACT_FIELDS = (
    "id", "question", "description", "slug", "conditionId", "category",
    "startDate", "endDate", "active", "closed", "archived", "image", "icon",
    "outcomes", "outcomePrices", "liquidity", "volume", "volume24hr",
    "bestBid", "bestAsk", "lastTradePrice", "spread", "clobTokenIds",
    "enableOrderBook", "resolutionSource",
)


@dataclass(frozen=True)
class TaggedMarket:
    tag_id: str
    source: dict[str, object]


@dataclass(frozen=True)
class MergeResult:
    markets: list[dict[str, object]]
    distinct_market_count: int
    source_conflict_count: int
    crypto_rejection_count: int


def compact_market(source: dict[str, object]) -> dict[str, object]:
    if type(source) is not dict:
        raise TypeError("market source must be an object")
    return {
        field: deepcopy(source[field])
        for field in _COMPACT_FIELDS
        if field in source
    }


def _tag_sort_key(tag_id: str) -> tuple[int, str]:
    return (int(tag_id), tag_id)


def _crypto_matches(source: dict[str, object]) -> list[str]:
    description = source.get("description")
    if not isinstance(description, str) or not description:
        return []
    folded = description.casefold()
    return sorted({word for word in CRYPTO_KEYWORDS if word in folded})


def merge_markets(rows: Iterable[TaggedMarket]) -> MergeResult:
    merged: dict[str, dict[str, object]] = {}
    valid_ids: set[str] = set()
    conflicts = 0
    crypto_rejections = 0

    for row in rows:
        if not isinstance(row, TaggedMarket):
            raise TypeError("tagged market rows must be TaggedMarket values")
        if row.tag_id not in TAG_CATEGORIES:
            raise ValueError("tagged market uses an unknown tag ID")
        source = row.source
        if type(source) is not dict:
            raise TypeError("tagged market source must be an object")
        market_id = source.get("id")
        if not isinstance(market_id, str) or not market_id.strip():
            continue
        if source.get("active") is not True or source.get("closed") is not False:
            continue
        valid_ids.add(market_id)

        category = TAG_CATEGORIES[row.tag_id]
        keywords: list[str] = []
        if category == "crypto":
            keywords = _crypto_matches(source)
            if not keywords:
                crypto_rejections += 1
                continue

        entry = merged.get(market_id)
        if entry is None:
            entry = {
                "market_id": market_id,
                "categories": set(),
                "matched_tag_ids": set(),
                "matched_crypto_keywords": set(),
                "source": deepcopy(source),
            }
            merged[market_id] = entry
        elif entry["source"] != source:
            conflicts += 1

        entry["categories"].add(category)
        entry["matched_tag_ids"].add(row.tag_id)
        entry["matched_crypto_keywords"].update(keywords)

    output = []
    for market_id in sorted(merged):
        entry = merged[market_id]
        output.append({
            "market_id": market_id,
            "categories": sorted(entry["categories"]),
            "matched_tag_ids": sorted(entry["matched_tag_ids"], key=_tag_sort_key),
            "matched_crypto_keywords": sorted(entry["matched_crypto_keywords"]),
            "source": entry["source"],
        })
    return MergeResult(output, len(valid_ids), conflicts, crypto_rejections)
