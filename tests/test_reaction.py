"""Tests for role 2's tradio/reaction.py on synthetic timelines.

    python -m unittest tests.test_reaction
"""

import json
import os
import sys
import unittest
from datetime import datetime, timedelta, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from tradio import reaction  # noqa: E402

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


if __name__ == "__main__":
    if "--write-fixture" in sys.argv:
        os.makedirs(os.path.dirname(FIXTURE), exist_ok=True)
        with open(FIXTURE, "w", encoding="utf-8") as f:
            json.dump(build_fixture(), f, indent=1)
        print("wrote", FIXTURE)
    else:
        unittest.main()
