from copy import deepcopy
from types import MappingProxyType
from unittest.mock import ANY

import pytest

from getMarket.Polymarket.tool.market_filter import CATEGORY_ORDER
from getMarket.Polymarket.tool.market_ranking import (
    RANKING_LIMIT,
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
        "crypto_topics": [],
        "matched_crypto_tag_slugs": [],
        "source": source,
    }


def test_category_order_matches_configured_output_order():
    assert CATEGORY_ORDER == (
        "politics", "geopolitics", "economy", "finance", "technology", "crypto",
    )


def test_each_metric_has_an_independent_top_ten_and_rank_sequence():
    rows = [
        candidate(f"liquidity-{index:02}", liquidity=index)
        for index in range(12)
    ]
    rows.extend(
        candidate(f"probability-{index:02}", probability=(index + 1) / 100)
        for index in range(12)
    )
    rows.extend(
        candidate(f"volume-{index:02}", volume=index)
        for index in range(12)
    )

    result = select_ranked_markets(rows)

    assert RANKING_LIMIT == 10
    assert len(result.selected) == 30
    assert [row["ranking_metric"] for row in result.selected] == (
        ["liquidity"] * 10
        + ["dominant_probability"] * 10
        + ["volume24hr"] * 10
    )
    assert [row["ranking_priority"] for row in result.selected] == (
        [1] * 10 + [2] * 10 + [3] * 10
    )
    assert [row["rank"] for row in result.selected] == list(range(1, 11)) * 3
    assert [row["market_id"] for row in result.selected] == (
        [f"liquidity-{index:02}" for index in range(11, 1, -1)]
        + [f"probability-{index:02}" for index in range(11, 1, -1)]
        + [f"volume-{index:02}" for index in range(11, 1, -1)]
    )
    assert result.rankings["politics"]["volume24hr"] == {
        "priority": 3,
        "selected_market_ids": [
            f"volume-{index:02}" for index in range(11, 1, -1)
        ],
        "selected_metrics": [str(index) for index in range(11, 1, -1)],
        "excluded_by_priorities": [],
    }
    assert tuple(result.rankings) == CATEGORY_ORDER
    assert all(
        tuple(result.rankings[category])
        == ("liquidity", "dominant_probability", "volume24hr")
        for category in CATEGORY_ORDER
    )


def test_lower_priority_excludes_prior_winners_before_truncation_and_refills():
    rows = [
        candidate(
            f"{index:02}", liquidity=100 - index, probability=1 - index / 100
        )
        for index in range(20)
    ]

    result = select_ranked_markets(rows)

    liquidity = result.rankings["politics"]["liquidity"]
    probability = result.rankings["politics"]["dominant_probability"]
    assert liquidity["selected_market_ids"] == [f"{index:02}" for index in range(10)]
    assert probability["selected_market_ids"] == [
        f"{index:02}" for index in range(10, 20)
    ]
    assert probability["excluded_by_priorities"] == [
        f"{index:02}" for index in range(10)
    ]
    assert [
        row["rank"]
        for row in result.selected
        if row["ranking_metric"] == "dominant_probability"
    ] == list(range(1, 11))


def test_short_rankings_select_each_market_once():
    rows = [
        candidate("liquidity", liquidity=5),
        candidate("probability", probability=0.8),
        candidate("volume", volume=12),
    ]

    result = select_ranked_markets(rows)

    assert [row["market_id"] for row in result.selected] == [
        "liquidity", "probability", "volume",
    ]
    assert [row["rank"] for row in result.selected] == [1, 1, 1]
    assert len({row["market_id"] for row in result.selected}) == 3


def test_invalid_metric_isolates_only_that_ranking():
    result = select_ranked_markets([
        candidate("shared", liquidity="bad", probability=0.8, volume=12),
    ])

    assert result.rankings["politics"]["liquidity"]["selected_market_ids"] == []
    assert result.rankings["politics"]["dominant_probability"][
        "selected_market_ids"
    ] == ["shared"]
    assert result.rankings["politics"]["volume24hr"]["selected_market_ids"] == []
    assert result.rankings["politics"]["volume24hr"][
        "excluded_by_priorities"
    ] == ["shared"]


def test_metric_ties_select_market_id_ascending_before_truncation():
    rows = [candidate(index, liquidity=10) for index in range(12, 0, -1)]

    result = select_ranked_markets(rows)

    assert [row["market_id"] for row in result.selected] == [
        f"{index:03}" for index in range(1, 11)
    ]


def test_same_market_id_in_different_categories_has_separate_identity():
    rows = [
        candidate("shared", liquidity=10, categories=("finance",)),
        candidate("shared", liquidity=10, categories=("politics",)),
    ]

    result = select_ranked_markets(rows)

    assert [row["selected_category"] for row in result.selected] == [
        "politics", "finance",
    ]
    assert [row["market_id"] for row in result.selected] == ["shared", "shared"]
    assert [row["rank"] for row in result.selected] == [1, 1]
    assert [
        (row["categories"][0], row["market_id"]) for row in result.candidates
    ] == [("politics", "shared"), ("finance", "shared")]


def test_candidates_sort_by_category_then_market_id():
    rows = [
        candidate("b", liquidity=1, categories=("finance",)),
        candidate("b", liquidity=1, categories=("politics",)),
        candidate("a", liquidity=1, categories=("finance",)),
        candidate("a", liquidity=1, categories=("politics",)),
    ]

    result = select_ranked_markets(rows)

    assert [
        (row["categories"][0], row["market_id"]) for row in result.candidates
    ] == [
        ("politics", "a"),
        ("politics", "b"),
        ("finance", "a"),
        ("finance", "b"),
    ]


def test_selected_rows_have_current_ranking_metadata_only():
    row = candidate(1, liquidity=1)
    row.update({
        "selected_by": "legacy",
        "priority": 99,
        "rank_in_category": 99,
        "rank_in_priority": 99,
        "unexpected_metadata": "discard",
    })

    result = select_ranked_markets([row])

    selected = result.selected[0]
    assert {
        key: selected[key] for key in ("ranking_metric", "ranking_priority", "rank")
    } == {
        "ranking_metric": "liquidity",
        "ranking_priority": 1,
        "rank": 1,
    }
    assert not {
        "selected_by", "priority", "rank_in_category", "rank_in_priority",
    }.intersection(selected)
    assert set(selected) == {
        "market_id",
        "categories",
        "matched_tag_ids",
        "crypto_topics",
        "matched_crypto_tag_slugs",
        "source",
        "normalized_metrics",
        "selected_category",
        "ranking_metric",
        "ranking_priority",
        "rank",
    }


def test_duplicate_category_and_market_id_pair_is_rejected():
    rows = [candidate("shared", liquidity=2), candidate("shared", liquidity=1)]

    with pytest.raises(
        ValueError, match="category and market ID pairs must be unique"
    ):
        select_ranked_markets(rows)


@pytest.mark.parametrize("market_id", [None, "", "   "])
def test_invalid_market_id_is_rejected(market_id):
    with pytest.raises(ValueError, match="non-whitespace string"):
        select_ranked_markets([candidate(market_id, liquidity=1)])


def test_ranking_does_not_mutate_input_candidates():
    rows = [
        candidate("liquidity", liquidity=2),
        candidate("probability", probability=0.7),
        candidate("volume", volume=8),
    ]
    original = deepcopy(rows)

    select_ranked_markets(rows)

    assert rows == original


@pytest.mark.parametrize(
    "categories",
    [[], ["sports"], "politics", [ANY], ["politics", "finance"]],
)
def test_candidate_must_have_exactly_one_configured_category(categories):
    with pytest.raises(ValueError, match="exactly one configured category"):
        select_ranked_markets([
            candidate(1, liquidity=1, categories=categories),
        ])


def test_ranked_market_must_be_a_mapping():
    with pytest.raises(TypeError, match="ranked markets must be mappings"):
        select_ranked_markets([object()])


def test_ranked_market_source_must_be_a_mapping():
    row = candidate(1, liquidity=1)
    row["source"] = []

    with pytest.raises(ValueError, match="ranked market source must be a mapping"):
        select_ranked_markets([row])


def test_ranked_market_accepts_a_non_dict_mapping_source():
    row = candidate(1, liquidity=1)
    row["source"] = MappingProxyType(row["source"])

    result = select_ranked_markets([row])

    assert result.selected[0]["normalized_metrics"]["liquidity"] == "1"


def test_fixed_maximum_is_thirty_rankings_per_configured_category():
    rows = []
    for category in CATEGORY_ORDER:
        rows.extend(
            candidate(
                f"{category}-liquidity-{index:02}",
                liquidity=index,
                categories=(category,),
            )
            for index in range(10)
        )
        rows.extend(
            candidate(
                f"{category}-probability-{index:02}",
                probability=(index + 1) / 10,
                categories=(category,),
            )
            for index in range(10)
        )
        rows.extend(
            candidate(
                f"{category}-volume-{index:02}",
                volume=index,
                categories=(category,),
            )
            for index in range(10)
        )

    result = select_ranked_markets(rows)

    assert len(result.selected) == 180
    assert [row["selected_category"] for row in result.selected] == [
        category for category in CATEGORY_ORDER for _ in range(30)
    ]
    for category in CATEGORY_ORDER:
        selected = [
            row for row in result.selected if row["selected_category"] == category
        ]
        assert len(selected) == 30
        assert len({row["market_id"] for row in selected}) == 30
        assert [row["rank"] for row in selected] == list(range(1, 11)) * 3
