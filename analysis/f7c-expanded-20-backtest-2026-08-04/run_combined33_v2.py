#!/usr/bin/env python3
"""Run the frozen V1/V2 exit comparison on the shared 33-token entry stream."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent
HERE = ROOT / "combined33"


def load_runner():
    path = ROOT / "run_v2_backtest.py"
    spec = importlib.util.spec_from_file_location("combined33_v2_runner", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    module.HERE = HERE
    return module


def main() -> None:
    runner = load_runner()
    trades_path = runner.prepare_closed_trades()
    manifest = HERE / "intraday-all-data/manifest.json"
    if not manifest.is_file():
        runner.fetch_intraday(trades_path)
    else:
        print("reusing existing combined33 intraday manifest")
    runner.run_exit_model(trades_path, "v1")
    runner.run_exit_model(trades_path, "v2")
    result = runner.run_weighted_portfolio(trades_path)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
