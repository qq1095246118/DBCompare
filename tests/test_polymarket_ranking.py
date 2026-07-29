import pytest

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


def candidate(index, liquidity, probability, volume):
    return {
        "market_id": f"{index:03}",
        "categories": ["politics"],
        "matched_tag_ids": ["2"],
        "matched_crypto_keywords": [],
        "source": {
            "id": f"{index:03}",
            "liquidity": str(liquidity),
            "outcomePrices": [str(probability), str(1 - probability)],
            "volume24hr": str(volume),
        },
    }


def test_select_ranked_markets_applies_priority_and_deduplicates():
    candidates = [candidate(i, i, i / 100, 1000 - i) for i in range(1, 36)]

    result = select_ranked_markets(candidates, per_priority=10)

    assert len(result.selected) == 30
    assert len({row["market_id"] for row in result.selected}) == 30
    assert [row["selected_by"] for row in result.selected[:10]] == ["liquidity"] * 10
    assert [row["selected_by"] for row in result.selected[10:20]] == ["dominant_probability"] * 10
    assert [row["selected_by"] for row in result.selected[20:]] == ["volume24hr"] * 10
    assert result.rankings["liquidity"]["selected_market_ids"] == [
        f"{i:03}" for i in range(35, 25, -1)
    ]


def test_ties_use_market_id_ascending_and_short_passes_are_not_compensated():
    rows = [candidate(i, 10, 0.5, 10) for i in (3, 1, 2)]

    result = select_ranked_markets(rows, per_priority=2)

    assert [row["market_id"] for row in result.selected] == ["001", "002", "003"]
    assert [row["selected_by"] for row in result.selected] == [
        "liquidity", "liquidity", "dominant_probability"
    ]
    assert result.rankings["volume24hr"]["selected_market_ids"] == []
