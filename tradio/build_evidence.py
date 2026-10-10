"""
tradio/build_evidence.py  (role 1)
==================================
Writes tradio/data/<event_id>/evidence.json (and a copy in web/data/tradio/<event_id>/)
for the page's evidence drawer: for every market, the key facts a viewer might click,
each backed by the RAW rows exactly as Kalshi/Polymarket returned them.

Per market:
  pre_event          last quote at or before T0 (+ its raw candle)
  first_quote_change first minute after T0 whose bid or ask differs (+ raw candles before/after)
  first_trade_outside_spread first trade after T0 priced outside the pre-T0 bid-ask
  biggest_trades     the 3 largest trades in T0..T0+30 min (raw rows)
  end_of_window      last quote in the window (+ raw candle)
  source             endpoint, query window, and raw file it came from

Run after build_timeline.py:
    python tradio/build_evidence.py
"""

import argparse
import os
import shutil
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tradio.common import DATA_DIR, WEB_DATA_DIR, load_events, parse_ts, raw_dir, read_json, write_json  # noqa: E402

KALSHI = "https://api.elections.kalshi.com/trade-api/v2"


def ts(text: str) -> float:
    return parse_ts(text).timestamp()


def kalshi_evidence(event_id: str, market: dict, timeline: dict) -> dict:
    ticker = market["ticker"]
    rdir = raw_dir(event_id)
    candles_raw = read_json(os.path.join(rdir, f"kalshi_candles_{ticker}.json"))
    trades_raw = read_json(os.path.join(rdir, f"kalshi_trades_{ticker}.json"))
    by_end = {int(c["end_period_ts"]): c for c in candles_raw["response"].get("candlesticks", [])}
    t0 = ts(timeline["t0"])
    quotes = [q for q in timeline["quotes"] if q["market"] == market["id"]]
    trades = [t for t in timeline["trades"] if t["market"] == market["id"]]
    raw_trades = trades_raw["trades"]

    def raw_candle(q):
        return None if q is None or q["imputed"] else by_end.get(int(ts(q["t"])))

    def raw_trade(t):
        return next((r for r in raw_trades if abs(ts(r["created_time"]) - ts(t["t"])) < 1e-3
                     and float(r.get("count_fp") or r.get("count") or 0) == float(t["size"])), None)

    pre = next((q for q in reversed(quotes) if ts(q["t"]) <= t0), None)
    after = [q for q in quotes if ts(q["t"]) > t0]
    change = next((q for q in after if pre and not q["imputed"]
                   and (q["bid"], q["ask"]) != (pre["bid"], pre["ask"])), None)
    prev_of_change = None
    if change:
        prev_of_change = next((q for q in reversed(quotes) if ts(q["t"]) < ts(change["t"])), None)
    lo = pre["bid"] if pre else None
    hi = pre["ask"] if pre else None
    new_level = None
    if lo is not None and hi is not None:
        new_level = next((t for t in trades if ts(t["t"]) > t0
                          and (t["price"] < lo - 1e-9 or t["price"] > hi + 1e-9)), None)
    early = [t for t in trades if t0 <= ts(t["t"]) <= t0 + 1800]
    biggest = sorted(early, key=lambda t: float(t["size"]), reverse=True)[:3]
    end = quotes[-1] if quotes else None

    source_trades = ("historical/trades" if "historical" in str(candles_raw["response"].get("_source", "historical"))
                     else "markets/trades")
    return {
        "market": market["id"], "label": market["label"], "venue": "kalshi",
        "pre_event": {"quote": pre, "raw_candle": raw_candle(pre)},
        "first_quote_change": None if change is None else {
            "quote": change, "raw_candle": raw_candle(change),
            "previous_quote": prev_of_change, "previous_raw_candle": raw_candle(prev_of_change),
            "minutes_after_t0": round((ts(change["t"]) - t0) / 60, 1)},
        "first_trade_outside_spread": None if new_level is None else {
            "trade": new_level, "raw_trade": raw_trade(new_level),
            "pre_event_spread": [lo, hi],
            "seconds_after_t0": round(ts(new_level["t"]) - t0, 3)},
        "biggest_trades": [{"trade": t, "raw_trade": raw_trade(t)} for t in biggest],
        "end_of_window": {"quote": end, "raw_candle": raw_candle(end)},
        "source": {
            "candles": {"endpoint": f"{KALSHI}/" + ("historical/markets/{ticker}/candlesticks"
                                                    if candles_raw["response"].get("_source", "historical") == "historical"
                                                    else "series/{series}/markets/{ticker}/candlesticks"),
                        "start_ts": candles_raw.get("start_ts"), "end_ts": candles_raw.get("end_ts"),
                        "period_interval": 1, "raw_file": f"tradio/data/{event_id}/raw/kalshi_candles_{ticker}.json"},
            "trades": {"endpoint_hint": f"{KALSHI}/{source_trades}", "min_ts": trades_raw.get("min_ts"),
                       "max_ts": trades_raw.get("max_ts"),
                       "raw_file": f"tradio/data/{event_id}/raw/kalshi_trades_{ticker}.json"},
            "note": "Quote time = END of the 1-minute candle (verified: candle volume equals trades in the minute before end_period_ts).",
        },
    }


def poly_evidence(event_id: str, market: dict, timeline: dict) -> dict:
    t0 = ts(timeline["t0"])
    pts = [q for q in timeline["quotes"] if q["market"] == market["id"]]
    pre = next((q for q in reversed(pts) if ts(q["t"]) <= t0), None)
    return {"market": market["id"], "label": market["label"], "venue": "polymarket", "indicative": True,
            "pre_event": {"point": pre}, "end_of_window": {"point": pts[-1] if pts else None},
            "conflicting_points": [q for q in pts if q.get("conflict")],
            "source": {"endpoint": "https://clob.polymarket.com/prices-history", "token": market["token"],
                       "fidelity": 1, "raw_file": f"tradio/data/{event_id}/raw/{market['id'].replace('polymarket:', 'poly_')}.json",
                       "note": "Indicative only: undocumented whether p is a trade or a midpoint."}}


def build(event_id: str) -> str:
    timeline = read_json(os.path.join(DATA_DIR, event_id, "timeline.json"))
    markets = [kalshi_evidence(event_id, m, timeline) if m["venue"] == "kalshi"
               else poly_evidence(event_id, m, timeline) for m in timeline["markets"]]
    out = {"event_id": event_id, "t0": timeline["t0"], "markets": markets}
    path = os.path.join(DATA_DIR, event_id, "evidence.json")
    write_json(path, out)
    web_dir = os.path.join(WEB_DATA_DIR, event_id)
    os.makedirs(web_dir, exist_ok=True)
    shutil.copyfile(path, os.path.join(web_dir, "evidence.json"))
    return path


def main():
    parser = argparse.ArgumentParser(description="Build evidence.json for the page's evidence drawer")
    parser.add_argument("--event", action="append", help="event id (default: every event with a timeline)")
    args = parser.parse_args()
    events = args.event or [e for e in load_events()["events"]
                            if os.path.isfile(os.path.join(DATA_DIR, e, "timeline.json"))]
    for event_id in events:
        path = build(event_id)
        ev = read_json(path)
        for m in ev["markets"]:
            if m["venue"] != "kalshi":
                continue
            fc = m["first_quote_change"]
            nl = m["first_trade_outside_spread"]
            print(f"{event_id} {m['label']:<22} first quote change: "
                  f"{(fc['quote']['t'][11:16] + ' (+' + str(fc['minutes_after_t0']) + ' min)') if fc else '—':<20} "
                  f"first trade outside pre-T0 spread: {(nl['trade']['t'][11:19] + ' @ ' + str(nl['trade']['price'])) if nl else '—'}")
        print(f"  -> {path}")


if __name__ == "__main__":
    main()
