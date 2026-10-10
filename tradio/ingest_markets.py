"""
tradio/ingest_markets.py  (role 1)
==================================
Pulls the market tape around each Tradio event and caches the RAW responses, so the
demo never needs the network:

  tradio/data/<event_id>/raw/kalshi_trades_<TICKER>.json   all trades in the window
  tradio/data/<event_id>/raw/kalshi_candles_<TICKER>.json  1-minute candles (bid/ask/last/volume)
  tradio/data/<event_id>/raw/poly_<id>.json                every Polymarket price-history chunk

    python tradio/ingest_markets.py                     # all events
    python tradio/ingest_markets.py --event bowman20250623
    python tradio/ingest_markets.py --force             # re-download even if cached

No API keys needed. Kalshi's historical endpoints serve settled 2025 markets.
Polymarket's price history is cached in <=70-minute chunks because the same contract
returned contradictory values for different query windows; build_timeline.py flags
any disagreement instead of hiding it.
"""

import argparse
import os
import sys
import time

import requests

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tradio.common import load_events, raw_dir, read_json, to_unix, write_json  # noqa: E402

KALSHI = "https://api.elections.kalshi.com/trade-api/v2"
POLY_HISTORY = "https://clob.polymarket.com/prices-history"
HEADERS = {"User-Agent": "Tradio/1.0 (HackKnight26 research)", "Accept": "application/json"}
TRADE_CHUNK_S = 30 * 60      # small windows so no page gets cut off
POLY_CHUNK_S = 70 * 60
CANDLE_PAD_S = 60 * 60       # extra hour each side, for placebo/noise estimates


def get_json(url: str, params: dict, tries: int = 5) -> dict:
    for attempt in range(tries):
        r = requests.get(url, params=params, headers=HEADERS, timeout=30)
        if r.status_code == 429 or r.status_code >= 500:
            wait = 2 * (attempt + 1)
            print(f"    {r.status_code} from {url}; retrying in {wait}s")
            time.sleep(wait)
            continue
        r.raise_for_status()
        return r.json()
    raise RuntimeError(f"Gave up on {url} {params}")


def kalshi_trades(ticker: str, start: int, end: int) -> list:
    trades, seen = [], set()
    for chunk_start in range(start, end, TRADE_CHUNK_S):
        chunk_end = min(chunk_start + TRADE_CHUNK_S, end)
        cursor = None
        while True:
            params = {"ticker": ticker, "min_ts": chunk_start, "max_ts": chunk_end, "limit": 1000}
            if cursor:
                params["cursor"] = cursor
            page = get_json(f"{KALSHI}/historical/trades", params)
            for t in page.get("trades", []):
                key = t.get("trade_id") or (t.get("created_time"), t.get("count_fp"), t.get("yes_price_dollars"))
                if key not in seen:
                    seen.add(key)
                    trades.append(t)
            cursor = page.get("cursor")
            if not cursor:
                break
            time.sleep(0.2)
    trades.sort(key=lambda t: t.get("created_time", ""))
    return trades


def kalshi_candles(ticker: str, start: int, end: int) -> dict:
    params = {"start_ts": start, "end_ts": end, "period_interval": 1}
    return get_json(f"{KALSHI}/historical/markets/{ticker}/candlesticks", params)


def poly_chunks(token: str, start: int, end: int) -> list:
    chunks = []
    for chunk_start in range(start, end, POLY_CHUNK_S):
        chunk_end = min(chunk_start + POLY_CHUNK_S, end)
        data = get_json(POLY_HISTORY, {"market": token, "startTs": chunk_start,
                                       "endTs": chunk_end, "fidelity": 1})
        chunks.append({"startTs": chunk_start, "endTs": chunk_end, "response": data})
        time.sleep(0.2)
    return chunks


def ingest(event_id: str, cfg: dict, force: bool) -> None:
    out = raw_dir(event_id)
    start, end = to_unix(cfg["window"]["start"]), to_unix(cfg["window"]["end"])
    print(f"\n{event_id}: {cfg['window']['start']} -> {cfg['window']['end']}")
    tickers = [t for group in cfg["markets"]["kalshi"].values() for t in group]
    for ticker in tickers:
        tpath = os.path.join(out, f"kalshi_trades_{ticker}.json")
        if force or not os.path.isfile(tpath):
            trades = kalshi_trades(ticker, start, end)
            write_json(tpath, {"ticker": ticker, "min_ts": start, "max_ts": end, "trades": trades})
        n = len(read_json(tpath)["trades"])
        cpath = os.path.join(out, f"kalshi_candles_{ticker}.json")
        if force or not os.path.isfile(cpath):
            candles = kalshi_candles(ticker, start - CANDLE_PAD_S, end + CANDLE_PAD_S)
            write_json(cpath, {"ticker": ticker, "start_ts": start - CANDLE_PAD_S,
                               "end_ts": end + CANDLE_PAD_S, "response": candles})
        m = len(read_json(cpath)["response"].get("candlesticks", []))
        print(f"  kalshi {ticker}: {n} trades, {m} candles")
    for pm in cfg["markets"]["polymarket"]:
        ppath = os.path.join(out, f"poly_{pm['id']}.json")
        if force or not os.path.isfile(ppath):
            write_json(ppath, {"id": pm["id"], "token": pm["token"],
                               "chunks": poly_chunks(pm["token"], start, end)})
        pts = sum(len(c["response"].get("history", [])) for c in read_json(ppath)["chunks"])
        print(f"  polymarket {pm['id']}: {pts} points (indicative)")


def main():
    parser = argparse.ArgumentParser(description="Cache Kalshi/Polymarket data for Tradio events")
    parser.add_argument("--event", action="append", help="event id (default: all)")
    parser.add_argument("--force", action="store_true", help="re-download cached files")
    args = parser.parse_args()
    events = load_events()["events"]
    for event_id in args.event or list(events):
        ingest(event_id, events[event_id], args.force)
    print("\nNext: python tradio/build_timeline.py")


if __name__ == "__main__":
    main()
