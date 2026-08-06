#!/usr/bin/env python3
"""Integrity checks for the multi-timeframe exit backtest outputs."""

from __future__ import annotations

import csv
import unittest
from collections import defaultdict
from pathlib import Path


HERE = Path(__file__).resolve().parent


class MultiTimeframeExitIntegrityTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        with (HERE / "multitimeframe-exit-v2-trades.csv").open(encoding="utf-8", newline="") as handle:
            cls.trades = list(csv.DictReader(handle))
        with (HERE / "multitimeframe-exit-v2-events.csv").open(encoding="utf-8", newline="") as handle:
            cls.events = list(csv.DictReader(handle))

    def test_all_fixed_entry_cases_are_present(self) -> None:
        self.assertEqual(len(self.trades), 26)
        self.assertEqual(len({trade["case_id"] for trade in self.trades}), 26)

    def test_every_case_is_fully_allocated_to_exits(self) -> None:
        fractions: dict[str, float] = defaultdict(float)
        for event in self.events:
            fractions[event["case_id"]] += float(event["sold_fraction"])
        self.assertEqual(set(fractions), {trade["case_id"] for trade in self.trades})
        for fraction in fractions.values():
            self.assertAlmostEqual(fraction, 1.0, places=10)

    def test_every_decision_precedes_next_bar_fill(self) -> None:
        for event in self.events:
            if not event["decision_close_time_ms"]:
                continue
            decision = int(event["decision_close_time_ms"])
            execution = int(event["execution_open_time_ms"])
            self.assertLess(decision, execution)
            self.assertLessEqual(execution - decision, 1)

    def test_scores_and_cumulative_fractions_are_bounded(self) -> None:
        previous: dict[str, float] = defaultdict(float)
        for event in self.events:
            if event["score"]:
                self.assertGreaterEqual(float(event["score"]), 0.0)
                self.assertLessEqual(float(event["score"]), 1.0)
            cumulative = float(event["cumulative_sold_fraction"])
            self.assertGreaterEqual(cumulative, previous[event["case_id"]])
            self.assertLessEqual(cumulative, 1.0 + 1e-12)
            previous[event["case_id"]] = cumulative

    def test_no_case_uses_cutoff_mark_for_reported_result(self) -> None:
        self.assertFalse(any(trade["right_censored_mark"] == "True" for trade in self.trades))

    def test_extreme_mfe_runner_is_causal_and_scoped(self) -> None:
        extreme = [event for event in self.events if event["reason"].startswith("V2极端MFE runner")]
        self.assertEqual(len(extreme), 1)
        event = extreme[0]
        self.assertEqual(event["case_id"], "20-VELVET-2026-06-06")
        self.assertEqual(event["reason"], "V2极端MFE runner回撤10%")
        self.assertEqual(event["execution_time_utc"], "2026-06-11T20:50:00+00:00")
        self.assertAlmostEqual(float(event["sold_fraction"]), 0.25, places=10)
        self.assertLess(int(event["decision_close_time_ms"]), int(event["execution_open_time_ms"]))


if __name__ == "__main__":
    unittest.main()
