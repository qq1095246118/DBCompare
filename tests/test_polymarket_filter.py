import pytest

from getMarket.Polymarket.tool.market_filter import (
    CATEGORY_ORDER,
    CATEGORY_TAG_IDS,
    TAG_CATEGORIES,
    MarketAccumulator,
    TaggedMarket,
    compact_market,
    merge_markets,
)


SINGLE_SLUG_TOPICS = {
    "regulation": (
        "crypto-policy", "crypto-legal", "regulation", "regulations", "sec",
        "cftc", "legal", "legal-proceedings", "ban",
    ),
    "etf": ("etf", "etfs", "etf-approval"),
    "stablecoin": ("stablecoins", "tether", "usdt", "usdc", "depeg"),
    "protocol_security": (
        "protocol-risk", "protocol-upgrade", "hack", "hacking", "hacker",
        "exploit", "exploits", "cybersecurity", "data-breach", "bybit-hack",
    ),
}


def market(market_id="10", *, tag_slugs=("election",), **overrides):
    row = {
        "id": market_id,
        "active": True,
        "closed": False,
        "tags": [
            {"id": f"tag-{index}", "slug": slug}
            for index, slug in enumerate(tag_slugs)
        ],
    }
    row.update(overrides)
    return row


def test_configures_exactly_one_official_tag_per_category():
    assert TAG_CATEGORIES == {
        "2": "politics",
        "100265": "geopolitics",
        "100328": "economy",
        "120": "finance",
        "1401": "technology",
        "21": "crypto",
    }
    assert CATEGORY_ORDER == (
        "politics", "geopolitics", "economy", "finance", "technology", "crypto",
    )
    assert CATEGORY_TAG_IDS == {
        "politics": 2,
        "geopolitics": 100265,
        "economy": 100328,
        "finance": 120,
        "technology": 1401,
        "crypto": 21,
    }


@pytest.mark.parametrize("removed_tag", ["105582", "22"])
def test_removed_technology_tags_are_rejected(removed_tag):
    with pytest.raises(ValueError, match="unknown tag ID"):
        merge_markets([TaggedMarket(removed_tag, market())])


def test_same_market_id_keeps_independent_category_candidates_and_sources():
    politics = market(question="politics source")
    finance = market(question="finance source")

    result = merge_markets([
        TaggedMarket("2", politics),
        TaggedMarket("120", finance),
    ])

    assert [row["categories"] for row in result.markets] == [
        ["politics"], ["finance"],
    ]
    assert [row["matched_tag_ids"] for row in result.markets] == [["2"], ["120"]]
    assert [row["source"]["question"] for row in result.markets] == [
        "politics source", "finance source",
    ]
    assert result.source_conflict_count == 0


@pytest.mark.parametrize(
    ("topic", "slug"),
    [
        (topic, slug)
        for topic, slugs in SINGLE_SLUG_TOPICS.items()
        for slug in slugs
    ],
)
def test_crypto_single_slug_rules_use_exact_official_tag_slugs(topic, slug):
    result = merge_markets([
        TaggedMarket("21", market(tag_slugs=(slug,), description="irrelevant")),
    ])

    assert result.markets[0]["categories"] == ["crypto"]
    assert result.markets[0]["crypto_topics"] == [topic]
    assert result.markets[0]["matched_crypto_tag_slugs"] == [slug]


@pytest.mark.parametrize("risk_slug", [
    "bankruptcy", "insolvency", "hack", "hacking", "exploit", "exploits",
    "cybersecurity", "data-breach",
])
@pytest.mark.parametrize("exchange_slug", ["exchange", "exchanges"])
def test_exchange_risk_requires_an_exchange_and_a_risk_slug(
    exchange_slug, risk_slug,
):
    result = merge_markets([
        TaggedMarket("21", market(tag_slugs=(exchange_slug, risk_slug))),
    ])

    assert "exchange_risk" in result.markets[0]["crypto_topics"]
    assert result.markets[0]["matched_crypto_tag_slugs"] == sorted(
        [exchange_slug, risk_slug]
    )


@pytest.mark.parametrize("tag_slugs", [
    ("exchange",),
    ("exchanges",),
    ("bankruptcy",),
    ("insolvency",),
])
def test_exchange_risk_rejects_either_group_on_its_own(tag_slugs):
    result = merge_markets([TaggedMarket("21", market(tag_slugs=tag_slugs))])

    assert result.markets == []
    assert result.crypto_rejection_count == 1


def test_shared_risk_slug_matches_protocol_security_without_exchange():
    result = merge_markets([TaggedMarket("21", market(tag_slugs=("hack",)))])

    assert result.markets[0]["crypto_topics"] == ["protocol_security"]
    assert "exchange_risk" not in result.markets[0]["crypto_topics"]


def test_crypto_market_keeps_all_topics_once_in_canonical_order():
    result = merge_markets([TaggedMarket("21", market(tag_slugs=(
        "usdc", "exchange", "sec", "hack", "etf",
    )))])

    assert result.markets[0]["crypto_topics"] == [
        "regulation", "etf", "exchange_risk", "stablecoin", "protocol_security",
    ]
    assert result.markets[0]["matched_crypto_tag_slugs"] == [
        "etf", "exchange", "hack", "sec", "usdc",
    ]


def test_description_and_question_never_make_crypto_eligible():
    result = merge_markets([TaggedMarket("21", market(
        tag_slugs=("bitcoin",),
        question="ETF stablecoin exchange hack?",
        description="SEC regulation, bankruptcy, USDC depeg and protocol exploit.",
    ))])

    assert result.markets == []
    assert result.crypto_rejection_count == 1


@pytest.mark.parametrize("slug", ["SEC", "etf-news", "stablecoin"])
def test_crypto_slug_matching_is_exact_and_case_sensitive(slug):
    result = merge_markets([TaggedMarket("21", market(tag_slugs=(slug,)))])

    assert result.markets == []
    assert result.crypto_rejection_count == 1


def test_tag_label_never_makes_crypto_eligible():
    result = merge_markets([TaggedMarket("21", market(tags=[{
        "slug": "bitcoin", "label": "SEC regulation ETF",
    }]))])

    assert result.markets == []
    assert result.crypto_rejection_count == 1


def test_evidence_excludes_a_slug_from_an_unsatisfied_and_rule():
    result = merge_markets([
        TaggedMarket("21", market(tag_slugs=("etf", "exchange"))),
    ])

    assert result.markets[0]["crypto_topics"] == ["etf"]
    assert result.markets[0]["matched_crypto_tag_slugs"] == ["etf"]


def test_valid_empty_tags_reject_crypto_without_failing():
    result = merge_markets([TaggedMarket("21", market(tag_slugs=()))])

    assert result.markets == []
    assert result.crypto_rejection_count == 1


def test_non_crypto_stream_accepts_valid_tags_without_topic_filtering():
    result = merge_markets([
        TaggedMarket("2", market(tag_slugs=(), description="No configured words.")),
    ])

    assert result.markets[0]["categories"] == ["politics"]
    assert result.markets[0]["crypto_topics"] == []
    assert result.markets[0]["matched_crypto_tag_slugs"] == []


def test_failed_crypto_rule_does_not_remove_other_category():
    source = market(tag_slugs=("bitcoin",), description="No matching Tag slug.")
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


def test_compact_market_keeps_tags_and_only_required_first_event_fields():
    source = market(
        question="Question",
        description="Description",
        acceptingOrders=True,
        liquidity="10",
        outcomePrices='["0.7","0.3"]',
        volume24hr="5",
        events=[
            {
                "id": "event-1", "title": "Event title", "slug": "event-slug",
                "active": True, "closed": False, "startDate": "start",
                "endDate": "end", "large": "drop this",
            },
            {"id": "event-2", "title": "do not retain a second event"},
        ],
        clobRewards=[{"large": "nested"}],
    )

    compact = compact_market(source)

    assert compact["question"] == "Question"
    assert compact["acceptingOrders"] is True
    assert compact["tags"] == source["tags"]
    assert compact["tags"] is not source["tags"]
    assert compact["events"] == [{
        "id": "event-1", "title": "Event title", "slug": "event-slug",
        "active": True, "closed": False, "startDate": "start", "endDate": "end",
    }]
    assert "clobRewards" not in compact


@pytest.mark.parametrize("tags", [
    None, {}, [None], [{}], [{"slug": ""}], [{"slug": 21}],
])
def test_merge_rejects_malformed_tags_when_called_outside_api_layer(tags):
    source = market()
    source["tags"] = tags

    with pytest.raises(ValueError, match="market tags must contain non-empty slugs"):
        merge_markets([TaggedMarket("2", source)])


def test_market_accumulator_merges_pages_without_retaining_page_batches():
    accumulator = MarketAccumulator()
    first = market(question="first")
    second = market(question="second")

    accumulator.add([TaggedMarket("2", first)])
    accumulator.add([TaggedMarket("120", second)])
    result = accumulator.result()

    assert [row["categories"] for row in result.markets] == [
        ["politics"], ["finance"],
    ]
    assert [row["source"]["question"] for row in result.markets] == [
        "first", "second",
    ]
    assert result.source_conflict_count == 0


def test_first_source_wins_only_for_a_repeat_in_the_same_category():
    first = market(question="first")
    second = market(question="second")

    result = merge_markets([
        TaggedMarket("2", first),
        TaggedMarket("2", second),
    ])

    assert len(result.markets) == 1
    assert result.markets[0]["source"]["question"] == "first"
    assert result.source_conflict_count == 1


def test_same_category_conflict_keeps_first_crypto_topic_evidence():
    first = market(tag_slugs=("stablecoins",), question="first")
    second = market(tag_slugs=("etf",), question="second")

    result = merge_markets([
        TaggedMarket("21", first),
        TaggedMarket("21", second),
    ])

    assert result.markets[0]["source"]["question"] == "first"
    assert result.markets[0]["crypto_topics"] == ["stablecoin"]
    assert result.markets[0]["matched_crypto_tag_slugs"] == ["stablecoins"]
    assert result.source_conflict_count == 1
