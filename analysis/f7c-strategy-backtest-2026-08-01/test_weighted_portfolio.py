#!/usr/bin/env python3
"""Integrity checks for the non-equal-weight portfolio simulation."""

from __future__ import annotations

import csv
import unittest
from collections import defaultdict
from pathlib import Path


HERE = Path(__file__).resolve().parent


def read(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


class WeightedPortfolioIntegrityTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.curve = read(HERE / "weighted-portfolio-v2-equity.csv")
        cls.attribution = read(HERE / "weighted-portfolio-v2-attribution.csv")

    def test_each_strategy_contains_all_26_entries(self) -> None:
        counts: dict[str, int] = defaultdict(int)
        for row in self.attribution:
            counts[row["strategy"]] += 1
        self.assertEqual(
            counts,
            {"old_daily": 26, "v1_multitimeframe": 26, "v2_multitimeframe": 26},
        )

    def test_position_limit_is_respected(self) -> None:
        self.assertLessEqual(max(int(row["open_positions"]) for row in self.curve), 3)

    def test_final_equity_reconciles_to_trade_pnl(self) -> None:
        for strategy in ("old_daily", "v1_multitimeframe", "v2_multitimeframe"):
            pnl = sum(
                float(row["total_net_pnl"])
                for row in self.attribution
                if row["strategy"] == strategy
            )
            final = [row for row in self.curve if row["strategy"] == strategy][-1]
            self.assertAlmostEqual(float(final["equity"]), 1 + pnl, places=10)

    def test_equity_stays_positive(self) -> None:
        self.assertGreater(min(float(row["equity"]) for row in self.curve), 0)


if __name__ == "__main__":
    unittest.main()
