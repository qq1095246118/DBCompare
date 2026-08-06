#!/usr/bin/env python3
"""Run the frozen V1/V2 exit and weighted portfolio models for the 169 pool."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path


HERE = Path(__file__).resolve().parent
RUNNER = HERE.parent / "f7c-expanded-20-backtest-2026-08-04" / "run_v2_backtest.py"


def load_runner():
    spec = importlib.util.spec_from_file_location("expanded169_v2_runner", RUNNER)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {RUNNER}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    module.HERE = HERE
    return module


def main() -> None:
    runner = load_runner()
    trades_path = runner.prepare_closed_trades()
    manifest = HERE / "intraday-all-data" / "manifest.json"
    if not manifest.is_file():
        raise FileNotFoundError(f"SSH intraday export is missing: {manifest}")
    runner.run_exit_model(trades_path, "v1")
    runner.run_exit_model(trades_path, "v2")
    result = runner.run_weighted_portfolio(trades_path)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
