# Asymmetric Member Transfers Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Publish complete per-member transfer files when Bubblemaps returns a transfer only from another Cluster member's endpoint.

**Architecture:** Keep raw API responses immutable, build the existing token-wide deduplicated union, then persist the derived `TransferResult.member_documents` back into the clean member files before writing the final token. Validate every final transfer against the union of every ordinary member's raw response for the same token.

**Tech Stack:** Python 3.12, pytest, existing Bubblemaps v3 artifact writers and validators.

---

### Task 1: Reproduce asymmetric member responses

**Files:**
- Modify: `tests/test_market_cli.py`

- [ ] **Step 1: Write the failing CLI regression test**

Add a two-member Cluster fixture with one represented reverse subgraph edge. Return that represented transfer from both member endpoints, but return a second forward transfer only when querying the sender. Run the real CLI and assert exit code `0`, then assert both clean member documents contain the second transfer and the manifest validates.

```python
def test_cli_merges_asymmetric_member_transfer_responses(tmp_path, monkeypatch):
    class AsymmetricClient:
        async def get_transfers(self, target, member_address):
            rows = [represented]
            if member_address == MEMBER:
                rows.append(only_returned_for_member)
            return _api_result(
                target, "transfers", rows, member_address=member_address
            )

    argv = _install_dependencies(monkeypatch, output_root, AsymmetricClient())
    assert market_cli.main(argv) == 0
    manifest, errors = read_validated_generation(output_root, DAY)
    assert manifest["status"] == "partial_success"
    assert errors[0]["type"] == "TransferSubgraphOmission"
    assert both_clean_member_files_contain(only_returned_for_member)
```

- [ ] **Step 2: Run the regression test and verify RED**

Run:

```bash
./.venv/bin/python -m pytest -q tests/test_market_cli.py::test_cli_merges_asymmetric_member_transfer_responses
```

Expected: FAIL because final validation reports that a member summary transfer count does not match its clean file.

### Task 2: Persist token-wide derived member views

**Files:**
- Modify: `getMarket/bubblemaps/tool/export_bubblemaps_market.py`
- Test: `tests/test_market_cli.py`

- [ ] **Step 1: Add a focused persistence helper**

Add a helper that rewrites each available ordinary member clean document from the already validated `TransferResult.member_documents`.

```python
def _write_assembled_member_transfers(staging, target, result):
    for member_address, document in result.member_documents.items():
        write_clean_member_transfers(
            staging,
            target,
            member_address,
            document["transfers"],
            cluster_rank=document["cluster_rank"],
        )
```

- [ ] **Step 2: Invoke it before final token writing**

In `run_generation`, call `_write_assembled_member_transfers(...)` after `_assembled_result(...)` and before `write_final_token(...)`. Do not add new manifest paths because these calls replace the existing clean member paths.

- [ ] **Step 3: Run the regression test**

Run the Task 1 command. Expected at this checkpoint: publication reaches generation validation, which rejects the merged member transfer because it is absent from that member's own raw response.

### Task 3: Validate against the token-wide raw union

**Files:**
- Modify: `getMarket/bubblemaps/tool/market_artifacts.py`
- Modify: `tests/test_market_artifacts.py`
- Test: `tests/test_market_cli.py`

- [ ] **Step 1: Add a negative artifact test**

Seed a valid API staging generation, inject a fabricated transfer into a clean member document and matching token summary, refresh artifact hashes, and assert `validate_staging_generation` rejects it because the transfer is absent from every raw response.

- [ ] **Step 2: Replace per-member raw checks with one raw-union check**

```python
raw_transfer_union = [
    transfer
    for payload in raw_transfers_by_member.values()
    for transfer in payload
]
_require_formal_transfers_in_raw_union(member_documents, raw_transfer_union)
```

This accepts endpoint asymmetry while retaining provenance enforcement.

- [ ] **Step 3: Run focused tests**

Run:

```bash
./.venv/bin/python -m pytest -q \
  tests/test_market_cli.py::test_cli_merges_asymmetric_member_transfer_responses \
  tests/test_market_artifacts.py::test_validation_rejects_transfer_absent_from_token_raw_union
```

Expected: `2 passed`.

### Task 4: Verify the real generation and the repository

**Files:**
- No production edits.

- [ ] **Step 1: Replay the latest failed token in a temporary directory**

Copy its clean and raw target directories into a temporary staging root, rebuild the token-wide result, rewrite merged member files, write the final token, and validate provenance. Assert member `0xfeb2c8eef2d8b7e97970144ffcc001315772a1cf` persists five transfers rather than three.

- [ ] **Step 2: Run all tests and compile checks**

```bash
./.venv/bin/python -m pytest -q
./.venv/bin/python -m compileall -q bubblemaps tests
```

Expected: all non-live tests pass; the opt-in live smoke test remains skipped.

- [ ] **Step 3: Confirm scope**

Review modified files and verify that Cluster construction, chain/token filtering, Supernode behavior, request execution, and historical `_failed` data were not changed.

No commit step is included because `/Users/wrh/Downloads/DBCompare` is not a Git repository.
