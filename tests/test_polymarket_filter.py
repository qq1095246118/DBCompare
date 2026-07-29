from copy import deepcopy

from getMarket.Polymarket.tool.market_filter import (
    TaggedMarket,
    compact_market,
    merge_markets,
)


def market(market_id="10", **overrides):
    row = {"id": market_id, "active": True, "closed": False}
    row.update(overrides)
    return row


def test_merge_markets_unions_categories_and_tag_ids():
    rows = [
        TaggedMarket("2", market()),
        TaggedMarket("120", market()),
    ]

    result = merge_markets(rows)

    assert result.markets[0]["market_id"] == "10"
    assert result.markets[0]["categories"] == ["finance", "politics"]
    assert result.markets[0]["matched_tag_ids"] == ["2", "120"]


def test_technology_tags_merge_into_one_category():
    result = merge_markets([
        TaggedMarket("105582", market()),
        TaggedMarket("1401", market()),
        TaggedMarket("22", market()),
    ])

    assert result.markets[0]["categories"] == ["technology"]
    assert result.markets[0]["matched_tag_ids"] == ["22", "1401", "105582"]


def test_crypto_matches_description_only_case_insensitively():
    result = merge_markets([TaggedMarket("21", market(
        question="Will an ETF be approved?",
        description="A STABLECOIN depeg would resolve this market.",
    ))])

    assert result.markets[0]["categories"] == ["crypto"]
    assert result.markets[0]["matched_crypto_keywords"] == ["depeg", "stablecoin"]


def test_crypto_keyword_in_question_only_is_rejected():
    result = merge_markets([TaggedMarket("21", market(
        question="Will an ETF be approved?", description="No extra context."
    ))])

    assert result.markets == []
    assert result.crypto_rejection_count == 1


def test_failed_crypto_rule_does_not_remove_other_category():
    source = market(description="No matching phrase.")
    result = merge_markets([
        TaggedMarket("21", source),
        TaggedMarket("2", source),
    ])

    assert result.markets[0]["categories"] == ["politics"]
    assert result.markets[0]["matched_tag_ids"] == ["2"]
    assert result.crypto_rejection_count == 1


def test_inactive_closed_and_invalid_ids_are_skipped():
    result = merge_markets([
        TaggedMarket("2", market("1", active=False)),
        TaggedMarket("2", market("2", closed=True)),
        TaggedMarket("2", market("")),
        TaggedMarket("2", market(3)),
    ])

    assert result.markets == []


def test_first_source_is_copied_and_conflicts_are_counted():
    first = market(question="first")
    original = deepcopy(first)
    second = market(question="second")

    result = merge_markets([
        TaggedMarket("2", first),
        TaggedMarket("120", second),
    ])
    first["question"] = "mutated"

    assert result.markets[0]["source"] == original
    assert result.source_conflict_count == 1
    assert result.distinct_market_count == 1


def test_compact_market_keeps_collection_fields_and_drops_nested_relations():
    source = market(
        question="Question", description="Description", liquidity="10",
        outcomePrices='["0.7","0.3"]', volume24hr="5",
        events=[{"large": "nested"}], clobRewards=[{"large": "nested"}],
    )

    compact = compact_market(source)

    assert compact["question"] == "Question"
    assert compact["liquidity"] == "10"
    assert "events" not in compact
    assert "clobRewards" not in compact
