# Polymarket Per-Category CLI Limit Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add `--per-category N` to the Polymarket API collector so operators can limit each configured category in `final.json`, while the default remains 20 and API collection plus `clean.json` remain complete.

**Architecture:** Reuse the CLI's existing positive-integer parser and forward the parsed value into the existing `select_ranked_markets(..., per_category=...)` boundary. The ranking implementation and artifact schemas remain unchanged; an end-to-end CLI test guards the distinction between selected output and the full candidate pool.

**Tech Stack:** Python 3, `argparse`, existing Polymarket ranking/collector modules, `pytest`, `pytest-asyncio`.

---

## Reference And Boundaries

The approved design is
`docs/superpowers/specs/2026-07-31-polymarket-per-category-cli-design.md`.

This feature changes only the API collector. It must not modify DB export code,
Gamma pagination, category membership, crypto filtering, ranking priorities,
tie-breaking, or any JSON field structure. `--page-limit` remains capped at 20
and controls request page size only. `--per-category` accepts any positive
integer and controls selected rows in `final.json` only.

Use the repository interpreter for every test command, including from an
isolated worktree:

```bash
/Users/wrh/Downloads/DBCompare/.venv/bin/python
```

Do not add, remove, stage, or edit generated files under these existing
untracked directories:

```text
getDB/Polymarket/db/
getMarket/Polymarket/market/
getMarket/bubblemaps/market/
```

## File Map

- Modify `tests/test_polymarket_cli.py`: cover the default and explicit parser
  values, invalid values, selected output size, cross-category preservation,
  ranks, and the unchanged complete candidate pool.
- Modify `getMarket/Polymarket/tool/export_polymarket_market.py`: expose
  `--per-category` and pass it to the existing ranking function.
- Modify `getMarket/Polymarket/README.md`: document configurable category
  selection independently from API page size.
- Modify `命令使用指南.md`: add an operator-ready command and parameter
  explanation.

`getMarket/Polymarket/tool/market_ranking.py` must remain unchanged because its
public function already validates and implements `per_category` correctly.

### Task 1: Add The CLI Contract With End-To-End Coverage

**Files:**
- Modify: `tests/test_polymarket_cli.py`
- Modify: `getMarket/Polymarket/tool/export_polymarket_market.py`

- [ ] **Step 1: Add parser tests that describe the new contract**

Extend `test_parse_args_uses_project_output_root` with the new default:

```python
def test_parse_args_uses_project_output_root():
    args = cli.parse_args([])

    assert args.output_root == cli._PROJECT_ROOT / "getMarket" / "Polymarket" / "market"
    assert args.page_limit == 20
    assert args.per_category == 20
```

Immediately after that test, add explicit accepted values. The `100` assertion
proves this option does not inherit the page-size maximum:

```python
def test_parse_args_accepts_configurable_per_category_limit():
    assert cli.parse_args(["--per-category", "10"]).per_category == 10
    assert cli.parse_args(["--per-category", "100"]).per_category == 100
```

Add these cases to the existing invalid-value parametrization:

```python
    ["--per-category", "0"],
    ["--per-category", "-1"],
    ["--per-category", "1.5"],
    ["--per-category", "ten"],
```

- [ ] **Step 2: Add a failing collector integration test**

Insert this test after the existing successful default-run test. It proves the
parameter reaches the ranking layer without truncating API collection or
`clean.json`:

```python
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
```

- [ ] **Step 3: Run the focused tests and verify RED**

Run:

```bash
/Users/wrh/Downloads/DBCompare/.venv/bin/python -m pytest \
  tests/test_polymarket_cli.py::test_parse_args_uses_project_output_root \
  tests/test_polymarket_cli.py::test_parse_args_accepts_configurable_per_category_limit \
  tests/test_polymarket_cli.py::test_run_limits_final_per_category_without_truncating_clean \
  -q
```

Expected: failure because the namespace has no `per_category` field and
`argparse` does not recognize `--per-category`. If the tests pass before the
production edit, stop and determine why they are not testing the missing
behavior.

- [ ] **Step 4: Add the CLI option with existing validation**

In `parse_args()`, add the option next to the other selection/request size
arguments:

```python
    parser.add_argument("--per-category", type=_positive_int, default=20)
    parser.add_argument("--page-limit", type=_positive_int, default=20)
```

Do not add an upper-bound check for `arguments.per_category`. Keep the existing
`arguments.page_limit > 20` check exactly as-is.

- [ ] **Step 5: Forward the value at the ranking boundary**

Replace the implicit default call in `run_async()`:

```python
        ranked = select_ranked_markets(merged.markets)
```

with:

```python
        ranked = select_ranked_markets(
            merged.markets,
            per_category=args.per_category,
        )
```

- [ ] **Step 6: Run the focused tests and verify GREEN**

Run the same focused command from Step 3.

Expected: all three selected tests pass.

- [ ] **Step 7: Run the full CLI test module**

```bash
/Users/wrh/Downloads/DBCompare/.venv/bin/python -m pytest \
  tests/test_polymarket_cli.py -q
```

Expected: every test passes. This includes the pre-existing default run, which
must still produce 20 records per category and 120 records total.

- [ ] **Step 8: Review and commit the behavior change**

```bash
git diff --check
git diff -- \
  tests/test_polymarket_cli.py \
  getMarket/Polymarket/tool/export_polymarket_market.py
git status --short
git add \
  tests/test_polymarket_cli.py \
  getMarket/Polymarket/tool/export_polymarket_market.py
git commit -m "feat: add Polymarket per-category CLI limit"
```

Before committing, confirm no generated artifact directory is staged.

### Task 2: Update Operator Documentation And Run Regressions

**Files:**
- Modify: `getMarket/Polymarket/README.md`
- Modify: `命令使用指南.md`

- [ ] **Step 1: Make the README semantics parameter-based**

Update the introductory selection description so it says the default is 20 per
category, each priority fills the configured limit rather than a hard-coded Top
20, and 120 is only the default maximum across six categories.

Update the runnable example to:

```bash
.venv/bin/python -m getMarket.Polymarket.tool.export_polymarket_market \
  --business-date 2026-07-28 \
  --per-category 10 \
  --page-limit 20
```

Change the available-parameter paragraph so it includes `--per-category`, then
add this explanation immediately after it:

```text
`--per-category` 控制每个大类写入 `final.json` 的最大条数，默认 20，接受任意
正整数。它不会减少 Gamma API 翻页，也不会截断 `clean.json`。`--page-limit`
只控制每页请求数量，允许范围仍为 1–20。
```

- [ ] **Step 2: Update the global command guide without preserving contradictions**

In the Polymarket section of `命令使用指南.md`:

- state that each category defaults to at most 20 records and the default total
  is at most 120;
- describe the three priorities as filling the configured category limit;
- add `--per-category 10` to the full command example; and
- add this parameter entry under “其它参数”:

```text
- `--per-category N`：每个大类写入 `final.json` 的最大条数，默认 20，接受任意
  正整数；不影响 API 翻页和 `clean.json`。
```

Keep the existing `--page-limit` range warning, but make its first sentence
explicit: it controls Gamma API page size, not final selection count.

- [ ] **Step 3: Check the documentation for stale fixed-limit claims**

```bash
rg -n "Top 20|不足 20|最多 20|最多 120|per-category|page-limit" \
  getMarket/Polymarket/README.md \
  命令使用指南.md
```

Expected: any remaining `20`/`120` statement is clearly labeled as the default,
and the two options have separate meanings.

- [ ] **Step 4: Run all offline Polymarket tests**

```bash
/Users/wrh/Downloads/DBCompare/.venv/bin/python -m pytest \
  tests/test_polymarket_*.py \
  -m "not live_polymarket" \
  -q
```

Expected: all selected tests pass; the live API smoke test is deselected.

- [ ] **Step 5: Run the complete offline regression suite**

```bash
/Users/wrh/Downloads/DBCompare/.venv/bin/python -m pytest \
  -m "not live_bubblemaps and not live_polymarket" \
  -q
```

Expected: the complete offline suite passes.

- [ ] **Step 6: Run static syntax and diff checks**

```bash
/Users/wrh/Downloads/DBCompare/.venv/bin/python -m compileall -q \
  getMarket/Polymarket/tool \
  tests/test_polymarket_cli.py
git diff --check
git status --short
```

Expected: compilation and whitespace checks succeed. Only the two documentation
files should remain modified; the three generated artifact directories may
still appear as unrelated untracked paths and must remain untouched.

- [ ] **Step 7: Review and commit the documentation**

```bash
git diff -- \
  getMarket/Polymarket/README.md \
  命令使用指南.md
git add \
  getMarket/Polymarket/README.md \
  命令使用指南.md
git commit -m "docs: explain Polymarket per-category limit"
```

Before committing, confirm no generated artifact directory is staged.

### Task 3: Final Review And Branch Completion

**Files:**
- Review only; fix only confirmed issues in the four implementation files.

- [ ] **Step 1: Request a whole-feature code review**

Use the `requesting-code-review` skill with the approved design and this plan.
The reviewer must inspect parameter validation, forwarding, default
compatibility, artifact boundaries, tests, and operator documentation.

- [ ] **Step 2: Resolve only verified findings**

For any reviewer finding, use `receiving-code-review`: reproduce the issue,
add or adjust a failing test when behavior is affected, make the smallest
correction, and rerun the relevant focused test before the full suites. Commit
verified corrections separately with a specific message.

- [ ] **Step 3: Perform final verification from a clean implementation state**

Use `verification-before-completion`, then run fresh commands:

```bash
/Users/wrh/Downloads/DBCompare/.venv/bin/python -m pytest \
  tests/test_polymarket_*.py \
  -m "not live_polymarket" \
  -q
/Users/wrh/Downloads/DBCompare/.venv/bin/python -m pytest \
  -m "not live_bubblemaps and not live_polymarket" \
  -q
/Users/wrh/Downloads/DBCompare/.venv/bin/python -m compileall -q \
  getMarket/Polymarket/tool \
  tests/test_polymarket_cli.py
git diff --check
git status --short --branch
```

Do not claim success from an earlier test run. Report the exact pass/deselect
counts from these fresh commands and distinguish the known unrelated untracked
artifact directories from feature changes.

- [ ] **Step 4: Finish the isolated branch**

Use `finishing-a-development-branch` and present its integration choices. Do
not push, merge, delete a worktree, or remove generated artifacts unless the
user explicitly chooses the corresponding action.
