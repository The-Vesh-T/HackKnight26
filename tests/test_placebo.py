"""Tests for role 2's tradio/placebo.py (offline; the network fetch is mocked).

    python -m unittest tests.test_placebo
"""

import json
import os
import shutil
import sys
import tempfile
import unittest
from datetime import date, timedelta
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from tradio import placebo, reaction  # noqa: E402
from tests.test_reaction import T0, market, quotes, step, timeline, trades  # noqa: E402

MID = "kalshi:KXFEDDECISION-25JUL-C25"


def stepped_timeline():
    """Active market that jumps +5 ticks at T0+3: reaction.py calls it reacted."""
    tl = timeline([market(MID)], quotes(MID, step(0.15, 5, 3)),
                  trades(MID, range(-60, 91, 3), step(0.15, 5, 3)))
    tl["event_id"] = "testevent"
    return tl


def candles_for(start, end, path):
    """Kalshi-shaped 1-minute candles (role 1's raw format) with mid = path(minute)."""
    out = []
    for i, ts in enumerate(range(start + 60, end + 1, 60)):
        m = path(i)
        out.append({"end_period_ts": ts, "volume": "5.00",
                    "yes_bid": {"close": f"{m - 0.005:.4f}"},
                    "yes_ask": {"close": f"{m + 0.005:.4f}"}})
    return {"candlesticks": out}


class PlaceboTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        os.makedirs(os.path.join(self.tmp, "testevent"))
        with open(os.path.join(self.tmp, "testevent", "timeline.json"), "w") as f:
            json.dump(stepped_timeline(), f)
        self.patches = [mock.patch.object(placebo, "DATA_DIR", self.tmp),
                        mock.patch.object(reaction, "DATA_DIR", self.tmp)]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        shutil.rmtree(self.tmp)

    def test_fake_t0s_rules(self):
        fakes = placebo.fake_t0s("testevent", T0, ["KXFEDDECISION-25JUL-C25"])
        self.assertEqual(fakes, placebo.fake_t0s("testevent", T0, ["KXFEDDECISION-25JUL-C25"]))
        self.assertEqual(len(fakes), len(set(fakes)))
        excluded = placebo.excluded_days()
        for f in fakes:
            self.assertEqual(f.timetz(), T0.timetz())
            self.assertLess(f.weekday(), 5)
            self.assertNotEqual(f.date(), T0.date())
            self.assertNotIn(f.date(), excluded)
            self.assertLessEqual(f.date(), date(2025, 7, 28))   # 2 days before Jul 30 FOMC
        self.assertNotIn(date(2025, 6, 18), {f.date() for f in fakes})  # June FOMC day

    def test_decision_day(self):
        self.assertEqual(placebo.decision_day("KXFEDDECISION-25JUL-C25"), date(2025, 7, 30))
        self.assertEqual(placebo.decision_day("KXFEDDECISION-26SEP-H0"), date(2026, 9, 16))
        self.assertIsNone(placebo.decision_day("KXRATECUTCOUNT-25DEC31-T0"))

    def _fetch_with(self, path_for_window):
        """Run placebo.fetch with role 1's network call mocked; conversion is role 1's."""
        from tradio import ingest_markets
        calls = []

        def fake_candles(ticker, start, end):
            calls.append(ticker)
            return candles_for(start, end, path_for_window(len(calls)))
        with mock.patch.object(ingest_markets, "kalshi_candles", side_effect=fake_candles):
            placebo.fetch("testevent")
        return calls

    def test_quiet_days_keep_reacted(self):
        calls = self._fetch_with(lambda k: (lambda i: 0.40))      # flat ordinary days
        self.assertEqual(set(calls), {"KXFEDDECISION-25JUL-C25"})
        out = placebo.run("testevent")["markets"][MID]
        self.assertGreaterEqual(out["n"], placebo.MIN_N)
        self.assertEqual(out["p"], 0.0)
        r = reaction.analyze(stepped_timeline())["markets"][MID]
        self.assertEqual(r["status"], "reacted")
        self.assertEqual(r["placebo_p"], 0.0)
        self.assertIn("placebo p=0", r["reason"])

    def test_noisy_days_downgrade(self):
        # every ordinary day jumps 6 ticks at the same point -> p = 1
        self._fetch_with(lambda k: (lambda i: 0.40 + (0.06 if i >= 93 else 0)))
        out = placebo.run("testevent")["markets"][MID]
        self.assertEqual(out["p"], 1.0)
        r = reaction.analyze(stepped_timeline())["markets"][MID]
        self.assertEqual(r["status"], "expected_no_reaction")
        self.assertIn("not distinguishable", r["reason"])

    def test_low_n_changes_nothing(self):
        with mock.patch.object(placebo, "N_FAKE", 10):
            self._fetch_with(lambda k: (lambda i: 0.40 + (0.06 if i >= 93 else 0)))
            out = placebo.run("testevent")["markets"][MID]
        self.assertTrue(out["low_n"])
        r = reaction.analyze(stepped_timeline())["markets"][MID]
        self.assertEqual(r["status"], "reacted")
        self.assertIn("too few ordinary days", r["reason"])

    def test_no_placebo_file_means_null(self):
        r = reaction.analyze(stepped_timeline())["markets"][MID]
        self.assertIsNone(r["placebo_p"])

    def test_fetch_resumes_from_cache(self):
        self._fetch_with(lambda k: (lambda i: 0.40))
        calls = self._fetch_with(lambda k: (lambda i: 0.40))
        self.assertEqual(calls, [])


if __name__ == "__main__":
    unittest.main()
