"""Tradio live mode (tradio/live.py) without network: Kalshi is mocked, files go to a temp dir.

    python -m unittest tests.test_live
"""

import json
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tradio import ingest_markets as im  # noqa: E402
from tradio import live  # noqa: E402

NOW = 1791000000 + 30            # 30 s into a minute: the current candle is still open
T0 = NOW - 10 * 60


def candle(end, bid, ask, vol=5):
    return {"end_period_ts": end, "yes_bid": {"close_dollars": f"{bid:.4f}"},
            "yes_ask": {"close_dollars": f"{ask:.4f}"}, "volume_fp": f"{vol}.00",
            "price": {"close_dollars": f"{bid:.4f}"}}


class LiveModeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        d = self.tmp.name
        self.data, self.web = os.path.join(d, "data"), os.path.join(d, "web")
        self.patches = [
            mock.patch.object(live, "DATA_DIR", self.data),
            mock.patch.object(live, "WEB_DATA_DIR", self.web),
            mock.patch.object(live, "LIVE_DIRS", [os.path.join(self.data, "live"), os.path.join(self.web, "live")]),
            mock.patch.object(live, "EVENTS_INDEX", os.path.join(self.web, "events.json")),
        ]
        for p in self.patches:
            p.start()
        im._CUTOFF = None

    def tearDown(self):
        for p in self.patches:
            p.stop()
        self.tmp.cleanup()

    def write_state(self, chunks):
        path = os.path.join(self.tmp.name, "live_state.json")
        with open(path, "w") as f:
            json.dump({"speaker": "christopher_waller", "source": "test", "status": "listening",
                       "baseline": {"mean": -0.09, "stdev": 0.32, "n": 219}, "chunks": chunks}, f)
        return path

    def fake_kalshi(self, url, params, tries=5):
        if url.endswith("/historical/cutoff"):
            return {"trades_created_ts": "2026-08-10T00:00:00Z"}
        if url.endswith("/markets") and params.get("series_ticker") == "KXFEDDECISION":
            return {"markets": [
                {"ticker": "KXFEDDECISION-26OCT-H0", "event_ticker": "KXFEDDECISION-26OCT", "close_time": "2026-10-28T18:00:00Z"},
                {"ticker": "KXFEDDECISION-26DEC-H0", "event_ticker": "KXFEDDECISION-26DEC", "close_time": "2026-12-09T19:00:00Z"}]}
        if url.endswith("/markets") and params.get("event_ticker") == "KXFEDDECISION-26OCT":
            return {"markets": [{"ticker": f"KXFEDDECISION-26OCT-{s}"} for s in ("C25", "H0", "H25", "C26", "H26")]}
        if "/historical/markets/" in url:
            raise im.NotFound(url)
        if url.endswith("/candlesticks"):
            end = NOW // 60 * 60
            # 12 closed minutes plus the minute that is still open (must not be written)
            return {"candlesticks": [candle(end - 60 * k, 0.30, 0.31) for k in range(12, 0, -1)]
                    + [candle(end, 0.40, 0.41), candle(end + 60, 0.99, 0.99)]}
        if url.endswith("/markets/trades"):
            if params["ticker"].endswith("-H0"):
                return {"trades": [{"trade_id": "x1", "created_time": "2026-10-03T13:38:00Z",
                                    "yes_price_dollars": "0.3100", "count_fp": "40.00",
                                    "taker_outcome_side": "yes"}], "cursor": ""}
            return {"trades": [], "cursor": ""}
        raise AssertionError(f"unexpected url {url} {params}")

    def test_speech_from_state_keeps_time_and_marks_reporters(self):
        path = self.write_state([
            {"time": "2026-10-03T13:41:02.123+00:00", "text": "We should cut.", "role": "speaker", "stance": -0.6, "z": -1.6},
            {"time": "2026-10-03T13:42:00.000+00:00", "text": "Will you cut?", "role": "reporter", "stance": 0, "z": None}])
        sp = live.speech_from_state(path)
        self.assertEqual([p["t"] for p in sp["paragraphs"]], ["2026-10-03T13:41:02Z", "2026-10-03T13:42:00Z"])
        self.assertEqual([p["relevant"] for p in sp["paragraphs"]], [True, False])
        self.assertTrue(sp["arrives_over_time"])

    def test_live_once_writes_closed_candles_only_and_finds_next_meeting(self):
        state = self.write_state([{"time": "2026-10-03T13:41:02.000+00:00", "text": "Cut.", "role": "speaker",
                                   "stance": -0.6, "z": -1.6}])
        with mock.patch.object(im, "get_json", self.fake_kalshi), mock.patch.object(im.time, "sleep"), \
                mock.patch.object(live.time, "time", return_value=NOW):
            live.main(["--t0", live.iso_from_unix(T0), "--once", "--state", state, "--speaker", "christopher_waller"])
        tl = json.load(open(os.path.join(self.web, "live", "timeline.json")))
        an = json.load(open(os.path.join(self.web, "live", "analysis.json")))
        self.assertEqual(len(tl["markets"]), 5)
        self.assertTrue(all("26OCT" in m["id"] for m in tl["markets"]))
        last_closed = NOW // 60 * 60
        self.assertEqual(max(live.to_unix(q["t"]) for q in tl["quotes"]), last_closed)
        self.assertNotIn(0.99, [q["bid"] for q in tl["quotes"]])
        self.assertEqual(len(tl["trades"]), 1)
        self.assertEqual(len(tl["speech"]["paragraphs"]), 1)
        self.assertEqual(tl["live"]["kind"], "live")
        self.assertTrue(an["provisional"])

    def test_archive_makes_a_history_event_and_refuses_simulations(self):
        state = self.write_state([])
        with mock.patch.object(im, "get_json", self.fake_kalshi), mock.patch.object(im.time, "sleep"), \
                mock.patch.object(live.time, "time", return_value=NOW):
            live.main(["--t0", live.iso_from_unix(T0), "--once", "--state", state])
        live.main(["archive", "waller20261003", "--title", "Waller, 2026-10-03"])
        tl = json.load(open(os.path.join(self.web, "waller20261003", "timeline.json")))
        self.assertEqual(tl["event_id"], "waller20261003")
        self.assertNotIn("live", tl)
        self.assertEqual(json.load(open(os.path.join(self.web, "events.json")))[0]["id"], "waller20261003")

        live_tl = os.path.join(self.data, "live", "timeline.json")
        d = json.load(open(live_tl))
        d["live"]["kind"] = "simulated"
        json.dump(d, open(live_tl, "w"))
        with self.assertRaises(SystemExit):
            live.main(["archive", "fake"])


if __name__ == "__main__":
    unittest.main()


class ProvisionalTest(unittest.TestCase):
    """Live verdicts before the 90-minute window closes, from the real Bowman tape."""

    def frame(self, minutes_after_t0):
        src = json.load(open(os.path.join(live.DATA_DIR, "bowman20250623", "timeline.json")))
        t0 = live.to_unix(src["t0"])
        now = t0 + minutes_after_t0 * 60
        tl = live.base_timeline("t", src["speaker"], t0, live.to_unix(src["window"]["start"]), now, "simulated", "x")
        tl["markets"] = src["markets"]
        tl["quotes"] = [q for q in src["quotes"] if live.to_unix(q["t"]) <= now]
        tl["trades"] = [t for t in src["trades"] if live.parse_ts(t["t"]).timestamp() <= now]
        from tradio.reaction import analyze
        return live.provisional(analyze(tl), tl)

    def test_alarm_shows_before_window_closes_without_a_final_move(self):
        an = self.frame(12)
        c25 = an["markets"]["kalshi:KXFEDDECISION-25JUL-C25"]
        self.assertTrue(an["provisional"])
        self.assertEqual(c25["status"], "alarm")
        self.assertEqual(c25["alarm_t"], "2025-06-23T14:07:00Z")
        self.assertIsNone(c25["move_ticks"])

    def test_before_the_alarm_it_is_watching(self):
        c25 = self.frame(4)["markets"]["kalshi:KXFEDDECISION-25JUL-C25"]
        self.assertEqual(c25["status"], "watching")

    def test_after_the_window_the_final_verdict_is_role_2s(self):
        an = self.frame(90)
        self.assertFalse(an["provisional"])
        self.assertEqual(an["markets"]["kalshi:KXFEDDECISION-25JUL-C25"]["status"], "reacted")
