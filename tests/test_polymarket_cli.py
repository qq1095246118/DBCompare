from collections import Counter
from datetime import date
import json

import pytest

from getMarket.Polymarket.tool import export_polymarket_market as cli
from getMarket.Polymarket.tool.final_contract import (
    CONTENT_FIELDS,
    EXTRA_DATA_FIELDS,
    OUTER_FIELDS,
)
from getMarket.Polymarket.tool.market_filter import CATEGORY_ORDER, TAG_CATEGORIES
from getMarket.Polymarket.tool.polymarket_api import MarketPage, PolymarketApiError


DAY = date(2026, 7, 28)
CAPTURED_AT = "2026-07-28T00:00:00Z"


class FakeClient:
    def __init__(self, pages_by_tag, failure=None):
        self.pages_by_tag = pages_by_tag
        self.failure = failure
        self.requested_tags = []
        self.requested_page_limits = []

    async def collect_tag(self, tag_id, *, page_limit):
        self.requested_tags.append(tag_id)
        self.requested_page_limits.append(page_limit)
        if self.failure and tag_id == self.failure.tag_id:
            raise self.failure
        return self.pages_by_tag[tag_id]

    async def iter_tag(self, tag_id, *, page_limit):
        self.requested_tags.append(tag_id)
        self.requested_page_limits.append(page_limit)
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
        "question": f"Question {market_id}?",
        "active": True,
        "closed": False,
        "description": "ETF regulation update.",
        "liquidity": str(liquidity),
        "outcomes": ["Yes", "No"],
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
    assert args.per_category == 20


def test_parse_args_accepts_configurable_per_category_limit():
    assert cli.parse_args(["--per-category", "10"]).per_category == 10
    assert cli.parse_args(["--per-category", "100"]).per_category == 100


@pytest.mark.parametrize("argv", [
    ["--timeout", "0"], ["--max-attempts", "0"], ["--page-limit", "0"],
    ["--page-limit", "21"],
    ["--per-category", "0"],
    ["--per-category", "-1"],
    ["--per-category", "1.5"],
    ["--per-category", "ten"],
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
    final_payload = json.loads((run_dir / "final.json").read_text())
    assert set(final_payload) == {"records"}
    records = final_payload["records"]
    assert len(records) == 120
    assert Counter(row["content"]["category"] for row in records) == Counter({
        category: 20 for category in CATEGORY_ORDER
    })
    assert [row["content"]["category"] for row in records] == [
        category for category in CATEGORY_ORDER for _ in range(20)
    ]
    assert [row["content"]["rank"] for row in records] == (
        list(range(1, 21)) * len(CATEGORY_ORDER)
    )
    assert [
        row["content"]["category"]
        for row in records
        if row["content"]["market_id"] == "shared"
    ] == ["politics", "finance"]
    representative = next(
        row for row in records
        if row["content"]["market_id"] == "shared"
        and row["content"]["category"] == "politics"
    )
    assert set(representative) == set(OUTER_FIELDS)
    assert set(representative["content"]) == set(CONTENT_FIELDS)
    assert set(representative["extra_data"]) == set(EXTRA_DATA_FIELDS)
    assert representative["id"] is None
    assert representative["data_type"] == "PREDICTION_MARKET_SELECTION"
    assert representative["from_source"] == "polymarket"
    assert representative["title"] == "Question shared?"
    assert representative["created_at"] == "2026-07-28T08:00:00+08:00"
    assert representative["updated_at"] == "2026-07-28T08:00:00+08:00"
    assert representative["content"]["dominant_outcome"] == "Yes"
    assert representative["content"]["dominant_probability"] == 0.6
    assert representative["content"]["fetched_at"] == CAPTURED_AT
    assert representative["content"]["snapshot_date"] == DAY.isoformat()
    assert representative["extra_data"]["fetched_at"] == "2026-07-28T00:00:00+00:00"
    assert representative["extra_data"]["snapshot_date"] == DAY.isoformat()
    assert not {
        "selected_category", "rank_in_category", "selected_by", "priority",
    } & representative.keys()

    clean = json.loads((run_dir / "clean.json").read_text())
    assert len(clean) == 125
    assert len({row["market_id"] for row in clean}) == 125
    shared_clean = next(row for row in clean if row["market_id"] == "shared")
    assert shared_clean["categories"] == ["finance", "politics"]
    assert shared_clean["normalized_metrics"]["liquidity"] == "100"
    assert shared_clean["source"]["question"] == "Question shared?"
    assert not {"selected_category", "selected_by", "priority"} & shared_clean.keys()

    raw_files = sorted((run_dir / "raw").glob("tag-*/page-*.json"))
    assert len(raw_files) == len(TAG_CATEGORIES)
    assert not (run_dir / "manifest.json").exists()


@pytest.mark.asyncio
async def test_run_limits_final_per_category_without_truncating_clean(
    tmp_path, monkeypatch,
):
    client = FakeClient(complete_pages())
    monkeypatch.setattr(cli, "_business_today", lambda: DAY)
    monkeypatch.setattr(cli, "_utc_now", lambda: CAPTURED_AT)
    monkeypatch.setattr(cli, "_run_name", lambda _day: "2026-07-28_080000_limit10")

    exit_code = await cli.run_async(
        cli.parse_args([
            "--output-root", str(tmp_path),
            "--per-category", "10",
        ]),
        client=client,
    )

    assert exit_code == 0
    assert client.requested_tags == list(TAG_CATEGORIES)
    assert client.requested_page_limits == [20] * len(TAG_CATEGORIES)
    run_dir = tmp_path / "2026-07-28_080000_limit10"
    records = json.loads((run_dir / "final.json").read_text())["records"]
    assert len(records) == 60
    assert Counter(row["content"]["category"] for row in records) == Counter({
        category: 10 for category in CATEGORY_ORDER
    })
    assert [row["content"]["category"] for row in records] == [
        category for category in CATEGORY_ORDER for _ in range(10)
    ]
    assert [row["content"]["rank"] for row in records] == (
        list(range(1, 11)) * len(CATEGORY_ORDER)
    )
    assert [
        row["content"]["category"]
        for row in records
        if row["content"]["market_id"] == "shared"
    ] == ["politics", "finance"]

    clean = json.loads((run_dir / "clean.json").read_text())
    assert len(clean) == 125
    assert len({row["market_id"] for row in clean}) == 125


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


@pytest.mark.asyncio
async def test_run_treats_final_conversion_failure_as_processing_error(
    tmp_path, monkeypatch,
):
    monkeypatch.setattr(cli, "_business_today", lambda: DAY)
    monkeypatch.setattr(cli, "_utc_now", lambda: CAPTURED_AT)
    monkeypatch.setattr(cli, "_run_name", lambda _day: "failed-conversion")

    def fail_conversion(*args, **kwargs):
        raise ValueError("private conversion detail")

    monkeypatch.setattr(cli, "build_db_aligned_final", fail_conversion)

    exit_code = await cli.run_async(
        cli.parse_args(["--output-root", str(tmp_path)]),
        client=FakeClient(complete_pages()),
    )

    assert exit_code == 1
    run_dir = tmp_path / "failed-conversion"
    error_path = run_dir / "error.json"
    error_payload = json.loads(error_path.read_text())
    assert error_payload["stage"] == "processing"
    assert error_payload["type"] == "ValueError"
    assert error_payload["message"] == "processing failed"
    assert "private conversion detail" not in error_path.read_text()
    assert not (run_dir / "clean.json").exists()
    assert not (run_dir / "final.json").exists()
    assert len(list((run_dir / "raw").glob("tag-*/page-*.json"))) == len(TAG_CATEGORIES)
