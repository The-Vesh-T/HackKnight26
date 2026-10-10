"""Tradio playbook: trading rules learned from every replayed and recorded speech.

    python tradio/playbook.py            # rebuild tradio/data/playbook.json (+ web copy)

Every event under tradio/data/<event_id>/ (the history replays plus every live session
saved with `tradio/live.py archive`) is one observation:
  - how big the speaker's surprise was (top |z| of the scored speech, vs. their own history),
  - which Kalshi Fed-decision contracts reacted, how fast (role 2's alarm), how fully (t50/t90),
  - which were too thin to judge,
  - and, for recorded live sessions, what the desk's orders made or lost.

From those it writes plain rules the MarketPulse desk follows when started with
`python live.py --playbook` (see gate()):
  min_abs_z        trade only surprises at least this big. Learned as the midpoint between
                   the biggest surprise that did NOT move markets and the smallest one that did.
  trade_kinds      only contract types that have actually reacted before (hold, cut 25, ...).
  tradeable_liq    only liquidity classes in which reactions were measurable ("active").
  exit_after_min   planned exit: the typical time for 90% of a move (median t90).
  skip_if_alarmed  don't chase: if Tradio's live tab already shows the market moved, skip it.

Every rule carries the events it came from. With a handful of events these are rules of
thumb, not statistics; the page says so, and each archived speech updates them.
"""

import glob
import json
import os
import statistics
import sys
from datetime import datetime, timezone

if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tradio.common import DATA_DIR, WEB_DATA_DIR, read_json, to_unix, write_json  # noqa: E402

PLAYBOOK = os.path.join(DATA_DIR, "playbook.json")
WEB_PLAYBOOK = os.path.join(WEB_DATA_DIR, "playbook.json")
LIVE_ANALYSIS = os.path.join(WEB_DATA_DIR, "live", "analysis.json")

# Same mapping as MarketPulse's live.py FED_OUTCOME_SIDES: which side a surprise favors.
KINDS = {"H0": "hold", "H25": "hike_small", "H26": "hike_big", "C25": "cut_small", "C26": "cut_big"}
SIDE = {"hold": {"HAWKISH": "no", "DOVISH": "yes"},
        "hike_small": {"HAWKISH": "yes", "DOVISH": "no"}, "hike_big": {"HAWKISH": "yes", "DOVISH": "no"},
        "cut_small": {"HAWKISH": "no", "DOVISH": "yes"}, "cut_big": {"HAWKISH": "no", "DOVISH": "yes"}}
CUT_KINDS, HIKE_KINDS = ("cut_small", "cut_big"), ("hike_small", "hike_big")
Z_FLOOR = 2.0                 # never below MarketPulse's own surprise threshold
DEFAULT_EXIT_MIN = 30


def kind_of(ticker: str):
    parts = ticker.split("-")
    return KINDS.get(parts[-1]) if parts[0] == "KXFEDDECISION" else None


def regime(mids: dict):
    """'easing' if the market prices a cut as likelier than a hike at the next meeting,
    'tightening' if the reverse, None if it can't tell. mids: {kind: yes price}."""
    cut = sum(mids.get(k) or 0 for k in CUT_KINDS)
    hike = sum(mids.get(k) or 0 for k in HIKE_KINDS)
    if abs(cut - hike) < 0.02:
        return None
    return "easing" if cut > hike else "tightening"


def side_for(kind: str, direction: str, reg) -> str:
    """Which side of a decision contract a surprise favors. Cuts and hikes are fixed, but
    'hold' flips with the regime: when the market leans to a cut, a dovish surprise makes
    the cut likelier and the hold LESS likely; when it leans to a hike, a dovish surprise
    makes the hold MORE likely. MarketPulse's fixed table assumed the second case."""
    if kind == "hold" and reg == "easing":
        return "no" if direction == "DOVISH" else "yes"
    return SIDE[kind][direction]


def observe(event_id: str):
    """One event -> {event, top_z, direction, markets: [...], trades: [...]} or None."""
    d = os.path.join(DATA_DIR, event_id)
    try:
        tl, an = read_json(os.path.join(d, "timeline.json")), read_json(os.path.join(d, "analysis.json"))
    except (OSError, ValueError):
        return None
    if an.get("provisional"):
        return None                                   # unfinished window: no final verdicts yet
    paras = [p for p in (tl.get("speech") or {}).get("paragraphs", []) if p.get("relevant") and p.get("z") is not None]
    if not paras:
        return None
    top = max(paras, key=lambda p: abs(p["z"]))
    direction = "DOVISH" if top["z"] < 0 else "HAWKISH"
    t0 = to_unix(tl["t0"])
    pre = {}
    for q in tl.get("quotes", []):                    # last quote at or before T0, per contract
        if to_unix(q["t"]) <= t0 and q.get("mid") is not None:
            pre[q["market"]] = q["mid"]
    reg = regime({kind_of(m["ticker"]): pre.get(m["id"]) for m in tl["markets"]
                  if m.get("venue") == "kalshi" and m.get("exposure") == "direct" and kind_of(m["ticker"])})
    markets = []
    for m in tl["markets"]:
        if m.get("venue") != "kalshi" or m.get("exposure") != "direct":
            continue
        k, r = kind_of(m["ticker"]), an["markets"].get(m["id"])
        if not k or not r:
            continue
        side = side_for(k, direction, reg)
        fixed = SIDE[k][direction]
        move = r.get("move_ticks")
        markets.append({
            "market": m["id"], "kind": k, "status": r["status"], "liquidity": r["liquidity"],
            "alarm_min": round((to_unix(r["alarm_t"]) - t0) / 60, 1) if r.get("alarm_t") else None,
            "t50_min": r.get("t50_min"), "t90_min": r.get("t90_min"), "move_ticks": move,
            "side": side,
            "direction_right": (None if r["status"] != "reacted" or not move
                                else (move > 0) == (side == "yes")),
            "fixed_table_right": (None if r["status"] != "reacted" or not move
                                  else (move > 0) == (fixed == "yes")),
        })
    desk = (tl.get("desk") or {}).get("trades", [])
    return {"event": event_id, "title": tl.get("title"), "top_z": round(top["z"], 2), "direction": direction,
            "regime": reg,
            "reacted": any(x["status"] == "reacted" for x in markets), "markets": markets,
            "trades": [t for t in desk if t.get("pnl_at_exit") is not None]}


def learn(obs: list) -> dict:
    reacted_z = [abs(o["top_z"]) for o in obs if o["reacted"]]
    quiet_z = [abs(o["top_z"]) for o in obs if not o["reacted"]]
    if reacted_z and quiet_z and max(quiet_z) < min(reacted_z):
        z_thr = (max(quiet_z) + min(reacted_z)) / 2
        z_why = (f"midpoint between the biggest surprise that didn't move markets (|z| {max(quiet_z):.2f}) "
                 f"and the smallest that did (|z| {min(reacted_z):.2f})")
    elif reacted_z:
        z_thr = min(reacted_z)
        z_why = f"smallest surprise that moved markets (|z| {min(reacted_z):.2f}); no clean separation yet"
    else:
        z_thr, z_why = Z_FLOOR, "no reactions recorded yet; using MarketPulse's default"
    z_thr = round(max(Z_FLOOR, z_thr), 2)

    rows = [(o, m) for o in obs for m in o["markets"]]
    reacted = [(o, m) for o, m in rows if m["status"] == "reacted"]
    kinds = sorted({m["kind"] for _, m in reacted})
    liq = sorted({m["liquidity"] for _, m in reacted})
    t90s = [m["t90_min"] for _, m in reacted if m["t90_min"] is not None]
    alarms = [m["alarm_min"] for _, m in reacted if m["alarm_min"] is not None]
    right = [m["direction_right"] for _, m in reacted if m["direction_right"] is not None]
    fixed = [m["fixed_table_right"] for _, m in reacted if m["fixed_table_right"] is not None]
    trades = [t for o in obs for t in o["trades"]]

    by_kind = {}
    for o, m in rows:
        k = by_kind.setdefault(m["kind"], {"seen": 0, "reacted": 0, "events": []})
        k["seen"] += 1
        k["reacted"] += m["status"] == "reacted"
        k["events"].append(f"{o['event']}: {m['status']}")

    return {
        "min_abs_z": {"value": z_thr, "why": z_why},
        "trade_kinds": {"value": kinds, "why": "contract types that have reacted to a surprise at least once",
                        "by_kind": by_kind},
        "tradeable_liquidity": {"value": liq or ["active"],
                                "why": "liquidity classes where reactions could be measured; thinner markets were 'can't tell'"},
        "exit_after_min": {"value": round(statistics.median(t90s)) if t90s else DEFAULT_EXIT_MIN,
                           "why": (f"median time to 90% of the move across {len(t90s)} reactions ({sorted(t90s)} min)"
                                   if t90s else "no measured reactions yet; default")},
        "skip_if_alarmed": {"value": True,
                            "why": (f"past alarms came {sorted(alarms)} min after T0; once a market has moved "
                                    "the surprise is priced in, so don't chase")},
        "direction_hit_rate": {"value": (round(sum(right) / len(right), 2) if right else None),
                               "why": (f"{sum(right)}/{len(right)} reactions moved the way the regime-aware mapping "
                                       f"predicts; MarketPulse's fixed table got {sum(fixed)}/{len(fixed)}")},
        "hold_side_by_regime": {"value": True,
                                "why": ("'hold' flips with the regime: in an easing market a dovish surprise "
                                        "lowers the hold (Bowman 2025), in a tightening market it raises it")},
        "desk_record": {
            "trades": len(trades),
            "pnl_at_exit": round(sum(t["pnl_at_exit"] for t in trades), 2) if trades else None,
            "why": "orders from recorded live sessions, marked at the planned exit with real Kalshi prices",
        },
    }


# ------------------------------------------------------------------ backtest

LATENCY_S = 60          # text released at T0 -> scored -> order sent; MarketPulse's live desk
                        # takes seconds, so one minute (the next closed candle) is conservative
BACKTEST_QTY = 10


def kalshi_fee(price: float) -> float:
    """Kalshi taker fee per contract (0.07 * p * (1 - p)), before rounding up to the cent."""
    return 0.07 * price * (1 - price)


def backtest(event_id: str, obs_all: list, latency_s: int = LATENCY_S, qty: int = BACKTEST_QTY) -> dict:
    """What the desk would have done on this event with rules learned from the OTHER events.
    Entry: the first closed candle at least latency_s after T0, buying at the ask (YES) or
    1 - bid (NO). Exit: exit_after_min later, selling at the bid (YES) or 1 - ask (NO).
    Taker fees on both legs. Prices are real Kalshi candles from timeline.json."""
    others = [o for o in obs_all if o["event"] != event_id]
    me = next((o for o in obs_all if o["event"] == event_id), None)
    tl = read_json(os.path.join(DATA_DIR, event_id, "timeline.json"))
    out = {"event_id": event_id, "method": "leave-one-out",
           "learned_from": [o["event"] for o in others], "latency_s": latency_s, "qty": qty,
           "trades": [], "skipped": []}
    if not me or not others:
        out["note"] = "not enough other events to learn from"
        return out
    rules = learn(others)
    out["rules"] = {k: v["value"] for k, v in rules.items() if isinstance(v, dict) and "value" in v}
    t0 = to_unix(tl["t0"])
    entry_t, exit_t = t0 + latency_s, t0 + latency_s + rules["exit_after_min"]["value"] * 60
    quotes = {}
    for q in tl["quotes"]:
        quotes.setdefault(q["market"], []).append(q)

    def at(mid, t):                                   # first closed candle at or after t
        for q in quotes.get(mid, []):
            if to_unix(q["t"]) >= t and q.get("bid") is not None and q.get("ask") is not None:
                return q
        return None

    def before(mid, t):
        last = None
        for q in quotes.get(mid, []):
            if to_unix(q["t"]) <= t and q.get("mid") is not None:
                last = q
        return last

    z = me["top_z"]
    reg = regime({kind_of(m["ticker"]): (before(m["id"], t0) or {}).get("mid") for m in tl["markets"]
                  if m.get("venue") == "kalshi" and kind_of(m["ticker"])})
    cands = []
    for m in tl["markets"]:
        if m.get("venue") != "kalshi" or m.get("exposure") != "direct" or not kind_of(m["ticker"]):
            continue
        e = at(m["id"], entry_t)
        cands.append({"venue": "kalshi", "market": m["ticker"], "label": m["label"],
                      "side": SIDE[kind_of(m["ticker"])]["DOVISH" if z < 0 else "HAWKISH"],
                      "quote": {"best_bid": e and e["bid"], "best_ask": e and e["ask"]}})
    # Liquidity is known before T0 (role 2's class); "already moved" can't be true 1 min in
    # for these events, but use role 2's alarm time to check honestly.
    an = read_json(os.path.join(DATA_DIR, event_id, "analysis.json"))
    live_like = {"markets": {}}
    for mid, r in an["markets"].items():
        moved = r.get("alarm_t") and to_unix(r["alarm_t"]) <= entry_t
        live_like["markets"][mid] = {"liquidity": r["liquidity"], "status": "alarm" if moved else "watching"}
    keep, skipped = gate(cands, z, {"rules": rules}, live_like, reg)
    out["skipped"] = [{"market": m["market"], "label": m["label"], "why": m["error"].replace("playbook: ", "")}
                      for m in skipped]
    for m in keep:
        mid = f"kalshi:{m['market']}"
        e, x = at(mid, entry_t), at(mid, exit_t)
        if not e or not x:
            out["skipped"].append({"market": m["market"], "label": m["label"], "why": "no quotes at entry/exit"})
            continue
        if m["side"] == "yes":
            cost, value = e["ask"], x["bid"]
        else:
            cost, value = 1 - e["bid"], 1 - x["ask"]
        fees = kalshi_fee(cost) + kalshi_fee(value)
        pnl = qty * (value - cost - fees)
        out["trades"].append({
            "market": mid, "label": m["label"], "side": m["side"], "qty": qty,
            "entry_t": e["t"], "entry_yes_bid": e["bid"], "entry_yes_ask": e["ask"], "cost": round(cost, 4),
            "exit_t": x["t"], "exit_yes_bid": x["bid"], "exit_yes_ask": x["ask"], "value": round(value, 4),
            "fees": round(qty * fees, 4), "pnl": round(pnl, 2),
            "return_pct": round(100 * pnl / (qty * cost), 1) if cost else None,
            "side_fixed": m["playbook"].get("side_fixed"),
        })
    out["regime"], out["z"] = reg, z
    out["total_pnl"] = round(sum(t["pnl"] for t in out["trades"]), 2)
    out["capital"] = round(sum(t["qty"] * t["cost"] for t in out["trades"]), 2)
    return out


def build() -> dict:
    events = sorted(e for e in os.listdir(DATA_DIR)
                    if e != "live" and os.path.isfile(os.path.join(DATA_DIR, e, "timeline.json")))
    obs = [o for o in (observe(e) for e in events) if o]
    pb = {"built_at": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
          "n_events": len(obs),
          "caveat": (f"Learned from {len(obs)} events. Rules of thumb, not statistics; every recorded "
                     "speech updates them. Not trading advice."),
          "rules": learn(obs), "events": obs}
    for path in (PLAYBOOK, WEB_PLAYBOOK):
        write_json(path, pb)
    for o in obs:                                     # leave-one-out backtest per event
        bt = backtest(o["event"], obs)
        for base in (DATA_DIR, WEB_DATA_DIR):
            if os.path.isdir(os.path.join(base, o["event"])):
                write_json(os.path.join(base, o["event"], "backtest.json"), bt)
        pb.setdefault("backtests", []).append(
            {"event": o["event"], "trades": len(bt["trades"]), "total_pnl": bt.get("total_pnl"),
             "capital": bt.get("capital")})
    for path in (PLAYBOOK, WEB_PLAYBOOK):
        write_json(path, pb)
    return pb


def load(path: str = PLAYBOOK):
    try:
        return read_json(path)
    except (OSError, ValueError):
        return None


def candidate_regime(candidates: list):
    """Regime from the desk's own fresh quotes on the next meeting's contracts."""
    mids = {}
    for m in candidates:
        q, k = m.get("quote") or {}, kind_of(m.get("market", ""))
        if k and m.get("venue") == "kalshi" and q.get("best_bid") is not None and q.get("best_ask") is not None:
            mids[k] = (q["best_bid"] + q["best_ask"]) / 2
    return regime(mids)


def gate(candidates: list, z: float, playbook: dict, live_analysis: dict = None, reg=None):
    """Split the desk's candidate markets into (keep, skipped) by the playbook's rules.
    candidates are MarketPulse watchlist entries (with fresh quotes): {"venue", "market",
    "side", "quote", ...}. The side of 'hold' contracts is corrected for the regime."""
    rules = playbook["rules"]
    keep, skipped = [], []
    reg = reg or candidate_regime(candidates)
    direction = "DOVISH" if z < 0 else "HAWKISH"

    def skip(m, why):
        skipped.append({**m, "error": f"playbook: {why}"})

    if abs(z) < rules["min_abs_z"]["value"]:
        for m in candidates:
            skip(m, f"|z| {abs(z):.2f} below learned threshold {rules['min_abs_z']['value']}")
        return keep, skipped
    live_markets = (live_analysis or {}).get("markets", {})
    for m in candidates:
        if m.get("venue") != "kalshi":
            skip(m, "only Kalshi (Polymarket history is indicative)")
            continue
        k = kind_of(m["market"])
        if k not in rules["trade_kinds"]["value"]:
            skip(m, f"{k or 'this contract type'} has never reacted in past events")
            continue
        r = live_markets.get(f"kalshi:{m['market']}")
        if r and r.get("liquidity") not in rules["tradeable_liquidity"]["value"]:
            skip(m, f"{r.get('liquidity')} right now; reactions in {r.get('liquidity')} markets weren't measurable")
            continue
        if r and rules["skip_if_alarmed"]["value"] and r.get("status") in ("alarm", "reacted"):
            skip(m, "already moved (Tradio alarm fired); too late to enter")
            continue
        side = side_for(k, direction, reg)
        note = {"exit_after_min": rules["exit_after_min"]["value"], "regime": reg}
        if side != m.get("side"):
            note["side_fixed"] = f"{m.get('side')} -> {side} ({reg} market)"
        keep.append({**m, "side": side,
                     "direction": "YES_UP" if side == "yes" else "YES_DOWN", "playbook": note})
    return keep, skipped


def main():
    pb = build()
    r = pb["rules"]
    print(f"playbook from {pb['n_events']} events -> {PLAYBOOK}")
    print(f"  min |z|          {r['min_abs_z']['value']}   ({r['min_abs_z']['why']})")
    print(f"  trade kinds      {', '.join(r['trade_kinds']['value'])}")
    print(f"  liquidity        {', '.join(r['tradeable_liquidity']['value'])}")
    print(f"  exit after       {r['exit_after_min']['value']} min   ({r['exit_after_min']['why']})")
    print(f"  direction right  {r['direction_hit_rate']['value']}   ({r['direction_hit_rate']['why']})")
    for o in pb["events"]:
        print(f"  {o['event']:<18} top z {o['top_z']:+.2f} {o['direction']:<8} {str(o['regime']):<10} reacted={o['reacted']}")
    print("leave-one-out backtests (rules learned from the other events, real Kalshi prices, fees in):")
    for b in pb.get("backtests", []):
        bt = read_json(os.path.join(DATA_DIR, b["event"], "backtest.json"))
        print(f"  {b['event']:<18} {b['trades']} trade(s)  P&L ${b['total_pnl']:+.2f} on ${b['capital']:.2f}")
        for t in bt["trades"]:
            print(f"     BUY {t['side'].upper()} {t['qty']} {t['label']:<22} {t['entry_t'][11:16]} @ {t['cost']:.2f} "
                  f"-> {t['exit_t'][11:16]} @ {t['value']:.2f}  ${t['pnl']:+.2f}" + (f"  [{t['side_fixed']}]" if t['side_fixed'] else ""))
        for sk in bt["skipped"][:6]:
            print(f"     skip {sk['label']:<22} {sk['why']}")


if __name__ == "__main__":
    main()
