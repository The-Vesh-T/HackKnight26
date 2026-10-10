"""Tradio live mode: the same replay screen, fed while a Fed speech is happening.

Every few seconds this writes tradio/data/live/{timeline,analysis}.json (and the copy in
web/data/tradio/live/) in exactly the schema the replay files use, so the LIVE tab on
web/tradio.html shows a speech the same way it shows a historical one:

  markets  Kalshi's next Fed-decision contracts (public endpoints, no keys), 1-minute
           candles + trades, polled live. Quotes are only written for candles that have
           closed, so the page never shows a half-finished minute.
  speech   read from MarketPulse's live_state.json, which `python live.py` writes while it
           transcribes and scores the speaker (z vs. that speaker's own baseline). Tradio only
           READS that file; it never imports the trading code. Run live.py with
           `--no-prewarm --mode dry` so no orders are possible.
  analysis role 2's tradio/reaction.py run on the window so far. Every status is provisional
           until the window closes, and the page says so.

When the speech is over, `archive` copies the live files into a normal event folder, so
the speech shows up as one more History tab next to Bowman, Waller and Warsh.

Usage (on the laptop that has internet; the demo itself still runs offline):

  # 1. Score the speech live (MarketPulse; dry, no watchlist => never trades)
  python speechtxt.py --url https://www.federalreserve.gov/live-broadcast.htm   # terminal 1
  python live.py --speaker christopher_waller --no-prewarm --mode dry           # terminal 2
  # 2. Live market tape + reaction checks for the LIVE tab
  python tradio/live.py --speaker christopher_waller --t0 now                   # terminal 3
  # 3. When it's over, keep it as a history event
  python tradio/live.py archive waller20261015 --title "Waller, 2026-10-15"

  # No speech on? Rehearse the LIVE tab offline by replaying a past event as if live:
  python tradio/live.py --simulate bowman20250623 --speed 30
"""

import argparse
import json
import os
import shutil
import sys
import time

if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tradio.common import (DATA_DIR, ROOT, WEB_DATA_DIR, iso_from_unix, parse_ts,  # noqa: E402
                           read_json, to_unix, write_json)

LIVE_ID = "live"
LIVE_DIRS = [os.path.join(DATA_DIR, LIVE_ID), os.path.join(WEB_DATA_DIR, LIVE_ID)]
LIVE_STATE = os.path.join(ROOT, "live_state.json")       # written by MarketPulse's live.py
EVENTS_INDEX = os.path.join(WEB_DATA_DIR, "events.json")  # extra history tabs for the page
DECISION_LEGS = ["H26", "H25", "H0", "C25", "C26"]
PRE_S = 60 * 60


def log(msg: str) -> None:
    print(time.strftime("%H:%M:%S"), msg, flush=True)


# ------------------------------------------------------------------ markets (live)

def next_fed_event() -> str:
    """Event ticker of the next Fed decision still open on Kalshi, e.g. KXFEDDECISION-26OCT."""
    from tradio import ingest_markets as im
    data = im.get_json(f"{im.KALSHI}/markets",
                       {"series_ticker": "KXFEDDECISION", "status": "open", "limit": 200})
    markets = data.get("markets", [])
    if not markets:
        raise SystemExit("Kalshi lists no open KXFEDDECISION markets; pass --event or --tickers.")
    return min(markets, key=lambda m: m.get("close_time") or "9999")["event_ticker"]


def event_tickers(event: str) -> list:
    from tradio import ingest_markets as im
    data = im.get_json(f"{im.KALSHI}/markets", {"event_ticker": event, "limit": 200})
    have = {m["ticker"].rsplit("-", 1)[-1]: m["ticker"] for m in data.get("markets", [])}
    return [have[leg] for leg in DECISION_LEGS if leg in have]


def market_entry(ticker: str) -> dict:
    from tradio.build_timeline import kalshi_label
    return {"id": f"kalshi:{ticker}", "venue": "kalshi", "ticker": ticker, "label": kalshi_label(ticker),
            "exposure": "direct", "indicative": False, "tick": 0.01}


class Tape:
    """Live Kalshi candles + trades for a few tickers, fetched incrementally."""

    def __init__(self, tickers: list, start: int):
        self.tickers, self.start = tickers, start
        self.trades = {t: {} for t in tickers}      # ticker -> {trade_id: raw trade}
        self.last_trade_ts = {t: start for t in tickers}

    def poll(self, now: int):
        from tradio import ingest_markets as im
        from tradio.build_timeline import kalshi_quotes, kalshi_trades
        end = now // 60 * 60                          # last CLOSED one-minute candle
        quotes, trades, problems = [], [], []
        for t in self.tickers:
            mid = f"kalshi:{t}"
            try:
                raw = im.kalshi_candles(t, self.start - 600, end)
                q, _ = kalshi_quotes({"response": raw}, mid, self.start, end)
                quotes += q
            except Exception as e:                    # keep going; show what we have
                problems.append(f"{t} candles: {e}")
            try:
                since = max(self.start, self.last_trade_ts[t] - 120)
                for tr in im.kalshi_trades(t, since, now + 1):
                    key = tr.get("trade_id") or (tr.get("created_time"), tr.get("count_fp"))
                    self.trades[t][key] = tr
                if self.trades[t]:
                    self.last_trade_ts[t] = max(to_unix(x["created_time"]) for x in self.trades[t].values())
                trades += kalshi_trades({"trades": list(self.trades[t].values())}, mid)
            except Exception as e:
                problems.append(f"{t} trades: {e}")
        return quotes, sorted(trades, key=lambda x: x["t"]), problems


# ------------------------------------------------------------------ speech (live)

def speech_from_state(path: str = LIVE_STATE):
    """MarketPulse live_state.json -> timeline 'speech' block. Passages arrive over time,
    so each paragraph carries its own time `t` (unlike a prepared text released at T0)."""
    if not os.path.isfile(path):
        return None
    try:
        st = read_json(path)
    except (OSError, json.JSONDecodeError):
        return None                                   # mid-write; next poll will get it
    paragraphs = []
    for i, c in enumerate(st.get("chunks", [])):
        try:
            t = iso_from_unix(parse_ts(c["time"]).timestamp())
        except Exception:
            continue
        speaker = c.get("role") == "speaker" and c.get("z") is not None
        paragraphs.append({"id": f"p{i}", "t": t, "text": c.get("text", ""), "stance": c.get("stance"),
                           "z": c.get("z"), "relevant": speaker, "key_sentence": None,
                           "summary": c.get("summary"), "role": c.get("role")})
    return {"source_url": None, "source": st.get("source"), "status": st.get("status"),
            "speaker": st.get("speaker"), "baseline": st.get("baseline"), "paragraphs": paragraphs,
            "arrives_over_time": True}


# ------------------------------------------------------------------ writing

POST_WINDOW_S = 90 * 60     # role 2 measures the final move at T0+75..90 min


def provisional(analysis: dict, timeline: dict) -> dict:
    """Before the 90-minute window has closed, role 2's final verdict can't exist yet (it
    needs the T0+75..90 min level), so every market would read "not enough quotes". Show
    what IS known so far, using only role 2's own outputs:
      - liquidity is a pre-T0 fact, so a dark market stays "can't tell";
      - role 2's band-exit alarm (alarm_t) => "alarm": the market has left its pre-event range;
      - otherwise "watching".
    No new numbers are computed here; final move, t50 and t90 stay empty until the window closes."""
    now, t0 = to_unix(timeline["live"]["now"]), to_unix(timeline["t0"])
    analysis["provisional"] = now < t0 + POST_WINDOW_S
    if not analysis["provisional"]:
        return analysis
    for r in analysis.get("markets", {}).values():
        r["final_status_pending"] = True
        r["move_ticks"] = r["t50_min"] = r["t90_min"] = None
        if r.get("liquidity") == "dark":
            continue
        if r.get("alarm_t") and not r.get("indicative"):
            mins = round((to_unix(r["alarm_t"]) - t0) / 60)
            r["status"] = "alarm"
            r["reason"] = (f"Alarm {r['alarm_t'][11:16]} UTC (T0+{mins} min): left its pre-event range for "
                           "2+ minutes. Final verdict when the 90-minute window closes.")
        else:
            r["status"] = "watching"
            r["reason"] = ("Before T0." if now < t0 else
                           "No alarm so far. Final verdict when the 90-minute window closes.")
    return analysis


def write_live(timeline: dict) -> None:
    from tradio.reaction import analyze
    try:
        analysis = provisional(analyze(timeline), timeline)
    except Exception as e:                            # early in a speech there may be too little data
        analysis = {"event_id": LIVE_ID, "markets": {}, "error": str(e), "provisional": True}
    for d in LIVE_DIRS:
        write_json(os.path.join(d, "timeline.json"), timeline)
        write_json(os.path.join(d, "analysis.json"), analysis)


def base_timeline(title, speaker, t0: int, start: int, now: int, kind: str, source: str) -> dict:
    return {"event_id": LIVE_ID, "title": title, "speaker": speaker,
            "t0": iso_from_unix(t0),
            "window": {"start": iso_from_unix(start), "end": iso_from_unix(max(now // 60 * 60, start + 60))},
            "live": {"kind": kind, "now": iso_from_unix(now), "updated_at": iso_from_unix(time.time()),
                     "source": source},
            "speech": {"paragraphs": []}, "markets": [], "quotes": [], "trades": []}


def run_live(a) -> None:
    tickers = a.tickers or event_tickers(a.event or next_fed_event())
    if not tickers:
        raise SystemExit("No Kalshi decision contracts found for that event.")
    started = int(time.time())
    if a.t0 and a.t0 != "now":
        t0 = to_unix(a.t0)
    else:
        t0 = started
    start = (t0 - PRE_S) // 60 * 60                  # candles end on whole minutes
    title = a.title or f"LIVE · {(a.speaker or 'Fed speaker').replace('_', ' ').title()}"
    log(f"tracking {', '.join(tickers)}; T0 {iso_from_unix(t0)}; writing {LIVE_DIRS[1]}")
    tape = Tape(tickers, start)
    markets = [market_entry(t) for t in tickers]
    while True:
        now = int(time.time())
        quotes, trades, problems = tape.poll(now)
        tl = base_timeline(title, a.speaker, t0, start, now, "live", "Kalshi public API")
        speech = speech_from_state(a.state)
        if speech:
            tl["speech"] = speech
            if speech.get("speaker") and not a.speaker:
                tl["speaker"] = speech["speaker"]
        tl.update(markets=markets, quotes=quotes, trades=trades)
        if problems:
            tl["live"]["problems"] = problems[:6]
        write_live(tl)
        n_par = len(tl["speech"]["paragraphs"])
        log(f"{len(quotes)} quote-minutes, {len(trades)} trades, {n_par} speech passages"
            + (f"  ({len(problems)} fetch problems)" if problems else ""))
        if a.once:
            return
        time.sleep(a.every)


def run_simulate(a) -> None:
    """Replay a cached event through the live files, as if it were happening now (offline)."""
    src = read_json(os.path.join(DATA_DIR, a.simulate, "timeline.json"))
    t0, start, end = to_unix(src["t0"]), to_unix(src["window"]["start"]), to_unix(src["window"]["end"])
    clock = t0 - a.lead * 60
    title = f"SIMULATED LIVE · {src['title']}"
    log(f"replaying {a.simulate} at {a.speed:g}x from T0-{a.lead} min; Ctrl+C to stop")
    while True:
        cut = clock // 60 * 60
        tl = base_timeline(title, src.get("speaker"), t0, start, clock, "simulated", f"replay of {a.simulate}")
        tl["markets"] = src["markets"]
        tl["quotes"] = [q for q in src["quotes"] if to_unix(q["t"]) <= cut]
        tl["trades"] = [t for t in src["trades"] if parse_ts(t["t"]).timestamp() <= clock]
        speech = dict(src.get("speech") or {})
        # A prepared text is released whole at T0, so it appears all at once (CLAUDE.md rule 3).
        speech["paragraphs"] = ([dict(p, t=src["t0"]) for p in speech.get("paragraphs", [])]
                                if clock >= t0 else [])
        tl["speech"] = speech
        write_live(tl)
        if a.once or clock >= end:
            log("end of window" if clock >= end else "wrote one frame")
            return
        time.sleep(a.every)
        clock = min(end, clock + int(a.every * a.speed))


def archive(event_id: str, title: str = None, sub: str = None) -> None:
    """Copy the live files into a normal event folder and list it as a History tab."""
    src = os.path.join(DATA_DIR, LIVE_ID)
    if not os.path.isfile(os.path.join(src, "timeline.json")):
        raise SystemExit("Nothing to archive: tradio/data/live/timeline.json doesn't exist.")
    tl = read_json(os.path.join(src, "timeline.json"))
    an = read_json(os.path.join(src, "analysis.json"))
    if tl.get("live", {}).get("kind") == "simulated":
        raise SystemExit("That's a simulated run; archive only real live sessions.")
    tl["event_id"] = an["event_id"] = event_id
    tl["title"] = title or tl.get("title", event_id).replace("LIVE · ", "")
    tl["recorded_live"] = tl.pop("live", {})
    if an.pop("provisional", False):
        print("note: the live window hadn't reached T0+90 min, so verdicts stay provisional "
              "(alarm / watching) in the archived event.")
    for base in (DATA_DIR, WEB_DATA_DIR):
        write_json(os.path.join(base, event_id, "timeline.json"), tl)
        write_json(os.path.join(base, event_id, "analysis.json"), an)
    index = read_json(EVENTS_INDEX) if os.path.isfile(EVENTS_INDEX) else []
    index = [e for e in index if e.get("id") != event_id]
    index.append({"id": event_id, "short": tl["title"].split(",")[0],
                  "sub": sub or f"{tl['t0'][:10]} · recorded live"})
    write_json(EVENTS_INDEX, index)
    log(f"archived as {event_id}; it now appears as a tab on tradio.html")


def main(argv=None):
    if argv is None:
        argv = sys.argv[1:]
    if argv and argv[0] == "archive":
        ap = argparse.ArgumentParser(prog="tradio/live.py archive")
        ap.add_argument("event_id")
        ap.add_argument("--title")
        ap.add_argument("--sub")
        a = ap.parse_args(argv[1:])
        return archive(a.event_id, a.title, a.sub)

    ap = argparse.ArgumentParser(description="Tradio live mode (see the module docstring)")
    ap.add_argument("--speaker", help="stance baseline key, e.g. christopher_waller (display only here)")
    ap.add_argument("--t0", help="ISO UTC time the speech starts, or 'now' (default)")
    ap.add_argument("--event", help="Kalshi event ticker, default = next Fed decision")
    ap.add_argument("--tickers", nargs="+", help="explicit Kalshi tickers instead of --event")
    ap.add_argument("--title")
    ap.add_argument("--state", default=LIVE_STATE, help="MarketPulse live_state.json to read the speech from")
    ap.add_argument("--every", type=float, default=None, help="seconds between updates")
    ap.add_argument("--once", action="store_true", help="write one update and exit")
    ap.add_argument("--simulate", metavar="EVENT_ID", help="replay a cached event as if live (offline)")
    ap.add_argument("--speed", type=float, default=30.0, help="simulation speed-up")
    ap.add_argument("--lead", type=int, default=5, help="simulation starts this many minutes before T0")
    a = ap.parse_args(argv)
    if a.every is None:
        a.every = 1.0 if a.simulate else 20.0
    try:
        run_simulate(a) if a.simulate else run_live(a)
    except KeyboardInterrupt:
        log("stopped")


if __name__ == "__main__":
    main()
