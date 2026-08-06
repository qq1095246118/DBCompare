# Polymarket Tag-Filtered Three-Ranking Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Collect six official Polymarket category Tag streams, filter crypto by exact official market Tag slugs, and write a DB-shaped `final.json` containing three priority-deduplicated Top-10 rankings per category.

**Architecture:** Keep the current page-at-a-time Gamma collector and unique run directory, request `market.tags`, write each parsed raw page, then validate and compact it into an incremental category accumulator. After all streams finish, run three fixed-capacity ranking passes with prior-winner exclusion before truncation and convert the selected rows through the pure API-only final-contract layer.

**Tech Stack:** Python 3.12 standard library (`argparse`, `asyncio`, `dataclasses`, `decimal`, `json`, `urllib`, `zoneinfo`), Polymarket Gamma `/markets/keyset`, `pytest`, and `pytest-asyncio`.

---

## Reference And Non-Negotiable Boundaries

The approved design is:

```text
docs/superpowers/specs/2026-08-01-polymarket-tag-filtered-three-ranking-design.md
```

It supersedes the July 28-31 Polymarket category, Top-20, final-output, and
`--per-category` designs. Those older plans are historical and must not be used
as requirements during implementation.

Use the repository interpreter for every command:

```bash
/Users/wrh/Downloads/DBCompare/.venv/bin/python
```

Do not edit `getDB/Polymarket/`. The API collector must not import `getDB`,
`psycopg`, or another PostgreSQL client. Do not edit, stage, or delete generated
artifacts under these existing directories:

```text
getDB/Polymarket/db/
getMarket/Polymarket/market/
getMarket/bubblemaps/market/
```

The six category streams, in request and output order, are fixed:

```python
TAG_CATEGORIES = {
    "2": "politics",
    "100265": "geopolitics",
    "100328": "economy",
    "120": "finance",
    "1401": "technology",
    "21": "crypto",
}
```

The fixed ranking passes are `liquidity` priority 1,
`dominant_probability` priority 2, and `volume24hr` priority 3. Each pass has
its own capacity of ten. Prior winners are excluded before the next pass is
truncated, so the lower-priority pass continues to rank 11 and later candidates
until it refills to ten or exhausts valid candidates.

For artifact ordering, a "raw page" means an HTTP 200 body that has already
decoded to the valid keyset envelope (`markets` list of objects plus a valid
cursor). Invalid JSON or an invalid envelope cannot be represented by the raw
JSON artifact and fails before a page file exists. Every envelope-valid page is
written before its required Tags relation is validated and before candidates
are compacted or filtered.

## File Map

- Modify `getMarket/Polymarket/tool/polymarket_api.py`: add
  `include_tag=true`, expose page-level `market.tags[].slug` validation, and
  preserve pagination and retry behavior.
- Modify `tests/test_polymarket_api.py`: cover request parameters, valid empty
  Tags, malformed Tags, and error metadata.
- Modify `tests/test_polymarket_live_smoke.py`: verify the live keyset response
  satisfies the Tags relation contract without asserting a live count.
- Modify `getMarket/Polymarket/tool/market_filter.py`: configure exactly six
  category streams, compact the required first-event and official Tag fields,
  replace description keywords with five exact crypto Tag rules, and retain
  topic/slug evidence.
- Modify `tests/test_polymarket_filter.py`: cover every allowed crypto slug,
  exchange-risk AND semantics, overlapping topics, nonmatching rejections,
  first-source behavior, and technology Tag removal.
- Modify `getMarket/Polymarket/tool/market_ranking.py`: remove the configurable
  category cap, give each metric a fixed capacity of ten, refill after
  priority exclusion, and assign ranking-local metadata.
- Modify `tests/test_polymarket_ranking.py`: cover the refill boundary, three
  independent ranks, short lists, invalid metrics, class-local uniqueness,
  cross-category preservation, and the 30/180 maxima.
- Modify `getMarket/Polymarket/tool/final_contract.py`: expand the current
  14/17/8 API shape to the verified 14/19-or-20/28 DB shape and map ranking,
  market, and first-event fields without reading the DB.
- Modify `tests/test_polymarket_final_contract.py`: assert exact field sets,
  values, nulls, crypto-only fields, input validation, order, and immutability.
- Modify `getMarket/Polymarket/tool/export_polymarket_market.py`: remove
  `--per-category`, keep complete six-stream pagination and page-at-a-time raw
  writes, and connect the revised filter/ranker/converter contracts.
- Modify `tests/test_polymarket_cli.py`: exercise the complete API-only pipeline,
  fixed three-ranking counts, artifacts, old-option rejection, and sanitized
  failures.
- Modify `getMarket/Polymarket/README.md`, `命令使用指南.md`, and `README.md`:
  replace every old multi-Tag technology, description-keyword, Top-20 fallback,
  configurable-cap, and 14/17/8 statement.

### Task 1: Request And Validate Official Market Tags

**Files:**
- Modify: `tests/test_polymarket_api.py`
- Modify: `getMarket/Polymarket/tool/polymarket_api.py`
- Modify: `tests/test_polymarket_live_smoke.py`

- [ ] **Step 1: Change the pagination fixture to the required response shape**

In `tests/test_polymarket_api.py`, add a small valid market helper after
`response()` and use it in every successful response fixture:

```python
def api_market(market_id="1", *, tags=None):
    return {
        "id": market_id,
        "tags": [{"id": "tag-1", "slug": "election"}] if tags is None else tags,
    }
```

For example, the two-page pagination test becomes:

```python
@pytest.mark.asyncio
async def test_collect_tag_follows_next_cursor_until_terminal_page():
    transport = FakeTransport([
        response(200, {"markets": [api_market("1")], "next_cursor": "abc"}),
        response(200, {"markets": [api_market("2")], "next_cursor": None}),
    ])
    client = PolymarketApiClient(transport=transport, retry_delay=0)

    pages = await client.collect_tag("2", page_limit=20)

    assert [page.cursor for page in pages] == [None, "abc"]
    assert [page.payload["markets"][0]["id"] for page in pages] == ["1", "2"]
    first = parse_qs(urlsplit(transport.requests[0].url).query)
    second = parse_qs(urlsplit(transport.requests[1].url).query)
    assert first == {
        "tag_id": ["2"],
        "active": ["true"],
        "closed": ["false"],
        "include_tag": ["true"],
        "limit": ["20"],
    }
    assert second["after_cursor"] == ["abc"]
    assert second["include_tag"] == ["true"]
```

Empty `markets` response fixtures remain valid as-is because there is no market
Tags relation to validate.

- [ ] **Step 2: Add failing response-contract tests**

Add these focused tests after the existing generic response-shape test:

```python
@pytest.mark.asyncio
async def test_iter_tag_yields_envelope_valid_page_before_tags_validation():
    transport = FakeTransport([
        response(200, {
            "markets": [{"id": "1", "tags": None}],
            "next_cursor": None,
        }),
    ])
    client = PolymarketApiClient(transport=transport, retry_delay=0)

    pages = [page async for page in client.iter_tag("21", page_limit=20)]

    assert pages[0].payload["markets"][0]["tags"] is None


@pytest.mark.asyncio
async def test_collect_tag_accepts_a_structurally_valid_empty_tags_relation():
    transport = FakeTransport([
        response(200, {"markets": [api_market(tags=[])], "next_cursor": None}),
    ])
    client = PolymarketApiClient(transport=transport, retry_delay=0)

    pages = await client.collect_tag("21", page_limit=20)

    assert pages[0].payload["markets"][0]["tags"] == []


@pytest.mark.asyncio
@pytest.mark.parametrize("market", [
    {"id": "1"},
    {"id": "1", "tags": None},
    api_market(tags={}),
    api_market(tags=[None]),
    api_market(tags=[{}]),
    api_market(tags=[{"slug": ""}]),
    api_market(tags=[{"slug": "   "}]),
    api_market(tags=[{"slug": 21}]),
])
async def test_collect_tag_rejects_missing_or_malformed_market_tags(market):
    transport = FakeTransport([
        response(200, {"markets": [market], "next_cursor": None}),
    ])
    client = PolymarketApiClient(transport=transport, retry_delay=0)

    with pytest.raises(PolymarketApiError, match="official response was invalid") as failure:
        await client.collect_tag("21", page_limit=20)

    assert type(failure.value).__name__ == "PolymarketTagsError"
    assert failure.value.status == 200
    assert failure.value.attempts == 1
    assert failure.value.tag_id == "21"
    assert failure.value.cursor is None
```

The first test uses the real client and transport path. It is an architecture
guard and already passes before the feature: `iter_tag()` must yield every
envelope-valid page without validating Tags so the exporter can persist it
first. The strict `collect_tag()` tests below it lock the separate convenience
method contract.

- [ ] **Step 3: Run the API tests and verify RED**

```bash
/Users/wrh/Downloads/DBCompare/.venv/bin/python -m pytest \
  tests/test_polymarket_api.py -q
```

Expected: the request-query assertion fails because `include_tag` is absent,
and every malformed-Tags case currently passes payload validation. The
`iter_tag()` architecture guard remains green.

- [ ] **Step 4: Add page-level Tags validation and the request parameter**

Keep `_validated_payload()` responsible for JSON, top-level `markets`, market
object, and cursor shape. This allows `iter_tag()` to yield a parsed page so the
exporter can persist it before processing its Tags. Add this page-level
processing error after the existing `PolymarketApiError` class, then add the
public validator:

```python
class PolymarketTagsError(PolymarketApiError):
    def __init__(self, message: str, *, page: MarketPage) -> None:
        super().__init__(
            message,
            status=page.status,
            attempts=page.attempts,
            tag_id=page.tag_id,
            cursor=page.cursor,
        )


def validate_market_tags(page: MarketPage) -> None:
    markets = page.payload["markets"]
    assert type(markets) is list
    malformed = any(
        type(market.get("tags")) is not list
        or any(
            type(tag) is not dict
            or not isinstance(tag.get("slug"), str)
            or not tag["slug"].strip()
            for tag in market["tags"]
        )
        for market in markets
    )
    if malformed:
        raise PolymarketTagsError(
            "official response was invalid", page=page,
        )
```

An empty `tags` list makes the nested `any()` false and is therefore valid. Keep
existing payload and `next_cursor` validation unchanged. In `_page()`, add the
new query value alongside the existing server filters:

```python
        query = {
            "tag_id": tag_id,
            "active": "true",
            "closed": "false",
            "include_tag": "true",
            "limit": str(page_limit),
        }
```

Change `collect_tag()` from a comprehension to a validating loop:

```python
    async def collect_tag(
        self,
        tag_id: str,
        *,
        page_limit: int,
        max_pages: int | None = None,
    ) -> list[MarketPage]:
        pages = []
        async for page in self.iter_tag(
            tag_id, page_limit=page_limit, max_pages=max_pages
        ):
            validate_market_tags(page)
            pages.append(page)
        return pages
```

The exporter will call the same validator immediately after writing each raw
page in Task 5. This explicit split satisfies both contracts: `collect_tag()`
is strict for callers and the collector preserves the malformed page before
failing with a processing-contract error.

- [ ] **Step 5: Run the API tests and verify GREEN**

Run the command from Step 3.

Expected: every test in `tests/test_polymarket_api.py` passes, including retry,
cursor, and page-limit regressions.

- [ ] **Step 6: Strengthen the read-only live contract test**

Replace the live test body with:

```python
@pytest.mark.live_polymarket
@pytest.mark.asyncio
async def test_configured_tags_return_valid_keyset_pages():
    client = PolymarketApiClient(max_attempts=2, timeout=20)
    observed_market_count = 0

    for tag_id in TAG_CATEGORIES:
        pages = await client.collect_tag(tag_id, page_limit=1, max_pages=1)
        assert len(pages) == 1
        markets = pages[0].payload["markets"]
        assert isinstance(markets, list)
        observed_market_count += len(markets)
        assert all(
            isinstance(market["tags"], list)
            and all(
                isinstance(tag, dict)
                and isinstance(tag["slug"], str)
                and bool(tag["slug"].strip())
                for tag in market["tags"]
            )
            for market in markets
        )

    assert observed_market_count > 0
```

Do not run this marked test until Task 7. It reaches the public live API and its
result can change independently of the repository. The final assertion avoids
a vacuous Tags check without pinning any category or total live market count.

- [ ] **Step 7: Review and commit the API contract**

```bash
git diff --check
git diff -- \
  tests/test_polymarket_api.py \
  tests/test_polymarket_live_smoke.py \
  getMarket/Polymarket/tool/polymarket_api.py
git status --short
git add \
  tests/test_polymarket_api.py \
  tests/test_polymarket_live_smoke.py \
  getMarket/Polymarket/tool/polymarket_api.py
git commit -m "feat: request official Polymarket tags"
```

### Task 2: Filter Crypto By Exact Tag Slugs

**Files:**
- Modify: `tests/test_polymarket_filter.py`
- Modify: `getMarket/Polymarket/tool/market_filter.py`

- [ ] **Step 1: Replace the test fixtures with Tag-aware sources**

Change the test helper so every direct filter input satisfies the required Tags
contract:

```python
def market(market_id="10", *, tag_slugs=("election",), **overrides):
    row = {
        "id": market_id,
        "active": True,
        "closed": False,
        "tags": [{"id": f"tag-{index}", "slug": slug} for index, slug in enumerate(tag_slugs)],
    }
    row.update(overrides)
    return row
```

Replace `test_technology_tags_merge_into_one_category` and
`test_merge_markets_unions_categories_and_tag_ids` with the exact mapping and
independent-category contracts:

```python
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
```

Add `pytest`, `CATEGORY_ORDER`, `CATEGORY_TAG_IDS`, `TAG_CATEGORIES`, and
`MarketAccumulator` to the test imports.

- [ ] **Step 2: Add a complete single-slug crypto topic table**

Define this expected table in `tests/test_polymarket_filter.py`; it is test data,
not an import from production configuration:

```python
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
```

Replace the old description-keyword tests with:

```python
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
```

`bankruptcy` and `insolvency` alone are rejected. Shared slugs such as `hack`
alone remain eligible only through `protocol_security`; they do not match
`exchange_risk` without `exchange` or `exchanges`.

- [ ] **Step 3: Add overlap, negative, and evidence tests**

Replace the existing `test_failed_crypto_rule_does_not_remove_other_category`
with the version below instead of defining it twice, and add the other tests
below the single-rule table:

```python
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
```

- [ ] **Step 4: Add compact-market and defensive Tags tests**

Replace the old nested-relation compaction test with the compact-event test
below. Delete `test_first_source_is_copied_and_conflicts_are_counted`; its
correct same-category replacement is included below with the incremental tests:

```python
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
```

- [ ] **Step 5: Run the filter tests and verify RED**

```bash
/Users/wrh/Downloads/DBCompare/.venv/bin/python -m pytest \
  tests/test_polymarket_filter.py -q
```

Expected: imports for `CATEGORY_TAG_IDS` fail first. After adding only the
constants, the old technology mappings, description matching, missing Tags
compaction, and topic evidence assertions still fail.

- [ ] **Step 6: Replace category and crypto configuration**

In `market_filter.py`, replace the old category-derived order and keyword tuple
with:

```python
CATEGORY_ORDER = (
    "politics", "geopolitics", "economy", "finance", "technology", "crypto",
)

TAG_CATEGORIES = {
    "2": "politics",
    "100265": "geopolitics",
    "100328": "economy",
    "120": "finance",
    "1401": "technology",
    "21": "crypto",
}

CATEGORY_TAG_IDS = {
    category: int(tag_id) for tag_id, category in TAG_CATEGORIES.items()
}

CRYPTO_TOPIC_ORDER = (
    "regulation", "etf", "exchange_risk", "stablecoin", "protocol_security",
)
_REGULATION_SLUGS = frozenset({
    "crypto-policy", "crypto-legal", "regulation", "regulations", "sec",
    "cftc", "legal", "legal-proceedings", "ban",
})
_ETF_SLUGS = frozenset({"etf", "etfs", "etf-approval"})
_EXCHANGE_SLUGS = frozenset({"exchange", "exchanges"})
_EXCHANGE_RISK_SLUGS = frozenset({
    "bankruptcy", "insolvency", "hack", "hacking", "exploit", "exploits",
    "cybersecurity", "data-breach",
})
_STABLECOIN_SLUGS = frozenset({
    "stablecoins", "tether", "usdt", "usdc", "depeg",
})
_PROTOCOL_SECURITY_SLUGS = frozenset({
    "protocol-risk", "protocol-upgrade", "hack", "hacking", "hacker",
    "exploit", "exploits", "cybersecurity", "data-breach", "bybit-hack",
})
```

- [ ] **Step 7: Compact official Tags and the first event**

Add `"acceptingOrders"` to `_COMPACT_FIELDS`. Keep `tags` and `events` out of
that flat tuple because they need explicit nested handling. Define:

```python
_EVENT_FIELDS = (
    "id", "title", "slug", "active", "closed", "startDate", "endDate",
)


def compact_market(source: dict[str, object]) -> dict[str, object]:
    if type(source) is not dict:
        raise TypeError("market source must be an object")
    compact = {
        field: deepcopy(source[field])
        for field in _COMPACT_FIELDS
        if field in source
    }
    if "tags" in source:
        compact["tags"] = deepcopy(source["tags"])
    events = source.get("events")
    if type(events) is list:
        compact["events"] = []
        if events and type(events[0]) is dict:
            compact["events"].append({
                field: deepcopy(events[0][field])
                for field in _EVENT_FIELDS
                if field in events[0]
            })
    return compact
```

- [ ] **Step 8: Implement exact topic matching and evidence**

Replace `_crypto_matches()` with these helpers:

```python
def _market_tag_slugs(source: dict[str, object]) -> set[str]:
    tags = source.get("tags")
    if type(tags) is not list or any(
        type(tag) is not dict
        or not isinstance(tag.get("slug"), str)
        or not tag["slug"].strip()
        for tag in tags
    ):
        raise ValueError("market tags must contain non-empty slugs")
    return {tag["slug"] for tag in tags}


def _crypto_matches(tag_slugs: set[str]) -> tuple[list[str], list[str]]:
    topics: list[str] = []
    evidence: set[str] = set()
    regulation = tag_slugs & _REGULATION_SLUGS
    if regulation:
        topics.append("regulation")
        evidence.update(regulation)
    etf = tag_slugs & _ETF_SLUGS
    if etf:
        topics.append("etf")
        evidence.update(etf)
    exchange = tag_slugs & _EXCHANGE_SLUGS
    exchange_risk = tag_slugs & _EXCHANGE_RISK_SLUGS
    if exchange and exchange_risk:
        topics.append("exchange_risk")
        evidence.update(exchange)
        evidence.update(exchange_risk)
    stablecoin = tag_slugs & _STABLECOIN_SLUGS
    if stablecoin:
        topics.append("stablecoin")
        evidence.update(stablecoin)
    protocol_security = tag_slugs & _PROTOCOL_SECURITY_SLUGS
    if protocol_security:
        topics.append("protocol_security")
        evidence.update(protocol_security)
    return topics, sorted(evidence)
```

Refactor the existing merge state into `MarketAccumulator`, validate Tags
immediately after validating each `source`, and use matched topic values instead
of description keywords. Use this complete stateful implementation:

```python
class MarketAccumulator:
    def __init__(self) -> None:
        self._merged: dict[tuple[str, str], dict[str, object]] = {}
        self._valid_ids: set[str] = set()
        self._conflicts = 0
        self._crypto_rejections = 0

    def add(self, rows: Iterable[TaggedMarket]) -> None:
        for row in rows:
            if not isinstance(row, TaggedMarket):
                raise TypeError("tagged market rows must be TaggedMarket values")
            if row.tag_id not in TAG_CATEGORIES:
                raise ValueError("tagged market uses an unknown tag ID")
            source = row.source
            if type(source) is not dict:
                raise TypeError("tagged market source must be an object")
            tag_slugs = _market_tag_slugs(source)
            market_id = source.get("id")
            if not isinstance(market_id, str) or not market_id.strip():
                continue
            if source.get("active") is not True or source.get("closed") is not False:
                continue
            self._valid_ids.add(market_id)

            category = TAG_CATEGORIES[row.tag_id]
            topics: list[str] = []
            matching_slugs: list[str] = []
            if category == "crypto":
                topics, matching_slugs = _crypto_matches(tag_slugs)
                if not topics:
                    self._crypto_rejections += 1
                    continue

            candidate_key = (category, market_id)
            entry = self._merged.get(candidate_key)
            if entry is None:
                entry = {
                    "market_id": market_id,
                    "categories": {category},
                    "matched_tag_ids": {row.tag_id},
                    "crypto_topics": set(topics),
                    "matched_crypto_tag_slugs": set(matching_slugs),
                    "source": deepcopy(source),
                }
                self._merged[candidate_key] = entry
            elif entry["source"] != source:
                self._conflicts += 1

    def result(self) -> MergeResult:
        output = []
        candidate_keys = sorted(
            self._merged,
            key=lambda key: (CATEGORY_ORDER.index(key[0]), key[1]),
        )
        for candidate_key in candidate_keys:
            entry = self._merged[candidate_key]
            market_id = candidate_key[1]
            output.append({
                "market_id": market_id,
                "categories": sorted(entry["categories"]),
                "matched_tag_ids": sorted(
                    entry["matched_tag_ids"], key=_tag_sort_key
                ),
                "crypto_topics": [
                    topic for topic in CRYPTO_TOPIC_ORDER
                    if topic in entry["crypto_topics"]
                ],
                "matched_crypto_tag_slugs": sorted(
                    entry["matched_crypto_tag_slugs"]
                ),
                "source": entry["source"],
            })
        return MergeResult(
            output,
            len(self._valid_ids),
            self._conflicts,
            self._crypto_rejections,
        )


def merge_markets(rows: Iterable[TaggedMarket]) -> MergeResult:
    accumulator = MarketAccumulator()
    accumulator.add(rows)
    return accumulator.result()
```

Preserve the current first-source conflict counter and the current rule that a
failed crypto row does not erase a previously accepted non-crypto membership.

- [ ] **Step 9: Run filter tests and the API regression**

```bash
/Users/wrh/Downloads/DBCompare/.venv/bin/python -m pytest \
  tests/test_polymarket_filter.py \
  tests/test_polymarket_api.py -q
```

Expected: every selected test passes. Confirm the crypto tests never inspect
`description`, `question`, Tag labels, or substrings to decide eligibility.

- [ ] **Step 10: Review and commit filtering**

```bash
git diff --check
git diff -- \
  tests/test_polymarket_filter.py \
  getMarket/Polymarket/tool/market_filter.py
git status --short
git add \
  tests/test_polymarket_filter.py \
  getMarket/Polymarket/tool/market_filter.py
git commit -m "feat: filter crypto markets by tag slug"
```

### Task 3: Produce Three Fixed Top-10 Rankings With Refill

**Files:**
- Modify: `tests/test_polymarket_ranking.py`
- Modify: `getMarket/Polymarket/tool/market_ranking.py`

- [ ] **Step 1: Update the candidate fixture to the new filter contract**

Keep the existing metric-normalization tests. Change the candidate fixture's
evidence fields to:

```python
    return {
        "market_id": market_id,
        "categories": categories if isinstance(categories, str) else list(categories),
        "matched_tag_ids": ["2"],
        "crypto_topics": [],
        "matched_crypto_tag_slugs": [],
        "source": source,
    }
```

Import `RANKING_LIMIT` with `normalize_metrics` and
`select_ranked_markets`.

- [ ] **Step 2: Replace fallback-limit tests with independent-ranking tests**

Remove every old single-capacity test, including these names even though some
rely on the default instead of passing `per_category`:

```text
test_liquidity_fills_politics_category_before_fallbacks
test_fallbacks_fill_remaining_capacity_with_continuous_category_ranks
test_short_category_finishes_without_duplicate_ids
test_each_configured_category_has_an_independent_limit_and_fixed_order
test_multi_category_market_is_selected_once_per_category_in_fixed_order
test_metric_ties_use_market_id_ascending
test_invalid_per_category_limit_is_rejected
```

Add:

```python
def test_each_metric_has_an_independent_top_ten_and_rank_sequence():
    rows = [
        *[
            candidate(f"liquidity-{index:02}", liquidity=100 - index)
            for index in range(12)
        ],
        *[
            candidate(f"probability-{index:02}", probability=1 - index / 100)
            for index in range(12)
        ],
        *[
            candidate(f"volume-{index:02}", volume=100 - index)
            for index in range(12)
        ],
    ]

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


def test_lower_priority_excludes_first_then_refills_from_later_candidates():
    rows = [
        candidate(
            f"{index:02}",
            liquidity=100 - index,
            probability=1 - index / 100,
        )
        for index in range(20)
    ]

    result = select_ranked_markets(rows)

    assert result.rankings["politics"]["liquidity"]["selected_market_ids"] == [
        f"{index:02}" for index in range(10)
    ]
    assert result.rankings["politics"]["dominant_probability"][
        "selected_market_ids"
    ] == [f"{index:02}" for index in range(10, 20)]
    assert result.rankings["politics"]["dominant_probability"][
        "excluded_by_priorities"
    ] == [f"{index:02}" for index in range(10)]
    assert [
        row["rank"] for row in result.selected
        if row["ranking_metric"] == "dominant_probability"
    ] == list(range(1, 11))
```

This second test is the authoritative resolution of the deduplication-order
ambiguity: candidates 10-19 must refill the probability list after liquidity
winners 00-09 are excluded.

- [ ] **Step 3: Add short-list, tie, metric-isolation, and category tests**

Add these tests. The tie test below replaces the deleted existing function of
the same name; do not retain two definitions:

```python
def test_short_rankings_succeed_without_padding_or_duplicate_ids():
    rows = [
        candidate("liquidity-only", liquidity=10),
        candidate("probability-only", probability=0.9),
        candidate("volume-only", volume=8),
    ]

    result = select_ranked_markets(rows)

    assert [row["market_id"] for row in result.selected] == [
        "liquidity-only", "probability-only", "volume-only",
    ]
    assert [row["rank"] for row in result.selected] == [1, 1, 1]
    assert len({row["market_id"] for row in result.selected}) == 3


def test_invalid_metric_excludes_only_that_ranking():
    row = candidate("market-1", liquidity="bad", probability=0.8, volume=12)

    result = select_ranked_markets([row])

    assert result.rankings["politics"]["liquidity"]["selected_market_ids"] == []
    assert result.rankings["politics"]["dominant_probability"][
        "selected_market_ids"
    ] == ["market-1"]
    assert result.rankings["politics"]["volume24hr"]["selected_market_ids"] == []


def test_metric_ties_use_market_id_ascending():
    rows = [candidate(index, liquidity=10) for index in range(12, 0, -1)]

    result = select_ranked_markets(rows)

    assert [row["market_id"] for row in result.selected] == [
        f"{index:03}" for index in range(1, 11)
    ]


def test_multi_category_market_is_selected_once_per_category():
    rows = [
        candidate("shared", liquidity=10, categories=("finance",)),
        candidate("shared", liquidity=9, categories=("politics",)),
    ]

    result = select_ranked_markets(rows)

    assert [row["selected_category"] for row in result.selected] == [
        "politics", "finance",
    ]
    assert [row["market_id"] for row in result.selected] == ["shared", "shared"]
    assert [row["rank"] for row in result.selected] == [1, 1]


def test_selected_rows_use_only_current_ranking_metadata_names():
    row = select_ranked_markets([candidate("market-1", liquidity=1)]).selected[0]

    assert (
        row["ranking_metric"], row["ranking_priority"], row["rank"]
    ) == ("liquidity", 1, 1)
    assert not {"selected_by", "priority", "rank_in_category", "rank_in_priority"} & row.keys()


def test_duplicate_market_id_is_rejected_only_within_the_same_category():
    with pytest.raises(ValueError, match="category and market ID pairs must be unique"):
        select_ranked_markets([
            candidate("shared", liquidity=2, categories=("politics",)),
            candidate("shared", liquidity=1, categories=("politics",)),
        ])


@pytest.mark.parametrize("market_id", [None, "", "   "])
def test_market_id_must_be_a_non_whitespace_string(market_id):
    with pytest.raises(ValueError, match="non-whitespace string"):
        select_ranked_markets([candidate(market_id, liquidity=1)])


def test_ranking_does_not_mutate_candidate_rows():
    rows = [candidate("market-1", liquidity=1)]
    before = deepcopy(rows)

    select_ranked_markets(rows)

    assert rows == before
```

Add `deepcopy` from `copy` to the test imports. The preceding multi-category
test proves the same ID remains valid in two different category candidates.
Use `test_selected_rows_use_only_current_ranking_metadata_names` as the
replacement for the old `test_selected_rows_do_not_include_rank_in_priority`;
do not retain both.
Update the existing invalid-category parametrization to include
`["politics", "finance"]` and expect `exactly one configured category`; each
candidate row now belongs to one independent category pool.

The invalid-metric test also proves priority exclusion: after probability
selects `market-1`, volume cannot select the same ID.

- [ ] **Step 4: Add the fixed 30-per-category and 180-total boundary test**

```python
def test_six_categories_can_emit_the_fixed_maximum_of_180_records():
    rows = []
    for category in CATEGORY_ORDER:
        rows.extend(
            candidate(
                f"{category}-liquidity-{index:02}",
                liquidity=100 - index,
                categories=(category,),
            )
            for index in range(10)
        )
        rows.extend(
            candidate(
                f"{category}-probability-{index:02}",
                probability=1 - index / 100,
                categories=(category,),
            )
            for index in range(10)
        )
        rows.extend(
            candidate(
                f"{category}-volume-{index:02}",
                volume=100 - index,
                categories=(category,),
            )
            for index in range(10)
        )

    result = select_ranked_markets(rows)

    assert len(result.selected) == 180
    assert [row["selected_category"] for row in result.selected] == [
        category for category in CATEGORY_ORDER for _ in range(30)
    ]
    assert all(
        len({
            row["market_id"] for row in result.selected
            if row["selected_category"] == category
        }) == 30
        for category in CATEGORY_ORDER
    )
    assert [row["rank"] for row in result.selected] == (
        list(range(1, 11)) * 3 * len(CATEGORY_ORDER)
    )
```

Keep the existing metric-parsing and invalid-category coverage. The duplicate
key and non-mutating cases are added explicitly in Step 3.

- [ ] **Step 5: Run the ranking tests and verify RED**

```bash
/Users/wrh/Downloads/DBCompare/.venv/bin/python -m pytest \
  tests/test_polymarket_ranking.py -q
```

Expected: `RANKING_LIMIT` is missing; after adding only that constant, current
selection stops after one total category capacity and emits continuous
`rank_in_category` values instead of three ranking-local sequences.

- [ ] **Step 6: Implement fixed per-ranking capacity and metadata**

Add the fixed constant next to `METRIC_PRIORITIES`:

```python
METRIC_PRIORITIES = ("liquidity", "dominant_probability", "volume24hr")
RANKING_LIMIT = 10
```

Remove the `per_category` keyword and its validation from the public function:

```python
def select_ranked_markets(
    markets: Iterable[Mapping[str, object]],
) -> RankingResult:
```

Replace the global-ID normalization loop with category-keyed validation so the
same market can carry independent sources in different category pools:

```python
    candidates: list[dict[str, object]] = []
    seen: set[tuple[str, str]] = set()
    for market in markets:
        if not isinstance(market, Mapping):
            raise TypeError("ranked markets must be mappings")
        market_id = market.get("market_id")
        source = market.get("source")
        if not isinstance(market_id, str) or not market_id.strip():
            raise ValueError("ranked market ID must be a non-whitespace string")
        if not isinstance(source, Mapping):
            raise ValueError("ranked market source must be a mapping")
        categories = market.get("categories")
        if (
            type(categories) is not list
            or len(categories) != 1
            or type(categories[0]) is not str
            or categories[0] not in CATEGORY_ORDER
        ):
            raise ValueError("ranked market must have exactly one configured category")
        candidate_key = (categories[0], market_id)
        if candidate_key in seen:
            raise ValueError("category and market ID pairs must be unique")
        seen.add(candidate_key)
        normalized = deepcopy(dict(market))
        normalized["normalized_metrics"] = normalize_metrics(source)
        candidates.append(normalized)
    candidates.sort(key=lambda row: (
        CATEGORY_ORDER.index(row["categories"][0]), row["market_id"],
    ))
```

Keep `_rank()` metric sorting, category iteration, and the category-local
`selected_ids` set. Replace the inner metric loop with:

```python
        for priority, metric in enumerate(METRIC_PRIORITIES, start=1):
            eligible, excluded = _rank(category_candidates, metric, selected_ids)
            winners = eligible[:RANKING_LIMIT]
            winner_ids = [row["market_id"] for row in winners]
            rankings[category][metric] = {
                "priority": priority,
                "selected_market_ids": winner_ids,
                "selected_metrics": [
                    row["normalized_metrics"][metric] for row in winners
                ],
                "excluded_by_priorities": excluded,
            }
            for rank, row in enumerate(winners, start=1):
                final = deepcopy(row)
                final.update({
                    "selected_category": category,
                    "ranking_metric": metric,
                    "ranking_priority": priority,
                    "rank": rank,
                })
                selected.append(final)
                selected_ids.add(row["market_id"])
```

Do not truncate `eligible` before excluding `selected_ids`; `_rank()` already
applies exclusion before sorting and this order is what provides refill.

- [ ] **Step 7: Run focused ranking and filter tests**

```bash
/Users/wrh/Downloads/DBCompare/.venv/bin/python -m pytest \
  tests/test_polymarket_ranking.py \
  tests/test_polymarket_filter.py -q
```

Expected: all tests pass. Inspect the 180-record test failure directly if the
count is anything other than exactly 180; do not relax the assertion.

- [ ] **Step 8: Review and commit ranking behavior**

```bash
git diff --check
git diff -- \
  tests/test_polymarket_ranking.py \
  getMarket/Polymarket/tool/market_ranking.py
git status --short
git add \
  tests/test_polymarket_ranking.py \
  getMarket/Polymarket/tool/market_ranking.py
git commit -m "feat: rank three Polymarket top tens"
```

### Task 4: Align Final Records With The Current DB Shape

**Files:**
- Modify: `tests/test_polymarket_final_contract.py`
- Modify: `getMarket/Polymarket/tool/final_contract.py`

- [ ] **Step 1: Update selected-record fixtures to ranking-local metadata**

Replace `rank_in_category` in the `selected()` helper with the three required
fields and add enough source data to exercise the current DB contract:

```python
def selected(**overrides):
    item = {
        "market_id": "market-1",
        "selected_category": "politics",
        "ranking_metric": "liquidity",
        "ranking_priority": 1,
        "rank": 1,
        "crypto_topics": [],
        "source": {
            "question": "Will this pass?",
            "description": "Resolution details.",
            "acceptingOrders": True,
            "active": True,
            "closed": False,
            "startDate": "2026-01-01T00:00:00Z",
            "endDate": "2026-12-31T00:00:00Z",
            "resolutionSource": "https://example.test/rules",
            "outcomes": ["Yes", "No"],
            "outcomePrices": ["0.4", "0.6"],
            "events": [{
                "id": "event-1",
                "title": "Event title",
                "slug": "event-slug",
                "active": True,
                "closed": False,
            }],
        },
        "normalized_metrics": {
            "liquidity": "1200.5",
            "volume24hr": "42",
            "dominant_probability": "0.6",
        },
    }
    item.update(overrides)
    return item
```

Import the new `CRYPTO_CONTENT_FIELDS` constant from `final_contract`.

- [ ] **Step 2: Replace exact field-order assertions with the verified shape**

Use these assertions in `test_constants_have_the_exact_db_aligned_field_order`:

```python
    assert OUTER_FIELDS == (
        "id", "data_type", "title", "summary", "content", "from_source",
        "source_url", "content_hash", "extra_data", "published_at", "created_at",
        "updated_at", "tags", "source_updated_at",
    )
    assert CONTENT_FIELDS == (
        "category", "dominant_outcome", "dominant_probability", "event_id",
        "fetched_at", "liquidity", "market_id", "market_question", "outcome",
        "probability", "rank", "ranking_metric", "ranking_priority",
        "record_type", "snapshot_date", "title", "volume24hr", "window_end",
        "window_start",
    )
    assert CRYPTO_CONTENT_FIELDS == (
        "category", "crypto_topics", "dominant_outcome", "dominant_probability",
        "event_id", "fetched_at", "liquidity", "market_id", "market_question",
        "outcome", "probability", "rank", "ranking_metric", "ranking_priority",
        "record_type", "snapshot_date", "title", "volume24hr", "window_end",
        "window_start",
    )
    assert EXTRA_DATA_FIELDS == (
        "acceptingOrders", "active", "category", "closed", "description",
        "dominant_outcome", "dominant_probability", "end_date", "endpoint",
        "event_active", "event_closed", "event_id", "fetched_at", "liquidity",
        "market_id", "outcome_prices", "outcomes", "rank", "ranking_metric",
        "ranking_priority", "resolution_source", "snapshot_date", "source_tag",
        "start_date", "title", "volume24hr", "window_end", "window_start",
    )
```

- [ ] **Step 3: Assert complete non-crypto field mapping**

Replace the old 14/17/8 expected-record test with exact shape assertions plus
this complete content and `extra_data` expectation:

```python
def test_builds_one_complete_current_db_aligned_record():
    record = build([selected()])["records"][0]

    assert set(record) == set(OUTER_FIELDS)
    assert set(record["content"]) == set(CONTENT_FIELDS)
    assert set(record["extra_data"]) == set(EXTRA_DATA_FIELDS)
    assert record["id"] is None
    assert record["data_type"] == "PREDICTION_MARKET_SELECTION"
    assert record["title"] == "Event title"
    assert record["summary"] == "Will this pass?"
    assert record["from_source"] == "polymarket"
    assert record["source_url"] == "https://polymarket.com/event/event-slug"
    assert record["content_hash"] is None
    assert record["published_at"] is None
    assert record["created_at"] == "2026-07-28T08:00:00+08:00"
    assert record["updated_at"] == "2026-07-28T08:00:00+08:00"
    assert record["tags"] == [
        "active", "category:politics", "prediction", "selected-market",
    ]
    assert record["source_updated_at"] is None
    assert record["content"] == {
        "category": "politics",
        "dominant_outcome": "No",
        "dominant_probability": 0.6,
        "event_id": "event-1",
        "fetched_at": "2026-07-28T00:00:00Z",
        "liquidity": 1200.5,
        "market_id": "market-1",
        "market_question": "Will this pass?",
        "outcome": "No",
        "probability": 0.6,
        "rank": 1,
        "ranking_metric": "liquidity",
        "ranking_priority": 1,
        "record_type": "prediction_market_selection",
        "snapshot_date": "2026-07-28",
        "title": "Event title",
        "volume24hr": 42.0,
        "window_end": None,
        "window_start": None,
    }
    assert record["extra_data"] == {
        "acceptingOrders": True,
        "active": True,
        "category": "politics",
        "closed": False,
        "description": "Resolution details.",
        "dominant_outcome": "No",
        "dominant_probability": 0.6,
        "end_date": "2026-12-31T00:00:00Z",
        "endpoint": "/markets/keyset",
        "event_active": True,
        "event_closed": False,
        "event_id": "event-1",
        "fetched_at": "2026-07-28T00:00:00+00:00",
        "liquidity": 1200.5,
        "market_id": "market-1",
        "outcome_prices": ["0.4", "0.6"],
        "outcomes": ["Yes", "No"],
        "rank": 1,
        "ranking_metric": "liquidity",
        "ranking_priority": 1,
        "resolution_source": "https://example.test/rules",
        "snapshot_date": "2026-07-28",
        "source_tag": 2,
        "start_date": "2026-01-01T00:00:00Z",
        "title": "Event title",
        "volume24hr": 42.0,
        "window_end": None,
        "window_start": None,
    }
```

- [ ] **Step 4: Add crypto shape and ranking-consistency tests**

```python
def test_crypto_content_adds_only_canonical_topics_to_the_non_crypto_shape():
    row = selected(
        selected_category="crypto",
        crypto_topics=["regulation", "protocol_security"],
        ranking_metric="dominant_probability",
        ranking_priority=2,
        rank=3,
    )

    record = build([row])["records"][0]

    assert set(record["content"]) == set(CRYPTO_CONTENT_FIELDS)
    assert record["content"]["crypto_topics"] == [
        "regulation", "protocol_security",
    ]
    assert record["extra_data"]["source_tag"] == 21
    assert (
        record["content"]["ranking_metric"],
        record["content"]["ranking_priority"],
        record["content"]["rank"],
    ) == ("dominant_probability", 2, 3)
    assert (
        record["extra_data"]["ranking_metric"],
        record["extra_data"]["ranking_priority"],
        record["extra_data"]["rank"],
    ) == ("dominant_probability", 2, 3)


def test_non_crypto_content_never_contains_crypto_topics():
    content = build([selected(crypto_topics=["regulation"])])["records"][0]["content"]

    assert set(content) == set(CONTENT_FIELDS)
    assert "crypto_topics" not in content
```

- [ ] **Step 5: Update null, validation, and duplicate-order tests**

Retain the existing timestamp, numeric, outcome, JSON-safety, and immutability
tests. Extend the missing-optional-values test to remove the event and assert:

```python
    row["source"].update({
        "question": None,
        "description": None,
        "acceptingOrders": None,
        "startDate": None,
        "endDate": None,
        "resolutionSource": None,
        "events": [],
    })
    record = build([row])["records"][0]
    assert record["title"] is None
    assert record["summary"] is None
    assert record["source_url"] is None
    assert record["content"]["event_id"] is None
    assert record["content"]["title"] is None
    assert record["extra_data"]["event_id"] is None
    assert record["extra_data"]["event_active"] is None
    assert record["extra_data"]["event_closed"] is None
    assert record["extra_data"]["title"] is None
```

Replace required-field validation cases with:

```python
@pytest.mark.parametrize(("override", "message"), [
    ({"market_id": "  "}, "market_id must be a non-whitespace string"),
    ({"selected_category": "sports"}, "selected_category must be configured"),
    ({"ranking_metric": "volume"}, "ranking metadata is invalid"),
    ({"ranking_priority": 3}, "ranking metadata is invalid"),
    (
        {"selected_category": "crypto", "crypto_topics": []},
        "crypto selection must contain at least one topic",
    ),
    ({"rank": 0}, "rank must be between 1 and 10"),
    ({"rank": 11}, "rank must be between 1 and 10"),
    ({"rank": True}, "rank must be between 1 and 10"),
    ({"source": None}, "source must be a mapping"),
])
def test_rejects_invalid_required_selected_fields(override, message):
    with pytest.raises(ValueError, match=f"^{message}$"):
        build([selected(**override)])
```

Replace the duplicate-category order test with an unsorted input that proves
the final converter independently enforces the published output order:

```python
def test_sorts_duplicate_market_ids_by_category_priority_and_rank():
    rows = [
        selected(selected_category="finance", rank=2),
        selected(selected_category="politics", rank=1),
    ]

    output = build(rows)["records"]

    assert [
        (
            row["content"]["market_id"],
            row["content"]["category"],
            row["content"]["ranking_priority"],
            row["content"]["rank"],
        )
        for row in output
    ] == [
        ("market-1", "politics", 1, 1),
        ("market-1", "finance", 1, 2),
    ]
```

Extend `test_rejects_stringifiable_objects_as_metrics_and_outcome_prices` so
the new nested array field is also safe:

```python
    record = build([row])["records"][0]
    assert record["content"]["dominant_outcome"] is None
    assert record["extra_data"]["outcome_prices"] is None
```

Add a non-standard-number case:

```python
def test_non_json_safe_source_arrays_become_null():
    row = selected(source={
        "question": "Unsafe array?",
        "outcomes": ["Yes", "No"],
        "outcomePrices": [float("nan"), "0"],
    })

    record = build([row])["records"][0]

    assert record["content"]["dominant_outcome"] is None
    assert record["extra_data"]["outcome_prices"] is None
```

- [ ] **Step 6: Run final-contract tests and verify RED**

```bash
/Users/wrh/Downloads/DBCompare/.venv/bin/python -m pytest \
  tests/test_polymarket_final_contract.py -q
```

Expected: `CRYPTO_CONTENT_FIELDS` is missing, the selected-item validator still
requires `rank_in_category`, and current output lacks the verified DB fields.

- [ ] **Step 7: Replace field constants and selected-item validation**

In `final_contract.py`, import the category/metric contracts:

```python
from getMarket.Polymarket.tool.market_filter import (
    CATEGORY_ORDER,
    CATEGORY_TAG_IDS,
    CRYPTO_TOPIC_ORDER,
)
from getMarket.Polymarket.tool.market_ranking import METRIC_PRIORITIES, RANKING_LIMIT
```

Replace the field constants with these literal tuples so accidental ordering
changes are visible in review:

```python
OUTER_FIELDS = (
    "id", "data_type", "title", "summary", "content", "from_source",
    "source_url", "content_hash", "extra_data", "published_at", "created_at",
    "updated_at", "tags", "source_updated_at",
)

CONTENT_FIELDS = (
    "category", "dominant_outcome", "dominant_probability", "event_id",
    "fetched_at", "liquidity", "market_id", "market_question", "outcome",
    "probability", "rank", "ranking_metric", "ranking_priority", "record_type",
    "snapshot_date", "title", "volume24hr", "window_end", "window_start",
)

CRYPTO_CONTENT_FIELDS = (
    "category", "crypto_topics", "dominant_outcome", "dominant_probability",
    "event_id", "fetched_at", "liquidity", "market_id", "market_question",
    "outcome", "probability", "rank", "ranking_metric", "ranking_priority",
    "record_type", "snapshot_date", "title", "volume24hr", "window_end",
    "window_start",
)

EXTRA_DATA_FIELDS = (
    "acceptingOrders", "active", "category", "closed", "description",
    "dominant_outcome", "dominant_probability", "end_date", "endpoint",
    "event_active", "event_closed", "event_id", "fetched_at", "liquidity",
    "market_id", "outcome_prices", "outcomes", "rank", "ranking_metric",
    "ranking_priority", "resolution_source", "snapshot_date", "source_tag",
    "start_date", "title", "volume24hr", "window_end", "window_start",
)
```

Replace `_validate_item()` with:

```python
def _validate_item(
    item: object,
) -> tuple[
    str, str, str, int, int, list[str], Mapping[object, object],
    Mapping[object, object] | None,
]:
    if not isinstance(item, Mapping):
        raise ValueError("selected item must be a mapping")
    market_id = item.get("market_id")
    if not isinstance(market_id, str) or not market_id.strip():
        raise ValueError("market_id must be a non-whitespace string")
    category = item.get("selected_category")
    if not isinstance(category, str) or category not in CATEGORY_ORDER:
        raise ValueError("selected_category must be configured")
    metric = item.get("ranking_metric")
    priority = item.get("ranking_priority")
    if (
        not isinstance(metric, str)
        or metric not in METRIC_PRIORITIES
        or type(priority) is not int
        or priority != METRIC_PRIORITIES.index(metric) + 1
    ):
        raise ValueError("ranking metadata is invalid")
    rank = item.get("rank")
    if type(rank) is not int or not 1 <= rank <= RANKING_LIMIT:
        raise ValueError("rank must be between 1 and 10")
    topics = item.get("crypto_topics")
    if type(topics) is not list or any(
        not isinstance(topic, str) or topic not in CRYPTO_TOPIC_ORDER
        for topic in topics
    ):
        raise ValueError("crypto_topics must be a configured topic list")
    canonical_topics = [topic for topic in CRYPTO_TOPIC_ORDER if topic in topics]
    if topics != canonical_topics:
        raise ValueError("crypto_topics must use canonical order without duplicates")
    if category == "crypto" and not topics:
        raise ValueError("crypto selection must contain at least one topic")
    source = item.get("source")
    if not isinstance(source, Mapping):
        raise ValueError("source must be a mapping")
    metrics = item.get("normalized_metrics")
    return (
        market_id, category, metric, priority, rank, list(topics), source,
        metrics if isinstance(metrics, Mapping) else None,
    )
```

- [ ] **Step 8: Add safe optional-value and first-event helpers**

Add these pure helpers near `_array()`:

```python
def _string(value: object) -> str | None:
    return value if isinstance(value, str) else None


def _boolean(value: object) -> bool | None:
    return value if type(value) is bool else None


def _first_event(source: Mapping[object, object]) -> Mapping[object, object] | None:
    events = source.get("events")
    if type(events) is list and events and isinstance(events[0], Mapping):
        return events[0]
    return None


def _event_url(event: Mapping[object, object] | None) -> str | None:
    slug = _string(event.get("slug")) if event is not None else None
    return f"https://polymarket.com/event/{slug}" if slug and slug.strip() else None
```

Import `deepcopy` and replace `_array()` with this independent-copy version:

```python
def _array(value: object) -> list[object] | None:
    if isinstance(value, list):
        decoded = value
    elif isinstance(value, str):
        try:
            decoded = json.loads(value)
        except (TypeError, json.JSONDecodeError):
            return None
    else:
        return None
    if not isinstance(decoded, list):
        return None
    try:
        json.dumps(decoded, allow_nan=False)
    except (TypeError, ValueError):
        return None
    return deepcopy(decoded)
```

- [ ] **Step 9: Build the expanded content and extra-data objects**

Inside `build_db_aligned_final()`, unpack the new validator result and compute
the shared values:

```python
        (
            market_id, category, ranking_metric, ranking_priority, rank,
            crypto_topics, source, metrics,
        ) = _validate_item(item)
        question = _string(source.get("question"))
        event = _first_event(source)
        event_id = _string(event.get("id")) if event is not None else None
        event_title = _string(event.get("title")) if event is not None else None
        title = event_title if event_title is not None else question
        liquidity = _json_number(metrics.get("liquidity")) if metrics is not None else None
        volume = _json_number(metrics.get("volume24hr")) if metrics is not None else None
        probability = (
            _json_number(metrics.get("dominant_probability"), maximum=Decimal("1"))
            if metrics is not None else None
        )
        outcome = _dominant_outcome(source)
```

Build the nineteen common content fields:

```python
        content = {
            "category": category,
            "dominant_outcome": outcome,
            "dominant_probability": probability,
            "event_id": event_id,
            "fetched_at": content_fetched_at,
            "liquidity": liquidity,
            "market_id": market_id,
            "market_question": question,
            "outcome": outcome,
            "probability": probability,
            "rank": rank,
            "ranking_metric": ranking_metric,
            "ranking_priority": ranking_priority,
            "record_type": "prediction_market_selection",
            "snapshot_date": snapshot_date,
            "title": title,
            "volume24hr": volume,
            "window_end": None,
            "window_start": None,
        }
        if category == "crypto":
            content = {
                "category": category,
                "crypto_topics": crypto_topics,
                **{key: value for key, value in content.items() if key != "category"},
            }
```

Build all twenty-eight `extra_data` fields:

```python
        extra_data = {
            "acceptingOrders": _boolean(source.get("acceptingOrders")),
            "active": _boolean(source.get("active")),
            "category": category,
            "closed": _boolean(source.get("closed")),
            "description": _string(source.get("description")),
            "dominant_outcome": outcome,
            "dominant_probability": probability,
            "end_date": _string(source.get("endDate")),
            "endpoint": "/markets/keyset",
            "event_active": _boolean(event.get("active")) if event is not None else None,
            "event_closed": _boolean(event.get("closed")) if event is not None else None,
            "event_id": event_id,
            "fetched_at": extra_fetched_at,
            "liquidity": liquidity,
            "market_id": market_id,
            "outcome_prices": _array(source.get("outcomePrices")),
            "outcomes": _array(source.get("outcomes")),
            "rank": rank,
            "ranking_metric": ranking_metric,
            "ranking_priority": ranking_priority,
            "resolution_source": _string(source.get("resolutionSource")),
            "snapshot_date": snapshot_date,
            "source_tag": CATEGORY_TAG_IDS[category],
            "start_date": _string(source.get("startDate")),
            "title": title,
            "volume24hr": volume,
            "window_end": None,
            "window_start": None,
        }
```

Build the outer record exactly as follows:

```python
        record = {
            "id": None,
            "data_type": "PREDICTION_MARKET_SELECTION",
            "title": title,
            "summary": question,
            "content": content,
            "from_source": "polymarket",
            "source_url": _event_url(event),
            "content_hash": None,
            "extra_data": extra_data,
            "published_at": None,
            "created_at": local_captured_at,
            "updated_at": local_captured_at,
            "tags": [
                "active", f"category:{category}", "prediction", "selected-market",
            ],
            "source_updated_at": None,
        }
```

After every record has passed `_validate_shape()`, sort before returning the
payload:

```python
    records.sort(key=lambda record: (
        CATEGORY_ORDER.index(record["content"]["category"]),
        record["content"]["ranking_priority"],
        record["content"]["rank"],
    ))
```

Ranking already supplies this order in normal collector use; converter-side
sorting makes the final artifact contract deterministic for every iterable
caller without deduplicating any record.

- [ ] **Step 10: Validate category-specific content shape**

Replace `_validate_shape()` with the complete category-aware validator:

```python
def _validate_shape(record: dict[str, object]) -> None:
    if set(record) != set(OUTER_FIELDS):
        raise ValueError("outer record fields do not match final contract")
    content = record.get("content")
    extra_data = record.get("extra_data")
    if not isinstance(content, dict):
        raise ValueError("content fields do not match final contract")
    expected_content = (
        CRYPTO_CONTENT_FIELDS
        if content.get("category") == "crypto"
        else CONTENT_FIELDS
    )
    if set(content) != set(expected_content):
        raise ValueError("content fields do not match final contract")
    if not isinstance(extra_data, dict) or set(extra_data) != set(EXTRA_DATA_FIELDS):
        raise ValueError("extra_data fields do not match final contract")
```

Keep the call to this validator for every record and the final
`json.dumps(..., allow_nan=False)` safety check.

- [ ] **Step 11: Run final-contract and ranking tests**

```bash
/Users/wrh/Downloads/DBCompare/.venv/bin/python -m pytest \
  tests/test_polymarket_final_contract.py \
  tests/test_polymarket_ranking.py -q
```

Expected: every test passes. Confirm `ranking_metric`, `ranking_priority`, and
`rank` are asserted equal across `content` and `extra_data` rather than merely
present in both.

- [ ] **Step 12: Review and commit final conversion**

```bash
git diff --check
git diff -- \
  tests/test_polymarket_final_contract.py \
  getMarket/Polymarket/tool/final_contract.py
git status --short
git add \
  tests/test_polymarket_final_contract.py \
  getMarket/Polymarket/tool/final_contract.py
git commit -m "feat: align Polymarket API final records"
```

### Task 5: Integrate Page Processing And Remove The Old CLI Limit

**Files:**
- Modify: `tests/test_polymarket_cli.py`
- Modify: `getMarket/Polymarket/tool/export_polymarket_market.py`

- [ ] **Step 1: Replace CLI fixtures with three disjoint ranking pools**

Import `ast`, `Path`, `CRYPTO_CONTENT_FIELDS`, `MarketAccumulator`, and the
revised contracts in the CLI test module. Add this recording subclass after
`FakeClient` so the integration test proves each page is processed separately:

```python
class RecordingAccumulator(MarketAccumulator):
    def __init__(self):
        super().__init__()
        self.batch_sizes = []

    def add(self, rows):
        batch = list(rows)
        self.batch_sizes.append(len(batch))
        super().add(batch)
```

Replace `source_market()` and `complete_pages()` with:

```python
def source_market(
    market_id,
    *,
    category,
    liquidity=None,
    probability=None,
    volume=None,
):
    tag_slugs = ("stablecoins",) if category == "crypto" else (f"fixture-{category}",)
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
```

This fixture contains 179 globally unique market IDs but 180 independent
category candidates because `shared` keeps different source snapshots in
politics and finance. Each category is split into page batches of 7, 7, 7, 7,
and 2.

- [ ] **Step 2: Replace parser tests with the fixed-capacity contract**

Update the default parser test:

```python
def test_parse_args_uses_project_output_root_and_fixed_ranking_size():
    args = cli.parse_args([])

    assert args.output_root == cli._PROJECT_ROOT / "getMarket" / "Polymarket" / "market"
    assert args.page_limit == 20
    assert not hasattr(args, "per_category")
```

Replace configurable-limit acceptance with explicit rejection:

```python
def test_parse_args_rejects_removed_per_category_option():
    with pytest.raises(SystemExit):
        cli.parse_args(["--per-category", "10"])
```

Remove all other `--per-category` cases from the invalid-value parametrization;
the dedicated test covers the removed option. Keep timeout, attempts,
page-limit, retry-delay, and business-date validation unchanged.

- [ ] **Step 3: Replace the successful end-to-end assertions**

Run with `--page-limit 7` to prove request page size does not change ranking
capacity:

```python
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
```

Continue that test with exact final and clean artifact checks:

```python
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
```

Delete `test_run_limits_final_per_category_without_truncating_clean`; the
removed parser option is already covered and the successful test proves fixed
capacity plus a complete `clean.json`.

- [ ] **Step 4: Add raw-before-processing failure coverage**

Add this test to lock the page ordering requirement:

```python
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
```

Keep the existing request-failure and final-conversion failure tests. Their
fixtures now contain valid Tags, and they must continue to prove that an
incomplete run never writes `clean.json` or `final.json`. In the conversion
failure test, update the completed raw-page count to:

```python
    assert len(list((run_dir / "raw").glob("tag-*/page-*.json"))) == (
        5 * len(TAG_CATEGORIES)
    )
```

- [ ] **Step 5: Add an API-only dependency boundary test**

```python
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
```

This test checks actual Python imports rather than searching comments or docs.

- [ ] **Step 6: Run CLI tests and verify RED**

```bash
/Users/wrh/Downloads/DBCompare/.venv/bin/python -m pytest \
  tests/test_polymarket_cli.py -q
```

Expected: the parser still exposes `--per-category`, `run_async()` passes the
removed ranking keyword, the collector retains all page candidates in one list,
and it does not call page-level Tags validation after the raw write. The
`raising=False` injection lets this RED test reach those behavior assertions
before production imports `MarketAccumulator` in Step 7.

- [ ] **Step 7: Remove the option and process compact candidates per page**

Change the imports in `export_polymarket_market.py`:

```python
from getMarket.Polymarket.tool.market_filter import (
    TAG_CATEGORIES,
    MarketAccumulator,
    TaggedMarket,
    compact_market,
)
from getMarket.Polymarket.tool.polymarket_api import (
    PolymarketApiClient,
    PolymarketApiError,
    PolymarketTagsError,
    validate_market_tags,
)
```

Add a Tags-contract branch before the existing `PolymarketApiError` request
branch in `_safe_error()`:

```python
    if isinstance(error, PolymarketTagsError):
        return {
            "stage": "processing",
            "tag_id": error.tag_id,
            "cursor": error.cursor,
            "attempt_count": error.attempts,
            "http_status": error.status,
            "type": type(error).__name__,
            "message": "processing failed",
            "captured_at": captured_at,
        }
```

Keep transport, HTTP, payload-shape, and cursor failures in the existing
`PolymarketApiError` request branch. Keep all other conversion/filtering errors
in the generic processing branch.

Remove this parser line completely:

```python
    parser.add_argument("--per-category", type=_positive_int, default=20)
```

Replace the all-pages `tagged` list and post-collection merge with an
accumulator. Raw must be written before validation and compaction:

```python
        accumulator = MarketAccumulator()
        for tag_id in TAG_CATEGORIES:
            page_index = 0
            async for page in api.iter_tag(tag_id, page_limit=args.page_limit):
                page_index += 1
                write_json_atomic(
                    run_directory / "raw" / f"tag-{tag_id}" / f"page-{page_index:04d}.json",
                    _raw_page(page),
                )
                validate_market_tags(page)
                accumulator.add(
                    TaggedMarket(tag_id, compact_market(row))
                    for row in page.payload["markets"]
                )
        merged = accumulator.result()
        ranked = select_ranked_markets(merged.markets)
```

Keep ranking after all six streams finish. Keep final conversion and atomic
`clean.json`/`final.json` writes after successful ranking only. Do not add a
replacement ranking-size option.

- [ ] **Step 8: Run focused integration tests and verify GREEN**

```bash
/Users/wrh/Downloads/DBCompare/.venv/bin/python -m pytest \
  tests/test_polymarket_cli.py \
  tests/test_polymarket_api.py \
  tests/test_polymarket_filter.py \
  tests/test_polymarket_ranking.py \
  tests/test_polymarket_final_contract.py -q
```

Expected: every selected test passes. The successful fake run makes exactly six
stream requests, retains 180 category candidates representing 179 distinct
market IDs, and emits 180 category-ranking records.

- [ ] **Step 9: Review and commit CLI integration**

```bash
git diff --check
git diff -- \
  tests/test_polymarket_cli.py \
  getMarket/Polymarket/tool/export_polymarket_market.py
git status --short
git add \
  tests/test_polymarket_cli.py \
  getMarket/Polymarket/tool/export_polymarket_market.py
git commit -m "feat: integrate Polymarket three-ranking export"
```

### Task 6: Replace Stale Operator Documentation

**Files:**
- Modify: `getMarket/Polymarket/README.md`
- Modify: `命令使用指南.md`
- Modify: `README.md`

- [ ] **Step 1: Rewrite the Polymarket feature README behavior section**

Replace its opening, category, and selection text with this contract:

```markdown
# Polymarket 市场采集

该采集器从 Polymarket Gamma `/markets/keyset` 获取活跃且未关闭的市场。大类
完全由 Polymarket 返回市场的官方 Tag 流决定，不根据题目、标题、描述或市场
含义重新分类。每个请求都带 `include_tag=true`，并完整翻页后再排行。

## 分类和加密过滤

| 分类 | Tag ID |
| --- | ---: |
| politics | 2 |
| geopolitics | 100265 |
| economy | 100328 |
| finance | 120 |
| technology | 1401 |
| crypto | 21 |

只有 crypto 需要二次过滤。过滤只精确比较官方 `market.tags[].slug`，不读取
description、question、title 或 Tag label：

- `regulation`：`crypto-policy`、`crypto-legal`、`regulation`、`regulations`、
  `sec`、`cftc`、`legal`、`legal-proceedings`、`ban`；
- `etf`：`etf`、`etfs`、`etf-approval`；
- `exchange_risk`：必须同时有 `exchange|exchanges` 之一和
  `bankruptcy|insolvency|hack|hacking|exploit|exploits|cybersecurity|data-breach`
  之一；
- `stablecoin`：`stablecoins`、`tether`、`usdt`、`usdc`、`depeg`；
- `protocol_security`：`protocol-risk`、`protocol-upgrade`、`hack`、`hacking`、
  `hacker`、`exploit`、`exploits`、`cybersecurity`、`data-breach`、`bybit-hack`。

一个 crypto 市场可以记录多个主题，但在 crypto 大类内仍是一个候选。缺失或
格式错误的官方 Tags 会使整次运行失败；合法但没有命中主题的 crypto 市场只会
被正常过滤掉。

## 排行和去重

每个大类固定生成三个独立排行：

1. `liquidity`，优先级 1，最多 10 条；
2. `dominant_probability`，优先级 2，最多 10 条；
3. `volume24hr`，优先级 3，最多 10 条。

低优先级排行先排除高优先级已选 `market_id`，再从完整排序结果中取最多 10
条。因此发生重复时会继续使用原始第 11 名及后续候选补位；候选不足时按实际
数量成功输出。每个排行的 `rank` 都从 1 开始。同一大类内不重复，不同大类之间
不去重。单类最多 30 条，六类最多 180 条。
```

- [ ] **Step 2: Replace README run and artifact sections**

Use this runnable command with no ranking-count option:

```bash
.venv/bin/python -m getMarket.Polymarket.tool.export_polymarket_market \
  --business-date 2026-07-31 \
  --page-limit 20 \
  --timeout 20 \
  --max-attempts 3 \
  --retry-delay 0.25
```

Document that `--page-limit` is request page size only and remains 1-20. List
`--output-root`, `--business-date`, `--timeout`, `--max-attempts`,
`--retry-delay`, and `--page-limit`; do not list `--per-category` or a
replacement.

Replace artifact wording with:

```markdown
每次运行创建独立目录 `market/YYYY-MM-DD_HHMMSS_<随机后缀>/`：

- `raw/tag-*/page-*.json`：每页收到后立即原子写入的原始响应；
- `clean.json`：完整候选集合，包含大类归属、规范化指标、官方 market Tags、
  crypto 主题和精确命中 slug 证据；候选按 `(category, market_id)` 独立，
  同一 market ID 属于多个大类时会保留多行及各自 source；
- `final.json`：顶层为 `{"records": [...]}`，按大类、排行优先级和榜内名次
  排序；
- `error.json`：失败运行的脱敏错误，本次不会同时写出 clean/final。

`final.json` 每条记录有 14 个外层字段。非 crypto `content` 有 19 个字段，
crypto 另有 `crypto_topics`，`extra_data` 有 28 个字段。
`ranking_metric`、`ranking_priority` 和 `rank` 在 content 与 extra_data 中值完全
相同。API 无法可靠提供的 DB 值使用 JSON `null`；这只保证最终文件结构对齐，
不是 DB generation，也不读取 PostgreSQL。
```

Include these direct inspection commands:

```bash
jq -r '.records[] | [.content.category, .content.ranking_metric,
  (.content.rank|tostring), .content.market_question] | @tsv' \
  getMarket/Polymarket/market/<运行目录>/final.json

jq -r '.[] | [.categories|join(","),
  (.source.tags|map(.slug)|sort|join(",")), .source.question] | @tsv' \
  getMarket/Polymarket/market/<运行目录>/clean.json
```

- [ ] **Step 3: Rewrite the global command guide Polymarket section**

In `命令使用指南.md`, make section 3 use the same command, six-Tag table,
crypto rules, ranking order, 0-30/category and 0-180 total bounds, artifact
contract, and failure semantics from Steps 1-2. Keep its read-only live check:

```bash
.venv/bin/python -m pytest \
  tests/test_polymarket_live_smoke.py \
  -m live_polymarket -q
```

State explicitly that this command checks current public API structure and does
not write business artifacts.

- [ ] **Step 4: Rewrite the root README Polymarket summary**

In the root `README.md`, update the Polymarket overview and example so it no
longer says global Top 30, Top-20 fallback, three technology Tags, description
keywords, 120 default rows, 14/17/8 fields, or configurable per-category count.
The concise summary must state:

```markdown
Polymarket API 采集固定读取六个官方分类 Tag 流，完整翻页，并只对 crypto 使用
官方 `market.tags[].slug` 白名单规则。每个大类分别输出 liquidity、
dominant_probability、volume24hr 三个最多 10 条的排行；按优先级在类内去重并
向后补位，不跨大类去重，因此最终最多 180 条分类记录。
```

Use the same no-`--per-category` command and 14/19-or-20/28 artifact summary as
the feature README.

- [ ] **Step 5: Scan only active operator docs for stale contracts**

```bash
rg -n -- \
  '--per-category|105582|technology 的三个|最多 20|最多 120|14/17/8|全局选择最多 30|description.*关键词' \
  README.md \
  命令使用指南.md \
  getMarket/Polymarket/README.md
```

Expected: no matches. Do not edit historical specs or plans merely to make this
scan global; the August 1 design already marks them superseded.

- [ ] **Step 6: Review and commit documentation**

```bash
git diff --check
git diff -- \
  README.md \
  命令使用指南.md \
  getMarket/Polymarket/README.md
git status --short
git add \
  README.md \
  命令使用指南.md \
  getMarket/Polymarket/README.md
git commit -m "docs: explain Polymarket tag rankings"
```

### Task 7: Run Full Verification And Independent Review

**Files:**
- Verify: `getMarket/Polymarket/tool/*.py`
- Verify: `tests/test_polymarket_*.py`
- Verify: `README.md`
- Verify: `命令使用指南.md`
- Verify: `getMarket/Polymarket/README.md`
- Modify only if verification or review finds an in-scope defect.

- [ ] **Step 1: Run all Polymarket offline tests**

```bash
/Users/wrh/Downloads/DBCompare/.venv/bin/python -m pytest \
  tests/test_polymarket_*.py \
  -m "not live_polymarket" -q
```

Expected: all selected API and DB tests pass. DB tests are included to prove the
API-only changes did not alter `getDB/Polymarket` behavior.

- [ ] **Step 2: Run the complete repository offline regression suite**

```bash
/Users/wrh/Downloads/DBCompare/.venv/bin/python -m pytest \
  -m "not live_bubblemaps and not live_polymarket" -q
```

Expected: all selected tests pass with no errors or warnings introduced by this
feature. Do not update unrelated assertions to hide a regression.

- [ ] **Step 3: Compile the changed Python packages**

```bash
/Users/wrh/Downloads/DBCompare/.venv/bin/python -m compileall -q \
  getMarket/Polymarket/tool \
  tests/test_polymarket_api.py \
  tests/test_polymarket_filter.py \
  tests/test_polymarket_ranking.py \
  tests/test_polymarket_final_contract.py \
  tests/test_polymarket_cli.py
```

Expected: exit status 0 and no output.

- [ ] **Step 4: Inspect the final CLI surface**

```bash
/Users/wrh/Downloads/DBCompare/.venv/bin/python -m \
  getMarket.Polymarket.tool.export_polymarket_market --help
```

Expected: help lists `--page-limit` and the timeout/retry/date/output options;
it does not list `--per-category` or another ranking-size option.

- [ ] **Step 5: Run the read-only live Polymarket contract test**

```bash
/Users/wrh/Downloads/DBCompare/.venv/bin/python -m pytest \
  tests/test_polymarket_live_smoke.py \
  -m live_polymarket -q
```

Expected: the six configured streams return valid keyset pages and every
returned market has a valid `tags[].slug` relation. A network, DNS, rate-limit,
or upstream availability failure must be reported separately from offline test
results; do not weaken the contract test to force it green.

- [ ] **Step 6: Run static contract scans**

```bash
rg -n -- \
  'getDB|psycopg|psycopg2|asyncpg' \
  getMarket/Polymarket/tool
```

Expected: no matches.

```bash
rg -n -- \
  'matched_crypto_keywords|CRYPTO_KEYWORDS|selected_by|rank_in_category|rank_in_priority|per_category' \
  getMarket/Polymarket/tool
```

Expected: no matches. Negative compatibility tests intentionally mention removed
names and options, so tests are outside this production-code scan. Historical
plans and specs are also intentionally outside it.

```bash
rg -n -- 'per_category\s*=' tests/test_polymarket_ranking.py
```

Expected: no matches. This narrower test scan permits the removed-option test
name while ensuring no old ranking call or fixture silently survives under a
duplicate test definition.

- [ ] **Step 7: Inspect the complete branch diff**

```bash
git diff --check
git status --short
git diff --stat 80c8b0ef..HEAD
git diff 80c8b0ef..HEAD -- \
  getMarket/Polymarket/tool \
  tests/test_polymarket_api.py \
  tests/test_polymarket_live_smoke.py \
  tests/test_polymarket_filter.py \
  tests/test_polymarket_ranking.py \
  tests/test_polymarket_final_contract.py \
  tests/test_polymarket_cli.py \
  README.md \
  命令使用指南.md \
  getMarket/Polymarket/README.md
```

Confirm the diff does not modify DB code, generated artifacts, unrelated
features, dependency metadata, or historical output files.

- [ ] **Step 8: Request an independent code review**

Use the `requesting-code-review` skill. Give the reviewer the approved August 1
design, this plan, the base commit `80c8b0ef`, and current `HEAD`. Require the
review to check these failure-prone boundaries explicitly:

```text
1. raw page write happens before Tags validation and page compaction;
2. only official exact lowercase Tag slugs filter crypto;
3. exchange risk implements both required groups;
4. ranking exclusion happens before the fixed Top-10 truncation;
5. ranks restart for every (category, ranking_metric);
6. duplicate IDs are removed only within a category;
7. final content is 19 fields or crypto 20, extra_data is 28, outer is 14;
8. ranking metadata matches in content and extra_data;
9. --per-category is absent and --page-limit remains 1-20;
10. runtime API code has no database dependency.
```

- [ ] **Step 9: Resolve review findings with TDD**

For each valid finding, first add or tighten the smallest failing test, run it
to observe the expected failure, make the minimal production or documentation
change, and rerun the focused plus complete offline suites. Do not implement a
review suggestion that contradicts the approved design; document the conflict
in the review response instead.

Commit only actual review fixes:

```bash
git diff --check
git status --short
git add \
  getMarket/Polymarket/tool/polymarket_api.py \
  getMarket/Polymarket/tool/market_filter.py \
  getMarket/Polymarket/tool/market_ranking.py \
  getMarket/Polymarket/tool/final_contract.py \
  getMarket/Polymarket/tool/export_polymarket_market.py \
  tests/test_polymarket_api.py \
  tests/test_polymarket_live_smoke.py \
  tests/test_polymarket_filter.py \
  tests/test_polymarket_ranking.py \
  tests/test_polymarket_final_contract.py \
  tests/test_polymarket_cli.py \
  README.md \
  命令使用指南.md \
  getMarket/Polymarket/README.md
git commit -m "fix: address Polymarket ranking review"
```

If review finds no valid defects, do not create an empty commit.

- [ ] **Step 10: Repeat completion verification after the last change**

Run Steps 1-7 again after any review fix. Completion requires fresh passing
offline output from the final worktree state, a recorded live-test result, a
clean `git diff --check`, and no unexpected working-tree paths.
