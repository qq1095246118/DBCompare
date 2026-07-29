# Feature-First Directory Layout Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Move the Bubblemaps database and market implementations beneath top-level feature packages while preserving all generated data and behavior.

**Architecture:** `common` contains only cross-feature utilities. `getDB.bubblemaps` and `getMarket.bubblemaps` are explicit Python packages, with generated artifacts retained below their owning implementation. Old import and output paths are removed rather than supported through compatibility shims.

**Tech Stack:** Python 3.12, setuptools, pytest, filesystem JSON artifacts

---

### Task 1: Record the migration baseline

**Files:**
- Inspect: `bubblemaps/common/`
- Inspect: `bubblemaps/getDB/`
- Inspect: `bubblemaps/getMarket/`

- [x] Run the offline suite before moving files:

```bash
.venv/bin/python -m pytest -q
```

Expected: all offline tests pass; the live smoke test may be skipped because it requires explicit opt-in.

- [x] Record file counts and SHA-256 values for `bubblemaps/getDB/db`, `bubblemaps/getMarket/db`, and `bubblemaps/getMarket/db_manual` so the migrated data can be compared byte-for-byte.

### Task 2: Move packages and data

**Files:**
- Create: `common/__init__.py`
- Create: `getDB/__init__.py`
- Create: `getMarket/__init__.py`
- Move: `bubblemaps/common/artifacts.py` to `common/artifacts.py`
- Move: `bubblemaps/common/time_window.py` to `common/time_window.py`
- Move: `bubblemaps/common/contract.py` to `getDB/bubblemaps/tool/contract.py`
- Move: `bubblemaps/getDB/tool/` to `getDB/bubblemaps/tool/`
- Move: `bubblemaps/getDB/db/` to `getDB/bubblemaps/db/`
- Move: `bubblemaps/getMarket/tool/` to `getMarket/bubblemaps/tool/`
- Move: `bubblemaps/getMarket/db/` to `getMarket/bubblemaps/market/`
- Move: `bubblemaps/getMarket/db_manual/` to `getMarket/bubblemaps/market_manual/`
- Move: `bubblemaps/README.md` to `getMarket/bubblemaps/README.md`

- [x] Create package directories and move source files with their existing contents unchanged.
- [x] Move generated and manual data directories without rewriting their contents.
- [x] Remove only the empty legacy `bubblemaps` directories after checking that no files remain.
- [x] Compare migrated data file counts and SHA-256 values with Task 1.

Expected: every baseline relative file and digest has an identical counterpart under the new path.

### Task 3: Update Python imports and output roots

**Files:**
- Modify: `common/*.py`
- Modify: `getDB/bubblemaps/tool/*.py`
- Modify: `getMarket/bubblemaps/tool/*.py`
- Modify: `tests/*.py`
- Modify: `pyproject.toml`

- [x] Replace `bubblemaps.common.artifacts` with `common.artifacts` and `bubblemaps.common.time_window` with `common.time_window`.
- [x] Replace `bubblemaps.common.contract` with `getDB.bubblemaps.tool.contract`.
- [x] Replace `bubblemaps.getDB.tool` with `getDB.bubblemaps.tool`.
- [x] Replace `bubblemaps.getMarket.tool` with `getMarket.bubblemaps.tool`.
- [x] Set the database exporter default root to `_PROJECT_ROOT / "getDB" / "bubblemaps" / "db"`.
- [x] Set the market exporter default root to `_PROJECT_ROOT / "getMarket" / "bubblemaps" / "market"`.
- [x] Change setuptools discovery to:

```toml
[tool.setuptools.packages.find]
include = ["common*", "getDB*", "getMarket*"]
namespaces = false
```

- [x] Run focused import and entry-point checks:

```bash
.venv/bin/python -m getDB.bubblemaps.tool.export_bubblemaps_db --help
.venv/bin/python -m getMarket.bubblemaps.tool.export_bubblemaps_market --help
```

Expected: both commands exit successfully and display usage without accessing PostgreSQL or the Bubblemaps API.

### Task 4: Update documentation and path assertions

**Files:**
- Modify: `getMarket/bubblemaps/README.md`
- Modify: `bubblemaps数据与结构基线.md`
- Modify: `docs/superpowers/specs/*.md`
- Modify: `docs/superpowers/plans/*.md`
- Modify: path assertions in `tests/*.py`

- [x] Replace active user instructions and assertions that reference the removed module commands or output directories.
- [x] Preserve historical migration mappings in the new design and implementation plan because those old paths document the source of this migration.
- [x] Search for stale active references:

```bash
rg -n 'bubblemaps\.(getDB|getMarket)|bubblemaps/(getDB|getMarket)' . \
  --glob '!*.json' --glob '!*.sql' \
  --glob '!docs/superpowers/specs/2026-07-28-feature-first-directory-layout-design.md' \
  --glob '!docs/superpowers/plans/2026-07-28-feature-first-directory-layout-plan.md'
```

Expected: no active source, test, or user-command references remain. Historical pre-migration documents may retain old paths when clearly describing past state.

### Task 5: Verify the completed migration

**Files:**
- Verify: all moved and modified files

- [x] Run the offline suite:

```bash
.venv/bin/python -m pytest -q
```

Expected: all offline tests pass; live smoke remains skipped without opt-in.

- [x] Verify package discovery:

```bash
.venv/bin/python -m pip install -e .
.venv/bin/python -c 'import common.artifacts, getDB.bubblemaps.tool.db_source, getMarket.bubblemaps.tool.market_identity'
```

Expected: installation and imports exit successfully.

- [x] Verify the final directory tree contains `common`, `getDB/bubblemaps`, and `getMarket/bubblemaps`, and no legacy root `bubblemaps` directory.
- [x] Re-run the data digest comparison from Task 1.

Expected: source layout matches the approved design and all generated/manual data remain byte-identical.
