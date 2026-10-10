"""Tests for tradio/history.py (backfill) and tradio/learner.py (prequential learner).
Run: python -m pytest tests/test_learner.py -q   (no network)"""

import json
import os
import random
import shutil
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from tradio import history, learner  # noqa: E402
from tradio.common import iso, read_json, write_json  # noqa: E402


class FeedParsing(unittest.TestCase):
    def test_hero_t0s_match_the_feed(self):
        # times on the Fed feed are US Eastern; these are the hand-checked hero T0s
        cases = {("6/23/2025 10:00:00 AM", "bowman20250623a"): "2025-06-23T14:00:00Z",
                 ("7/17/2025 6:30:00 PM", "waller20250717a"): "2025-07-17T22:30:00Z",
                 ("8/28/2026 10:00:00 AM", "warsh20260828a"): "2026-08-28T14:00:00Z",
                 ("1/9/2025 9:00:00 AM", "bowman20250109a"): "2025-01-09T14:00:00Z"}   # EST
        for (d, sid), want in cases.items():
            s = history.parse_feed_entry({"d": d, "l": f"/newsevents/speech/{sid}.htm",
                                          "s": "Governor Christopher J. Waller"})
            self.assertEqual(s["t0"], want, sid)

    def test_roles_and_speaker(self):
        s = history.parse_feed_entry({"d": "6/23/2025 10:00:00 AM", "l": "/newsevents/speech/bowman20250623a.htm",
                                      "s": "Vice Chair for Supervision Michelle W. Bowman"})
        self.assertEqual((s["role"], s["speaker"]), ("vice_chair_supervision", "michelle_bowman"))
        s = history.parse_feed_entry({"d": "8/28/2026 10:00:00 AM", "l": "/newsevents/speech/warsh20260828a.htm",
                                      "s": "Chairman Kevin Warsh"})
        self.assertEqual((s["role"], s["speaker"]), ("chair", "kevin_warsh"))
        s = history.parse_feed_entry({"d": "1/1/2025", "l": "/newsevents/speech/cook20250101a.htm",
                                      "s": "Governor Lisa D. Cook"})
        self.assertIsNone(s["t0"])
        self.assertIsNone(history.parse_feed_entry({"d": "1/1/2025", "l": "/x.pdf", "s": ""}))

    def test_next_decision_and_ticker(self):
        self.assertEqual(history.event_ticker(history.next_decision("2025-06-23T14:00:00Z")),
                         "KXFEDDECISION-25JUL")
        self.assertEqual(history.event_ticker(history.next_decision("2026-08-28T14:00:00Z")),
                         "KXFEDDECISION-26SEP")
        # a speech on a decision day belongs to the NEXT meeting
        self.assertEqual(history.event_ticker(history.next_decision("2025-07-30T14:00:00Z")),
                         "KXFEDDECISION-25SEP")

    def test_committed_speech_list(self):
        sp = history.load_speeches()
        ids = {s["id"]: s for s in sp}
        self.assertEqual(ids["bowman20250623a"]["t0"], "2025-06-23T14:00:00Z")
        self.assertEqual(ids["waller20250717a"]["t0"], "2025-07-17T22:30:00Z")
        self.assertTrue(all(s["t0"] >= history.DEFAULT_SINCE for s in sp))


def candles(start, end, mid_at):
    out = []
    for ts in range(start + 60, end + 1, 60):
        m = mid_at(ts)
        out.append({"end_period_ts": ts, "yes_bid": {"close_dollars": f"{m - 0.005:.4f}"},
                    "yes_ask": {"close_dollars": f"{m + 0.005:.4f}"}, "volume_fp": "10.00"})
    return {"candlesticks": out}


class HistoryPipeline(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.p = [mock.patch.object(history, "HIST_DIR", self.tmp),
                  mock.patch.object(history, "SPEECHES", os.path.join(self.tmp, "speeches.json"))]
        for p in self.p:
            p.start()
        self.s = {"id": "testgov20250623a", "date": "20250623", "t0": "2025-06-23T14:00:00Z",
                  "who": "Governor Test Person", "role": "governor", "speaker": "test_person",
                  "title": "", "url": "https://example.invalid"}

    def tearDown(self):
        for p in self.p:
            p.stop()
        shutil.rmtree(self.tmp)

    def test_pull_then_analyze_offline(self):
        from tradio import ingest_markets as im
        t0 = int(datetime(2025, 6, 23, 14, tzinfo=timezone.utc).timestamp())

        def fake_candles(tk, start, end):
            if tk.endswith("C26"):
                raise im.NotFound("404")
            step = 0.07 if tk.endswith("C25") else 0.0
            return candles(start, end, lambda ts: 0.15 + (step if ts > t0 + 300 else 0))

        def fake_trades(tk, start, end):
            return [{"created_time": iso(datetime.fromtimestamp(start + 60 * i, tz=timezone.utc)),
                     "yes_price_dollars": "0.15", "count_fp": "5", "taker_outcome_side": "yes"}
                    for i in range(0, 150, 5)]
        with mock.patch.object(history, "discover", lambda ev: [f"{ev}-{x}" for x in history.LEGS]), \
                mock.patch.object(im, "kalshi_candles", side_effect=fake_candles), \
                mock.patch.object(im, "kalshi_trades", side_effect=fake_trades):
            msg = history.pull_one(self.s)
        self.assertIn("KXFEDDECISION-25JUL: 4/5", msg)
        history.analyze([self.s], [self.s])
        an = read_json(os.path.join(self.tmp, self.s["id"], "analysis.json"))
        c25 = an["markets"]["kalshi:KXFEDDECISION-25JUL-C25"]
        self.assertEqual(c25["status"], "reacted")
        self.assertEqual(c25["move_ticks"], 7)
        self.assertEqual(an["markets"]["kalshi:KXFEDDECISION-25JUL-H0"]["status"], "expected_no_reaction")
        self.assertNotIn("kalshi:KXFEDDECISION-25JUL-C26", an["markets"])
        meta = read_json(os.path.join(self.tmp, self.s["id"], "meta.json"))
        self.assertEqual(meta["next_decision"], "2025-07-30")
        self.assertEqual(meta["days_to_decision"], 37)
        self.assertIn("data releases", meta["flags"][0])          # 10:00 ET


# ------------------------------------------------------------------ learner

LEGS = ["H0", "C25", "C26", "H25", "H26"]


def synth_event(base, i, t0, rnd, learnable=True):
    """History-style event folder. In 'learnable' worlds, active contracts react when the
    speaker is the chair or the stance surprise is big; otherwise labels are coin flips."""
    sid = f"synth{i:03d}a"
    d = os.path.join(base, sid)
    role = rnd.choice(["chair", "governor", "governor", "vice_chair"])
    z = rnd.gauss(0, 2)
    markets = {}
    for leg in LEGS:
        active = rnd.random() < 0.6
        signal = active and (role == "chair" or abs(z) > 2.5)
        react = signal if learnable else rnd.random() < 0.3
        st = "reacted" if react else ("expected_no_reaction" if active or not learnable else "cant_tell_illiquid")
        markets[f"kalshi:KXFEDDECISION-25JUL-{leg}"] = {
            "liquidity": "active" if active else "thin", "pre_mid": round(rnd.uniform(0.05, 0.9), 3),
            "status": st, "move_ticks": 5 if react else 0, "indicative": False,
            "liquidity_detail": {"trades": 30 if active else 2, "quote_change_minutes": 5,
                                 "median_spread_ticks": 1.0 if active else 4.0, "imputed_minutes": 0}}
    write_json(os.path.join(d, "analysis.json"), {"event_id": sid, "markets": markets})
    write_json(os.path.join(d, "meta.json"), {
        "speech_id": sid, "t0": iso(t0), "role": role, "days_to_decision": 20, "flags": [],
        "stance": {"n": 5, "top_z": round(z, 3)}})
    return sid


class LearnerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.hist = os.path.join(self.tmp, "history")
        os.makedirs(self.hist)
        self.p = [mock.patch.object(learner, "DATA_DIR", self.tmp),
                  mock.patch.object(learner, "HIST_DIR", self.hist),
                  mock.patch.object(learner, "MODEL", os.path.join(self.tmp, "model.json")),
                  mock.patch.object(learner, "WEB_MODEL", os.path.join(self.tmp, "web_model.json")),
                  mock.patch.object(learner, "BOOTSTRAP", 200)]
        for p in self.p:
            p.start()

    def tearDown(self):
        for p in self.p:
            p.stop()
        shutil.rmtree(self.tmp)

    def make(self, n, learnable=True, seed=1):
        rnd = random.Random(seed)
        t = datetime(2025, 1, 2, 15, tzinfo=timezone.utc)
        return [synth_event(self.hist, i, t + timedelta(days=i), rnd, learnable) for i in range(n)]

    def test_cant_tell_rows_are_not_labels(self):
        self.make(5)
        for ev, rows in learner.dataset():
            for r in rows:
                self.assertEqual(r["label"] is None, r["status"] == "cant_tell_illiquid")

    def test_no_peeking(self):
        """An event's prequential prediction must not change when its own labels (or any
        later event's) change."""
        ids = self.make(30)
        before = learner.prequential(learner.dataset())
        target = before[5]["event"]
        later = ids[ids.index(target):]
        for sid in later:                                 # flip this and every later label
            path = os.path.join(self.hist, sid, "analysis.json")
            an = read_json(path)
            for r in an["markets"].values():
                if r["status"] == "reacted":
                    r["status"] = "expected_no_reaction"
                elif r["status"] == "expected_no_reaction":
                    r["status"] = "reacted"
            write_json(path, an)
        after = learner.prequential(learner.dataset())
        a = next(e for e in after if e["event"] == target)
        self.assertEqual([r["label"] for r in a["rows"]],
                         [1 - r["label"] for r in before[5]["rows"]])   # labels did flip
        for rb, ra in zip(before[5]["rows"], a["rows"]):
            for m in ("base_rate", "playbook", "logistic", "boosting", "mlp"):
                self.assertEqual(rb[m], ra[m], m)

    def test_learns_a_real_signal(self):
        self.make(80, learnable=True)
        out = learner.build()
        best = out["scoreboard"]["models"]
        self.assertTrue(out["champion_beats_base_rate"], out["verdict"])
        self.assertLess(best["logistic"]["log_loss"], best["base_rate"]["log_loss"])
        self.assertTrue(os.path.isfile(os.path.join(self.tmp, "web_model.json")))

    def test_says_not_yet_on_noise(self):
        self.make(60, learnable=False, seed=7)
        out = learner.build()
        self.assertIn(out["champion"], out["scoreboard"]["models"])
        # coin-flip labels: nothing should be declared better than the base rate
        self.assertFalse(out["champion_beats_base_rate"], out["verdict"])

    def test_few_events_waits(self):
        self.make(4)
        out = learner.build()
        self.assertEqual(out["scoreboard"]["n_events"], 0)
        self.assertIn("start predicting", out["verdict"])

    def test_predict_uses_only_earlier_events(self):
        ids = self.make(20)
        res = learner.predict(os.path.join(self.hist, ids[12]), "logistic")
        self.assertEqual(res["trained_on_events"], 12)
        self.assertEqual(len(res["markets"]), 5)
        for r in res["markets"].values():
            self.assertTrue(0 < r["p_react"] < 1)


if __name__ == "__main__":
    unittest.main()
