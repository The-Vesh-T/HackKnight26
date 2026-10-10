"""Tradio playbook (tradio/playbook.py) and the live desk's P&L marking (tradio/live.py).

    python -m unittest tests.test_playbook
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tradio import live  # noqa: E402
from tradio import playbook as pb  # noqa: E402


def obs_all():
    return [o for o in (pb.observe(e) for e in ("bowman20250623", "waller20250717", "warsh20260828")) if o]


class RulesTest(unittest.TestCase):
    def test_threshold_separates_waller_from_the_reactions(self):
        rules = pb.learn(obs_all())
        self.assertGreater(rules["min_abs_z"]["value"], 2.84)     # Waller didn't move markets
        self.assertLess(rules["min_abs_z"]["value"], 3.05)        # Warsh did

    def test_hold_side_follows_the_regime(self):
        self.assertEqual(pb.side_for("hold", "DOVISH", "easing"), "no")       # Bowman 2025
        self.assertEqual(pb.side_for("hold", "DOVISH", "tightening"), "yes")
        self.assertEqual(pb.side_for("hold", "HAWKISH", "tightening"), "no")  # Warsh 2026
        self.assertEqual(pb.side_for("cut_small", "DOVISH", "easing"), "yes")

    def test_regime_aware_mapping_beats_the_fixed_table(self):
        rules = pb.learn(obs_all())
        self.assertEqual(rules["direction_hit_rate"]["value"], 1.0)
        self.assertIn("3/4", rules["direction_hit_rate"]["why"])

    def test_regimes_read_from_pre_t0_prices(self):
        regs = {o["event"]: o["regime"] for o in obs_all()}
        self.assertEqual(regs, {"bowman20250623": "easing", "waller20250717": "easing",
                                "warsh20260828": "tightening"})


class GateTest(unittest.TestCase):
    rules = {"rules": pb.learn(obs_all())}

    def cand(self, suffix, side="yes", venue="kalshi", bid=0.30, ask=0.31):
        return {"venue": venue, "market": f"KXFEDDECISION-26OCT-{suffix}", "side": side,
                "quote": {"best_bid": bid, "best_ask": ask}}

    def test_small_surprise_trades_nothing(self):
        keep, skipped = pb.gate([self.cand("H0")], -2.5, self.rules)
        self.assertEqual(keep, [])
        self.assertIn("below learned threshold", skipped[0]["error"])

    def test_skips_polymarket_untested_kinds_illiquid_and_already_moved(self):
        live_an = {"markets": {"kalshi:KXFEDDECISION-26OCT-C25": {"liquidity": "thin", "status": "watching"},
                               "kalshi:KXFEDDECISION-26OCT-H25": {"liquidity": "active", "status": "alarm"}}}
        c = [self.cand("C26"), self.cand("C25"), self.cand("H25"),
             {"venue": "polymarket", "market": "x", "side": "yes", "quote": {}}]
        keep, skipped = pb.gate(c, 3.4, self.rules, live_an, "tightening")
        self.assertEqual(keep, [])
        why = " | ".join(s["error"] for s in skipped)
        for text in ("never reacted", "thin right now", "already moved", "only Kalshi"):
            self.assertIn(text, why)

    def test_corrects_the_hold_side_in_an_easing_market(self):
        keep, _ = pb.gate([self.cand("H0", side="yes")], -3.4, self.rules, None, "easing")
        self.assertEqual(keep[0]["side"], "no")
        self.assertIn("yes -> no", keep[0]["playbook"]["side_fixed"])


class BacktestTest(unittest.TestCase):
    def test_leave_one_out_never_learns_from_the_event_it_trades(self):
        o = obs_all()
        for e in ("bowman20250623", "waller20250717", "warsh20260828"):
            bt = pb.backtest(e, o)
            self.assertNotIn(e, bt["learned_from"])

    def test_bowman_trade_uses_real_prices_and_fees(self):
        bt = pb.backtest("bowman20250623", obs_all())
        self.assertEqual(len(bt["trades"]), 1)
        t = bt["trades"][0]
        self.assertEqual((t["market"], t["side"]), ("kalshi:KXFEDDECISION-25JUL-H0", "no"))
        self.assertEqual(t["entry_t"], "2025-06-23T14:01:00Z")
        self.assertAlmostEqual(t["cost"], 1 - t["entry_yes_bid"])
        self.assertAlmostEqual(t["value"], 1 - t["exit_yes_ask"])
        gross = t["qty"] * (t["value"] - t["cost"])
        self.assertLess(t["pnl"], gross)                                 # fees came off

    def test_running_pnl_ends_at_the_final_pnl(self):
        t = pb.backtest("bowman20250623", obs_all())["trades"][0]
        path = t["pnl_path"]
        self.assertEqual((path[0]["t"], path[-1]["t"]), (t["entry_t"], t["exit_t"]))
        self.assertEqual(len(path), 30)                                  # one point per closed candle
        self.assertAlmostEqual(path[-1]["pnl"], t["pnl"], places=2)
        self.assertLess(path[0]["pnl"], 0)                               # entering costs the spread and fees

    def test_account_view_follows_the_fixed_risk_rule(self):
        bt = pb.backtest("bowman20250623", obs_all())
        t = bt["trades"][0]
        a = t["account"]
        self.assertLessEqual(a["risk_usd"], pb.ACCOUNT_USD * pb.RISK_PCT / 100)
        per = t["value"] - t["cost"] - t["fees"] / t["qty"]               # t["pnl"] is rounded to cents
        self.assertAlmostEqual(a["pnl"], per * a["qty"], delta=a["qty"] * 0.0001 + 0.01)
        self.assertAlmostEqual(t["pnl_path"][-1]["pnl_account"], a["pnl"], places=2)
        self.assertEqual(bt["account"]["pnl"], a["pnl"])
        self.assertGreater(a["qty"], a["entry_candle_volume"])           # the page must warn: fills would be worse

    def test_waller_sits_out(self):
        self.assertEqual(pb.backtest("waller20250717", obs_all())["trades"], [])


class DeskMarkTest(unittest.TestCase):
    def test_marks_with_bid_for_yes_and_one_minus_ask_for_no(self):
        quotes = [{"t": "2026-10-03T14:01:00Z", "market": "kalshi:A", "bid": 0.30, "ask": 0.31, "mid": 0.305},
                  {"t": "2026-10-03T14:10:00Z", "market": "kalshi:A", "bid": 0.40, "ask": 0.41, "mid": 0.405}]
        st = {"mode": "demo", "trades": [
            {"time": "2026-10-03T14:00:30+00:00", "venue": "kalshi", "market": "A", "side": "yes", "qty": 10,
             "entry_quote": {"bid": 0.30, "ask": 0.31}, "status": "SENT (demo)", "playbook": {"exit_after_min": 5}},
            {"time": "2026-10-03T14:00:30+00:00", "venue": "kalshi", "market": "A", "side": "no", "qty": 10,
             "entry_quote": {"bid": 0.30, "ask": 0.31}, "status": "SENT (demo)"},
            {"time": "2026-10-03T14:00:30+00:00", "venue": "kalshi", "market": "B", "side": "yes",
             "skipped": True, "status": "skipped: playbook: thin"}]}
        d = live.desk_from_state(st, quotes, live.to_unix("2026-10-03T14:10:30Z"))
        yes, no, skip = d["trades"]
        self.assertAlmostEqual(yes["pnl_now"], 10 * (0.40 - 0.31))
        self.assertAlmostEqual(no["pnl_now"], 10 * ((1 - 0.41) - (1 - 0.30)))
        self.assertAlmostEqual(yes["pnl_at_exit"], 10 * (0.30 - 0.31))   # frozen at 14:05 (last quote 14:01)
        self.assertNotIn("pnl_now", skip)


if __name__ == "__main__":
    unittest.main()
