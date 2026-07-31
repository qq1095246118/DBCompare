# CEX Flow Factors Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Turn CEX net inflow and net outflow into two no-lookahead daily factors for all 13 samples, show and highlight them in the standalone dashboard, and publish grouped T+N Rank IC results.

**Architecture:** Add one pure CEX window aggregator and one factor calculator to `build_dashboard.py`, then reuse their nested `bar["cex"]` payload in both the HTML and a focused IC script. Keep the original six factors stable by appending CEX-I and CEX-O at indices 6 and 7, while the existing F1-F6 IC reader continues to consume indices 0-5 only.

**Tech Stack:** Python 3.12, pytest 8, standard-library statistics/CSV/JSON, embedded HTML/CSS/JavaScript.

## Global Constraints

- Daily D may use only transfers from D-7 through D-1.
- The four baseline windows cover D-35 through D-8 and do not overlap.
- CEX-I and CEX-O require both a 3x history burst or zero launch and a Cluster share of at least 0.1%.
- CEX-to-CEX transfers count as CEX-related transfers but do not contribute to inflow or outflow.
- Raw token amounts remain explanatory fields and never serve as cross-asset IC values.
- IC horizons are T+1, T+3, T+5, T+7, T+14, and T+30.
- IC results are split into in-sample, out-of-sample, and all-sample groups.
- No new runtime dependency is allowed.

---

### Task 1: Calculate CEX direction windows and factor values

**Files:**
- Create: `tests/test_cex_flow_factors.py`
- Modify: `analysis/binance-bubblemaps-factor-kline-2026-07-30/build_dashboard.py:225-306`

**Interfaces:**
- Consumes: existing `transfer_context(state, record)` and records containing `day`, `amount`, chain/token/from/to fields.
- Produces: `cex_window_summary(state: dict[str, Any], start: date, end: date) -> dict[str, Any]` and `cex_factor_summary(state: dict[str, Any], anchor: date) -> dict[str, Any]`.

`cex_factor_summary` returns the exact public payload below. `i` and `o` each contain `amount`, `baseline`, `share_pct`, `burst`, `zero_launch`, and `trigger`.

```python
{
    "inflow_7d": float,
    "outflow_7d": float,
    "net_signed_7d": float,
    "transfer_count_7d": int,
    "labels_7d": list[str],
    "i": dict[str, float | bool | None],
    "o": dict[str, float | bool | None],
}
```

- [ ] **Step 1: Add an import fixture and failing direction test**

Load `build_dashboard.py` with `importlib.util`, build literal transfers for non-CEX→CEX, CEX→non-CEX, and CEX→CEX, then assert the hand-calculated totals.

```python
def test_cex_window_summary_separates_in_out_and_cex_to_cex():
    state = cex_state(
        records=[
            transfer("wallet", "binance", 120, date(2026, 7, 9)),
            transfer("binance", "wallet", 40, date(2026, 7, 10)),
            transfer("binance", "gate", 900, date(2026, 7, 11)),
        ],
        cluster_amount=100_000,
    )
    got = dashboard.cex_window_summary(
        state, date(2026, 7, 8), date(2026, 7, 14)
    )
    assert got == {
        "inflow": 120.0,
        "outflow": 40.0,
        "signed_net": 80.0,
        "net_inflow": 80.0,
        "net_outflow": 0.0,
        "transfer_count": 3,
        "labels": ["Binance", "Gate"],
    }
```

- [ ] **Step 2: Run the focused test and verify RED**

Run: `.venv/bin/python -m pytest -q tests/test_cex_flow_factors.py::test_cex_window_summary_separates_in_out_and_cex_to_cex`

Expected: FAIL because `cex_window_summary` does not exist.

- [ ] **Step 3: Implement the minimal window aggregator**

Add a function that filters the inclusive date range, calls `transfer_context`, increments the CEX-related count, ignores CEX-to-CEX amounts, and returns sorted unique labels. Derive the signed and one-sided net values from the two gross totals.

```python
def cex_window_summary(state, start, end):
    inflow = outflow = 0.0
    transfer_count = 0
    labels = set()
    for record in state["records"]:
        if not start <= record["day"] <= end:
            continue
        context = transfer_context(state, record)
        direction = context["direction"]
        if direction not in {"流入CEX", "流出CEX", "CEX间转移"}:
            continue
        transfer_count += 1
        amount = float(record["amount"])
        if direction == "流入CEX":
            inflow += amount
        elif direction == "流出CEX":
            outflow += amount
        for key in context["counterpart_keys"]:
            metadata = state["address_metadata"].get(key, {})
            if endpoint_type(metadata) == "CEX":
                label = str(metadata.get("label") or metadata.get("entity_id") or "")
                if label:
                    labels.add(label)
    signed_net = inflow - outflow
    return {
        "inflow": inflow,
        "outflow": outflow,
        "signed_net": signed_net,
        "net_inflow": max(signed_net, 0.0),
        "net_outflow": max(-signed_net, 0.0),
        "transfer_count": transfer_count,
        "labels": sorted(labels),
    }
```

- [ ] **Step 4: Verify the direction test passes**

Run the Step 2 command. Expected: `1 passed`.

- [ ] **Step 5: Add failing baseline, zero-launch, threshold, and no-lookahead tests**

Use literal weekly values with anchor `2026-07-16`: prior net inflows `[10, 20, 30, 40]` have median `25`, current net inflow `100` has burst `4`, and with Cluster amount `100_000` its share is `0.1%`. Add cases proving baseline-zero/current-positive is a zero launch, baseline-zero/current-zero is not, a `0.099%` share does not trigger, and a D-day transfer is ignored.

```python
def test_cex_factor_summary_uses_four_prior_weeks_and_dual_threshold():
    state = state_with_weekly_net_inflows(
        anchor=date(2026, 7, 16),
        prior=[10, 20, 30, 40],
        current=100,
        cluster_amount=100_000,
    )
    got = dashboard.cex_factor_summary(state, date(2026, 7, 16))
    assert got["i"] == {
        "amount": 100.0,
        "baseline": 25.0,
        "share_pct": 0.1,
        "burst": 4.0,
        "zero_launch": False,
        "trigger": True,
    }
    assert got["o"]["trigger"] is False

def test_cex_factor_summary_marks_zero_launch_without_fabricating_burst():
    state = state_with_weekly_net_inflows(
        anchor=date(2026, 7, 16),
        prior=[0, 0, 0, 0],
        current=100,
        cluster_amount=100_000,
    )
    got = dashboard.cex_factor_summary(state, date(2026, 7, 16))
    assert got["i"]["burst"] is None
    assert got["i"]["zero_launch"] is True
    assert got["i"]["trigger"] is True
```

- [ ] **Step 6: Run the new tests and verify RED**

Run: `.venv/bin/python -m pytest -q tests/test_cex_flow_factors.py`

Expected: FAIL because `cex_factor_summary` does not exist.

- [ ] **Step 7: Implement the factor calculator**

Compute current D-7:D-1 plus four windows ending at D-8, D-15, D-22, and D-29. Use `statistics.median` for each one-sided direction, return `burst=None` only for a positive zero launch, express share in percent, and apply `share_pct >= 0.1 and (zero_launch or burst >= 3)`.

- [ ] **Step 8: Run the focused suite and commit**

Run `.venv/bin/python -m pytest -q tests/test_cex_flow_factors.py`, stage `tests/test_cex_flow_factors.py` and `build_dashboard.py`, then commit with message `feat: calculate CEX flow factors`.

### Task 2: Embed and render CEX-I and CEX-O in the dashboard

**Files:**
- Modify: `analysis/binance-bubblemaps-factor-kline-2026-07-30/build_dashboard.py:387-638`
- Modify: `analysis/binance-bubblemaps-factor-kline-2026-07-30/build_dashboard.py:753-1516`
- Modify: `tests/test_cex_flow_factors.py`

**Interfaces:**
- Consumes: `cex_factor_summary(state, anchor)` from Task 1.
- Produces: `bar["cex"]`, `bar["f"][6:8]`, `bar["z"][6:8]`, and signal indices 6/7 for HTML and IC consumers.

- [ ] **Step 1: Add a failing factor-row payload test**

Pass a fake engine whose `window_features` returns literal F1-F6 values and a state that triggers CEX-I. Assert the real `factor_row` appends two values, two zero flags, the CEX payload, and signal index 6 without disturbing indices 0-5.

```python
def test_factor_row_appends_cex_factors_and_signal_indices():
    row = dashboard.factor_row(
        FakeEngine(), triggering_cex_inflow_state(), sample_bar(), [sample_bar()], 0
    )
    assert len(row["f"]) == 8
    assert len(row["z"]) == 8
    assert row["cex"]["i"]["trigger"] is True
    assert 6 in row["sig"]
    assert 7 not in row["sig"]
```

- [ ] **Step 2: Run the payload test and verify RED**

Run: `.venv/bin/python -m pytest -q tests/test_cex_flow_factors.py::test_factor_row_appends_cex_factors_and_signal_indices`

Expected: FAIL because `factor_row` still emits six values and no `cex` key.

- [ ] **Step 3: Integrate the payload and remove duplicate CEX aggregation**

Call `cex_factor_summary` once in `factor_row`. Append `i.burst` and `o.burst` to `f`, append their zero-launch flags to `z`, append indices 6/7 to `sig` on trigger, store the full result as `cex`, and pass the result into `f5_breakdown` so its existing raw CEX fields come from the same calculation.

- [ ] **Step 4: Run the payload test and verify GREEN**

Run the Step 2 command. Expected: `1 passed`.

- [ ] **Step 5: Add a failing rendered-dashboard test**

Extract a `render_dashboard(dataset)` function from the current final string replacement, render a one-bar literal dataset, and assert the output exposes eight factor definitions and the one-bar CEX payload. This tests the produced artifact rather than grepping source code.

```python
def test_render_dashboard_exposes_cex_cards_and_payload():
    rendered = dashboard.render_dashboard(one_bar_dataset())
    assert 'id:"CEX-I"' in rendered
    assert 'id:"CEX-O"' in rendered
    assert '"cex":{"inflow_7d":120.0' in rendered
```

- [ ] **Step 6: Run the render test and verify RED**

Run: `.venv/bin/python -m pytest -q tests/test_cex_flow_factors.py::test_render_dashboard_exposes_cex_cards_and_payload`

Expected: FAIL because `render_dashboard` and the two factor definitions are absent.

- [ ] **Step 7: Update the HTML template and renderer**

Rename visible “六因子” text to “八因子”, append CEX-I/CEX-O to `FACTORS`, render each card with amount/share/burst/zero-launch/threshold/labels/count, include their indices in status tags and yellow-diamond logic, and add two detailed formula articles plus the snapshot-label and Cluster-denominator limitations.

- [ ] **Step 8: Run dashboard tests and commit**

Run `.venv/bin/python -m pytest -q tests/test_cex_flow_factors.py`, stage the test and dashboard builder, then commit with message `feat: show CEX factors in dashboard`.

### Task 3: Calculate grouped T+N Rank IC

**Files:**
- Create: `analysis/binance-bubblemaps-factor-kline-2026-07-30/calculate_cex_flow_ic.py`
- Create: `tests/test_cex_flow_ic.py`
- Generate: `analysis/binance-bubblemaps-factor-kline-2026-07-30/cex-flow-forward-ic.csv`
- Generate: `analysis/binance-bubblemaps-factor-kline-2026-07-30/cex-flow-ic-report.md`

**Interfaces:**
- Consumes: dashboard `DATA` rows with `bar["cex"]["i"|"o"]` and close prices.
- Produces: `build_panel(dataset)`, `calculate(rows)`, `write_csv(results)`, and `write_report(dataset, results, counts, eligible_dates)`.

- [ ] **Step 1: Add failing panel and grouping tests**

Load the IC module by path, feed a literal dataset containing one `样本内`, one `样本外`, and one `新增样本外` token, and assert that the latter two both map to `out_sample`. Assert factor variants use `share_pct`, finite `burst`, daily rank-safe zero-launch encoding, and Boolean trigger.

```python
def test_build_panel_maps_groups_and_factor_variants():
    rows, counts = cex_ic.build_panel(literal_dataset())
    assert [row["group"] for row in rows] == [
        "in_sample", "out_sample", "out_sample"
    ]
    assert rows[0]["values"]["cex_i"]["share"] == 0.2
    assert rows[0]["values"]["cex_i"]["trigger"] == 1.0
    assert counts[("all", "cex_i")] == 1
```

- [ ] **Step 2: Run the panel test and verify RED**

Run: `.venv/bin/python -m pytest -q tests/test_cex_flow_ic.py::test_build_panel_maps_groups_and_factor_variants`

Expected: FAIL because `calculate_cex_flow_ic.py` does not exist.

- [ ] **Step 3: Implement dataset loading and panel construction**

Reuse `load_dataset`, `spearman`, and `newey_west_tstat` from `calculate_f5_subfactor_ic.py`. Store factor names `cex_i`/`cex_o`, variants `share`/`burst`/`trigger`, group names `in_sample`/`out_sample`/`all`, and the six fixed horizons.

For a daily burst cross-section, replace each zero-launch `None` only at ranking time with `max(finite_bursts, default=0) + 1`; do not write that synthetic rank value back into the dashboard payload.

- [ ] **Step 4: Add a failing IC result-shape test**

Build at least five assets per group across enough dates and assert the result contains exactly `2 × 3 × 3 × 6 = 108` rows, each with group, variant, factor, horizon, mean/median IC, standard deviation, Newey-West t, positive rate, valid days, eligible dates, valid ratio, and observations.

```python
def test_calculate_emits_all_factor_variant_group_horizon_rows():
    rows, _ = cex_ic.build_panel(cross_section_dataset())
    results, eligible = cex_ic.calculate(rows)
    assert len(results) == 108
    assert {
        (row["group"], row["variant"], row["factor"], row["horizon_days"])
        for row in results
    } == expected_result_keys()
    assert eligible[("all", 1)] > 0
```

- [ ] **Step 5: Run the result test and verify RED**

Run: `.venv/bin/python -m pytest -q tests/test_cex_flow_ic.py`

Expected: FAIL until grouped calculation and all 108 rows are implemented.

- [ ] **Step 6: Implement calculation and report writers**

Calculate daily cross-sectional Spearman IC only when at least five usable assets and two distinct factor values exist. Write CSV columns in stable dictionary order. Write a Chinese Markdown report with methodology, trigger counts, tables for every group/variant, strongest results, coverage, and all design limitations.

- [ ] **Step 7: Run focused tests and commit**

Run `.venv/bin/python -m pytest -q tests/test_cex_flow_ic.py`, stage the test and IC script, then commit with message `feat: calculate grouped CEX factor IC`.

### Task 4: Regenerate artifacts, document results, and verify

**Files:**
- Modify: `analysis/binance-bubblemaps-factor-kline-2026-07-30/README.md`
- Regenerate: `analysis/binance-bubblemaps-factor-kline-2026-07-30/factor-kline-dashboard.html`
- Regenerate: `analysis/binance-bubblemaps-factor-kline-2026-07-30/cex-flow-forward-ic.csv`
- Regenerate: `analysis/binance-bubblemaps-factor-kline-2026-07-30/cex-flow-ic-report.md`

**Interfaces:**
- Consumes: completed builder and IC calculator.
- Produces: the user-facing dashboard and reproducible statistics for all 13 samples.

- [ ] **Step 1: Regenerate the HTML from existing OHLCV and current snapshots**

Run: `.venv/bin/python analysis/binance-bubblemaps-factor-kline-2026-07-30/build_dashboard.py --reuse-data`

Expected: the script writes `factor-kline-dashboard.html` without network access.

- [ ] **Step 2: Generate CEX IC artifacts**

Run: `.venv/bin/python analysis/binance-bubblemaps-factor-kline-2026-07-30/calculate_cex_flow_ic.py`

Expected: the script writes the CEX CSV and Markdown report.

- [ ] **Step 3: Update README**

Document eight factor cards, the CEX-I/CEX-O formulas and threshold, the CEX priority rule, the three IC groups, the new script/output files, zero-launch treatment, current-snapshot denominator, and current-label backfill limitation.

- [ ] **Step 4: Run the full focused verification**

Run these commands independently:

```bash
.venv/bin/python -m pytest -q tests/test_cex_flow_factors.py tests/test_cex_flow_ic.py
.venv/bin/python -m py_compile analysis/binance-bubblemaps-factor-kline-2026-07-30/build_dashboard.py analysis/binance-bubblemaps-factor-kline-2026-07-30/calculate_cex_flow_ic.py
.venv/bin/python analysis/binance-bubblemaps-factor-kline-2026-07-30/calculate_f1_f6_ic.py
.venv/bin/python analysis/binance-bubblemaps-factor-kline-2026-07-30/calculate_f5_subfactor_ic.py
git diff --check
```

Expected: focused tests pass, both Python files compile, the existing F1-F6 and F5 reports still regenerate, and `git diff --check` emits no errors.

- [ ] **Step 5: Verify generated-data invariants**

Parse the embedded `DATA` with the existing loader and assert:

```python
assert len(dataset["tokens"]) == 13
assert sum(len(token["bars"]) for token in dataset["tokens"]) == 2669
assert all(len(bar["f"]) == 8 for token in dataset["tokens"] for bar in token["bars"])
assert all(len(bar["z"]) == 8 for token in dataset["tokens"] for bar in token["bars"])
assert all("cex" in bar for token in dataset["tokens"] for bar in token["bars"])
assert len(ic_rows) == 108
```

- [ ] **Step 6: Review the HTML visually**

Open the local dashboard, inspect at least one ordinary day, one CEX-I trigger, one CEX-O trigger, one zero launch, and EVAA around 2026-06-16 through 2026-07-07. Confirm zoom, hover, factor cards, tags, and F5 details remain readable.

- [ ] **Step 7: Commit generated artifacts and documentation**

Stage `README.md`, `factor-kline-dashboard.html`, `cex-flow-forward-ic.csv`, and `cex-flow-ic-report.md`, then commit with message `docs: publish CEX factor dashboard and IC`.
