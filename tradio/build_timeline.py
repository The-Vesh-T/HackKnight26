"""
tradio/build_timeline.py  (role 1)
==================================
Turns the raw files from ingest_markets.py into the compact, schema-conforming
tradio/data/<event_id>/timeline.json (schema in CLAUDE.md), copies it to
web/data/tradio/<event_id>/ for the page, and writes tradio/data/NOTES.md with:

  * the candle-timing check (does a candle's volume match trades in the minute
    BEFORE its end_period_ts, as we assume, or the minute after?)
  * spot checks against values we verified by hand (events.yaml)
  * Polymarket chunk disagreements and imputed (missing) Kalshi minutes

    python tradio/build_timeline.py
    python tradio/build_timeline.py --event bowman20250623

Never smooths or fixes prices: missing Kalshi minutes are carried forward and marked
imputed; Polymarket points are marked indicative.
"""

import argparse
import os
import shutil
import sys
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tradio.common import (DATA_DIR, WEB_DATA_DIR, iso_from_unix, load_events,  # noqa: E402
                           parse_ts, raw_dir, read_json, to_unix, write_json)

DECISION_OUTCOMES = {"H26": "hike >25 bp", "H25": "hike 25 bp", "H0": "hold",
                     "C25": "cut 25 bp", "C26": "cut >25 bp"}
MONTHS = {"JAN": "Jan", "FEB": "Feb", "MAR": "Mar", "APR": "Apr", "MAY": "May", "JUN": "Jun",
          "JUL": "Jul", "AUG": "Aug", "SEP": "Sep", "OCT": "Oct", "NOV": "Nov", "DEC": "Dec"}


def kalshi_label(ticker: str) -> str:
    """KXFEDDECISION-25JUL-C25 -> 'Jul 2025: cut 25 bp'; KXRATECUTCOUNT-25DEC31-T2 ->
    '2025: 2 cuts'; KXRATEHIKE-2026-4PLUS -> '2026: 4+ hikes'. Falls back to the ticker."""
    parts = ticker.split("-")
    try:
        if parts[0] == "KXFEDDECISION":
            yy, mon = parts[1][:2], parts[1][2:5]
            return f"{MONTHS[mon]} 20{yy}: {DECISION_OUTCOMES[parts[2]]}"
        if parts[0] == "KXRATECUTCOUNT":
            n = int(parts[2].lstrip("T"))
            return f"20{parts[1][:2]}: {n} cut{'s' if n != 1 else ''}"
        if parts[0] == "KXRATEHIKE":
            n = parts[2].replace("PLUS", "+")
            return f"{parts[1]}: {n} hike{'' if n == '1' else 's'}"
    except (IndexError, KeyError, ValueError):
        pass
    return ticker


# ------------------------------------------------------------------ parsing helpers

def price(value):
    """Kalshi prices arrive as dollar strings ("0.1500") or, in older payloads, cents (15)."""
    if value is None or value == "":
        return None
    v = float(value)
    return round(v / 100.0, 4) if v > 1.5 else round(v, 4)


def size(value):
    if value is None or value == "":
        return 0
    v = float(value)
    return int(v) if v.is_integer() else v


def first(d: dict, *keys):
    for k in keys:
        if isinstance(d, dict) and d.get(k) not in (None, ""):
            return d[k]
    return None


def candle_side(candle: dict, side: str):
    obj = candle.get(side) or {}
    return price(first(obj, "close", "close_dollars")) if isinstance(obj, dict) else price(obj)


def norm_time(text: str) -> str:
    """Keep Kalshi's own precision, normalized to ...Z."""
    return parse_ts(text).isoformat().replace("+00:00", "Z")


# ------------------------------------------------------------------ builders

def kalshi_trades(raw: dict, market_id: str) -> list:
    out = []
    for t in raw["trades"]:
        p = price(first(t, "yes_price_dollars", "yes_price"))
        if p is None or not t.get("created_time"):
            continue
        out.append({"t": norm_time(t["created_time"]), "market": market_id, "price": p,
                    "size": size(first(t, "count_fp", "count")),
                    "taker_side": first(t, "taker_outcome_side", "taker_side")})
    out.sort(key=lambda x: x["t"])
    return out


def kalshi_quotes(raw: dict, market_id: str, start: int, end: int):
    """One quote per minute END in (start, end]; gaps carried forward with imputed=True."""
    by_end = {int(c["end_period_ts"]): c for c in raw["response"].get("candlesticks", [])}
    last, quotes, imputed = None, [], 0
    for ts in sorted(k for k in by_end if k <= start):          # seed from padding
        last = by_end[ts]
    for ts in range(start + 60, end + 1, 60):
        c = by_end.get(ts)
        is_imputed = c is None
        if c is not None:
            last = c
        if last is None:
            continue
        bid, ask = candle_side(last, "yes_bid"), candle_side(last, "yes_ask")
        mid = round((bid + ask) / 2, 4) if bid is not None and ask is not None else None
        vol = 0 if is_imputed else size(first(last, "volume", "volume_fp"))
        quotes.append({"t": iso_from_unix(ts), "market": market_id, "bid": bid, "ask": ask,
                       "mid": mid, "imputed": is_imputed, "volume": vol})
        imputed += is_imputed
    return quotes, imputed


def poly_quotes(raw: dict, market_id: str, start: int, end: int):
    """Merge chunks; when two chunks disagree about the same timestamp, keep the first
    value, mark it conflict=True, and count it (shown in NOTES.md)."""
    seen, conflicts = {}, 0
    for chunk in raw["chunks"]:
        for pt in chunk["response"].get("history", []):
            ts, p = int(pt["t"]), round(float(pt["p"]), 4)
            if not (start <= ts <= end):
                continue
            if ts in seen and abs(seen[ts]["mid"] - p) > 1e-9:
                seen[ts]["conflict"] = True
                conflicts += 1
            elif ts not in seen:
                seen[ts] = {"t": iso_from_unix(ts), "market": market_id, "bid": None, "ask": None,
                            "mid": p, "imputed": False, "volume": None}
    return [seen[k] for k in sorted(seen)], conflicts


def candle_timing(raw_candles: dict, trades: list, start: int, end_window: int) -> dict:
    """For in-window candles with volume, compare with trade volume in [end-60, end) vs
    [end, end+60). Padding candles are skipped: we only pull trades inside the window."""
    vol_by_sec = defaultdict(float)
    for t in trades:
        vol_by_sec[int(parse_ts(t["t"]).timestamp())] += float(t["size"])
    def window_sum(a, b):
        return sum(v for s, v in vol_by_sec.items() if a <= s < b)
    before = after = checked = 0
    for c in raw_candles["response"].get("candlesticks", []):
        v = float(first(c, "volume", "volume_fp") or 0)
        if v <= 0:
            continue
        end = int(c["end_period_ts"])
        if not (start < end <= end_window):
            continue
        checked += 1
        before += abs(window_sum(end - 60, end) - v) < 0.5
        after += abs(window_sum(end, end + 60) - v) < 0.5
    return {"candles_checked": checked, "match_minute_before_end": before, "match_minute_after_end": after}


def run_spot_checks(checks: list, trades: list, quotes: list) -> list:
    results = []
    for chk in checks or []:
        mid = f"kalshi:{chk['ticker']}"
        target = parse_ts(chk["t"]).timestamp()
        if chk["kind"] == "trade":
            ok = any(t["market"] == mid and abs(parse_ts(t["t"]).timestamp() - target) < 1.0
                     and abs(t["price"] - chk["price"]) < 1e-6 for t in trades)
            desc = f"trade {chk['ticker']} {chk['t']} @ {chk['price']}"
        else:
            ok = any(q["market"] == mid and abs(parse_ts(q["t"]).timestamp() - target) < 1.0
                     and q["bid"] == chk["bid"] and q["ask"] == chk["ask"] for q in quotes)
            desc = f"quote {chk['ticker']} {chk['t']} {chk['bid']}/{chk['ask']}"
        results.append(("PASS" if ok else "FAIL", desc))
    return results


def build(event_id: str, cfg: dict, spot: list) -> list:
    rdir = raw_dir(event_id)
    start, end = to_unix(cfg["window"]["start"]), to_unix(cfg["window"]["end"])
    markets, quotes, trades, notes = [], [], [], []
    for exposure, tickers in cfg["markets"]["kalshi"].items():
        for ticker in tickers:
            mid = f"kalshi:{ticker}"
            markets.append({"id": mid, "venue": "kalshi", "ticker": ticker,
                            "label": kalshi_label(ticker),
                            "exposure": exposure, "indicative": False, "tick": 0.01})
            tr = read_json(os.path.join(rdir, f"kalshi_trades_{ticker}.json"))
            cr = read_json(os.path.join(rdir, f"kalshi_candles_{ticker}.json"))
            mt = kalshi_trades(tr, mid)
            mq, imputed = kalshi_quotes(cr, mid, start, end)
            trades += mt
            quotes += mq
            timing = candle_timing(cr, mt, start, end)
            notes.append(f"- `{ticker}`: {len(mt)} trades, {len(mq)} quote minutes "
                         f"({imputed} imputed); candle timing: {timing}")
    for pm in cfg["markets"]["polymarket"]:
        mid = f"polymarket:{pm['id']}"
        markets.append({"id": mid, "venue": "polymarket", "token": pm["token"], "label": pm["label"],
                        "exposure": pm["exposure"], "indicative": True, "tick": 0.01})
        pq, conflicts = poly_quotes(read_json(os.path.join(rdir, f"poly_{pm['id']}.json")), mid, start, end)
        quotes += pq
        notes.append(f"- Polymarket `{pm['id']}`: {len(pq)} points, {conflicts} chunk disagreements (indicative)")
    quotes.sort(key=lambda q: (q["t"], q["market"]))
    trades.sort(key=lambda t: (t["t"], t["market"]))

    out_path = os.path.join(DATA_DIR, event_id, "timeline.json")
    speech = {"source_url": cfg["source_url"], "speech_id": cfg["speech_id"], "paragraphs": []}
    if os.path.isfile(out_path):                      # keep role 3's scored paragraphs
        old = read_json(out_path).get("speech", {})
        if old.get("paragraphs"):
            speech["paragraphs"] = old["paragraphs"]
    timeline = {"event_id": event_id, "title": cfg["title"], "speaker": cfg["speaker"],
                "t0": cfg["t0"], "window": cfg["window"], "speech": speech,
                "markets": markets, "quotes": quotes, "trades": trades}
    write_json(out_path, timeline)

    web_dir = os.path.join(WEB_DATA_DIR, event_id)
    os.makedirs(web_dir, exist_ok=True)
    shutil.copyfile(out_path, os.path.join(web_dir, "timeline.json"))
    analysis = os.path.join(DATA_DIR, event_id, "analysis.json")
    if os.path.isfile(analysis):
        shutil.copyfile(analysis, os.path.join(web_dir, "analysis.json"))

    checks = run_spot_checks(spot, trades, quotes)
    size_kb = os.path.getsize(out_path) // 1024
    print(f"{event_id}: {len(markets)} markets, {len(quotes)} quotes, {len(trades)} trades -> "
          f"{out_path} ({size_kb} KB)")
    for status, desc in checks:
        print(f"  {status} {desc}")
    lines = [f"## {event_id}", "", f"timeline.json: {len(quotes)} quotes, {len(trades)} trades, {size_kb} KB", ""]
    lines += notes + ["", "Spot checks:"] + [f"- {s} {d}" for s, d in checks] + [""]
    return lines


def main():
    parser = argparse.ArgumentParser(description="Build Tradio timeline.json files from cached raw data")
    parser.add_argument("--event", action="append", help="event id (default: all)")
    args = parser.parse_args()
    cfg = load_events()
    lines = ["# Tradio data notes (generated by build_timeline.py)", "",
             "Candle timing: `match_minute_before_end` counts candles whose volume equals trades in",
             "the 60 s before `end_period_ts` (our assumption: quote time = END of the minute).",
             "If `match_minute_after_end` is higher instead, tell the team: quote times shift by a minute.", ""]
    for event_id in args.event or list(cfg["events"]):
        lines += build(event_id, cfg["events"][event_id], cfg.get("spot_checks", {}).get(event_id))
    with open(os.path.join(DATA_DIR, "NOTES.md"), "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    print(f"\nWrote {os.path.join(DATA_DIR, 'NOTES.md')}")


if __name__ == "__main__":
    main()
