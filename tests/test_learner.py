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
        # like reaction.py: an active market ends reacted / no reaction; a thin one can
        # only end reacted or can't-tell
        if active:
            react = (role == "chair" or abs(z) > 2.5) if learnable else rnd.random() < 0.3
            st = "reacted" if react else "expected_no_reaction"
        else:
            react = rnd.random() < 0.3
            st = "reacted" if react else "cant_tell_illiquid"
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

    def test_only_active_rows_are_labels(self):
        # thin rows can only be reacted/can't-tell, so labeling them would leak
        self.make(10)
        n_thin_reacted = 0
        for ev, rows in learner.dataset():
            for r in rows:
                self.assertEqual(r["label"] is None, r["liquidity"] != "active")
                n_thin_reacted += r["liquidity"] == "thin" and r["status"] == "reacted"
        self.assertGreater(n_thin_reacted, 0)       # the case exists and is excluded

    def test_overlapping_events_never_train_on_each_other(self):
        ids = self.make(25)
        # make event 20 start 30 min after event 19 (same tape, both open)
        e19 = read_json(os.path.join(self.hist, ids[19], "meta.json"))
        m20 = os.path.join(self.hist, ids[20], "meta.json")
        e20 = read_json(m20)
        e20["t0"] = iso(datetime.fromisoformat(e19["t0"].replace("Z", "+00:00")) + timedelta(minutes=30))
        write_json(m20, e20)
        data = learner.dataset()
        ev20 = next(e for e, _ in data if e["event"] == ids[20])
        ev19 = next(e for e, _ in data if e["event"] == ids[19])
        self.assertNotIn(ids[19], [e["event"] for e, _ in learner._closed_before(data, ev20)])
        self.assertNotIn(ids[20], [e["event"] for e, _ in learner._closed_before(data, ev19)])
        scored = learner.prequential(data)
        groups = learner.clusters(scored)
        pos = {s["event"]: i for i, s in enumerate(scored)}
        self.assertTrue(any(pos[ids[19]] in g and pos[ids[20]] in g for g in groups))

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
            if r["liquidity"] == "active":
                self.assertTrue(0 < r["p_react"] < 1)
            else:
                self.assertIsNone(r["p_react"])


if __name__ == "__main__":
    unittest.main()


class TextAndCalendarTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.hist = os.path.join(self.tmp, "history")
        os.makedirs(self.hist)
        self.p = [mock.patch.object(learner, "DATA_DIR", self.tmp),
                  mock.patch.object(learner, "HIST_DIR", self.hist),
                  mock.patch.object(learner, "MODEL", os.path.join(self.tmp, "model.json")),
                  mock.patch.object(learner, "WEB_MODEL", os.path.join(self.tmp, "web_model.json")),
                  mock.patch.object(learner, "BOOTSTRAP", 100)]
        for p in self.p:
            p.start()

    def tearDown(self):
        for p in self.p:
            p.stop()
        shutil.rmtree(self.tmp)

    def test_release_window(self):
        # 2025-08-01 jobs report at 8:30 ET; a speech at 8:00 ET has it inside its window
        self.assertEqual(learner.release_features("2025-08-01T12:00:00Z"), (1.0, 1.0))
        # same day, speech at 1:00 pm ET: release day, but the release was 4.5 h earlier
        self.assertEqual(learner.release_features("2025-08-01T17:00:00Z"), (0.0, 1.0))
        self.assertEqual(learner.release_features("2025-08-04T14:00:00Z"), (0.0, 0.0))

    def make(self, n=40, seed=3):
        rnd = random.Random(seed)
        t = datetime(2025, 1, 2, 15, tzinfo=timezone.utc)
        ids, speeches, emb = [], [], {}
        for i in range(n):
            sid = synth_event(self.hist, i, t + timedelta(days=i), rnd)
            an = read_json(os.path.join(self.hist, sid, "analysis.json"))
            reacted = any(r["status"] == "reacted" for r in an["markets"].values())
            base = [1.0, 0.0, 0.0] if reacted else [0.0, 1.0, 0.0]
            emb[sid] = [b + rnd.gauss(0, 0.2) for b in base] + [rnd.gauss(0, 0.2) for _ in range(9)]
            speeches.append({"id": sid, "t0": iso(t + timedelta(days=i)),
                             "speaker": "a" if i % 2 else "b", "role": "governor"})
            ids.append(sid)
        write_json(os.path.join(self.hist, "embeddings.json"), {"speeches": emb})
        write_json(os.path.join(self.hist, "speeches.json"), {"speeches": speeches})
        return ids

    def test_text_models_run_and_novelty_uses_only_the_past(self):
        ids = self.make()
        data = learner.dataset()
        names = [m.name for m in learner.make_models()]
        self.assertIn("logistic_text", names)
        self.assertIn("mlp_text", names)
        self.assertIn("ensemble", names)
        x10 = next(r for e, rs in data if e["event"] == ids[10] for r in rs)["x"]
        # change a LATER speech's text: speech 10's novelty must not move
        path = os.path.join(self.hist, "embeddings.json")
        E = read_json(path)
        E["speeches"][ids[30]] = [5.0] * 12
        write_json(path, E)
        os.utime(path, (1, 1))
        data2 = learner.dataset()
        x10b = next(r for e, rs in data2 if e["event"] == ids[10] for r in rs)["x"]
        self.assertEqual(x10, x10b)
        out = learner.build()
        for n in ("logistic_text", "mlp_text", "ensemble"):
            self.assertIn(n, out["scoreboard"]["models"])


class LiveOddsTests(unittest.TestCase):
    setUp, tearDown, make = LearnerTests.setUp, LearnerTests.tearDown, LearnerTests.make

    def test_live_odds_for_a_provisional_event(self):
        self.make(20)
        learner.build(extra_mlps=False)
        live = os.path.join(self.tmp, "live")
        sid = sorted(os.listdir(self.hist))[3]
        an = read_json(os.path.join(self.hist, sid, "analysis.json"))
        an["provisional"] = True
        for r in an["markets"].values():
            r["status"] = "watching"
        write_json(os.path.join(live, "analysis.json"), an)
        meta = read_json(os.path.join(self.hist, sid, "meta.json"))
        meta["t0"] = "2026-10-09T14:00:00Z"
        meta["speech_id"] = "live-session"
        write_json(os.path.join(live, "meta.json"), meta)
        odds = learner.live_odds(live)
        self.assertEqual(len(odds["markets"]), 5)
        for mid, p in odds["markets"].items():
            liq = an["markets"][mid]["liquidity"]
            self.assertEqual(p is None, liq != "active")
