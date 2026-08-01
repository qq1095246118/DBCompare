import ast
from collections import Counter
from datetime import date
import json
from pathlib import Path

import pytest

from getMarket.Polymarket.tool import export_polymarket_market as cli
from getMarket.Polymarket.tool.final_contract import (
    CONTENT_FIELDS,
    CRYPTO_CONTENT_FIELDS,
    EXTRA_DATA_FIELDS,
    OUTER_FIELDS,
)
from getMarket.Polymarket.tool.market_filter import (
    CATEGORY_ORDER,
    TAG_CATEGORIES,
    MarketAccumulator,
)
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


class RecordingAccumulator(MarketAccumulator):
    def __init__(self):
        super().__init__()
        self.batch_sizes = []

    def add(self, rows):
        batch = list(rows)
        self.batch_sizes.append(len(batch))
        super().add(batch)


def page(tag_id, markets):
    return MarketPage(
        tag_id, None, CAPTURED_AT, f"https://example.test?tag_id={tag_id}",
        200, 1, {"markets": markets, "next_cursor": None},
    )


def source_market(
    market_id,
    *,
    category,
    liquidity=None,
    probability=None,
    volume=None,
):
    tag_slugs = ("stablecoins",) if category == "crypto" else (
        f"fixture-{category}",
    )
    row = {
        "id": market_id,
        "question": f"Question {market_id}?",
        "description": f"Description {market_id}",
        "active": True,
        "closed": False,
        "acceptingOrders": True,
        "tags": [{"slug": slug} for slug in tag_slugs],
        "outcomes": ["Yes", "No"],
        "events": [{
            "id": f"event-{market_id}",
            "title": f"Event {market_id}",
            "slug": f"event-{market_id}",
            "active": True,
            "closed": False,
        }],
    }
    if liquidity is not None:
        row["liquidity"] = str(liquidity)
    if probability is not None:
        row["outcomePrices"] = [str(probability), str(1 - probability)]
    if volume is not None:
        row["volume24hr"] = str(volume)
    return row


def complete_pages():
    rows_by_category = {}
    for category in CATEGORY_ORDER:
        rows_by_category[category] = [
            *[
                source_market(
                    f"{category}-liquidity-{index:02}",
                    category=category,
                    liquidity=100 - index,
                )
                for index in range(10)
            ],
            *[
                source_market(
                    f"{category}-probability-{index:02}",
                    category=category,
                    probability=0.99 - index / 100,
                )
                for index in range(10)
            ],
            *[
                source_market(
                    f"{category}-volume-{index:02}",
                    category=category,
                    volume=100 - index,
                )
                for index in range(10)
            ],
        ]
    politics_shared = source_market(
        "shared", category="politics", liquidity=1000,
    )
    finance_shared = source_market(
        "shared", category="finance", liquidity=900,
    )
    finance_shared["question"] = "Finance-specific shared question?"
    rows_by_category["politics"][0] = politics_shared
    rows_by_category["finance"][0] = finance_shared
    return {
        tag_id: [
            page(tag_id, rows_by_category[category][offset:offset + 7])
            for offset in range(0, len(rows_by_category[category]), 7)
        ]
        for tag_id, category in TAG_CATEGORIES.items()
    }


def test_parse_args_uses_project_output_root_and_fixed_ranking_size():
    args = cli.parse_args([])

    assert args.output_root == cli._PROJECT_ROOT / "getMarket" / "Polymarket" / "market"
    assert args.page_limit == 20
    assert not hasattr(args, "per_category")


def test_parse_args_rejects_removed_per_category_option():
    with pytest.raises(SystemExit):
        cli.parse_args(["--per-category", "10"])


@pytest.mark.parametrize("argv", [
    ["--timeout", "0"], ["--max-attempts", "0"], ["--page-limit", "0"],
    ["--page-limit", "21"],
    ["--business-date", "2026/07/28"],
])
def test_parse_args_rejects_invalid_values(argv):
    with pytest.raises(SystemExit):
        cli.parse_args(argv)


@pytest.mark.asyncio
async def test_run_collects_six_streams_and_publishes_three_rankings(
    tmp_path, monkeypatch,
):
    client = FakeClient(complete_pages())
    accumulator = RecordingAccumulator()
    monkeypatch.setattr(cli, "_business_today", lambda: DAY)
    monkeypatch.setattr(cli, "_utc_now", lambda: CAPTURED_AT)
    monkeypatch.setattr(cli, "_run_name", lambda _day: "2026-07-28_080000_run1")
    monkeypatch.setattr(
        cli, "MarketAccumulator", lambda: accumulator, raising=False,
    )

    exit_code = await cli.run_async(
        cli.parse_args([
            "--output-root", str(tmp_path), "--page-limit", "7",
        ]),
        client=client,
    )

    assert exit_code == 0
    assert client.requested_tags == list(TAG_CATEGORIES)
    assert client.requested_page_limits == [7] * len(TAG_CATEGORIES)
    assert accumulator.batch_sizes == [7, 7, 7, 7, 2] * len(TAG_CATEGORIES)
    run_dir = tmp_path / "2026-07-28_080000_run1"
    final_payload = json.loads((run_dir / "final.json").read_text())
    assert set(final_payload) == {"records"}
    records = final_payload["records"]
    assert len(records) == 180
    assert Counter(row["content"]["category"] for row in records) == Counter({
        category: 30 for category in CATEGORY_ORDER
    })
    assert [row["content"]["category"] for row in records] == [
        category for category in CATEGORY_ORDER for _ in range(30)
    ]
    assert [row["content"]["ranking_metric"] for row in records] == (
        ["liquidity"] * 10
        + ["dominant_probability"] * 10
        + ["volume24hr"] * 10
    ) * len(CATEGORY_ORDER)
    assert [row["content"]["ranking_priority"] for row in records] == (
        [1] * 10 + [2] * 10 + [3] * 10
    ) * len(CATEGORY_ORDER)
    assert [row["content"]["rank"] for row in records] == (
        list(range(1, 11)) * 3 * len(CATEGORY_ORDER)
    )
    assert all(
        (
            row["content"]["ranking_metric"],
            row["content"]["ranking_priority"],
            row["content"]["rank"],
        ) == (
            row["extra_data"]["ranking_metric"],
            row["extra_data"]["ranking_priority"],
            row["extra_data"]["rank"],
        )
        for row in records
    )
    assert [
        row["content"]["category"]
        for row in records
        if row["content"]["market_id"] == "shared"
    ] == ["politics", "finance"]

    representative = next(
        row for row in records
        if row["content"]["market_id"] == "politics-probability-00"
    )
    assert set(representative) == set(OUTER_FIELDS)
    assert set(representative["content"]) == set(CONTENT_FIELDS)
    assert set(representative["extra_data"]) == set(EXTRA_DATA_FIELDS)
    assert representative["id"] is None
    assert representative["content_hash"] is None
    assert representative["created_at"] == "2026-07-28T08:00:00+08:00"

    crypto_record = next(
        row for row in records
        if row["content"]["category"] == "crypto"
    )
    assert set(crypto_record["content"]) == set(CRYPTO_CONTENT_FIELDS)
    assert crypto_record["content"]["crypto_topics"] == ["stablecoin"]

    clean = json.loads((run_dir / "clean.json").read_text())
    assert len(clean) == 180
    assert len({row["market_id"] for row in clean}) == 179
    shared_clean = [row for row in clean if row["market_id"] == "shared"]
    assert [row["categories"] for row in shared_clean] == [
        ["politics"], ["finance"],
    ]
    assert [row["normalized_metrics"]["liquidity"] for row in shared_clean] == [
        "1000", "900",
    ]
    assert [row["source"]["tags"] for row in shared_clean] == [
        [{"slug": "fixture-politics"}],
        [{"slug": "fixture-finance"}],
    ]
    crypto_clean = next(row for row in clean if row["categories"] == ["crypto"])
    assert crypto_clean["crypto_topics"] == ["stablecoin"]
    assert crypto_clean["matched_crypto_tag_slugs"] == ["stablecoins"]
    assert crypto_clean["source"]["tags"] == [{"slug": "stablecoins"}]

    raw_files = sorted((run_dir / "raw").glob("tag-*/page-*.json"))
    assert len(raw_files) == 5 * len(TAG_CATEGORIES)
    assert not (run_dir / "manifest.json").exists()


@pytest.mark.asyncio
async def test_run_writes_raw_page_before_malformed_tags_fail_processing(
    tmp_path, monkeypatch,
):
    pages = complete_pages()
    first_tag = next(iter(TAG_CATEGORIES))
    pages[first_tag][0].payload["markets"][0]["tags"] = None
    monkeypatch.setattr(cli, "_business_today", lambda: DAY)
    monkeypatch.setattr(cli, "_utc_now", lambda: CAPTURED_AT)
    monkeypatch.setattr(cli, "_run_name", lambda _day: "malformed-tags")

    exit_code = await cli.run_async(
        cli.parse_args(["--output-root", str(tmp_path)]),
        client=FakeClient(pages),
    )

    assert exit_code == 1
    run_dir = tmp_path / "malformed-tags"
    assert (run_dir / f"raw/tag-{first_tag}/page-0001.json").exists()
    assert len(list((run_dir / "raw").glob("tag-*/page-*.json"))) == 1
    error = json.loads((run_dir / "error.json").read_text())
    assert error["stage"] == "processing"
    assert error["tag_id"] == first_tag
    assert error["message"] == "processing failed"
    assert not (run_dir / "clean.json").exists()
    assert not (run_dir / "final.json").exists()


@pytest.mark.asyncio
async def test_run_preserves_safe_failure_without_replacing_success(
    tmp_path, monkeypatch,
):
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
    assert len(list((run_dir / "raw").glob("tag-*/page-*.json"))) == (
        5 * len(TAG_CATEGORIES)
    )


def test_polymarket_api_collector_does_not_import_database_modules():
    imported_roots = set()
    for path in Path(cli.__file__).parent.glob("*.py"):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported_roots.update(
                    alias.name.split(".", 1)[0] for alias in node.names
                )
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported_roots.add(node.module.split(".", 1)[0])

    assert not imported_roots & {"getDB", "psycopg", "psycopg2", "asyncpg"}
