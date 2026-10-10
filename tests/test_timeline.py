"""Tests for role 1's build_timeline.py on synthetic raw files shaped like the APIs.

    python -m unittest tests.test_timeline
"""

import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tradio import build_timeline as bt  # noqa: E402
from tradio import common  # noqa: E402

T0 = 1750687200  # 2025-06-23T14:00:00Z
CFG = {
    "title": "Test", "speaker": "michelle_bowman", "speech_id": "bowman20250623a",
    "source_url": "https://example.test/speech", "t0": "2025-06-23T14:00:00Z",
    "window": {"start": "2025-06-23T13:58:00Z", "end": "2025-06-23T14:03:00Z"},
    "markets": {"kalshi": {"direct": ["KXFEDDECISION-25JUL-C25"]},
                "polymarket": [{"id": "cut25", "label": "PM cut", "exposure": "direct", "token": "1"}]},
}


def candle(end, bid, ask, vol, last=None):
    return {"end_period_ts": end, "volume": f"{vol:.2f}",
            "yes_bid": {"close": f"{bid:.4f}"}, "yes_ask": {"close": f"{ask:.4f}"},
            "price": {"close": None if last is None else f"{last:.4f}"}}


class BuildTimelineTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        data = os.path.join(self.tmp.name, "data")
        web = os.path.join(self.tmp.name, "web")
        self.patches = [mock.patch.object(common, "DATA_DIR", data),
                        mock.patch.object(bt, "DATA_DIR", data),
                        mock.patch.object(bt, "WEB_DATA_DIR", web)]
        for p in self.patches:
            p.start()
        raw = common.raw_dir("evt")
        # Minute ending 14:01 is MISSING from the candle feed (must be imputed).
        candles = [candle(T0 - 180, 0.14, 0.15, 0),            # padding, seeds carry-forward
                   candle(T0 - 60, 0.14, 0.15, 0),
                   candle(T0, 0.14, 0.15, 100, 0.15),
                   candle(T0 + 120, 0.15, 0.16, 300, 0.16),
                   candle(T0 + 180, 0.16, 0.17, 0)]
        common.write_json(os.path.join(raw, "kalshi_candles_KXFEDDECISION-25JUL-C25.json"),
                          {"response": {"candlesticks": candles}})
        trades = [{"created_time": "2025-06-23T13:59:30.5Z", "yes_price_dollars": "0.1500", "count_fp": "100.00",
                   "taker_outcome_side": "yes", "trade_id": "a"},
                  {"created_time": "2025-06-23T14:01:10.25Z", "yes_price_dollars": "0.1600", "count_fp": "300.00",
                   "taker_outcome_side": "yes", "trade_id": "b"}]
        common.write_json(os.path.join(raw, "kalshi_trades_KXFEDDECISION-25JUL-C25.json"), {"trades": trades})
        poly = {"chunks": [
            {"response": {"history": [{"t": T0 + 6, "p": 0.125}, {"t": T0 + 66, "p": 0.135}]}},
            {"response": {"history": [{"t": T0 + 66, "p": 0.2}, {"t": T0 + 126, "p": 0.16}]}},  # disagrees at T0+66
        ]}
        common.write_json(os.path.join(raw, "poly_cut25.json"), poly)
        self.spot = [{"kind": "trade", "ticker": "KXFEDDECISION-25JUL-C25", "t": "2025-06-23T14:01:10Z", "price": 0.16},
                     {"kind": "quote", "ticker": "KXFEDDECISION-25JUL-C25", "t": "2025-06-23T14:02:00Z", "bid": 0.15, "ask": 0.16},
                     {"kind": "trade", "ticker": "KXFEDDECISION-25JUL-C25", "t": "2025-06-23T14:01:10Z", "price": 0.99}]
        self.lines = bt.build("evt", CFG, self.spot)
        self.timeline = common.read_json(os.path.join(data, "evt", "timeline.json"))

    def tearDown(self):
        for p in self.patches:
            p.stop()
        self.tmp.cleanup()

    def test_schema_keys(self):
        for key in ("event_id", "title", "speaker", "t0", "window", "speech", "markets", "quotes", "trades"):
            self.assertIn(key, self.timeline)
        self.assertEqual(self.timeline["speech"]["paragraphs"], [])

    def test_kalshi_quotes_carry_forward_and_imputed(self):
        k = [q for q in self.timeline["quotes"] if q["market"].startswith("kalshi")]
        self.assertEqual([q["t"] for q in k], ["2025-06-23T13:59:00Z", "2025-06-23T14:00:00Z",
                                               "2025-06-23T14:01:00Z", "2025-06-23T14:02:00Z",
                                               "2025-06-23T14:03:00Z"])
        missing = k[2]
        self.assertTrue(missing["imputed"])
        self.assertEqual((missing["bid"], missing["ask"], missing["volume"]), (0.14, 0.15, 0))
        self.assertEqual(k[3]["mid"], 0.155)

    def test_trades_parsed(self):
        t = self.timeline["trades"]
        self.assertEqual(len(t), 2)
        self.assertEqual(t[1], {"t": "2025-06-23T14:01:10.250000Z", "market": "kalshi:KXFEDDECISION-25JUL-C25",
                                "price": 0.16, "size": 300, "taker_side": "yes"})

    def test_polymarket_conflict_flagged_not_hidden(self):
        p = [q for q in self.timeline["quotes"] if q["market"] == "polymarket:cut25"]
        self.assertEqual(len(p), 3)
        conflicted = [q for q in p if q.get("conflict")]
        self.assertEqual(len(conflicted), 1)
        self.assertEqual(conflicted[0]["mid"], 0.135)   # first value kept, flagged
        self.assertTrue(all(q["bid"] is None for q in p))
        self.assertTrue(next(m for m in self.timeline["markets"] if m["venue"] == "polymarket")["indicative"])

    def test_candle_timing_check(self):
        note = next(l for l in self.lines if "candle timing" in l)
        self.assertIn("'match_minute_before_end': 2", note)

    def test_spot_checks(self):
        joined = "\n".join(self.lines)
        self.assertIn("PASS trade KXFEDDECISION-25JUL-C25 2025-06-23T14:01:10Z @ 0.16", joined)
        self.assertIn("PASS quote", joined)
        self.assertIn("FAIL trade KXFEDDECISION-25JUL-C25 2025-06-23T14:01:10Z @ 0.99", joined)

    def test_web_copy_and_cents_parsing(self):
        self.assertEqual(bt.price(15), 0.15)
        self.assertEqual(bt.price("0.2200"), 0.22)
        self.assertTrue(os.path.isfile(os.path.join(bt.WEB_DATA_DIR, "evt", "timeline.json")))

    def test_rebuild_keeps_role3_paragraphs(self):
        path = os.path.join(bt.DATA_DIR, "evt", "timeline.json")
        tl = common.read_json(path)
        tl["speech"]["paragraphs"] = [{"id": "p0", "text": "x", "stance": -0.5, "z": -2.0}]
        common.write_json(path, tl)
        bt.build("evt", CFG, [])
        self.assertEqual(len(common.read_json(path)["speech"]["paragraphs"]), 1)


if __name__ == "__main__":
    unittest.main()
