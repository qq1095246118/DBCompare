from collections import Counter
from datetime import date
import json

import pytest

from getMarket.Polymarket.tool import export_polymarket_market as cli
from getMarket.Polymarket.tool.market_filter import CATEGORY_ORDER, TAG_CATEGORIES
from getMarket.Polymarket.tool.polymarket_api import MarketPage, PolymarketApiError


DAY = date(2026, 7, 28)
CAPTURED_AT = "2026-07-28T00:00:00Z"


class FakeClient:
    def __init__(self, pages_by_tag, failure=None):
        self.pages_by_tag = pages_by_tag
        self.failure = failure
        self.requested_tags = []

    async def collect_tag(self, tag_id, *, page_limit):
        self.requested_tags.append(tag_id)
        if self.failure and tag_id == self.failure.tag_id:
            raise self.failure
        return self.pages_by_tag[tag_id]

    async def iter_tag(self, tag_id, *, page_limit):
        self.requested_tags.append(tag_id)
        if self.failure and tag_id == self.failure.tag_id:
            raise self.failure
        for item in self.pages_by_tag[tag_id]:
            yield item


def page(tag_id, markets):
    return MarketPage(
        tag_id, None, CAPTURED_AT, f"https://example.test?tag_id={tag_id}",
        200, 1, {"markets": markets, "next_cursor": None},
    )


def source_market(market_id, liquidity):
    return {
        "id": market_id,
        "active": True,
        "closed": False,
        "description": "ETF regulation update.",
        "liquidity": str(liquidity),
        "outcomePrices": ["0.6", "0.4"],
        "volume24hr": str(1000 - liquidity),
    }


def complete_pages():
    rows_by_category = {
        category: [
            source_market(f"{category}-{index:02d}", index)
            for index in range(1, 22)
        ]
        for category in CATEGORY_ORDER
    }
    shared = source_market("shared", 100)
    rows_by_category["politics"][0] = shared
    rows_by_category["finance"][0] = shared
    return {
        tag_id: [page(tag_id, rows_by_category[category])]
        for tag_id, category in TAG_CATEGORIES.items()
    }


def test_parse_args_uses_project_output_root():
    args = cli.parse_args([])

    assert args.output_root == cli._PROJECT_ROOT / "getMarket" / "Polymarket" / "market"
    assert args.page_limit == 20


@pytest.mark.parametrize("argv", [
    ["--timeout", "0"], ["--max-attempts", "0"], ["--page-limit", "0"],
    ["--page-limit", "21"],
    ["--business-date", "2026/07/28"],
])
def test_parse_args_rejects_invalid_values(argv):
    with pytest.raises(SystemExit):
        cli.parse_args(argv)


@pytest.mark.asyncio
async def test_run_collects_all_tags_and_publishes_ranked_generation(tmp_path, monkeypatch):
    client = FakeClient(complete_pages())
    monkeypatch.setattr(cli, "_business_today", lambda: DAY)
    monkeypatch.setattr(cli, "_utc_now", lambda: CAPTURED_AT)
    monkeypatch.setattr(cli, "_run_name", lambda _day: "2026-07-28_080000_run1")

    exit_code = await cli.run_async(
        cli.parse_args(["--output-root", str(tmp_path)]), client=client,
    )

    assert exit_code == 0
    assert client.requested_tags == list(TAG_CATEGORIES)
    run_dir = tmp_path / "2026-07-28_080000_run1"
    final = json.loads((run_dir / "final.json").read_text())
    assert len(final) == 120
    assert Counter(row["selected_category"] for row in final) == Counter({
        category: 20 for category in CATEGORY_ORDER
    })
    assert [row["selected_category"] for row in final] == [
        category for category in CATEGORY_ORDER for _ in range(20)
    ]
    assert [row["rank_in_category"] for row in final] == (
        list(range(1, 21)) * len(CATEGORY_ORDER)
    )
    assert [row["selected_by"] for row in final] == ["liquidity"] * 120
    assert [row["priority"] for row in final] == [1] * 120
    assert [
        row["selected_category"] for row in final if row["market_id"] == "shared"
    ] == ["politics", "finance"]

    clean = json.loads((run_dir / "clean.json").read_text())
    assert len(clean) == 125
    assert len({row["market_id"] for row in clean}) == 125
    shared_clean = next(row for row in clean if row["market_id"] == "shared")
    assert shared_clean["categories"] == ["finance", "politics"]
    assert "selected_category" not in shared_clean

    raw_files = sorted((run_dir / "raw").glob("tag-*/page-*.json"))
    assert len(raw_files) == len(TAG_CATEGORIES)
    assert not (run_dir / "manifest.json").exists()


@pytest.mark.asyncio
async def test_run_preserves_safe_failure_without_replacing_success(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "_business_today", lambda: DAY)
    monkeypatch.setattr(cli, "_utc_now", lambda: CAPTURED_AT)
    names = iter(["2026-07-28_080000_run1", "2026-07-28_080001_run2"])
    monkeypatch.setattr(cli, "_run_name", lambda _day: next(names))
    args = cli.parse_args(["--output-root", str(tmp_path)])
    assert await cli.run_async(args, client=FakeClient(complete_pages())) == 0
    failure = PolymarketApiError(
        "secret upstream detail", status=503, attempts=3, tag_id="100265"
    )

    exit_code = await cli.run_async(
        args, client=FakeClient(complete_pages(), failure=failure)
    )

    assert exit_code == 1
    assert (tmp_path / "2026-07-28_080000_run1/final.json").exists()
    error = tmp_path / "2026-07-28_080001_run2/error.json"
    assert json.loads(error.read_text())["message"] == "request failed"
    assert "secret" not in error.read_text()
    failed_run = tmp_path / "2026-07-28_080001_run2"
    assert not (failed_run / "clean.json").exists()
    assert not (failed_run / "final.json").exists()
