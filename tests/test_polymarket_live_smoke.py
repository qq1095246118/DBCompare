import pytest

from getMarket.Polymarket.tool.market_filter import TAG_CATEGORIES
from getMarket.Polymarket.tool.polymarket_api import PolymarketApiClient


@pytest.mark.live_polymarket
@pytest.mark.asyncio
async def test_configured_tags_return_valid_keyset_pages():
    client = PolymarketApiClient(max_attempts=2, timeout=20)

    for tag_id in TAG_CATEGORIES:
        pages = await client.collect_tag(tag_id, page_limit=1, max_pages=1)
        assert len(pages) == 1
        assert isinstance(pages[0].payload["markets"], list)
