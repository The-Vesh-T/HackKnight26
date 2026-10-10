"""Endpoint selection in tradio/ingest_markets.py (no network: get_json is mocked).

    python -m unittest tests.test_ingest
"""

import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tradio import ingest_markets as im  # noqa: E402

CUTOFF = 1786320000  # 2026-08-10T00:00:00Z


class IngestEndpointsTest(unittest.TestCase):
    def setUp(self):
        im._CUTOFF = None
        self.calls = []

    def fake(self, responses):
        def get_json(url, params, tries=5):
            self.calls.append(url)
            for key, value in responses.items():
                if url.endswith(key):
                    if isinstance(value, Exception):
                        raise value
                    return value
            raise AssertionError(f"unexpected url {url}")
        return get_json

    def test_old_window_uses_historical_trades_only(self):
        r = {"/historical/cutoff": {"trades_created_ts": "2026-08-10T00:00:00Z"},
             "/historical/trades": {"trades": [{"trade_id": "a", "created_time": "2025-06-23T14:01:49Z"}], "cursor": ""}}
        with mock.patch.object(im, "get_json", self.fake(r)), mock.patch.object(im.time, "sleep"):
            trades = im.kalshi_trades("T", 1750687200, 1750687200 + 1800)
        self.assertEqual(len(trades), 1)
        self.assertFalse(any(u.endswith("/markets/trades") for u in self.calls))

    def test_new_window_uses_live_trades_only(self):
        r = {"/historical/cutoff": {"trades_created_ts": "2026-08-10T00:00:00Z"},
             "/markets/trades": {"trades": [{"trade_id": "b", "created_time": "2026-08-28T14:01:00Z"}], "cursor": ""}}
        with mock.patch.object(im, "get_json", self.fake(r)), mock.patch.object(im.time, "sleep"):
            trades = im.kalshi_trades("T", 1787925600, 1787925600 + 1800)
        self.assertEqual([t["trade_id"] for t in trades], ["b"])
        self.assertFalse(any(u.endswith("/historical/trades") for u in self.calls))

    def test_straddling_window_asks_both_and_dedupes(self):
        same = {"trade_id": "x", "created_time": "2026-08-10T00:00:05Z"}
        r = {"/historical/cutoff": {"trades_created_ts": "2026-08-10T00:00:00Z"},
             "/historical/trades": {"trades": [same], "cursor": ""},
             "/markets/trades": {"trades": [same, {"trade_id": "y", "created_time": "2026-08-10T00:00:09Z"}], "cursor": ""}}
        with mock.patch.object(im, "get_json", self.fake(r)), mock.patch.object(im.time, "sleep"):
            trades = im.kalshi_trades("T", CUTOFF - 600, CUTOFF + 600)
        self.assertEqual([t["trade_id"] for t in trades], ["x", "y"])

    def test_candles_fall_back_to_live_series_endpoint(self):
        r = {"/historical/markets/KXFEDDECISION-26SEP-H25/candlesticks": im.NotFound("404"),
             "/series/KXFEDDECISION/markets/KXFEDDECISION-26SEP-H25/candlesticks": {"candlesticks": [{"end_period_ts": 1}]}}
        with mock.patch.object(im, "get_json", self.fake(r)):
            data = im.kalshi_candles("KXFEDDECISION-26SEP-H25", 0, 60)
        self.assertEqual(data["_source"], "live")

    def test_candles_prefer_historical(self):
        r = {"/historical/markets/KXFEDDECISION-25JUL-C25/candlesticks": {"candlesticks": [{"end_period_ts": 1}]}}
        with mock.patch.object(im, "get_json", self.fake(r)):
            data = im.kalshi_candles("KXFEDDECISION-25JUL-C25", 0, 60)
        self.assertEqual(data["_source"], "historical")


if __name__ == "__main__":
    unittest.main()
