from unittest.mock import ANY

import pytest

from getMarket.Polymarket.tool.market_filter import CATEGORY_ORDER
from getMarket.Polymarket.tool.market_ranking import (
    normalize_metrics,
    select_ranked_markets,
)


def test_normalize_metrics_parses_gamma_strings():
    metrics = normalize_metrics({
        "liquidity": "1200.50",
        "volume24hr": 42,
        "outcomePrices": '["0.82", "0.18"]',
    })

    assert metrics == {
        "liquidity": "1200.5",
        "dominant_probability": "0.82",
        "volume24hr": "42",
    }


@pytest.mark.parametrize("value", [True, "NaN", "Infinity", -1, "bad"])
def test_invalid_liquidity_is_isolated(value):
    metrics = normalize_metrics({
        "liquidity": value,
        "volume24hr": "9",
        "outcomePrices": ["0.6", "0.4"],
    })

    assert metrics["liquidity"] is None
    assert metrics["volume24hr"] == "9"


@pytest.mark.parametrize("prices", [[], '["1.1", "-0.1"]', "bad", [True, 0]])
def test_invalid_outcome_prices_do_not_invalidate_other_metrics(prices):
    metrics = normalize_metrics({
        "liquidity": "1", "volume24hr": "2", "outcomePrices": prices,
    })

    assert metrics["dominant_probability"] is None
    assert metrics["liquidity"] == "1"


def candidate(
    index, *, liquidity=None, probability=None, volume=None, categories=("politics",)
):
    market_id = f"{index:03}" if isinstance(index, int) else index
    source = {"id": market_id}
    if liquidity is not None:
        source["liquidity"] = str(liquidity)
    if probability is not None:
        source["outcomePrices"] = [str(probability), "0"]
    if volume is not None:
        source["volume24hr"] = str(volume)
    return {
        "market_id": market_id,
        "categories": categories if isinstance(categories, str) else list(categories),
        "matched_tag_ids": ["2"],
        "matched_crypto_keywords": [],
        "source": source,
    }


def test_category_order_matches_configured_output_order():
    assert CATEGORY_ORDER == (
        "politics", "geopolitics", "economy", "finance", "technology", "crypto",
    )


def test_liquidity_fills_politics_category_before_fallbacks():
    rows = [candidate(i, liquidity=i, probability=i / 100, volume=1000 - i) for i in range(1, 26)]

    result = select_ranked_markets(rows)

    assert [row["market_id"] for row in result.selected] == [
        f"{i:03}" for i in range(25, 5, -1)
    ]
    assert [row["selected_by"] for row in result.selected] == ["liquidity"] * 20
    assert result.rankings["politics"]["dominant_probability"]["selected_market_ids"] == []
    assert result.rankings["politics"]["volume24hr"]["selected_market_ids"] == []
    assert tuple(result.rankings) == CATEGORY_ORDER
    assert all(
        tuple(result.rankings[category]) == (
            "liquidity", "dominant_probability", "volume24hr",
        )
        for category in CATEGORY_ORDER
    )


def test_fallbacks_fill_remaining_capacity_with_continuous_category_ranks():
    rows = [
        candidate(1, liquidity=10),
        candidate(2, liquidity=0),
        candidate(3, probability=0.9),
        candidate(4, probability=0.8),
        candidate(5, volume=100),
        candidate(6, volume=90),
    ]

    result = select_ranked_markets(rows, per_category=5)

    assert [row["market_id"] for row in result.selected] == ["001", "002", "003", "004", "005"]
    assert [row["selected_by"] for row in result.selected] == [
        "liquidity", "liquidity", "dominant_probability", "dominant_probability", "volume24hr",
    ]
    assert [row["priority"] for row in result.selected] == [1, 1, 2, 2, 3]
    assert [row["rank_in_category"] for row in result.selected] == [1, 2, 3, 4, 5]


def test_short_category_finishes_without_duplicate_ids():
    rows = [candidate(i, liquidity=i) for i in range(1, 4)]

    result = select_ranked_markets(rows, per_category=5)

    assert [row["market_id"] for row in result.selected] == ["003", "002", "001"]
    assert len({row["market_id"] for row in result.selected}) == 3
    assert result.rankings["politics"]["dominant_probability"]["selected_market_ids"] == []


def test_multi_category_market_is_selected_once_per_category_in_fixed_order():
    rows = [candidate(1, liquidity=10, categories=("finance", "politics"))]

    result = select_ranked_markets(rows, per_category=1)

    assert [row["selected_category"] for row in result.selected] == ["politics", "finance"]
    assert [row["market_id"] for row in result.selected] == ["001", "001"]
    assert [row["market_id"] for row in result.candidates] == ["001"]


def test_each_configured_category_has_an_independent_limit_and_fixed_order():
    rows = [
        candidate(
            f"{category}-{index:03}", liquidity=index, categories=(category,)
        )
        for category in CATEGORY_ORDER
        for index in range(1, 22)
    ]

    result = select_ranked_markets(rows)

    assert len(result.selected) == 120
    assert [row["selected_category"] for row in result.selected] == [
        category for category in CATEGORY_ORDER for _ in range(20)
    ]
    assert all(set(result.rankings[category]) == set((
        "liquidity", "dominant_probability", "volume24hr",
    )) for category in CATEGORY_ORDER)


def test_metric_ties_use_market_id_ascending():
    rows = [candidate(index, liquidity=10) for index in (3, 1, 2)]

    result = select_ranked_markets(rows, per_category=2)

    assert [row["market_id"] for row in result.selected] == ["001", "002"]


@pytest.mark.parametrize("limit", [0, -1, True, 1.5])
def test_invalid_per_category_limit_is_rejected(limit):
    with pytest.raises(ValueError, match="^per-category limit must be positive$"):
        select_ranked_markets([candidate(1, liquidity=1)], per_category=limit)


@pytest.mark.parametrize("categories", [[], ["sports"], "politics", [ANY]])
def test_invalid_category_membership_is_rejected(categories):
    with pytest.raises(ValueError, match="ranked market categories"):
        select_ranked_markets([candidate(1, liquidity=1, categories=categories)])


def test_selected_rows_do_not_include_rank_in_priority():
    result = select_ranked_markets([candidate(1, liquidity=1)])

    assert "rank_in_priority" not in result.selected[0]
