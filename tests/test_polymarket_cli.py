from datetime import date
import json

import pytest

from getMarket.Polymarket.tool import export_polymarket_market as cli
from getMarket.Polymarket.tool.market_filter import TAG_CATEGORIES
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


def complete_pages():
    rows = [{
        "id": f"{index:03}", "active": True, "closed": False,
        "liquidity": str(index),
        "outcomePrices": [str(index / 100), str(1 - index / 100)],
        "volume24hr": str(1000 - index),
    } for index in range(1, 36)]
    return {
        tag_id: [page(tag_id, rows if tag_id == "2" else [])]
        for tag_id in TAG_CATEGORIES
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
    assert len(final) == 30
    assert len({row["market_id"] for row in final}) == 30
    assert [row["selected_by"] for row in final[:10]] == ["liquidity"] * 10
    clean = json.loads((run_dir / "clean.json").read_text())
    assert len(clean) == 35
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
