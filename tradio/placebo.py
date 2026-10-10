"""Role 2: placebo test. Is a market's move after the speech bigger than on ordinary days?

For every Kalshi market that `reaction.py` calls `reacted` or `candidate_delay`, take
200 fake T0s at the same UTC time of day, on weekdays with no Fed event, and measure the
same final move (median mid at T0+75..90 minus median mid at T0-10..T0) on each.
p = share of fake days whose |move| >= the real |move|. `reaction.py` then reads
`placebo.json`: a market stays `reacted` only if p < 0.05.

Two steps, because the fetch needs internet and the demo must not:

    python -m tradio.placebo fetch --all     # needs internet; no API keys (~4 min/event)
    python -m tradio.placebo run --all       # offline; writes placebo.json per event
    python -m tradio.reaction --all          # picks placebo_p up

Files (per event, in tradio/data/<event_id>/):
    placebo_windows.json  compact per-minute midpoints for each fake window (fetch)
    placebo.json          fake T0s, moves, p-values (run)

Choices (documented so the team can check them):
- Fake days are weekdays from 300 days before the event up to 2 days before the
  contract's FOMC decision, never the event day itself.
- "Fed event" days excluded: FOMC decision days and the day before, FOMC minutes days,
  Jackson Hole symposium days, and every date we hold a Fed speech transcript for
  (transcripts/speech_*.json). FOMC_DATES below is typed from the Fed's published
  calendars; check it before quoting p-values.
- Sampling uses a fixed seed per event, so the fake T0s are the same on every run.
- Windows with no quotes (market not listed yet, or no candles) are dropped; `n` reports
  how many were usable. A p-value from fewer than 50 usable windows is reported but
  marked `low_n` and does not change any status.
"""

import argparse
import hashlib
import json
import os
import random
import re
import sys
from datetime import date, datetime, time, timedelta, timezone

if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tradio import reaction  # noqa: E402
from tradio.common import DATA_DIR, ROOT, iso, parse_ts  # noqa: E402

N_FAKE = 200
P_THRESHOLD = 0.05
MIN_N = 50
LOOKBACK_DAYS = 300
TESTED_STATUSES = ("reacted", "candidate_delay")

# FOMC statement days (decision announced). 2025 and 2026 published schedules.
FOMC_DATES = [
    "2025-01-29", "2025-03-19", "2025-05-07", "2025-06-18", "2025-07-30", "2025-09-17",
    "2025-10-29", "2025-12-10",
    "2026-01-28", "2026-03-18", "2026-04-29", "2026-06-17", "2026-07-29", "2026-09-16",
    "2026-10-28", "2026-12-09",
]
# FOMC minutes releases (three weeks after each meeting).
MINUTES_DATES = [
    "2025-02-19", "2025-04-09", "2025-05-28", "2025-07-09", "2025-08-20", "2025-10-08",
    "2025-11-19", "2025-12-30",
    "2026-02-18", "2026-04-08", "2026-05-20", "2026-07-08", "2026-08-19", "2026-10-07",
    "2026-11-18", "2026-12-30",
]
JACKSON_HOLE = ["2025-08-21", "2025-08-22", "2025-08-23",
                "2026-08-27", "2026-08-28", "2026-08-29"]
MONTHS = {"JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MAY": 5, "JUN": 6, "JUL": 7,
          "AUG": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12}


def _d(s):
    return date.fromisoformat(s)


def transcript_dates():
    """Dates of every Fed speech we hold a transcript for."""
    out = set()
    tdir = os.path.join(ROOT, "transcripts")
    if not os.path.isdir(tdir):
        return out
    for name in os.listdir(tdir):
        m = re.search(r"(20\d{2})(\d{2})(\d{2})", name)
        if name.startswith("speech_") and m:
            try:
                out.add(date(int(m[1]), int(m[2]), int(m[3])))
            except ValueError:
                pass
    return out


def excluded_days():
    days = set(map(_d, MINUTES_DATES + JACKSON_HOLE))
    for s in FOMC_DATES:
        days |= {_d(s), _d(s) - timedelta(days=1)}
    return days | transcript_dates()


def decision_day(ticker):
    """KXFEDDECISION-25JUL-C25 -> the July 2025 FOMC statement day, else None."""
    m = re.match(r"KXFEDDECISION-(\d{2})([A-Z]{3})-", ticker)
    if not m or m[2] not in MONTHS:
        return None
    year, month = 2000 + int(m[1]), MONTHS[m[2]]
    for s in FOMC_DATES:
        d = _d(s)
        if d.year == year and d.month == month:
            return d
    return None


def event_seed(event_id):
    return int(hashlib.sha256(event_id.encode()).hexdigest()[:8], 16)


def fake_t0s(event_id, t0, tickers, n=None, excluded=None):
    """Fixed-seed sample of fake T0s: same UTC time of day, weekdays, no Fed events."""
    n = N_FAKE if n is None else n
    excluded = excluded if excluded is not None else excluded_days()
    real_day = t0.date()
    last = real_day + timedelta(days=60)
    for tk in tickers:
        dd = decision_day(tk)
        if dd is not None:
            last = min(last, dd - timedelta(days=2))
    first = real_day - timedelta(days=LOOKBACK_DAYS)
    cands = []
    d = first
    while d <= last:
        if d != real_day and d.weekday() < 5 and d not in excluded:
            cands.append(datetime.combine(d, t0.timetz()))
        d += timedelta(days=1)
    rnd = random.Random(event_seed(event_id))
    picked = rnd.sample(cands, min(n, len(cands)))
    return sorted(picked)


def _timeline_path(event_id):
    return os.path.join(DATA_DIR, event_id, "timeline.json")


def tested_markets(event_id):
    """Kalshi market ids reaction.py calls reacted/candidate_delay, with observed move."""
    with open(_timeline_path(event_id), encoding="utf-8") as f:
        tl = json.load(f)
    res = reaction.analyze(tl, use_placebo=False)["markets"]
    out = {}
    for m in tl["markets"]:
        r = res[m["id"]]
        if m["venue"] == "kalshi" and r["status"] in TESTED_STATUSES:
            out[m["id"]] = {"ticker": m["id"].split(":", 1)[1], "tick": m.get("tick", 0.01),
                            "observed_move_ticks": r["move_ticks"]}
    return tl, out


# ------------------------------------------------------------------ fetch (network)

def fetch(event_id, force=False):
    from tradio import build_timeline, ingest_markets   # role 1's code, used read-only
    path = os.path.join(DATA_DIR, event_id, "placebo_windows.json")
    store = {}
    if os.path.isfile(path) and not force:
        with open(path, encoding="utf-8") as f:
            store = json.load(f)
    tl, markets = tested_markets(event_id)
    t0 = parse_ts(tl["t0"])
    fakes = fake_t0s(event_id, t0, [m["ticker"] for m in markets.values()])
    store.setdefault("event_id", event_id)
    store.setdefault("windows", {})
    total = len(fakes) * len(markets)
    done = 0
    for mid, info in markets.items():
        per = store["windows"].setdefault(mid, {})
        for ft in fakes:
            key = iso(ft)
            done += 1
            if key in per:
                continue
            start = int((ft - timedelta(minutes=reaction.PRE_MIN)).timestamp())
            end = int((ft + timedelta(minutes=reaction.POST_MIN)).timestamp())
            try:
                raw = ingest_markets.kalshi_candles(info["ticker"], start - 30 * 60, end)
                quotes, _ = build_timeline.kalshi_quotes({"response": raw}, mid, start, end)
            except Exception as exc:   # one bad window must not stop the run
                print(f"  {mid} {key}: {exc}")
                quotes = []
            per[key] = [[q["t"], q["mid"], q["imputed"]] for q in quotes if q["mid"] is not None]
            if done % 20 == 0:
                print(f"  {event_id}: {done}/{total} windows")
                _write(path, store)
    _write(path, store)
    print(f"wrote {path}")


def _write(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, separators=(",", ":"))
    os.replace(tmp, path)


# ------------------------------------------------------------------ run (offline)

def window_move(rows, fake_t0, tick):
    """Same final move as reaction.absorption, on one fake window."""
    quotes = [{"t": t, "mid": m, "imputed": imp} for t, m, imp in rows]
    series = reaction.build_series(quotes, fake_t0)
    _, move, _, _ = reaction.absorption(series, fake_t0, tick)
    return move


def run(event_id):
    path = os.path.join(DATA_DIR, event_id, "placebo_windows.json")
    tl, markets = tested_markets(event_id)
    t0 = parse_ts(tl["t0"])
    fakes = [iso(f) for f in fake_t0s(event_id, t0, [m["ticker"] for m in markets.values()])]
    if not markets:
        print(f"{event_id}: no reacted/candidate_delay Kalshi market; nothing to test")
        return None
    if not os.path.isfile(path):
        print(f"{event_id}: no {os.path.basename(path)} yet; run `fetch` first (needs internet)")
        return None
    with open(path, encoding="utf-8") as f:
        windows = json.load(f).get("windows", {})
    out = {"event_id": event_id, "seed": event_seed(event_id), "n_requested": N_FAKE,
           "fake_t0s": fakes, "p_threshold": P_THRESHOLD, "min_n": MIN_N, "markets": {}}
    for mid, info in markets.items():
        obs = abs(info["observed_move_ticks"])
        moves = {}
        for key in fakes:
            rows = windows.get(mid, {}).get(key)
            if rows:
                mv = window_move(rows, parse_ts(key), info["tick"])
                if mv is not None:
                    moves[key] = mv
        n = len(moves)
        n_ge = sum(1 for mv in moves.values() if abs(mv) >= obs)
        out["markets"][mid] = {
            "observed_move_ticks": info["observed_move_ticks"], "n": n, "n_ge": n_ge,
            "p": round(n_ge / n, 4) if n else None, "low_n": n < MIN_N,
            "fake_moves": moves,
        }
    dest = os.path.join(DATA_DIR, event_id, "placebo.json")
    with open(dest, "w", encoding="utf-8") as f:
        json.dump(out, f, indent=1)
    print(f"wrote {dest}")
    for mid, r in out["markets"].items():
        print(f"  {mid:36s} observed {r['observed_move_ticks']:+g} ticks  n={r['n']}  "
              f"p={r['p']}{'  (low n)' if r['low_n'] else ''}")
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("step", choices=("fetch", "run"))
    ap.add_argument("events", nargs="*")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--force", action="store_true", help="fetch: re-download cached windows")
    a = ap.parse_args(argv)
    events = a.events
    if a.all:
        events = sorted(e for e in os.listdir(DATA_DIR) if os.path.isfile(_timeline_path(e)))
    if not events:
        ap.error("give event ids or --all")
    for e in events:
        if a.step == "fetch":
            fetch(e, a.force)
        else:
            run(e)


if __name__ == "__main__":
    main()
