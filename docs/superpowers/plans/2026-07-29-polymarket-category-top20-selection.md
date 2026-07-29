# Polymarket Category Top-20 Selection Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the global maximum-30 Polymarket ranking with six independent category Top-20 selections that use liquidity, dominant probability, and 24-hour volume as ordered fallback passes.

**Architecture:** Keep the existing Tag collection and global candidate merge unchanged. Define the stable category order beside the Tag-to-category configuration, then make `market_ranking.py` normalize candidates once and run a fresh fallback selection for each category; `clean.json` remains globally unique while `final.json` receives category-expanded records. The CLI continues to publish the two existing `RankingResult` lists without new options or artifact types.

**Tech Stack:** Python 3.12, `Decimal`, dataclasses, pytest, pytest-asyncio, existing atomic JSON artifact helper.

---

## Reference Contract

Implement against `docs/superpowers/specs/2026-07-29-polymarket-category-top20-selection-design.md`. That specification supersedes the old global-30 selection section of `docs/superpowers/specs/2026-07-28-polymarket-ranked-market-collection-design.md`.

## File Map

- Modify `getMarket/Polymarket/tool/market_filter.py`: expose the one authoritative stable category order next to `TAG_CATEGORIES`; keep Tag membership and crypto filtering behavior unchanged.
- Modify `getMarket/Polymarket/tool/market_ranking.py`: change the selection limit and scope, validate category memberships, and emit category-expanded metadata.
- Modify `tests/test_polymarket_ranking.py`: replace global-ranking assertions with per-category fallback, capacity, order, tie, and duplication tests.
- Modify `tests/test_polymarket_cli.py`: exercise all six categories through the real merge/rank/write pipeline and preserve the incomplete-Tag failure assertion.
- Modify `getMarket/Polymarket/README.md`: document per-category Top 20, fallback semantics, and output shapes.
- Modify `命令使用指南.md`: update only the Polymarket section. This file is currently untracked; preserve every unrelated section and add it only with the documentation commit.
- No production changes are expected in `market_filter.py` beyond the category-order constant, or in `export_polymarket_market.py`, `polymarket_api.py`, and the artifact helpers. Existing tests already cover technology Tag merging, crypto rejection isolation, pagination, raw-page persistence, atomic writes, and failure handling.

### Task 1: Implement independent category fallback ranking

**Files:**
- Modify: `getMarket/Polymarket/tool/market_filter.py:9-18`
- Modify: `getMarket/Polymarket/tool/market_ranking.py:1-140`
- Modify: `tests/test_polymarket_ranking.py:1-84`

- [ ] **Step 1: Replace the global selection tests with the category contract**

Keep the existing three metric-normalization tests. Add the `Counter` and `CATEGORY_ORDER` imports, then replace the current `candidate()` helper and both selection tests with this code:

```python
from collections import Counter

from getMarket.Polymarket.tool.market_filter import CATEGORY_ORDER


def candidate(
    market_id,
    *,
    categories=("politics",),
    liquidity="1",
    outcome_prices=("0.5", "0.5"),
    volume="1",
):
    prices = list(outcome_prices) if isinstance(outcome_prices, tuple) else outcome_prices
    return {
        "market_id": market_id,
        "categories": list(categories),
        "matched_tag_ids": [],
        "matched_crypto_keywords": [],
        "source": {
            "id": market_id,
            "liquidity": liquidity,
            "outcomePrices": prices,
            "volume24hr": volume,
        },
    }


def test_liquidity_fills_category_limit_before_fallback_metrics():
    rows = [
        candidate(
            f"p-{index:02d}",
            liquidity=str(index),
            outcome_prices=("0.99", "0.01"),
            volume=str(1000 - index),
        )
        for index in range(1, 26)
    ]

    result = select_ranked_markets(rows)

    assert [row["market_id"] for row in result.selected] == [
        f"p-{index:02d}" for index in range(25, 5, -1)
    ]
    assert [row["selected_category"] for row in result.selected] == ["politics"] * 20
    assert [row["selected_by"] for row in result.selected] == ["liquidity"] * 20
    assert [row["priority"] for row in result.selected] == [1] * 20
    assert [row["rank_in_category"] for row in result.selected] == list(range(1, 21))
    assert all("rank_in_priority" not in row for row in result.selected)
    assert result.rankings["politics"]["dominant_probability"]["selected_market_ids"] == []
    assert result.rankings["politics"]["volume24hr"]["selected_market_ids"] == []


def test_probability_then_volume_fill_only_remaining_category_capacity():
    rows = [
        candidate("001", liquidity="100", outcome_prices="bad", volume="bad"),
        candidate("002", liquidity="0", outcome_prices="bad", volume="bad"),
        candidate("003", liquidity="bad", outcome_prices=("0.9", "0.1"), volume="bad"),
        candidate("004", liquidity="bad", outcome_prices=("0.8", "0.2"), volume="bad"),
        candidate("005", liquidity="bad", outcome_prices="bad", volume="70"),
        candidate("006", liquidity="bad", outcome_prices="bad", volume="60"),
    ]

    result = select_ranked_markets(rows, per_category=5)

    assert [row["market_id"] for row in result.selected] == [
        "001", "002", "003", "004", "005"
    ]
    assert [row["selected_by"] for row in result.selected] == [
        "liquidity",
        "liquidity",
        "dominant_probability",
        "dominant_probability",
        "volume24hr",
    ]
    assert [row["priority"] for row in result.selected] == [1, 1, 2, 2, 3]
    assert [row["rank_in_category"] for row in result.selected] == [1, 2, 3, 4, 5]


def test_category_can_finish_below_limit_without_reselecting_markets():
    rows = [
        candidate("001", liquidity="3"),
        candidate("002", liquidity="bad", outcome_prices=("0.6", "0.4")),
        candidate("003", liquidity="bad", outcome_prices="bad", volume="2"),
    ]

    result = select_ranked_markets(rows)

    ids = [row["market_id"] for row in result.selected]
    assert ids == ["001", "002", "003"]
    assert len(ids) == len(set(ids))


def test_same_market_is_unique_within_category_and_expanded_across_categories():
    rows = [
        candidate("shared", categories=("finance", "politics"), liquidity="100"),
        candidate("politics-only", categories=("politics",), liquidity="10"),
        candidate("finance-only", categories=("finance",), liquidity="20"),
    ]

    result = select_ranked_markets(rows, per_category=2)

    assert [(row["selected_category"], row["market_id"]) for row in result.selected] == [
        ("politics", "shared"),
        ("politics", "politics-only"),
        ("finance", "shared"),
        ("finance", "finance-only"),
    ]
    assert [row["rank_in_category"] for row in result.selected] == [1, 2, 1, 2]
    assert [row["market_id"] for row in result.candidates] == [
        "finance-only", "politics-only", "shared"
    ]


def test_category_order_matches_output_contract():
    assert CATEGORY_ORDER == (
        "politics",
        "geopolitics",
        "economy",
        "finance",
        "technology",
        "crypto",
    )


def test_all_categories_are_capped_and_emitted_in_fixed_order():
    rows = [
        candidate(
            f"{position}-{index:02d}",
            categories=(category,),
            liquidity=str(100 - index),
        )
        for position, category in enumerate(CATEGORY_ORDER)
        for index in range(21)
    ]

    result = select_ranked_markets(rows)

    counts = Counter(row["selected_category"] for row in result.selected)
    assert counts == Counter({category: 20 for category in CATEGORY_ORDER})
    assert len(result.selected) == 120
    assert [row["selected_category"] for row in result.selected] == [
        category for category in CATEGORY_ORDER for _ in range(20)
    ]


def test_metric_ties_use_market_id_ascending():
    rows = [candidate(market_id, liquidity="10") for market_id in ("003", "001", "002")]

    result = select_ranked_markets(rows, per_category=2)

    assert [row["market_id"] for row in result.selected] == ["001", "002"]


@pytest.mark.parametrize("limit", [0, -1, True, 1.5])
def test_per_category_limit_must_be_a_positive_integer(limit):
    with pytest.raises(ValueError, match="per-category limit must be positive"):
        select_ranked_markets([], per_category=limit)


@pytest.mark.parametrize("categories", [[], ["sports"], "politics"])
def test_ranker_rejects_missing_or_unknown_category_memberships(categories):
    row = candidate("001")
    row["categories"] = categories

    with pytest.raises(ValueError, match="ranked market categories"):
        select_ranked_markets([row])
```

- [ ] **Step 2: Run the ranking tests and confirm the old interface fails**

Run:

```bash
.venv/bin/python -m pytest tests/test_polymarket_ranking.py -q
```

Expected: FAIL during collection because `CATEGORY_ORDER` does not exist yet, or FAIL on the first selection call because `per_category` and the category-expanded fields are not implemented. Do not weaken the new assertions.

- [ ] **Step 3: Add the authoritative category order**

Add this immediately after `TAG_CATEGORIES` in `market_filter.py`:

```python
CATEGORY_ORDER = tuple(dict.fromkeys(TAG_CATEGORIES.values()))
```

This derives `("politics", "geopolitics", "economy", "finance", "technology", "crypto")` from the existing ordered Tag configuration, with the three technology Tags collapsed to one category.

- [ ] **Step 4: Replace global ranking with per-category fallback selection**

Change the module docstring, import `CATEGORY_ORDER`, keep `_decimal()`, `_canonical()`, `_probability()`, and `normalize_metrics()` unchanged, then replace `_rank()` and `select_ranked_markets()` with the following implementation:

```python
"""Strict metric normalization and per-category fallback market ranking."""

from getMarket.Polymarket.tool.market_filter import CATEGORY_ORDER


def _categories(value: object) -> list[str]:
    if type(value) is not list or not value:
        raise ValueError("ranked market categories must be a non-empty list")
    if any(type(category) is not str or category not in CATEGORY_ORDER for category in value):
        raise ValueError("ranked market categories must use configured category names")
    return value


def _rank(
    candidates: list[dict[str, object]], metric: str, excluded: set[str]
) -> tuple[list[dict[str, object]], list[str]]:
    eligible = [
        row for row in candidates
        if row["market_id"] not in excluded
        and row["normalized_metrics"][metric] is not None
    ]
    eligible.sort(key=lambda row: row["market_id"])
    eligible.sort(
        key=lambda row: Decimal(row["normalized_metrics"][metric]), reverse=True
    )
    excluded_by_priority = [
        row["market_id"] for row in candidates
        if row["market_id"] in excluded
        and row["normalized_metrics"][metric] is not None
    ]
    return eligible, sorted(excluded_by_priority)


def select_ranked_markets(
    markets: Iterable[Mapping[str, object]], *, per_category: int = 20
) -> RankingResult:
    if type(per_category) is not int or per_category < 1:
        raise ValueError("per-category limit must be positive")

    candidates: list[dict[str, object]] = []
    seen: set[str] = set()
    for market in markets:
        if not isinstance(market, Mapping):
            raise TypeError("ranked markets must be mappings")
        market_id = market.get("market_id")
        source = market.get("source")
        categories = _categories(market.get("categories"))
        if not isinstance(market_id, str) or not market_id:
            raise ValueError("ranked market ID must be a non-empty string")
        if market_id in seen:
            raise ValueError("ranked market IDs must be unique")
        if not isinstance(source, Mapping):
            raise ValueError("ranked market source must be a mapping")
        seen.add(market_id)
        normalized = deepcopy(dict(market))
        normalized["categories"] = deepcopy(categories)
        normalized["normalized_metrics"] = normalize_metrics(source)
        candidates.append(normalized)
    candidates.sort(key=lambda row: row["market_id"])

    selected: list[dict[str, object]] = []
    rankings: dict[str, object] = {}
    for category in CATEGORY_ORDER:
        category_candidates = [
            row for row in candidates if category in row["categories"]
        ]
        selected_ids: set[str] = set()
        category_rankings: dict[str, object] = {}
        for priority, metric in enumerate(METRIC_PRIORITIES, start=1):
            eligible, excluded = _rank(category_candidates, metric, selected_ids)
            remaining = per_category - len(selected_ids)
            winners = eligible[:remaining]
            category_rankings[metric] = {
                "priority": priority,
                "selected_market_ids": [row["market_id"] for row in winners],
                "selected_metrics": [
                    row["normalized_metrics"][metric] for row in winners
                ],
                "excluded_by_priorities": excluded,
            }
            for row in winners:
                final = deepcopy(row)
                final.update({
                    "selected_category": category,
                    "selected_by": metric,
                    "priority": priority,
                    "rank_in_category": len(selected_ids) + 1,
                })
                selected.append(final)
                selected_ids.add(row["market_id"])
        rankings[category] = category_rankings

    return RankingResult(candidates, selected, rankings)
```

- [ ] **Step 5: Run focused filter and ranking tests**

Run:

```bash
.venv/bin/python -m pytest tests/test_polymarket_filter.py tests/test_polymarket_ranking.py -q
```

Expected: PASS. This run must include the existing technology three-Tag deduplication test and the existing crypto-rejection-isolation test.

- [ ] **Step 6: Commit the ranking behavior**

```bash
git add getMarket/Polymarket/tool/market_filter.py \
  getMarket/Polymarket/tool/market_ranking.py \
  tests/test_polymarket_ranking.py
git commit -m "feat: rank Polymarket markets per category"
```

### Task 2: Prove the category-expanded artifact contract through the CLI

**Files:**
- Modify: `tests/test_polymarket_cli.py:1-117`
- Verify unchanged: `getMarket/Polymarket/tool/export_polymarket_market.py:137-175`

- [ ] **Step 1: Replace the single-category fixture with all configured categories**

Add `Counter` and `CATEGORY_ORDER` to the imports, then replace `complete_pages()` with these helpers:

```python
from collections import Counter

from getMarket.Polymarket.tool.market_filter import CATEGORY_ORDER, TAG_CATEGORIES


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
```

All three technology Tag streams intentionally receive the same 21 markets, proving that their candidate pool is deduplicated before the technology Top 20. The shared politics/finance market intentionally proves category expansion. The description makes crypto candidates pass the existing description-only keyword rule.

- [ ] **Step 2: Rewrite the successful-run assertions for 120 category records**

Replace the assertions after `final.json` is loaded in `test_run_collects_all_tags_and_publishes_ranked_generation()` with:

```python
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
```

- [ ] **Step 3: Tighten the failed-Tag assertion**

After the existing safe-error assertions, add:

```python
    failed_run = tmp_path / "2026-07-28_080001_run2"
    assert not (failed_run / "clean.json").exists()
    assert not (failed_run / "final.json").exists()
```

- [ ] **Step 4: Run the CLI integration tests**

Run:

```bash
.venv/bin/python -m pytest tests/test_polymarket_cli.py -q
```

Expected: PASS without modifying `export_polymarket_market.py`. Its existing call to `select_ranked_markets(merged.markets)` must pick up the new default of 20 per category, and its existing exception boundary must continue to suppress `final.json` for an incomplete Tag stream.

- [ ] **Step 5: Commit the integration contract**

```bash
git add tests/test_polymarket_cli.py
git commit -m "test: cover category-expanded Polymarket output"
```

### Task 3: Update operator documentation

**Files:**
- Modify: `getMarket/Polymarket/README.md:3-57`
- Modify: `命令使用指南.md:128-172`

- [ ] **Step 1: Replace the README selection description**

Replace the opening behavior description through the paragraph before `## 分类` with:

```markdown
该采集器从 Polymarket 公共 Gamma API 获取活跃且未关闭的市场。分类完全依据
Polymarket Tag 归属，不根据题目、标题或描述重新判断。每个大类独立选择最多
20 条记录，三级指标是补足同一个 Top 20 的顺序回退：

1. 先按 `liquidity` 降序填充；
2. 不足 20 条时，从尚未入选的市场中按 `dominant_probability` 降序补足；
3. 仍不足 20 条时，再按 `volume24hr` 降序补足。

`dominant_probability` 是 `outcomePrices` 中的最大有效值。指标相同时按
`market_id` 升序。同一市场在一个大类中最多出现一次；若 Polymarket 将它放入
多个配置大类，它会在每个符合的大类中各保留一条。因此 `final.json` 最多包含
120 条分类记录，但全局不同的 `market_id` 数量可能少于 120。
```

Keep the existing category table and crypto description rule. After the technology row, add:

```markdown
technology 的三个 Tag 合并为一个候选池，并在该大类内按 `market_id` 去重。
```

- [ ] **Step 2: Replace the README artifact descriptions**

Replace the `clean.json` and `final.json` bullets with:

```markdown
- `clean.json`：按 `market_id` 全局唯一的候选市场，保留全部分类归属和规范化指标；
- `final.json`：按固定大类顺序展开的结果，增加 `selected_category`、
  `selected_by`、`priority` 和 `rank_in_category`；
```

- [ ] **Step 3: Update only the global guide's Polymarket behavior section**

Replace lines 130-135 of the current guide with:

```markdown
业务用途：从 Polymarket Gamma API 获取活跃、未关闭市场。分类以 Polymarket
Tag 归属为准，政治、地缘政治、经济、金融、科技和加密六个大类分别选择最多
20 条记录，最终最多 120 条分类记录。每个大类依次按以下指标补足同一个 Top 20：

1. `liquidity`；
2. 不足时使用 `dominant_probability`；
3. 仍不足时使用 `volume24hr`。

同一 `market_id` 在一个大类内去重，但同时属于多个大类时会在各大类分别保留。
technology 的三个 Tag 合并为一个大类；crypto 仍要求 `description` 命中关键词。
```

After the output tree, replace the current one-line artifact note with:

```markdown
`raw` 页面会边采集边写入；`clean.json` 保持全局 `market_id` 唯一，
`final.json` 按大类展开并包含大类内排名。`final.json` 不存在表示本次没有完整成功。
```

- [ ] **Step 4: Check for stale global-30 language and malformed Markdown**

Run:

```bash
rg -n "最多 30|前 10|rank_in_priority|per_priority" \
  getMarket/Polymarket/README.md 命令使用指南.md
git diff --check
```

Expected: `rg` exits 1 with no matches, and `git diff --check` exits 0 with no whitespace errors.

- [ ] **Step 5: Commit the documentation**

Review `git diff -- getMarket/Polymarket/README.md 命令使用指南.md` and confirm that non-Polymarket guide sections are unchanged. Then run:

```bash
git add getMarket/Polymarket/README.md 命令使用指南.md
git commit -m "docs: explain Polymarket category Top 20"
```

### Task 4: Run complete verification

**Files:**
- Verify: `getMarket/Polymarket/tool/*.py`
- Verify: `tests/test_polymarket_*.py`
- Verify: repository-wide offline test suite

- [ ] **Step 1: Run all Polymarket offline tests**

Run:

```bash
.venv/bin/python -m pytest \
  tests/test_polymarket_api.py \
  tests/test_polymarket_filter.py \
  tests/test_polymarket_ranking.py \
  tests/test_polymarket_cli.py -q
```

Expected: PASS with no skipped tests in these four files.

- [ ] **Step 2: Run the repository offline suite**

Run:

```bash
.venv/bin/python -m pytest \
  -m "not live_bubblemaps and not live_polymarket" -q
```

Expected: PASS. Any unrelated pre-existing failure must be recorded with its exact test name and traceback instead of being attributed to this change.

- [ ] **Step 3: Compile the Python packages and check the patch**

Run:

```bash
.venv/bin/python -m compileall -q common getDB getMarket
git diff --check
git status --short
```

Expected: compilation and whitespace checks exit 0. The status may list only intentional plan/worktree state; it must not contain generated market artifacts, credentials, or unrelated modified files.

- [ ] **Step 4: Run the read-only live Tag contract test**

Run:

```bash
.venv/bin/python -m pytest tests/test_polymarket_live_smoke.py \
  -m live_polymarket -q
```

Expected: PASS for all configured Tag streams. A network or upstream failure is external evidence to report, not a reason to relax offline assertions.

- [ ] **Step 5: Run one real collection outside the repository**

Run:

```bash
.venv/bin/python -m getMarket.Polymarket.tool.export_polymarket_market \
  --output-root /tmp/dbcompare-polymarket-category-top20-smoke
```

Expected: exit 0 and one new unique run directory under the temporary output root.

- [ ] **Step 6: Validate the real artifact invariants**

Run:

```bash
.venv/bin/python - <<'PY'
from collections import Counter
import json
from pathlib import Path

categories = (
    "politics", "geopolitics", "economy",
    "finance", "technology", "crypto",
)
root = Path("/tmp/dbcompare-polymarket-category-top20-smoke")
run = max((path for path in root.iterdir() if path.is_dir()), key=lambda path: path.stat().st_mtime)
clean = json.loads((run / "clean.json").read_text())
final = json.loads((run / "final.json").read_text())

assert len(clean) == len({row["market_id"] for row in clean})
assert len(final) <= 120
counts = Counter(row["selected_category"] for row in final)
assert set(counts) <= set(categories)
assert all(count <= 20 for count in counts.values())
assert [row["selected_category"] for row in final] == sorted(
    (row["selected_category"] for row in final),
    key=categories.index,
)
for category in categories:
    rows = [row for row in final if row["selected_category"] == category]
    assert [row["rank_in_category"] for row in rows] == list(range(1, len(rows) + 1))
    assert len(rows) == len({row["market_id"] for row in rows})
    assert all(row["selected_by"] in {
        "liquidity", "dominant_probability", "volume24hr"
    } for row in rows)

print(run)
print({category: counts[category] for category in categories})
PY
```

Expected: the script prints the selected run directory and six category counts without raising an assertion. Empty or shorter categories are valid; a missing `final.json` requires inspection of that run's sanitized `error.json`.

- [ ] **Step 7: Review the final commit range**

Run:

```bash
git log --oneline --decorate -5
git status --short
```

Expected: the implementation is represented by focused ranking, integration-test, and documentation commits. No API output directory or temporary smoke artifact is tracked.
