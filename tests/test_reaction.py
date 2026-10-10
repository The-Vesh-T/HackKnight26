"""Tests for role 2's tradio/reaction.py and tradio/placebo.py (offline).

    python -m unittest tests.test_reaction
"""

import json
import os
import shutil
import sys
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from tradio import placebo, reaction  # noqa: E402
from tradio.common import parse_ts  # noqa: E402

FIXTURE = os.path.join(ROOT, "tests", "fixtures", "bowman_small.json")
T0 = datetime(2025, 6, 23, 14, 0, tzinfo=timezone.utc)


def _iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%S") + (f".{dt.microsecond:06d}Z" if dt.microsecond else "Z")


def market(mid, exposure="direct", venue="kalshi"):
    return {"id": mid, "venue": venue, "label": mid, "exposure": exposure,
            "indicative": venue == "polymarket", "tick": 0.01}


def quotes(mid, path, spread=0.01, imputed_every=None, indicative=False):
    """path(minute_offset) -> midpoint, for quote END times T0-59 ... T0+90."""
    out = []
    for i, off in enumerate(range(-59, 91)):
        m = round(path(off), 4)
        imp = bool(imputed_every and i % imputed_every)
        out.append({"t": _iso(T0 + timedelta(minutes=off)), "market": mid,
                    "bid": None if indicative else round(m - spread / 2, 4),
                    "ask": None if indicative else round(m + spread / 2, 4),
                    "mid": m, "imputed": imp, "volume": 0 if imp else 100})
    return out


def trades(mid, minutes, price_at, every_sec=30):
    out = []
    for off in minutes:
        t = T0 + timedelta(minutes=off - 1, seconds=every_sec, microseconds=123456)
        out.append({"t": _iso(t), "market": mid, "price": round(price_at(off), 2),
                    "size": 10, "taker_side": "yes"})
    return out


def timeline(markets, qs, ts):
    return {"event_id": "test", "title": "test", "speaker": "x", "t0": _iso(T0),
            "window": {"start": _iso(T0 - timedelta(minutes=60)),
                       "end": _iso(T0 + timedelta(minutes=90))},
            "speech": {"source_url": "test", "paragraphs": []},
            "markets": markets, "quotes": qs, "trades": ts}


def step(base, ticks, at):
    return lambda off: base + (ticks * 0.01 if off >= at else 0)


def build_fixture():
    """Small hand-made Bowman-shaped timeline: one stepped active market, one frozen
    market, one noisy active market, one indicative Polymarket series."""
    ms = [market("kalshi:STEP"), market("kalshi:FROZEN"), market("kalshi:NOISE"),
          market("polymarket:cut25", venue="polymarket")]
    qs = (quotes("kalshi:STEP", step(0.15, 5, 3))
          + quotes("kalshi:FROZEN", lambda o: 0.30, imputed_every=1)
          + quotes("kalshi:NOISE", lambda o: 0.50 + (0.01 if o % 2 else 0))
          + quotes("polymarket:cut25", step(0.15, 5, 4), indicative=True))
    ts = (trades("kalshi:STEP", range(-60, 91, 3), step(0.15, 5, 3))
          + trades("kalshi:NOISE", range(-59, 91, 3), lambda o: 0.50))
    return timeline(ms, qs, ts)


class ReactionTests(unittest.TestCase):
    def setUp(self):
        self.res = reaction.analyze(build_fixture())["markets"]

    def test_frozen_market_is_cant_tell(self):
        r = self.res["kalshi:FROZEN"]
        self.assertEqual(r["liquidity"], "dark")
        self.assertEqual(r["status"], "cant_tell_illiquid")

    def test_step_in_active_market_reacts_fast(self):
        r = self.res["kalshi:STEP"]
        self.assertEqual(r["liquidity"], "active")
        self.assertEqual(r["status"], "reacted")
        alarm = reaction.parse_ts(r["alarm_t"])
        self.assertTrue(T0 + timedelta(minutes=3) <= alarm <= T0 + timedelta(minutes=5))
        self.assertEqual(r["move_ticks"], 5)
        self.assertEqual(r["confirmed_by_trade"], "2025-06-23T14:02:30.123456Z")

    def test_one_tick_noise_no_alarm(self):
        r = self.res["kalshi:NOISE"]
        self.assertIsNone(r["alarm_t"])
        self.assertNotIn(r["status"], ("reacted", "candidate_delay"))

    def test_polymarket_indicative_has_no_timing(self):
        r = self.res["polymarket:cut25"]
        self.assertTrue(r["indicative"])
        self.assertEqual(r["liquidity"], "indicative")
        self.assertTrue(r["reason"].startswith("Indicative:"))
        for key in ("alarm_t", "t50_min", "t90_min", "cusum_alarm_t", "confirmed_by_trade"):
            self.assertIsNone(r[key], key)
        self.assertNotEqual(r["status"], "candidate_delay")

    def test_staircase_move_alarms(self):
        """Real Bowman shape: 1-tick steps with flat minutes between."""
        mid = "kalshi:STAIRS"
        path = lambda o: 0.145 + 0.01 * sum(1 for s in (7, 21, 23, 24, 25, 29, 30) if o >= s)
        tl = timeline([market(mid)], quotes(mid, path),
                      trades(mid, range(-59, 91, 3), path))
        r = reaction.analyze(tl)["markets"][mid]
        self.assertEqual(r["status"], "reacted")
        self.assertEqual(r["alarm_t"], "2025-06-23T14:07:00Z")
        self.assertEqual(r["move_ticks"], 7)

    def test_small_drift_does_not_react(self):
        mid = "kalshi:DRIFT"
        tl = timeline([market(mid)], quotes(mid, step(0.95, -1, 6)),
                      trades(mid, range(-59, 91, 3), lambda o: 0.95))
        r = reaction.analyze(tl)["markets"][mid]
        self.assertNotIn(r["status"], ("reacted", "candidate_delay"))

    def test_thin_late_mover_never_candidate_delay(self):
        mid = "kalshi:THIN"
        tl = timeline([market(mid)], quotes(mid, step(0.40, 6, 30)),
                      trades(mid, [-20, -5], lambda o: 0.40))
        r = reaction.analyze(tl)["markets"][mid]
        self.assertEqual(r["liquidity"], "thin")
        self.assertNotEqual(r["status"], "candidate_delay")

    def test_active_late_mover_is_candidate_delay(self):
        mid = "kalshi:LATE"
        tl = timeline([market(mid)], quotes(mid, step(0.40, 6, 30)),
                      trades(mid, range(-59, 91, 3), step(0.40, 6, 30)))
        r = reaction.analyze(tl)["markets"][mid]
        self.assertEqual(r["status"], "candidate_delay")

    def test_outcome_sum_check_only_on_complete_ladder(self):
        legs = {"H26": 0.01, "H25": 0.01, "H0": 0.60, "C25": 0.40, "C26": 0.02}  # sums 1.04
        ms, qs = [], []
        for leg, p in legs.items():
            mid = f"kalshi:KXFEDDECISION-25JUL-{leg}"
            ms.append(market(mid))
            qs += quotes(mid, lambda o, p=p: p)
        res = reaction.analyze(timeline(ms, qs, []))["markets"]
        self.assertTrue(res["kalshi:KXFEDDECISION-25JUL-H0"]["flags"])
        partial = reaction.analyze(timeline(ms[2:4], [q for q in qs if q["market"] in
                                                      (ms[2]["id"], ms[3]["id"])], []))
        self.assertFalse(partial["markets"]["kalshi:KXFEDDECISION-25JUL-H0"]["flags"])

    def test_statuses_and_wording(self):
        for r in self.res.values():
            self.assertIn(r["status"], reaction.STATUSES)
            text = r["reason"].lower()
            self.assertNotIn("opportunit", text)
            self.assertNotIn("mispric", text)

    def test_fixture_file_matches_builder(self):
        with open(FIXTURE, encoding="utf-8") as f:
            self.assertEqual(json.load(f), build_fixture())


def _real(event_id):
    path = os.path.join(ROOT, "tradio", "data", event_id, "timeline.json")
    if not os.path.isfile(path):
        return None
    with open(path, encoding="utf-8") as f:
        return reaction.analyze(json.load(f))["markets"]


class RealDataChecks(unittest.TestCase):
    """Checks on role 1's real files (skipped if not pulled). These are the role file's
    "expected on real Bowman data" checks, not targets to tune toward."""

    def test_bowman(self):
        res = _real("bowman20250623")
        if res is None:
            self.skipTest("no Bowman timeline")
        for k in ("kalshi:KXFEDDECISION-25JUL-C25", "kalshi:KXFEDDECISION-25JUL-H0"):
            self.assertEqual(res[k]["status"], "reacted", k)
            alarm = reaction.parse_ts(res[k]["alarm_t"])
            self.assertTrue(T0 < alarm <= T0 + timedelta(minutes=10), k)
        self.assertEqual(res["kalshi:KXRATECUTCOUNT-25DEC31-T0"]["status"], "cant_tell_illiquid")

    def test_waller_control_has_no_reaction(self):
        res = _real("waller20250717")
        if res is None:
            self.skipTest("no Waller timeline")
        for k, r in res.items():
            self.assertNotIn(r["status"], ("reacted", "candidate_delay"), k)

    def test_every_event_runs(self):
        events = [e for e in os.listdir(os.path.join(ROOT, "tradio", "data"))
                  if os.path.isfile(os.path.join(ROOT, "tradio", "data", e, "timeline.json"))]
        if not events:
            self.skipTest("no timelines")
        for e in events:
            for k, r in _real(e).items():
                self.assertIn(r["status"], reaction.STATUSES, (e, k))
                if r["indicative"]:
                    self.assertIsNone(r["alarm_t"], (e, k))

    def test_contract_fields(self):
        res = _real("bowman20250623")
        if res is None:
            self.skipTest("no Bowman timeline")
        need = {"liquidity", "pre_mid", "alarm_t", "confirmed_by_trade", "move_ticks",
                "t50_min", "t90_min", "status", "reason", "placebo_p"}
        for k, r in res.items():
            self.assertTrue(need <= set(r), k)
            self.assertIn(r["status"], reaction.STATUSES)


# ------------------------------------------------------------------ placebo

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


class PlaceboCalendarTests(unittest.TestCase):
    def test_real_calendar_rules(self):
        """With the real calendar: after listing (2025-06-18), no Fed-event days."""
        fakes = placebo.fake_t0s("bowman20250623", T0, ["KXFEDDECISION-25JUL-C25"])
        excluded = placebo.excluded_days()
        self.assertTrue(fakes)
        for f in fakes:
            self.assertGreater(f.date(), date(2025, 6, 18))
            self.assertNotIn(f.date(), excluded)
        for d in (date(2025, 6, 24), date(2025, 6, 25), date(2025, 7, 1), date(2025, 7, 9)):
            self.assertIn(d, excluded)          # testimony x2, Sintra, June minutes


class PlaceboTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        os.makedirs(os.path.join(self.tmp, "testevent"))
        with open(os.path.join(self.tmp, "testevent", "timeline.json"), "w") as f:
            json.dump(stepped_timeline(), f)
        # Synthetic calendar: no listing date, no Fed-event days, so enough fake days exist.
        self.patches = [mock.patch.object(placebo, "DATA_DIR", self.tmp),
                        mock.patch.object(reaction, "DATA_DIR", self.tmp),
                        mock.patch.object(placebo, "LISTED", {}),
                        mock.patch.object(placebo, "excluded_days", lambda: set())]
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
        for f in fakes:
            self.assertEqual(f.timetz(), T0.timetz())
            self.assertLess(f.weekday(), 5)
            self.assertNotEqual(f.date(), T0.date())
            self.assertLessEqual(f.date(), date(2025, 7, 28))   # 2 days before Jul 30 FOMC

    def test_decision_day(self):
        self.assertEqual(placebo.decision_day("KXFEDDECISION-25JUL-C25"), date(2025, 7, 30))
        self.assertEqual(placebo.decision_day("KXFEDDECISION-26SEP-H0"), date(2026, 9, 16))
        self.assertIsNone(placebo.decision_day("KXRATECUTCOUNT-25DEC31-T0"))

    def _fetch_with(self, path_for_window):
        """Run placebo.fetch with role 1's network call mocked; conversion is role 1's."""
        from tradio import ingest_markets  # role 1's module, network call mocked
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

    def test_import_compact_matches_fetch(self):
        # noisy windows with imputed-looking flat stretches; compact export of the same
        # windows must give exactly the same fake moves as the full windows
        self._fetch_with(lambda k: (lambda i: 0.40 + 0.005 * ((i * k) % 7) + (0.03 if i >= 93 else 0)))
        full = placebo.run("testevent")["markets"][MID]["fake_moves"]
        path = os.path.join(self.tmp, "testevent", "placebo_windows.json")
        with open(path) as f:
            windows = json.load(f)["windows"][MID]
        lines = []
        for key, rows in windows.items():
            ft = parse_ts(key)
            segs = []
            for t, m, _ in rows:          # fetch rows here are never imputed
                off = round((parse_ts(t) - ft).total_seconds() / 60)
                if not (-10 < off <= 0 or 75 <= off <= 90):
                    continue
                v = round(m * 10000)
                if segs and segs[-1][1] == off - 1 and segs[-1][2] == v:
                    segs[-1][1] = off
                else:
                    segs.append([off, off, v])
            body = ",".join(f"{a}:{v}" if a == b else f"{a}_{b}:{v}" for a, b, v in segs)
            lines.append(f"C25|{int(ft.timestamp())} {body}")
        src = os.path.join(self.tmp, "testevent", "placebo_compact.dat")
        with open(src, "w") as f:
            f.write("\n".join(lines) + "\nC25|1 \n")    # plus one empty window
        os.remove(path)
        placebo.main(["import", "testevent"])
        self.assertEqual(placebo.run("testevent")["markets"][MID]["fake_moves"], full)

    def test_fetch_resumes_from_cache(self):
        self._fetch_with(lambda k: (lambda i: 0.40))
        calls = self._fetch_with(lambda k: (lambda i: 0.40))
        self.assertEqual(calls, [])


if __name__ == "__main__":
    if "--write-fixture" in sys.argv:
        os.makedirs(os.path.dirname(FIXTURE), exist_ok=True)
        with open(FIXTURE, "w", encoding="utf-8") as f:
            json.dump(build_fixture(), f, indent=1)
        print("wrote", FIXTURE)
    else:
        unittest.main()
