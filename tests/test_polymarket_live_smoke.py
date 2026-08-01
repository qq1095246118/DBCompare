import pytest

from getMarket.Polymarket.tool.market_filter import TAG_CATEGORIES
from getMarket.Polymarket.tool.polymarket_api import PolymarketApiClient


@pytest.mark.live_polymarket
@pytest.mark.asyncio
async def test_live_polymarket():
    client = PolymarketApiClient(max_attempts=2, timeout=20)
    observed_market_count = 0

    for tag_id in TAG_CATEGORIES:
        pages = await client.collect_tag(tag_id, page_limit=1, max_pages=1)
        assert len(pages) == 1
        for market in pages[0].payload["markets"]:
            assert isinstance(market["tags"], list)
            for tag in market["tags"]:
                assert isinstance(tag, dict)
                assert isinstance(tag["slug"], str)
                assert tag["slug"].strip()
        observed_market_count += len(pages[0].payload["markets"])

    assert observed_market_count > 0
