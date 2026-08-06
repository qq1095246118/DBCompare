"""Category filtering and deterministic merging for Polymarket markets."""

from __future__ import annotations

from collections.abc import Iterable
from copy import deepcopy
from dataclasses import dataclass


CATEGORY_ORDER = (
    "politics", "geopolitics", "economy", "finance", "technology", "crypto",
)

TAG_CATEGORIES = {
    "2": "politics",
    "100265": "geopolitics",
    "100328": "economy",
    "120": "finance",
    "1401": "technology",
    "21": "crypto",
}

CATEGORY_TAG_IDS = {
    category: int(tag_id) for tag_id, category in TAG_CATEGORIES.items()
}

CRYPTO_TOPIC_ORDER = (
    "regulation", "etf", "exchange_risk", "stablecoin", "protocol_security",
)
_REGULATION_SLUGS = frozenset({
    "crypto-policy", "crypto-legal", "regulation", "regulations", "sec",
    "cftc", "legal", "legal-proceedings", "ban",
})
_ETF_SLUGS = frozenset({"etf", "etfs", "etf-approval"})
_EXCHANGE_SLUGS = frozenset({"exchange", "exchanges"})
_EXCHANGE_RISK_SLUGS = frozenset({
    "bankruptcy", "insolvency", "hack", "hacking", "exploit", "exploits",
    "cybersecurity", "data-breach",
})
_STABLECOIN_SLUGS = frozenset({
    "stablecoins", "tether", "usdt", "usdc", "depeg",
})
_PROTOCOL_SECURITY_SLUGS = frozenset({
    "protocol-risk", "protocol-upgrade", "hack", "hacking", "hacker",
    "exploit", "exploits", "cybersecurity", "data-breach", "bybit-hack",
})

_COMPACT_FIELDS = (
    "id", "question", "description", "slug", "conditionId", "category",
    "startDate", "endDate", "active", "closed", "archived", "image", "icon",
    "outcomes", "outcomePrices", "liquidity", "volume", "volume24hr",
    "bestBid", "bestAsk", "lastTradePrice", "spread", "clobTokenIds",
    "enableOrderBook", "resolutionSource", "acceptingOrders",
)

_EVENT_FIELDS = (
    "id", "title", "slug", "active", "closed", "startDate", "endDate",
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
    compact = {
        field: deepcopy(source[field])
        for field in _COMPACT_FIELDS
        if field in source
    }
    if "tags" in source:
        compact["tags"] = deepcopy(source["tags"])
    events = source.get("events")
    if type(events) is list:
        compact["events"] = []
        if events and type(events[0]) is dict:
            compact["events"].append({
                field: deepcopy(events[0][field])
                for field in _EVENT_FIELDS
                if field in events[0]
            })
    return compact


def _tag_sort_key(tag_id: str) -> tuple[int, str]:
    return (int(tag_id), tag_id)


def _market_tag_slugs(source: dict[str, object]) -> set[str]:
    tags = source.get("tags")
    if type(tags) is not list or any(
        type(tag) is not dict
        or not isinstance(tag.get("slug"), str)
        or not tag["slug"].strip()
        for tag in tags
    ):
        raise ValueError("market tags must contain non-empty slugs")
    return {tag["slug"] for tag in tags}


def _crypto_matches(tag_slugs: set[str]) -> tuple[list[str], list[str]]:
    topics: list[str] = []
    evidence: set[str] = set()
    regulation = tag_slugs & _REGULATION_SLUGS
    if regulation:
        topics.append("regulation")
        evidence.update(regulation)
    etf = tag_slugs & _ETF_SLUGS
    if etf:
        topics.append("etf")
        evidence.update(etf)
    exchange = tag_slugs & _EXCHANGE_SLUGS
    exchange_risk = tag_slugs & _EXCHANGE_RISK_SLUGS
    if exchange and exchange_risk:
        topics.append("exchange_risk")
        evidence.update(exchange)
        evidence.update(exchange_risk)
    stablecoin = tag_slugs & _STABLECOIN_SLUGS
    if stablecoin:
        topics.append("stablecoin")
        evidence.update(stablecoin)
    protocol_security = tag_slugs & _PROTOCOL_SECURITY_SLUGS
    if protocol_security:
        topics.append("protocol_security")
        evidence.update(protocol_security)
    return topics, sorted(evidence)


class MarketAccumulator:
    def __init__(self) -> None:
        self._merged: dict[tuple[str, str], dict[str, object]] = {}
        self._valid_ids: set[str] = set()
        self._conflicts = 0
        self._crypto_rejections = 0

    def add(self, rows: Iterable[TaggedMarket]) -> None:
        for row in rows:
            if not isinstance(row, TaggedMarket):
                raise TypeError("tagged market rows must be TaggedMarket values")
            if row.tag_id not in TAG_CATEGORIES:
                raise ValueError("tagged market uses an unknown tag ID")
            source = row.source
            if type(source) is not dict:
                raise TypeError("tagged market source must be an object")
            tag_slugs = _market_tag_slugs(source)
            market_id = source.get("id")
            if not isinstance(market_id, str) or not market_id.strip():
                continue
            if source.get("active") is not True or source.get("closed") is not False:
                continue
            self._valid_ids.add(market_id)

            category = TAG_CATEGORIES[row.tag_id]
            topics: list[str] = []
            matching_slugs: list[str] = []
            if category == "crypto":
                topics, matching_slugs = _crypto_matches(tag_slugs)
                if not topics:
                    self._crypto_rejections += 1
                    continue

            candidate_key = (category, market_id)
            entry = self._merged.get(candidate_key)
            if entry is None:
                entry = {
                    "market_id": market_id,
                    "categories": {category},
                    "matched_tag_ids": {row.tag_id},
                    "crypto_topics": set(topics),
                    "matched_crypto_tag_slugs": set(matching_slugs),
                    "source": deepcopy(source),
                }
                self._merged[candidate_key] = entry
            elif entry["source"] != source:
                self._conflicts += 1

    def result(self) -> MergeResult:
        output = []
        candidate_keys = sorted(
            self._merged,
            key=lambda key: (CATEGORY_ORDER.index(key[0]), key[1]),
        )
        for candidate_key in candidate_keys:
            entry = self._merged[candidate_key]
            market_id = candidate_key[1]
            output.append({
                "market_id": market_id,
                "categories": sorted(entry["categories"]),
                "matched_tag_ids": sorted(
                    entry["matched_tag_ids"], key=_tag_sort_key
                ),
                "crypto_topics": [
                    topic for topic in CRYPTO_TOPIC_ORDER
                    if topic in entry["crypto_topics"]
                ],
                "matched_crypto_tag_slugs": sorted(
                    entry["matched_crypto_tag_slugs"]
                ),
                "source": deepcopy(entry["source"]),
            })
        return MergeResult(
            output,
            len(self._valid_ids),
            self._conflicts,
            self._crypto_rejections,
        )


def merge_markets(rows: Iterable[TaggedMarket]) -> MergeResult:
    accumulator = MarketAccumulator()
    accumulator.add(rows)
    return accumulator.result()
